"""Verification suites and platform re-runs (v1.6.2 D22, D32, D40). Every result is ``signal_usage: "monitor_only"``."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import _actor, _query_tenant, run_missing
from app.api.routers.change_requests import api_requester
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.services import change_requests
from app.services.audit_log import get_run
from app.services.auth import AuthContext, get_auth_context, require_role
from app.services.verification import publishing, sampling, suites
from app.services.verification.errors import VerificationError

router = APIRouter(prefix="/v1", tags=["verification"])

READ_ROLES = ("admin", "tenant_admin", "reviewer", "auditor")
REQUEST_ROLES = ("admin", "tenant_admin", "reviewer")
SUITE_ROLES = ("admin", "tenant_admin")
_FORM_OVERHEAD_BYTES = 65_536
_UPLOAD_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["name", "file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary", "description": "The snapshot as tar.gz"},
                        "name": {"type": "string"},
                        "tenant_id": {"type": "string"},
                        "base_ref": {"type": "string"},
                        "image_name": {"type": "string", "default": "python"},
                        "test_command": {"type": "string", "description": "JSON array of pytest arguments"},
                        "test_paths": {"type": "string", "description": "JSON array of glob patterns"},
                    },
                }
            }
        },
    }
}


def _json_list(value: Any, field: str) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(str(value))
    except ValueError as exc:
        raise VerificationError(f"suite_{field}_invalid", f"{field} must be a JSON array.") from exc


async def _suite_or_404(session: AsyncSession, suite_id: str, auth_context: AuthContext) -> Any:
    suite = await suites.get_suite(session, suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail={"code": "verification_suite_not_found", "message": "Suite not found."})
    _query_tenant(suite.tenant_id, auth_context)
    return suite


@router.post("/verification-suites", status_code=201, openapi_extra=_UPLOAD_SCHEMA)
async def upload_verification_suite(
    request: Request, auth_context: AuthContext = Depends(get_auth_context), session: AsyncSession = Depends(get_session)
) -> Any:
    """Upload a ``tar.gz`` snapshot. The form is read only after the caller is known and the declared size fits."""
    require_role(auth_context, *SUITE_ROLES)
    settings = get_settings()
    declared = request.headers.get("content-length", "")
    if not declared.isdigit() or int(declared) > int(settings.sandbox_artifact_max_bytes) + _FORM_OVERHEAD_BYTES:
        raise VerificationError("artifact_too_large", "The upload needs a Content-Length within SANDBOX_ARTIFACT_MAX_BYTES.", 413)
    form = await request.form()
    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        raise VerificationError("artifact_invalid_archive", "Send the snapshot in the form field 'file'.")
    outcome = await publishing.upload(
        session,
        settings,
        data=await upload.read(),
        tenant_id=_query_tenant(str(form.get("tenant_id") or "") or None, auth_context) or "default",
        name=str(form.get("name") or ""),
        base_ref=str(form.get("base_ref") or ""),
        image_name=str(form.get("image_name") or "python"),
        test_command=_json_list(form.get("test_command"), "test_command"),
        test_paths=_json_list(form.get("test_paths"), "test_paths"),
        requester=api_requester(auth_context),
    )
    if outcome.pending:
        return JSONResponse(status_code=202, content=change_requests.pending_response(outcome))
    return suites.suite_to_dict(outcome.result)


@router.get("/verification-suites")
async def list_verification_suites(
    tenant_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_role(auth_context, *READ_ROLES)
    scoped = _query_tenant(tenant_id, auth_context)
    rows = await suites.list_suites(session, tenant_id=scoped, limit=limit)
    default_id = await suites.default_suite_id(session, scoped) if scoped else ""
    return {"suites": [suites.suite_to_dict(row) for row in rows], "count": len(rows), "default_suite_id": default_id}


@router.get("/verification-suites/{suite_id}")
async def get_verification_suite(
    suite_id: str, auth_context: AuthContext = Depends(get_auth_context), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    require_role(auth_context, *READ_ROLES)
    return suites.suite_to_dict(await _suite_or_404(session, suite_id, auth_context))


@router.post("/verification-suites/{suite_id}/disable")
async def disable_verification_suite(
    suite_id: str, auth_context: AuthContext = Depends(get_auth_context), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    require_role(auth_context, *SUITE_ROLES)
    suite = await _suite_or_404(session, suite_id, auth_context)
    return suites.suite_to_dict(await suites.disable(session, suite, actor=_actor(auth_context)))


@router.post("/verification-suites/{suite_id}/default")
async def set_default_verification_suite(
    suite_id: str,
    enabled: bool = Query(default=True),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Choose (or with ``enabled=false`` clear) the suite new ``code_fix`` runs of the tenant are bound to."""
    require_role(auth_context, *SUITE_ROLES)
    suite = await _suite_or_404(session, suite_id, auth_context)
    current = await suites.set_default(session, suite, enabled=enabled, actor=_actor(auth_context))
    return {"tenant_id": suite.tenant_id, "default_verification_suite_id": current}


async def _visible_run(session: AsyncSession, run_id: str, auth_context: AuthContext) -> Any:
    run = await get_run(session, run_id)
    if run is None:
        raise await run_missing(session, run_id, auth_context)
    _query_tenant(run.tenant_id, auth_context)
    return run


@router.get("/runs/{run_id}/verifications")
async def list_run_verifications(
    run_id: str, auth_context: AuthContext = Depends(get_auth_context), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    require_role(auth_context, *READ_ROLES)
    await _visible_run(session, run_id, auth_context)
    rows = await sampling.list_verifications(session, run_id=run_id, limit=100)
    return {"verifications": [sampling.verification_to_dict(row) for row in rows], "count": len(rows), "signal_usage": sampling.SIGNAL_USAGE}


@router.post("/runs/{run_id}/verifications", status_code=202)
async def request_run_verification(
    run_id: str, auth_context: AuthContext = Depends(get_auth_context), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Re-run the latest outcome. A daily cap or a closed gate answers with a ``skipped`` record that says why."""
    require_role(auth_context, *REQUEST_ROLES)
    run = await _visible_run(session, run_id, auth_context)
    row, created = await sampling.request_manual(session, get_settings(), run=run, requested_by=_actor(auth_context))
    return {"verification": sampling.verification_to_dict(row), "created": created, "signal_usage": sampling.SIGNAL_USAGE}


@router.get("/verifications/{verification_id}")
async def get_run_verification(
    verification_id: str, auth_context: AuthContext = Depends(get_auth_context), session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    require_role(auth_context, *READ_ROLES)
    row = await sampling.get_verification(session, verification_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "verification_not_found", "message": "Verification not found."})
    _query_tenant(row.tenant_id, auth_context)
    return sampling.verification_to_dict(row)
