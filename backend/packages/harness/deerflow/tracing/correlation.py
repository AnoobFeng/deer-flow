"""DeerFlow trace correlation helpers."""

from __future__ import annotations

import uuid
from typing import Any

DEERFLOW_TRACE_ID_METADATA_KEY = "deerflow_trace_id"


def new_deerflow_trace_id() -> str:
    """Return a new DeerFlow correlation id."""
    return str(uuid.uuid4())


def build_deerflow_correlation_metadata(deerflow_trace_id: str | None = None) -> dict[str, str]:
    """Build the minimal metadata payload used to correlate DeerFlow traces."""
    return {DEERFLOW_TRACE_ID_METADATA_KEY: deerflow_trace_id or new_deerflow_trace_id()}


def get_deerflow_trace_id(config: dict[str, Any] | None) -> str | None:
    """Read the DeerFlow trace id from a RunnableConfig-like dict."""
    if not isinstance(config, dict):
        return None
    metadata = config.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get(DEERFLOW_TRACE_ID_METADATA_KEY)
        if isinstance(value, str) and value:
            return value
    context = config.get("context")
    if isinstance(context, dict):
        value = context.get(DEERFLOW_TRACE_ID_METADATA_KEY)
        if isinstance(value, str) and value:
            return value
    return None


def ensure_deerflow_trace_id(config: dict[str, Any], deerflow_trace_id: str | None = None) -> str:
    """Ensure ``config`` carries a DeerFlow trace id in metadata and context."""
    trace_id = deerflow_trace_id or get_deerflow_trace_id(config) or new_deerflow_trace_id()
    metadata = config.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata[DEERFLOW_TRACE_ID_METADATA_KEY] = trace_id
    context = config.setdefault("context", {})
    if isinstance(context, dict):
        context[DEERFLOW_TRACE_ID_METADATA_KEY] = trace_id
    return trace_id
