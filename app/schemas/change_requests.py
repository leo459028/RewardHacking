"""v1.6.1 Change Request API bodies (plan D37-D43)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChangeRequestCancelRequest(BaseModel):
    reason: str = Field(default="", max_length=2000)
