"""v1.6.0 sandbox status endpoint (D21-D24)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import get_settings
from app.schemas.sandbox import SandboxStatusResponse
from app.services.auth import AuthContext, get_auth_context, require_role
from app.services.sandbox.health import status_report

router = APIRouter(prefix="/v1", tags=["sandbox"])


@router.get("/sandbox/status", response_model=SandboxStatusResponse)
async def get_sandbox_status(auth_context: AuthContext = Depends(get_auth_context)):
    require_role(auth_context, "viewer", "operator", "admin")
    return status_report(get_settings())
