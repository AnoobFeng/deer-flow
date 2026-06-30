"""Tests for enhanced logging context wiring."""

from __future__ import annotations

import io
import json
import logging

import pytest
from pydantic import ValidationError

from deerflow.config.logging_config import LoggingConfig
from deerflow.logging_config import bind_observability_context, configure_logging, reset_observability_context


@pytest.fixture
def isolated_root_logger():
    root = logging.root
    original_level = root.level
    original_handlers = list(root.handlers)
    for handler in original_handlers:
        root.removeHandler(handler)
    try:
        yield root
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)


def test_configure_logging_disabled_does_not_duplicate_handlers(isolated_root_logger) -> None:
    configure_logging("info", LoggingConfig())
    first_handlers = list(logging.root.handlers)

    configure_logging("debug", LoggingConfig())

    assert list(logging.root.handlers) == first_handlers


def test_enhanced_text_logging_injects_trace_id(isolated_root_logger) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.root.addHandler(handler)
    logger = logging.getLogger("deerflow.test_logging_config")

    configure_logging("info", LoggingConfig(enhance={"enabled": True}))
    token = bind_observability_context("trace-xyz")
    try:
        logger.info("hello")
    finally:
        reset_observability_context(token)

    assert "[trace_id=trace-xyz]" in stream.getvalue()


def test_configure_logging_disabled_restores_formatter_after_enhanced(isolated_root_logger) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("plain:%(message)s"))
    logging.root.addHandler(handler)
    logger = logging.getLogger("deerflow.test_logging_config")

    configure_logging("info", LoggingConfig(enhance={"enabled": True}))
    configure_logging("info", LoggingConfig())
    logger.info("hello")

    assert stream.getvalue().strip() == "plain:hello"


def test_enhanced_json_logging_emits_only_trace_id_context(isolated_root_logger) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.root.addHandler(handler)
    logger = logging.getLogger("deerflow.test_logging_config")

    configure_logging("info", LoggingConfig(enhance={"enabled": True, "format": "json"}))
    token = bind_observability_context("trace-xyz")
    try:
        logger.info("hello", extra={"thread_id": "thread-1", "user_id": "user-1"})
    finally:
        reset_observability_context(token)

    payload = json.loads(stream.getvalue())
    assert payload["trace_id"] == "trace-xyz"
    assert "thread_id" not in payload
    assert "user_id" not in payload


def test_logging_context_fields_are_not_supported() -> None:
    with pytest.raises(ValidationError):
        LoggingConfig(enhance={"enabled": False, "context_fields": ["trace_id"]})
