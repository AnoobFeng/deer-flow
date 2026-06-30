"""Logging enhancement configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoggingEnhanceConfig(BaseModel):
    """Optional single-field trace correlation configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Enable the trace_id field in log output.")
    format: Literal["text", "json"] = Field(default="text", description="Enhanced log output format.")


class LoggingConfig(BaseModel):
    """Top-level logging configuration."""

    enhance: LoggingEnhanceConfig = Field(default_factory=LoggingEnhanceConfig, description="Optional trace correlation logging.")
