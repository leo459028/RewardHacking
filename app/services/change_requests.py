"""v1.6.1 dual control: Change Requests | 變更覆核 (plan D37-D43, feature nine).

``execute_or_request()`` is the single enforcement point (D39). The API routers and the Dashboard both call it, so an
admin API key cannot bypass it (Q5):

- the action is not listed in ``DUAL_CONTROL_ACTIONS`` (the default) -> it runs at once, exactly as before;
- it is listed -> a ``pending`` change request is stored (or the existing one for the same target is returned) and
  nothing changes.

``approve()`` runs the stored action in the same request after re-checking everything (D40): the approver is a
signed-in Dashboard user (never an API key or the synthetic ``off``-mode principal), is not the requester, holds the
action's own permission, recently re-authenticated, and a tenant-bound approver only handles its own tenant. The
payload is bound to the target state at request time (D41): a plan digest, a legal hold, a judge configuration /
probe fingerprint or the previous evaluation policy; if it changed, the request ends as ``failed`` with
``change_request_stale`` (or the underlying service's own error code) and the target is untouched.

Every transition is a ``change_request.*`` governance event, so it lands in the audit chain (P5 session hook).
Naming is deliberately "change request", not approval or review (AGENTS.md 2.3 Approval != Review): a change request
never dispatches a run, publishes to Kafka or touches token leases (import-linter contract in ``pyproject.toml``).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.digests import iso_utc, sha256_hex
from app.models.access import ChangeRequest
from app.observability.metrics import CHANGE_REQUESTS_TOTAL
from app.services.access.permissions import has_permission
from app.services.access.principal import Principal
from app.services.access.sessions import reauth_is_fresh
from app.services.access.users import SYSTEM_TENANT
from app.services.governance_audit import add_governance_event

logger = logging.getLogger(__name__)

RETENTION_APPLY = "retention.apply"
LEGAL_HOLD_RELEASE = "legal_hold.release"
JUDGE_ACTIVATE = "judge.activate"
POLICY_PROTECTED_PATHS = "evaluation_policy.protected_paths"
ACTIONS: tuple[str, ...] = (RETENTION_APPLY, LEGAL_HOLD_RELEASE, JUDGE_ACTIVATE, POLICY_PROTECTED_PATHS)

# D40: the approver must hold the action's own Dashboard permission (tenant_admin can therefore only decide
# evaluation-policy requests, feature six).
ACTION_PERMISSIONS: dict[str, str] = {
    RETENTION_APPLY: "retention.apply",
    LEGAL_HOLD_RELEASE: "legal_hold.release",
    JUDGE_ACTIVATE: "judge.profile.write",
    POLICY_PROTECTED_PATHS: "evaluation_policy.relax",
}
ACTION_LABELS: dict[str, str] = {
    RETENTION_APPLY: "Apply retention purge plan",
    LEGAL_HOLD_RELEASE: "Release legal hold",
    JUDGE_ACTIVATE: "Activate judge profile",
    POLICY_PROTECTED_PATHS: "Remove protected paths",
}

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTED = "executed"
FAILED = "failed"
EXPIRED = "expired"
CANCELLED = "cancelled"
STATUSES: tuple[str, ...] = (PENDING, APPROVED, REJECTED, EXECUTED, FAILED, EXPIRED, CANCELLED)

PENDING_SECOND_APPROVAL = "pending_second_approval"
EXECUTION_ERROR = "change_request_execution_error"


class ChangeRequestError(Exception):
    """A refused change-request operation (``{"error": {"code": ...}}``, handler in ``app/main.py``)."""

    def __init__(self, code: str, message: str, status_code: int = 409, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True)
class Requester:
    """Who asked for a change. ``actor`` is the audit string; ``user_id`` is set for signed-in Dashboard users."""

    actor: str
    kind: str  # dashboard | local (Dashboard sign-in off) | api_key | job
    role: str = ""
    tenant_id: str | None = None
    user_id: str = ""

    @classmethod
    def from_principal(cls, principal: Principal, actor: str) -> "Requester":
        return cls(
            actor=actor,
            kind="local" if principal.synthetic else "dashboard",
            role=principal.role,
            tenant_id=principal.tenant_id,
            user_id="" if principal.synthetic else principal.user_id,
        )

    @classmethod
    def from_api(cls, *, actor: str, role: str, tenant_id: str | None) -> "Requester":
        return cls(actor=actor, kind="api_key", role=role, tenant_id=tenant_id)

    def as_dict(self) -> dict[str, Any]:
        return {"actor": self.actor, "kind": self.kind, "role": self.role, "tenant_id": self.tenant_id, "user_id": self.user_id}


@dataclass
class Outcome:
    """What ``execute_or_request()`` did: ``executed`` (``result`` is the service's own return value) or
    ``pending_second_approval`` (``change_request`` holds the stored request; ``created`` is False for a duplicate)."""

    status: str
    result: Any = None
    change_request: ChangeRequest | None = None
    created: bool = False

    @property
    def pending(self) -> bool:
        return self.status == PENDING_SECOND_APPROVAL


@dataclass(frozen=True)
class Prepared:
    target_ref: str
    tenant_id: str | None
    payload: dict[str, Any]
    expires_cap: datetime | None = None  # retention.apply: the plan's own expires_at


@dataclass(frozen=True)
class ExecContext:
    actor: str
    role: str
    state_store: Any = None
    change_id: str = ""  # set when a change request is executed after approval


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def configured_actions(settings: Settings) -> tuple[frozenset[str], list[str]]:
    """``DUAL_CONTROL_ACTIONS`` split into known actions and unknown names (reported by the operations check)."""
    names = [item.strip().lower() for item in str(settings.dual_control_actions or "").split(",") if item.strip()]
    known = frozenset(name for name in names if name in ACTIONS)
    unknown = sorted({name for name in names if name not in ACTIONS})
    return known, unknown


def is_enabled(settings: Settings, action: str) -> bool:
    return action in configured_actions(settings)[0]


def _ttl(settings: Settings) -> timedelta:
    return timedelta(hours=max(1, int(settings.dual_control_request_ttl_hours or 24)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# per-action specs: prepare (request time), check binding + execute (approval time or direct)
# ---------------------------------------------------------------------------


async def _prepare_retention_apply(session: AsyncSession, settings: Settings, params: dict[str, Any], requester: Requester) -> Prepared:
    from app.services import retention

    mode = retention.purge_mode(settings)
    if mode == "off":
        raise retention.RetentionError("retention_disabled", "AUDIT_RETENTION_PURGE_MODE is off.", 409)
    if mode != "apply":
        raise retention.RetentionError("retention_apply_disabled", "Set AUDIT_RETENTION_PURGE_MODE=apply to apply purge plans.", 409)
    if params.get("confirm") is not True:
        raise retention.RetentionError("retention_confirm_required", "Applying a purge plan requires confirm: true.", 422)
    purge = await retention.get_purge(session, str(params["purge_id"]))
    if purge is None:
        raise retention.RetentionError("retention_plan_not_found", f"Purge plan {params['purge_id']} not found.", 404)
    if purge.status != "planned" or _aware(purge.expires_at) <= _now():
        raise retention.RetentionError("retention_plan_expired" if purge.status in {"planned", "expired"} else "retention_plan_already_applied",
                                       "The purge plan cannot be applied any more.", 409)
    if params.get("plan_digest") != purge.plan_digest:
        raise retention.RetentionError("retention_plan_digest_mismatch", "plan_digest does not match the stored plan.", 409)
    return Prepared(
        target_ref=f"purge:{purge.purge_id}",
        tenant_id=None,
        payload={"purge_id": purge.purge_id, "plan_digest": purge.plan_digest, "confirm": True},
        expires_cap=_aware(purge.expires_at),
    )


async def _execute_retention_apply(session: AsyncSession, settings: Settings, payload: dict[str, Any], ctx: ExecContext) -> Any:
    from app.services import retention

    return await retention.apply_plan(
        session,
        settings,
        str(payload["purge_id"]),
        plan_digest=str(payload.get("plan_digest") or ""),
        confirm=payload.get("confirm") is True,
        actor=ctx.actor,
        state_store=ctx.state_store,
    )


def _summary_retention_apply(result: Any) -> dict[str, Any]:
    return {
        "purge_id": result.purge_id,
        "status": result.status,
        "applied_counts": dict(result.applied_counts or {}),
        "skipped_at_apply": len(result.skipped_at_apply or []),
        "failures": len(result.failures or []),
        "receipt_digest": result.receipt_digest or "",
    }


async def _prepare_hold_release(session: AsyncSession, settings: Settings, params: dict[str, Any], requester: Requester) -> Prepared:
    from app.models.retention import LegalHold
    from app.services import retention

    if requester.role != "admin":
        raise retention.RetentionError("legal_hold_forbidden", "Only admin can release legal holds.", 403)
    reason = str(params.get("release_reason") or "").strip()
    if not reason:
        raise retention.RetentionError("legal_hold_release_reason_required", "Releasing a legal hold needs a release_reason.", 422)
    hold = (await session.execute(select(LegalHold).where(LegalHold.hold_id == str(params["hold_id"])))).scalar_one_or_none()
    if hold is None:
        raise retention.RetentionError("legal_hold_not_found", f"Legal hold {params['hold_id']} not found.", 404)
    if hold.status != "active":
        raise retention.RetentionError("legal_hold_not_active", "This legal hold is already released.", 409)
    return Prepared(target_ref=f"hold:{hold.hold_id}", tenant_id=hold.tenant_id, payload={"hold_id": hold.hold_id, "release_reason": reason})


async def _execute_hold_release(session: AsyncSession, settings: Settings, payload: dict[str, Any], ctx: ExecContext) -> Any:
    from app.services import retention

    return await retention.release_hold(
        session, str(payload["hold_id"]), actor=ctx.actor, actor_role=ctx.role, release_reason=str(payload.get("release_reason") or "")
    )


def _summary_hold_release(result: Any) -> dict[str, Any]:
    return {"hold_id": result.hold_id, "status": result.status, "released_by": result.released_by or ""}


async def _judge_binding(session: AsyncSession, settings: Settings, name: str) -> dict[str, Any]:
    from app.services.evaluation.judges.profiles import config_fingerprint
    from app.services.judge_activation import state_row
    from app.services.judge_profiles_admin import _profile_or_error

    profile = _profile_or_error(settings, name)
    row = await state_row(session, profile.name)
    return {
        "profile": profile.name,
        "config_fingerprint": config_fingerprint(profile, settings),
        "probe_fingerprint": (row.probe_fingerprint or "") if row is not None else "",
    }


async def _prepare_judge_activate(session: AsyncSession, settings: Settings, params: dict[str, Any], requester: Requester) -> Prepared:
    from app.services.judge_activation import maybe_refresh, readiness
    from app.services.judge_profiles_admin import JudgeProfileAdminError, _profile_or_error

    await maybe_refresh(session, settings, force=True)
    profile = _profile_or_error(settings, str(params["profile"]))
    ready = await readiness(session, settings, profile)
    if not ready["ready"]:
        raise JudgeProfileAdminError(
            "judge_profile_not_ready", "; ".join(item["message"] for item in ready["reasons"]), 409, extra={"reasons": ready["reasons"]}
        )
    binding = await _judge_binding(session, settings, profile.name)
    return Prepared(target_ref=f"judge:{profile.name}", tenant_id=None, payload=binding)


async def _check_judge_activate(session: AsyncSession, settings: Settings, payload: dict[str, Any]) -> None:
    current = await _judge_binding(session, settings, str(payload["profile"]))
    if current != {key: payload.get(key) for key in current}:
        raise ChangeRequestError("change_request_stale", "The judge profile configuration or probe changed since the request.", 409)


async def _execute_judge_activate(session: AsyncSession, settings: Settings, payload: dict[str, Any], ctx: ExecContext) -> Any:
    from app.services.judge_profiles_admin import activate_profile

    return await activate_profile(session, settings, str(payload["profile"]), actor=ctx.actor)


def _summary_judge_activate(result: Any) -> dict[str, Any]:
    return {"profile": result.get("profile"), "active": bool(result.get("active")), "fingerprint": result.get("fingerprint", "")}


def _policy_state(tenant_id: str, policy: Any) -> dict[str, Any]:
    from app.services.review_queue import policy_to_dict

    view = policy_to_dict(tenant_id, policy)
    return {
        "exists": policy is not None,
        "protected_paths": view["protected_paths"],
        "allow_default_protected_paths": view["allow_default_protected_paths"],
        "allow_external_judge": view["allow_external_judge"],
        "external_judge_profiles": view["external_judge_profiles"],
    }


async def policy_digest(session: AsyncSession, tenant_id: str) -> str:
    """D41: hash of the tenant's current evaluation policy (the "previous setting" bound into a request)."""
    from app.services.review_queue import get_evaluation_policy

    return sha256_hex(_policy_state(tenant_id, await get_evaluation_policy(session, tenant_id)))


async def _policy_needs_dual_control(session: AsyncSession, params: dict[str, Any]) -> bool:
    """D42: only a relaxation (fewer effective protected paths, defaults off) needs a second person."""
    from app.services.review_queue import policy_relaxes

    return await policy_relaxes(
        session,
        tenant_id=str(params["tenant_id"]),
        protected_paths=list(params.get("protected_paths") or []),
        allow_default_protected_paths=bool(params.get("allow_default_protected_paths")),
    )


async def _prepare_policy(session: AsyncSession, settings: Settings, params: dict[str, Any], requester: Requester) -> Prepared:
    tenant_id = str(params["tenant_id"])
    payload = {
        "tenant_id": tenant_id,
        "protected_paths": [str(item) for item in params.get("protected_paths") or []],
        "allow_default_protected_paths": bool(params.get("allow_default_protected_paths")),
        "allow_external_judge": params.get("allow_external_judge"),
        "external_judge_profiles": params.get("external_judge_profiles"),
        "updated_by": str(params.get("updated_by") or ""),
        "previous_policy_digest": await policy_digest(session, tenant_id),
    }
    return Prepared(target_ref=f"tenant:{tenant_id}", tenant_id=tenant_id, payload=payload)


async def _check_policy(session: AsyncSession, settings: Settings, payload: dict[str, Any]) -> None:
    if payload.get("previous_policy_digest") != await policy_digest(session, str(payload["tenant_id"])):
        raise ChangeRequestError("change_request_stale", "The evaluation policy changed since the request.", 409)


async def _execute_policy(session: AsyncSession, settings: Settings, payload: dict[str, Any], ctx: ExecContext) -> Any:
    from app.services.review_queue import upsert_evaluation_policy

    return await upsert_evaluation_policy(
        session,
        tenant_id=str(payload["tenant_id"]),
        protected_paths=list(payload.get("protected_paths") or []),
        allow_default_protected_paths=bool(payload.get("allow_default_protected_paths")),
        updated_by=ctx.actor if ctx.change_id else str(payload.get("updated_by") or ""),
        allow_external_judge=payload.get("allow_external_judge"),
        external_judge_profiles=payload.get("external_judge_profiles"),
    )


def _summary_policy(result: Any) -> dict[str, Any]:
    from app.services.review_queue import policy_to_dict

    view = policy_to_dict(result.tenant_id, result)
    return {"tenant_id": result.tenant_id, "effective_protected_paths": view["effective_protected_paths"]}


async def _no_check(session: AsyncSession, settings: Settings, payload: dict[str, Any]) -> None:
    """Retention apply and hold release re-check inside the service itself (digest, TTL, mode, hold status)."""
    return None


async def _always(session: AsyncSession, params: dict[str, Any]) -> bool:
    return True


@dataclass(frozen=True)
class _Spec:
    prepare: Callable[[AsyncSession, Settings, dict[str, Any], Requester], Awaitable[Prepared]]
    check: Callable[[AsyncSession, Settings, dict[str, Any]], Awaitable[None]]
    execute: Callable[[AsyncSession, Settings, dict[str, Any], ExecContext], Awaitable[Any]]
    summary: Callable[[Any], dict[str, Any]]
    needs_dual_control: Callable[[AsyncSession, dict[str, Any]], Awaitable[bool]] = _always


SPECS: dict[str, _Spec] = {
    RETENTION_APPLY: _Spec(_prepare_retention_apply, _no_check, _execute_retention_apply, _summary_retention_apply),
    LEGAL_HOLD_RELEASE: _Spec(_prepare_hold_release, _no_check, _execute_hold_release, _summary_hold_release),
    JUDGE_ACTIVATE: _Spec(_prepare_judge_activate, _check_judge_activate, _execute_judge_activate, _summary_judge_activate),
    POLICY_PROTECTED_PATHS: _Spec(_prepare_policy, _check_policy, _execute_policy, _summary_policy, _policy_needs_dual_control),
}

# Errors that end a change request as ``failed`` with the service's own code (never retried, D40).
def _domain_error_code(exc: Exception) -> str | None:
    from app.services.judge_profiles_admin import JudgeProfileAdminError
    from app.services.retention import RetentionError

    if isinstance(exc, (ChangeRequestError, RetentionError, JudgeProfileAdminError)):
        return str(exc.code)
    return None


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


def _event(session: AsyncSession, cr: ChangeRequest, status: str, *, actor: str, reason: str, extra: dict[str, Any] | None = None) -> None:
    """D43: one ``change_request.<status>`` governance event per transition (appended to the audit chain at commit)."""
    add_governance_event(
        session,
        tenant_id=cr.tenant_id or SYSTEM_TENANT,
        user_id=actor[:128],
        event_type=f"change_request.{status}",
        decision=status,
        reason=reason[:500],
        details={
            "change_id": cr.change_id,
            "action": cr.action,
            "target_ref": cr.target_ref,
            "payload_digest": cr.payload_digest,
            "requested_by": cr.requested_by,
            "decided_by": cr.decided_by or "",
            "error_code": cr.error_code or "",
            **(extra or {}),
        },
    )
    CHANGE_REQUESTS_TOTAL.labels(action=cr.action, status=status).inc()


# ---------------------------------------------------------------------------
# request
# ---------------------------------------------------------------------------


def _new_change_id() -> str:
    return "chg_" + secrets.token_hex(12)


async def _lock_target(session: AsyncSession, action: str, target_ref: str) -> None:
    """At most one ``pending`` request per ``(action, target_ref)`` (D40): serialize creation on Postgres."""
    bind = session.bind
    if bind is None or getattr(bind.dialect, "name", "") != "postgresql":
        return
    key = int.from_bytes(hashlib.sha256(f"change_request:{action}:{target_ref}".encode()).digest()[:8], "big", signed=True)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def pending_for(session: AsyncSession, action: str, target_ref: str) -> ChangeRequest | None:
    return await session.scalar(
        select(ChangeRequest)
        .where(ChangeRequest.action == action, ChangeRequest.target_ref == target_ref, ChangeRequest.status == PENDING)
        .order_by(ChangeRequest.id.desc())
        .limit(1)
    )


async def execute_or_request(
    session: AsyncSession,
    settings: Settings,
    action: str,
    params: dict[str, Any],
    requester: Requester,
    *,
    reason: str = "",
    state_store: Any = None,
    direct_actor: str | None = None,
) -> Outcome:
    """D39: run ``action`` now, or store a ``pending`` change request when dual control is on for it.

    ``direct_actor`` keeps an endpoint's historical audit string when the action runs at once (for example
    ``api:<key>`` for judge activation); a stored request always records ``requester.actor``."""
    if action not in SPECS:
        raise ValueError(f"Unknown change request action: {action}")
    spec = SPECS[action]
    if not is_enabled(settings, action) or not await spec.needs_dual_control(session, params):
        ctx = ExecContext(actor=direct_actor or requester.actor, role=requester.role, state_store=state_store)
        return Outcome(status=EXECUTED, result=await spec.execute(session, settings, dict(params), ctx))

    prepared = await spec.prepare(session, settings, dict(params), requester)
    digest = sha256_hex(prepared.payload)
    await _lock_target(session, action, prepared.target_ref)
    await expire_stale(session, settings)
    existing = await pending_for(session, action, prepared.target_ref)
    if existing is not None:
        if existing.payload_digest != digest:
            raise ChangeRequestError(
                "change_request_pending_exists",
                f"Change request {existing.change_id} is already pending for this target with a different payload; cancel it first.",
                409,
                details={"change_id": existing.change_id},
            )
        return Outcome(status=PENDING_SECOND_APPROVAL, change_request=existing, created=False)

    now = _now()
    expires_at = now + _ttl(settings)
    if prepared.expires_cap is not None:
        expires_at = min(expires_at, prepared.expires_cap)
    cr = ChangeRequest(
        change_id=_new_change_id(),
        action=action,
        target_ref=prepared.target_ref,
        tenant_id=prepared.tenant_id,
        payload=prepared.payload,
        payload_digest=digest,
        status=PENDING,
        requested_by=requester.actor[:128],
        requested_reason=(reason or "").strip()[:2000],
        requested_at=now,
        expires_at=expires_at,
        result={"requester": requester.as_dict()},
        error_code="",
    )
    session.add(cr)
    _event(session, cr, "created", actor=requester.actor, reason=f"{ACTION_LABELS[action]} requested; a second person must approve it.")
    await session.commit()
    return Outcome(status=PENDING_SECOND_APPROVAL, change_request=cr, created=True)


# ---------------------------------------------------------------------------
# decide, cancel, expire
# ---------------------------------------------------------------------------


async def get_change_request(session: AsyncSession, change_id: str) -> ChangeRequest | None:
    return await session.scalar(select(ChangeRequest).where(ChangeRequest.change_id == change_id))


async def _load_pending(session: AsyncSession, settings: Settings, change_id: str) -> ChangeRequest:
    cr = await get_change_request(session, change_id)
    if cr is None:
        raise ChangeRequestError("change_request_not_found", f"Change request {change_id} not found.", 404)
    if cr.status == PENDING and _aware(cr.expires_at) <= _now():
        await _expire(session, cr)
        await session.commit()
    if cr.status == EXPIRED:
        raise ChangeRequestError("change_request_expired", "This change request expired; submit a new one.", 409)
    if cr.status != PENDING:
        raise ChangeRequestError("change_request_not_pending", f"This change request is {cr.status}.", 409)
    return cr


def requester_user_id(cr: ChangeRequest) -> str:
    return str(((cr.result or {}).get("requester") or {}).get("user_id") or "")


def _check_decider(cr: ChangeRequest, approver: Principal | None, settings: Settings) -> Principal:
    """D40 rules shared by approve and reject. Raises ``ChangeRequestError`` (403) before anything changes."""
    if approver is None or approver.synthetic or not approver.session_id:
        raise ChangeRequestError(
            "change_request_sign_in_required", "Only a signed-in Dashboard user can approve or reject a change request.", 403
        )
    if approver.actor == cr.requested_by or (requester_user_id(cr) and approver.user_id == requester_user_id(cr)):
        raise ChangeRequestError(
            "change_request_self_approval_forbidden", "The requester cannot approve or reject their own change request.", 403
        )
    if not has_permission(approver.role, ACTION_PERMISSIONS[cr.action]):
        raise ChangeRequestError(
            "change_request_forbidden",
            f"Deciding this change request needs the {ACTION_PERMISSIONS[cr.action]} permission.",
            403,
            details={"permission": ACTION_PERMISSIONS[cr.action]},
        )
    if not approver.is_platform and (cr.tenant_id or "") != approver.tenant_id:
        raise ChangeRequestError("change_request_tenant_forbidden", "This change request belongs to another tenant.", 403)
    if not reauth_is_fresh(approver.reauth_at, settings):
        raise ChangeRequestError("change_request_reauth_required", "Re-authenticate before deciding a change request.", 403)
    return approver


async def _claim(session: AsyncSession, cr: ChangeRequest, status: str, *, actor: str, reason: str, now: datetime) -> None:
    claimed = await session.execute(
        update(ChangeRequest)
        .where(ChangeRequest.change_id == cr.change_id, ChangeRequest.status == PENDING)
        .values(status=status, decided_by=actor[:128], decision_reason=(reason or "").strip()[:2000], decided_at=now)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        await session.rollback()
        raise ChangeRequestError("change_request_not_pending", "This change request was decided by someone else.", 409)
    cr.status = status
    cr.decided_by = actor[:128]
    cr.decision_reason = (reason or "").strip()[:2000]
    cr.decided_at = now


async def approve(
    session: AsyncSession,
    settings: Settings,
    change_id: str,
    approver: Principal | None,
    *,
    reason: str = "",
    state_store: Any = None,
) -> ChangeRequest:
    """D40: re-check, mark ``approved`` and run the stored action now; ends ``executed`` or ``failed``."""
    cr = await _load_pending(session, settings, change_id)
    approver = _check_decider(cr, approver, settings)
    now = _now()
    await _claim(session, cr, APPROVED, actor=approver.actor, reason=reason, now=now)
    _event(session, cr, "approved", actor=approver.actor, reason=f"Approved by {approver.actor}; executing now.")
    await session.commit()

    spec = SPECS[cr.action]
    payload = dict(cr.payload or {})
    requester = dict((cr.result or {}).get("requester") or {})
    try:
        await spec.check(session, settings, payload)
        result = await spec.execute(session, settings, payload, ExecContext(actor=approver.actor, role=approver.role, state_store=state_store, change_id=cr.change_id))
    except Exception as exc:  # noqa: BLE001 - every failure is recorded (unexpected ones are also logged)
        code = _domain_error_code(exc)
        await session.rollback()
        cr = await get_change_request(session, change_id)
        cr.status = FAILED
        cr.error_code = (code or EXECUTION_ERROR)[:64]
        cr.executed_at = None
        cr.result = {"requester": requester, "error": {"code": cr.error_code, "message": str(getattr(exc, "message", "") or exc)[:500]}}
        _event(session, cr, "failed", actor=approver.actor, reason=f"Execution failed: {cr.error_code}.")
        await session.commit()
        if code is None:
            logger.exception("change request %s failed with an unexpected error", change_id)
        return cr

    summary = spec.summary(result)
    cr = await get_change_request(session, change_id)
    cr.status = EXECUTED
    cr.executed_at = _now()
    cr.result = {"requester": requester, "execution": summary}
    _event(session, cr, "executed", actor=approver.actor, reason=f"{ACTION_LABELS[cr.action]} executed.", extra={"execution": summary})
    await session.commit()
    return cr


async def reject(session: AsyncSession, settings: Settings, change_id: str, approver: Principal | None, *, reason: str = "") -> ChangeRequest:
    cr = await _load_pending(session, settings, change_id)
    approver = _check_decider(cr, approver, settings)
    await _claim(session, cr, REJECTED, actor=approver.actor, reason=reason, now=_now())
    _event(session, cr, "rejected", actor=approver.actor, reason=f"Rejected by {approver.actor}; nothing was executed.")
    await session.commit()
    return cr


def is_requester(cr: ChangeRequest, requester: Requester) -> bool:
    if requester.user_id and requester_user_id(cr):
        return requester.user_id == requester_user_id(cr)
    return requester.actor == cr.requested_by


async def cancel(session: AsyncSession, settings: Settings, change_id: str, requester: Requester, *, reason: str = "") -> ChangeRequest:
    """D40: only the requester can cancel a pending request (nothing is executed)."""
    cr = await _load_pending(session, settings, change_id)
    if not is_requester(cr, requester):
        raise ChangeRequestError("change_request_cancel_forbidden", "Only the requester can cancel a change request.", 403)
    await _claim(session, cr, CANCELLED, actor=requester.actor, reason=reason, now=_now())
    _event(session, cr, "cancelled", actor=requester.actor, reason="Cancelled by the requester; nothing was executed.")
    await session.commit()
    return cr


async def _expire(session: AsyncSession, cr: ChangeRequest) -> None:
    cr.status = EXPIRED
    cr.decided_at = _now()
    cr.decided_by = ""
    _event(session, cr, "expired", actor="system:change_requests", reason="Expired without a second approval; nothing was executed.")


async def expire_stale(session: AsyncSession, settings: Settings, *, now: datetime | None = None) -> int:
    """Mark pending requests past ``expires_at`` as ``expired`` (one event each). Commits when anything changed."""
    now = _aware(now) or _now()
    rows = list((await session.scalars(select(ChangeRequest).where(ChangeRequest.status == PENDING))).all())
    stale = [row for row in rows if _aware(row.expires_at) <= now]
    for row in stale:
        await _expire(session, row)
    if stale:
        await session.commit()
    return len(stale)


async def list_change_requests(
    session: AsyncSession, *, tenant_id: str | None = None, status: str | None = None, action: str | None = None, limit: int = 100
) -> list[ChangeRequest]:
    stmt = select(ChangeRequest).order_by(ChangeRequest.id.desc()).limit(max(1, min(int(limit), 500)))
    if tenant_id is not None:
        stmt = stmt.where(ChangeRequest.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(ChangeRequest.status == status)
    if action:
        stmt = stmt.where(ChangeRequest.action == action)
    return list((await session.scalars(stmt)).all())


def to_dict(cr: ChangeRequest) -> dict[str, Any]:
    result = dict(cr.result or {})
    return {
        "change_id": cr.change_id,
        "action": cr.action,
        "action_label": ACTION_LABELS.get(cr.action, cr.action),
        "required_permission": ACTION_PERMISSIONS.get(cr.action, ""),
        "target_ref": cr.target_ref,
        "tenant_id": cr.tenant_id,
        "payload": dict(cr.payload or {}),
        "payload_digest": cr.payload_digest,
        "status": cr.status,
        "requested_by": cr.requested_by,
        "requested_reason": cr.requested_reason or "",
        "requested_at": iso_utc(cr.requested_at),
        "decided_by": cr.decided_by or "",
        "decision_reason": cr.decision_reason or "",
        "decided_at": iso_utc(cr.decided_at),
        "expires_at": iso_utc(cr.expires_at),
        "executed_at": iso_utc(cr.executed_at),
        "execution": result.get("execution") or {},
        "error": result.get("error") or {},
        "error_code": cr.error_code or "",
    }


def pending_response(outcome: Outcome) -> dict[str, Any]:
    """D39: the body the four API endpoints return with ``202`` when dual control applies."""
    assert outcome.change_request is not None
    return {"status": PENDING_SECOND_APPROVAL, "created": outcome.created, "change_request": to_dict(outcome.change_request)}


__all__ = [
    "ACTIONS",
    "ACTION_LABELS",
    "ACTION_PERMISSIONS",
    "ChangeRequestError",
    "Outcome",
    "PENDING_SECOND_APPROVAL",
    "Requester",
    "STATUSES",
    "approve",
    "cancel",
    "configured_actions",
    "execute_or_request",
    "expire_stale",
    "get_change_request",
    "is_enabled",
    "is_requester",
    "list_change_requests",
    "pending_response",
    "policy_digest",
    "reject",
    "to_dict",
]
