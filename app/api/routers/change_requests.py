"""v1.6.1 Change Request API (plan D39-D43, feature nine).

Read and cancel only: ``GET /v1/change-requests``, ``GET /v1/change-requests/{change_id}`` and
``POST /v1/change-requests/{change_id}/cancel`` (the requester only). **There is no approve endpoint**: only a
signed-in Dashboard user can approve or reject (D40, Q5), so an API key can never be the second person.

Requests are created by the four guarded endpoints themselves (``POST /v1/retention/plans/{id}/apply``,
``POST /v1/legal-holds/{id}/release``, ``POST /v1/evaluations/judge/profiles/{name}/activate`` and
``PUT /v1/tenants/{id}/evaluation-policy`` when it removes protected paths), which answer ``202`` with
``{"status": "pending_second_approval", "change_request": {...}}`` while their action is in ``DUAL_CONTROL_ACTIONS``.
Admin keys see every request; other keys only their own tenant's.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.common import _actor
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.change_requests import ChangeRequestCancelRequest
from app.services import change_requests
from app.services.auth import AuthContext, auth_error, get_auth_context, require_role

router = APIRouter(prefix="/v1", tags=["change-requests"])

READ_ROLES = ("viewer", "operator", "admin")


def api_requester(auth_context: AuthContext) -> change_requests.Requester:
    """The requester of a change created through the API (``key:<key_id>``; auth disabled = local admin)."""
    role = "admin" if (not auth_context.enabled or auth_context.role == "super_admin") else auth_context.role
    return change_requests.Requester.from_api(actor=_actor(auth_context), role=role, tenant_id=auth_context.tenant_id)


def _platform(auth_context: AuthContext) -> bool:
    return not auth_context.enabled or auth_context.role in {"admin", "super_admin"}


def _ensure_visible(cr: Any, auth_context: AuthContext) -> None:
    if _platform(auth_context):
        return
    if (cr.tenant_id or "") != (auth_context.tenant_id or "default"):
        raise change_requests.ChangeRequestError("change_request_not_found", "Change request not found.", 404)


@router.get("/change-requests")
async def list_change_requests(
    status: str | None = Query(default=None, pattern="^(pending|approved|rejected|executed|failed|expired|cancelled)$"),
    action: str | None = Query(default=None, max_length=64),
    tenant_id: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=100, ge=1, le=500),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_role(auth_context, *READ_ROLES)
    if not _platform(auth_context):
        own = auth_context.tenant_id or "default"
        if tenant_id and tenant_id != own:
            raise auth_error(403, "tenant_mismatch", "Query tenant_id does not match API key tenant.")
        tenant_id = own
    await change_requests.expire_stale(session, get_settings())
    rows = await change_requests.list_change_requests(session, tenant_id=tenant_id, status=status, action=action, limit=limit)
    return {"change_requests": [change_requests.to_dict(row) for row in rows], "count": len(rows)}


@router.get("/change-requests/{change_id}")
async def get_change_request(
    change_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_role(auth_context, *READ_ROLES)
    await change_requests.expire_stale(session, get_settings())
    cr = await change_requests.get_change_request(session, change_id)
    if cr is None:
        raise change_requests.ChangeRequestError("change_request_not_found", "Change request not found.", 404)
    _ensure_visible(cr, auth_context)
    return change_requests.to_dict(cr)


@router.post("/change-requests/{change_id}/cancel")
async def cancel_change_request(
    change_id: str,
    payload: ChangeRequestCancelRequest | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    require_role(auth_context, "tenant_admin", "admin")
    cr = await change_requests.get_change_request(session, change_id)
    if cr is None:
        raise change_requests.ChangeRequestError("change_request_not_found", "Change request not found.", 404)
    _ensure_visible(cr, auth_context)
    cr = await change_requests.cancel(
        session, get_settings(), change_id, api_requester(auth_context), reason=(payload.reason if payload else "")
    )
    return change_requests.to_dict(cr)
