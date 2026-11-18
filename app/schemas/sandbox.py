"""Response schema for ``GET /v1/sandbox/status`` (v1.6.0 D21-D24, v1.6.2 D14, D40)."""

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
    gate: dict[str, Any] = Field(default_factory=dict)  # v1.6.2: ready, reasons, fingerprint
    runners: list[dict[str, Any]] = Field(default_factory=list)  # v1.6.2: live heartbeats
