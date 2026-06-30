"""Tests for DeerFlow trace correlation helpers."""

from __future__ import annotations

import uuid

from deerflow.tracing import build_deerflow_correlation_metadata, ensure_deerflow_trace_id, get_deerflow_trace_id


def test_build_deerflow_correlation_metadata_generates_uuid() -> None:
    metadata = build_deerflow_correlation_metadata()

    trace_id = metadata["deerflow_trace_id"]
    assert str(uuid.UUID(trace_id)) == trace_id


def test_ensure_deerflow_trace_id_writes_metadata_and_context() -> None:
    config: dict = {}

    trace_id = ensure_deerflow_trace_id(config, "trace-1")

    assert trace_id == "trace-1"
    assert config["metadata"]["deerflow_trace_id"] == "trace-1"
    assert config["context"]["deerflow_trace_id"] == "trace-1"
    assert get_deerflow_trace_id(config) == "trace-1"


def test_ensure_deerflow_trace_id_preserves_existing_value() -> None:
    config = {"metadata": {"deerflow_trace_id": "existing"}}

    trace_id = ensure_deerflow_trace_id(config)

    assert trace_id == "existing"
    assert config["metadata"]["deerflow_trace_id"] == "existing"


def test_ensure_deerflow_trace_id_explicit_value_overrides_stale_context() -> None:
    config = {
        "metadata": {"deerflow_trace_id": "stale-metadata"},
        "context": {"deerflow_trace_id": "stale-context"},
    }

    trace_id = ensure_deerflow_trace_id(config, "authoritative")

    assert trace_id == "authoritative"
    assert config["metadata"]["deerflow_trace_id"] == "authoritative"
    assert config["context"]["deerflow_trace_id"] == "authoritative"


def test_ensure_deerflow_trace_id_reconciles_metadata_and_context() -> None:
    config = {
        "metadata": {"deerflow_trace_id": "metadata-wins"},
        "context": {"deerflow_trace_id": "stale-context"},
    }

    trace_id = ensure_deerflow_trace_id(config)

    assert trace_id == "metadata-wins"
    assert config["metadata"]["deerflow_trace_id"] == "metadata-wins"
    assert config["context"]["deerflow_trace_id"] == "metadata-wins"
