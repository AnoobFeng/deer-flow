"""Logging setup and single-field trace correlation context."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime

from deerflow.config.app_config import apply_logging_level
from deerflow.config.logging_config import LoggingConfig

DEFAULT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
ENHANCED_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [trace_id=%(trace_id)s] - %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MISSING = "-"
_trace_id_context: ContextVar[str | None] = ContextVar("deerflow_trace_id_context", default=None)
_enhanced_logging_enabled = False
_original_formatters: dict[int, logging.Formatter | None] = {}


def get_observability_context() -> dict[str, str]:
    """Return a copy of the current logging context."""
    trace_id = _trace_id_context.get()
    return {"trace_id": trace_id} if trace_id else {}


def bind_observability_context(trace_id: str | None) -> Token[str | None]:
    """Bind the current DeerFlow trace id for log correlation."""
    return _trace_id_context.set(str(trace_id) if trace_id else None)


def reset_observability_context(token: Token[str | None]) -> None:
    """Reset the logging context to a previous token."""
    _trace_id_context.reset(token)


@contextmanager
def observability_context(trace_id: str | None) -> Iterator[None]:
    """Temporarily bind the current DeerFlow trace id."""
    token = bind_observability_context(trace_id)
    try:
        yield
    finally:
        reset_observability_context(token)


class ObservabilityContextFilter(logging.Filter):
    """Inject DeerFlow observability context into each ``LogRecord``."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id_context.get() or _MISSING
        return True


class DeerFlowJsonFormatter(logging.Formatter):
    """Small JSON formatter for enhanced logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", _MISSING),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _ensure_filter(handler: logging.Handler) -> None:
    if not any(isinstance(item, ObservabilityContextFilter) for item in handler.filters):
        handler.addFilter(ObservabilityContextFilter())


def _remove_filters(handler: logging.Handler) -> None:
    for item in list(handler.filters):
        if isinstance(item, ObservabilityContextFilter):
            handler.removeFilter(item)


def configure_logging(log_level: str | None, logging_config: LoggingConfig | None = None) -> None:
    """Configure DeerFlow logging without duplicating root handlers."""
    global _enhanced_logging_enabled

    logging.basicConfig(
        level=logging.INFO,
        format=DEFAULT_LOG_FORMAT,
        datefmt=DEFAULT_DATE_FORMAT,
    )

    enhance = (logging_config or LoggingConfig()).enhance
    _enhanced_logging_enabled = bool(enhance.enabled)

    if enhance.enabled:
        if enhance.format == "json":
            formatter: logging.Formatter = DeerFlowJsonFormatter()
        else:
            formatter = logging.Formatter(ENHANCED_LOG_FORMAT, datefmt=DEFAULT_DATE_FORMAT)
        for handler in logging.root.handlers:
            already_enhanced = any(isinstance(item, ObservabilityContextFilter) for item in handler.filters)
            if not already_enhanced:
                _original_formatters[id(handler)] = handler.formatter
            _ensure_filter(handler)
            handler.setFormatter(formatter)
    else:
        for handler in logging.root.handlers:
            _remove_filters(handler)
            handler_id = id(handler)
            if handler_id in _original_formatters:
                handler.setFormatter(_original_formatters.pop(handler_id))

    apply_logging_level(log_level)


def is_enhanced_logging_enabled() -> bool:
    """Return whether enhanced logging was enabled at startup."""
    return _enhanced_logging_enabled
