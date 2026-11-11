"""Response schema for ``GET /v1/sandbox/status`` (v1.6.0 D21-D24)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SandboxStatusResponse(BaseModel):
    configured_backend: str
    status: str  # pass | fail
    capabilities: dict[str, Any] | None = None
    executes_tools: bool
    isolation_policy: dict[str, Any] = Field(default_factory=dict)
    smoke: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
