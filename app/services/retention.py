"""v1.6.0 audit retention: plan -> apply purges, legal holds, hash-only tombstones (D9-D17).

Safety rules (see ``docs/plan`` D9-D16):

- A run is purged only through a stored plan (``build_plan``) applied with the same ``plan_digest`` inside
  the plan TTL (``apply_plan``), and only when ``AUDIT_RETENTION_PURGE_MODE=apply``. The default is ``plan``.
- Every run is re-checked inside its own transaction before deletion; a run that gained an exclusion
  (for example a new legal hold) is recorded in ``skipped_at_apply`` and left untouched.
- Excluded runs (``EXCLUSION_REASONS``) are never purged. Human review labels that were not exported to the
  benchmark corpus and active benchmark samples block a purge, so ground truth is never lost.
- The FinOps ledger (usage / cost events, budget violations), benchmark, honeypot and judge profile tables and
  governance events without a ``run_id`` are never deleted (D10, D14).
- A purged run leaves a tombstone with record counts and a ``content_digest`` made of hashes only.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core import tracing
from app.core.digests import canonical_json, sha256_hex
from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.models.approval_event import ApprovalEvent
from app.models.benchmark import BenchmarkSample, HoneypotLaunch
from app.models.checkpoint import ReplayAttempt, RunCheckpoint
from app.models.control_plane import ExecutionPlan, ExecutionPlanStep
from app.models.evaluation_result import EvaluationResultRecord
from app.models.evaluation_review import EvaluationReview, RunOutcome
from app.models.finops import BudgetViolation, CostEvent, UsageEvent
from app.models.governance_event import GovernanceEvent
from app.models.incident import Incident
from app.models.judge import JudgeAssessment, ReviewSamplingDecision
from app.models.retention import LegalHold, RetentionPurge, RetentionTombstone
from app.models.tool_execution import ToolExecution
from app.models.worker_attempt import WorkerAttempt
from app.services.governance_audit import persist_governance_event

RETENTION_POLICY_VERSION = "v1.6.0"
JOB_ACTOR = "job:retention"
SYSTEM_TENANT = "system"
ADVISORY_LOCK_KEY = 0x52485F5245544E  # fixed key ("RH_RETN"); Postgres only
PURGE_MODES = ("off", "plan", "apply")
TERMINAL_STATUSES = frozenset({"completed", "denied", "rejected", "failed"})
OPEN_REVIEW_STATUSES = ("open", "needs_info")
HIGH_SEVERITIES = ("high", "critical")
# Mirrors app.services.benchmark.EXPORTABLE_LABELS: a closed review with one of these labels is ground truth.
EXPORTABLE_LABELS = ("confirmed_hacking", "false_positive", "accepted")
EXCLUSION_REASONS: tuple[str, ...] = (
    "legal_hold",
    "run_not_terminal",
    "dlq_unresolved",
    "review_open",
    "unexported_label",
    "benchmark_reference",
    "high_severity_incident",
    "checkpoint_reference",
    "replay_reference",
    "honeypot_measurement",
)
# Skip-only codes used at apply time in addition to the D9 reasons.
SKIP_RECENT_ACTIVITY = "recent_activity"
SKIP_RUN_MISSING = "run_missing"
HOLD_SCOPES = ("tenant", "run")
HOLD_CREATE_ROLES = ("admin", "tenant_admin")
EXAMPLES_PER_REASON = 5
SCAN_PAGE_LIMIT = 20  # build_plan scans at most batch_size * SCAN_PAGE_LIMIT expired runs
DELETE_CHUNK = 500
IN_CHUNK = 500

# D10: run-scoped tables deleted for a purged run, children first; (label, model, run column, extra filter).
# Deliberately absent: usage_events, cost_events, budget_violations (FinOps ledger), benchmark_*, honeypot_*,
# judge_profile_*, governance events without run_id, operations_*, tenants and keys.
_DELETE_SPECS: tuple[tuple[str, Any, Any, Any], ...] = (
    ("approval_events", ApprovalEvent, ApprovalEvent.run_id, None),
    ("worker_attempts", WorkerAttempt, WorkerAttempt.run_id, None),
    ("tool_executions", ToolExecution, ToolExecution.run_id, None),
    ("evaluation_results", EvaluationResultRecord, EvaluationResultRecord.run_id, None),
    ("review_sampling_decisions", ReviewSamplingDecision, ReviewSamplingDecision.run_id, None),
    ("judge_assessments", JudgeAssessment, JudgeAssessment.run_id, JudgeAssessment.benchmark_run_id == ""),
    ("evaluation_reviews", EvaluationReview, EvaluationReview.run_id, None),
    ("run_outcomes", RunOutcome, RunOutcome.run_id, None),
    ("incidents", Incident, Incident.run_id, Incident.severity.notin_(HIGH_SEVERITIES)),
    ("execution_plan_steps", ExecutionPlanStep, ExecutionPlanStep.run_id, None),
    ("execution_plans", ExecutionPlan, ExecutionPlan.run_id, None),
    ("run_checkpoints", RunCheckpoint, RunCheckpoint.run_id, None),
    ("replay_attempts", ReplayAttempt, ReplayAttempt.source_run_id, None),
    ("governance_events", GovernanceEvent, GovernanceEvent.run_id, None),
    ("agent_runs", AgentRun, AgentRun.run_id, None),
)
DELETED_TABLES: tuple[str, ...] = tuple(spec[0] for spec in _DELETE_SPECS)
LEDGER_TABLES = (("usage_events", UsageEvent), ("cost_events", CostEvent), ("budget_violations", BudgetViolation))


class RetentionError(Exception):
    """A refused retention operation; ``code`` is the stable API error code, ``status_code`` its HTTP status."""

    def __init__(self, code: str, message: str, status_code: int = 409, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC so comparisons and digests are stable."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.astimezone(timezone.utc).isoformat() if value else None


# canonical_json() / sha256_hex() moved to app.core.digests in v1.6.1 (plan D29); re-exported here.


def row_digest(row: Any) -> str:
    """sha256 of a row's canonical JSON (every mapped column, datetimes as UTC ISO-8601)."""
    return sha256_hex({column.key: getattr(row, column.key) for column in row.__mapper__.column_attrs})


def content_digest(entries: Iterable[tuple[str, int, str]]) -> str:
    """D13: sha256 of the sorted ``(table, primary key, row digest)`` list. Hashes only, never content."""
    return sha256_hex(sorted([table, int(pk), digest] for table, pk, digest in entries))


def purge_mode(settings: Settings) -> str:
    mode = str(getattr(settings, "audit_retention_purge_mode", "plan") or "").strip().lower()
    return mode if mode in PURGE_MODES else "off"  # fail closed on a typo


def _batch_size(settings: Settings) -> int:
    return max(1, int(settings.audit_retention_purge_batch_size))


def policy_snapshot(settings: Settings) -> dict[str, Any]:
    return {
        "retention_days": int(settings.audit_retention_days),
        "batch_size": _batch_size(settings),
        "policy_version": RETENTION_POLICY_VERSION,
    }


def compute_plan_digest(cutoff: datetime, snapshot: dict[str, Any], candidate_run_ids: Iterable[str]) -> str:
    """D11: sha256 of canonical JSON (cutoff, settings snapshot, sorted candidate run ids)."""
    return sha256_hex({"cutoff": _iso(cutoff), "settings": snapshot, "candidates": sorted(candidate_run_ids)})


def _chunks(items: list[str], size: int = IN_CHUNK) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def _is_postgres(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "postgresql"


# ---------------------------------------------------------------------------
# D9: eligibility and exclusion reasons
# ---------------------------------------------------------------------------


async def _latest_activity(session: AsyncSession, run_ids: list[str]) -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    sources = (
        (RunOutcome.run_id, RunOutcome.created_at),
        (EvaluationReview.run_id, EvaluationReview.updated_at),
        (WorkerAttempt.run_id, WorkerAttempt.created_at),
    )
    for chunk in _chunks(run_ids):
        for run_col, time_col in sources:
            rows = await session.execute(select(run_col, func.max(time_col)).where(run_col.in_(chunk)).group_by(run_col))
            for run_id, moment in rows.all():
                moment = _aware(moment)
                if moment and (run_id not in latest or moment > latest[run_id]):
                    latest[run_id] = moment
    return latest


async def _reasons_for_runs(session: AsyncSession, runs: list[AgentRun], cutoff: datetime) -> dict[str, list[str]]:
    """D9 exclusion reasons for each run (all of them, in ``EXCLUSION_REASONS`` order)."""
    found: dict[str, set[str]] = {run.run_id: set() for run in runs}
    run_ids = list(found)
    tenant_of = {run.run_id: run.tenant_id for run in runs}
    for run in runs:
        if run.status not in TERMINAL_STATUSES:
            found[run.run_id].add("run_not_terminal")

    held_tenants: set[str] = set()
    tenants = sorted(set(tenant_of.values()))
    for chunk in _chunks(tenants):
        rows = await session.execute(
            select(LegalHold.scope_id).where(LegalHold.status == "active", LegalHold.scope_type == "tenant", LegalHold.scope_id.in_(chunk))
        )
        held_tenants.update(rows.scalars().all())
    for run_id, tenant_id in tenant_of.items():
        if tenant_id in held_tenants:
            found[run_id].add("legal_hold")

    async def mark(reason: str, column: Any, *conditions: Any) -> None:
        for chunk in _chunks(run_ids):
            rows = await session.execute(select(column).where(column.in_(chunk), *conditions).distinct())
            for run_id in rows.scalars().all():
                found[run_id].add(reason)

    await mark("legal_hold", LegalHold.scope_id, LegalHold.status == "active", LegalHold.scope_type == "run")
    await mark("dlq_unresolved", WorkerAttempt.run_id, WorkerAttempt.status == "dlq")
    await mark("review_open", EvaluationReview.run_id, EvaluationReview.status.in_(OPEN_REVIEW_STATUSES))
    await mark(
        "unexported_label",
        EvaluationReview.run_id,
        EvaluationReview.label.in_(EXPORTABLE_LABELS),
        EvaluationReview.exported_sample_id == "",
    )
    await mark("benchmark_reference", BenchmarkSample.origin_run_id, BenchmarkSample.status == "active")
    await mark("high_severity_incident", Incident.run_id, Incident.severity.in_(HIGH_SEVERITIES))
    # A checkpoint / replay that is itself still inside the retention window protects the run; expired ones
    # are purged together with it (D9 / D10).
    await mark("checkpoint_reference", RunCheckpoint.run_id, RunCheckpoint.created_at >= cutoff)
    await mark("replay_reference", ReplayAttempt.source_run_id, ReplayAttempt.created_at >= cutoff)
    await mark("honeypot_measurement", HoneypotLaunch.run_id)
    return {run_id: [reason for reason in EXCLUSION_REASONS if reason in reasons] for run_id, reasons in found.items()}


def retention_cutoff(settings: Settings, now: datetime | None = None) -> datetime:
    return (_aware(now) or _now()) - timedelta(days=int(settings.audit_retention_days))


async def exclusion_reasons(
    session: AsyncSession, run_id: str, *, settings: Settings, now: datetime | None = None
) -> list[str]:
    """D9 exclusion reason codes for one run (empty list = not excluded). Unknown run -> ``[]``."""
    run = (await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))).scalar_one_or_none()
    if run is None:
        return []
    return (await _reasons_for_runs(session, [run], retention_cutoff(settings, now)))[run_id]


async def _expired_by_age(session: AsyncSession, runs: list[AgentRun], cutoff: datetime) -> set[str]:
    latest = await _latest_activity(session, [run.run_id for run in runs])
    return {
        run.run_id
        for run in runs
        if _aware(run.updated_at) < cutoff and (run.run_id not in latest or latest[run.run_id] < cutoff)
    }


# ---------------------------------------------------------------------------
# D11: plan
# ---------------------------------------------------------------------------


async def _estimated_counts(session: AsyncSession, run_ids: list[str]) -> dict[str, int]:
    counts = {label: 0 for label in DELETED_TABLES}
    for chunk in _chunks(run_ids):
        for label, _model, run_col, extra in _DELETE_SPECS:
            stmt = select(func.count()).where(run_col.in_(chunk))
            if extra is not None:
                stmt = stmt.where(extra)
            counts[label] += int((await session.execute(stmt)).scalar_one())
    return counts


async def build_plan(
    session: AsyncSession, settings: Settings, *, actor: str = "system", now: datetime | None = None
) -> RetentionPurge:
    """Read-only selection of purge candidates, stored as a ``planned`` RetentionPurge with a TTL (D11)."""
    with tracing.start_span("retention.plan") as span:
        purge = await _build_plan(session, settings, actor=actor, now=now)
        span.set_attributes(
            {
                "rh.status": purge.status,
                "rh.retention.purge_id": purge.purge_id,
                "rh.retention.candidates": len(purge.candidate_run_ids or []),
                "rh.retention.excluded_runs": int((purge.scan_summary or {}).get("excluded_runs", 0)),
            }
        )
        return purge


async def _build_plan(
    session: AsyncSession, settings: Settings, *, actor: str, now: datetime | None
) -> RetentionPurge:
    if purge_mode(settings) == "off":
        raise RetentionError("retention_disabled", "AUDIT_RETENTION_PURGE_MODE is off.", 409)
    now = _aware(now) or _now()
    cutoff = retention_cutoff(settings, now)
    batch = _batch_size(settings)
    candidates: list[str] = []
    excluded: dict[str, dict[str, Any]] = {}
    excluded_runs = 0
    recent_activity = 0
    scanned = 0
    last_id = 0
    truncated = False
    for page in range(SCAN_PAGE_LIMIT + 1):
        if page == SCAN_PAGE_LIMIT:
            truncated = True
            break
        runs = list(
            (
                await session.execute(
                    select(AgentRun).where(AgentRun.updated_at < cutoff, AgentRun.id > last_id).order_by(AgentRun.id.asc()).limit(batch)
                )
            ).scalars()
        )
        if not runs:
            break
        last_id = runs[-1].id
        scanned += len(runs)
        expired = await _expired_by_age(session, runs, cutoff)
        recent_activity += len(runs) - len(expired)
        aged = [run for run in runs if run.run_id in expired]
        reasons = await _reasons_for_runs(session, aged, cutoff) if aged else {}
        for run in aged:
            run_reasons = reasons[run.run_id]
            if run_reasons:
                excluded_runs += 1
                for reason in run_reasons:
                    entry = excluded.setdefault(reason, {"count": 0, "examples": []})
                    entry["count"] += 1
                    if len(entry["examples"]) < EXAMPLES_PER_REASON:
                        entry["examples"].append(run.run_id)
            elif len(candidates) < batch:
                candidates.append(run.run_id)
        if len(candidates) >= batch:
            truncated = True
            break

    snapshot = policy_snapshot(settings)
    purge = RetentionPurge(
        purge_id=_new_id("purge_"),
        status="planned",
        plan_digest=compute_plan_digest(cutoff, snapshot, candidates),
        cutoff_at=cutoff,
        expires_at=now + timedelta(minutes=max(1, int(settings.audit_retention_plan_ttl_minutes))),
        settings_snapshot=snapshot,
        candidate_run_ids=sorted(candidates),
        excluded_summary={reason: excluded[reason] for reason in EXCLUSION_REASONS if reason in excluded},
        estimated_counts=await _estimated_counts(session, candidates),
        scan_summary={
            "scanned_runs": scanned,
            "excluded_runs": excluded_runs,
            "recent_activity_runs": recent_activity,
            "truncated": truncated,
        },
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    session.add(purge)
    await session.commit()
    await persist_governance_event(
        session,
        tenant_id=SYSTEM_TENANT,
        user_id=actor,
        event_type="retention.plan_created",
        decision="planned",
        reason="Retention purge plan created.",
        used=len(candidates),
        details={
            "purge_id": purge.purge_id,
            "plan_digest": purge.plan_digest,
            "candidate_count": len(candidates),
            "excluded": {reason: data["count"] for reason, data in purge.excluded_summary.items()},
        },
    )
    return purge


# ---------------------------------------------------------------------------
# D12 / D13: apply
# ---------------------------------------------------------------------------


async def _delete_run_records(session: AsyncSession, run_id: str) -> tuple[dict[str, int], list[tuple[str, int, str]]]:
    """Delete one run's D10 rows (keyset batches by id). Returns per-table counts and digest entries."""
    counts: dict[str, int] = {}
    entries: list[tuple[str, int, str]] = []
    for label, model, run_col, extra in _DELETE_SPECS:
        total = 0
        last_id = 0
        while True:
            stmt = select(model).where(run_col == run_id, model.id > last_id).order_by(model.id.asc()).limit(DELETE_CHUNK)
            if extra is not None:
                stmt = stmt.where(extra)
            rows = list((await session.execute(stmt)).scalars())
            if not rows:
                break
            ids = [row.id for row in rows]
            entries.extend((label, row.id, row_digest(row)) for row in rows)
            await session.execute(delete(model).where(model.id.in_(ids)).execution_options(synchronize_session=False))
            total += len(rows)
            last_id = ids[-1]
        counts[label] = total
    return counts, entries


async def _purge_one(
    session: AsyncSession, purge_id: str, cutoff: datetime, run_id: str, settings: Settings, now: datetime
) -> tuple[str, Any]:
    """One run in one transaction: lock, re-check D9, delete, tombstone, commit.

    Returns plain values only: a rollback expires ORM instances, and async sessions cannot lazy-refresh them.
    """
    stmt = select(AgentRun).where(AgentRun.run_id == run_id)
    if _is_postgres(session):
        stmt = stmt.with_for_update()
    run = (await session.execute(stmt)).scalar_one_or_none()
    if run is None:
        await session.rollback()
        return "skipped", [SKIP_RUN_MISSING]
    if run.run_id not in await _expired_by_age(session, [run], cutoff):
        await session.rollback()
        return "skipped", [SKIP_RECENT_ACTIVITY]
    reasons = (await _reasons_for_runs(session, [run], cutoff))[run.run_id]
    if reasons:
        await session.rollback()
        return "skipped", reasons
    tenant_id, final_status, run_created_at = run.tenant_id, run.status, _aware(run.created_at)
    counts, entries = await _delete_run_records(session, run_id)
    tombstone = RetentionTombstone(
        tombstone_id=_new_id("tomb_"),
        run_id=run_id,
        tenant_id=tenant_id,
        purge_id=purge_id,
        purged_at=now,
        run_created_at=run_created_at,
        final_status=final_status,
        record_counts=counts,
        content_digest=content_digest(entries),
        policy_snapshot={**policy_snapshot(settings), "cutoff": _iso(cutoff)},
        chain_version="",
        prev_hash=None,
        entry_hash=None,
    )
    session.add(tombstone)
    await session.commit()
    return "purged", {"run_id": run_id, "content_digest": tombstone.content_digest, "record_counts": counts}


async def _drop_state_keys(state_store: Any, run_id: str) -> None:
    deleter = getattr(state_store, "delete_run_keys", None)
    if deleter is None:
        return
    try:
        await deleter(run_id)
    except Exception:  # noqa: BLE001 - keys expire on their own (TTL <= 24 h); never fail a committed purge
        return


def receipt_digest(pairs: Iterable[tuple[str, str]]) -> str:
    """D13: sha256 of the sorted ``(run_id, content_digest)`` pairs of the tombstones written by one apply."""
    return sha256_hex(sorted([run_id, digest] for run_id, digest in pairs))


async def _apply_locked(
    session: AsyncSession,
    settings: Settings,
    purge: RetentionPurge,
    *,
    actor: str,
    now: datetime,
    state_store: Any,
) -> RetentionPurge:
    purge_id = purge.purge_id
    cutoff = _aware(purge.cutoff_at)
    candidates = list(purge.candidate_run_ids or [])
    claimed = await session.execute(
        update(RetentionPurge)
        .where(RetentionPurge.purge_id == purge_id, RetentionPurge.status == "planned")
        .values(status="applying", applied_by=actor, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if claimed.rowcount != 1:
        raise RetentionError("retention_plan_already_applied", "This plan was already applied or is being applied.", 409)

    applied_counts = {label: 0 for label in DELETED_TABLES}
    skipped: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    purged: list[dict[str, Any]] = []
    for run_id in candidates:
        try:
            outcome, payload = await _purge_one(session, purge_id, cutoff, run_id, settings, now)
        except Exception as exc:  # noqa: BLE001 - recorded in the receipt; committed runs stay purged (D12)
            await session.rollback()
            failures.append({"run_id": run_id, "error": type(exc).__name__})
            continue
        if outcome == "skipped":
            skipped.append({"run_id": run_id, "reasons": payload})
            continue
        purged.append(payload)
        for label, count in payload["record_counts"].items():
            applied_counts[label] = applied_counts.get(label, 0) + int(count)
        if state_store is not None:
            await _drop_state_keys(state_store, run_id)

    purge = (await session.execute(select(RetentionPurge).where(RetentionPurge.purge_id == purge_id))).scalar_one()
    purge.status = "partial" if failures else "applied"
    purge.applied_at = now
    purge.applied_by = actor
    purge.applied_counts = {**applied_counts, "runs": len(purged)}
    purge.skipped_at_apply = skipped
    purge.failures = failures
    purge.receipt_digest = receipt_digest((item["run_id"], item["content_digest"]) for item in purged)
    purge.updated_at = now
    await session.commit()
    await persist_governance_event(
        session,
        tenant_id=SYSTEM_TENANT,
        user_id=actor,
        event_type="retention.purge_applied",
        decision=purge.status,
        reason="Retention purge applied.",
        used=len(purged),
        details={
            "purge_id": purge.purge_id,
            "receipt_digest": purge.receipt_digest,
            "purged_runs": len(purged),
            "skipped_runs": len(skipped),
            "failed_runs": len(failures),
            "applied_counts": applied_counts,
        },
    )
    return purge


async def apply_plan(
    session: AsyncSession,
    settings: Settings,
    purge_id: str,
    *,
    plan_digest: str,
    confirm: bool,
    actor: str = "system",
    now: datetime | None = None,
    state_store: Any = None,
) -> RetentionPurge:
    """Apply a stored plan (D11 / D12). Raises ``RetentionError`` for every refused case."""
    with tracing.start_span("retention.apply", attributes={"rh.retention.purge_id": purge_id}) as span:
        try:
            purge = await _apply_plan(
                session, settings, purge_id, plan_digest=plan_digest, confirm=confirm, actor=actor, now=now, state_store=state_store
            )
        except RetentionError as exc:
            span.set_attributes({"rh.status": "refused", "rh.retention.error": exc.code})
            raise
        span.set_attributes(
            {
                "rh.status": purge.status,
                "rh.retention.purged_runs": int((purge.applied_counts or {}).get("runs", 0)),
                "rh.retention.skipped_runs": len(purge.skipped_at_apply or []),
                "rh.retention.failed_runs": len(purge.failures or []),
            }
        )
        return purge


async def _apply_plan(
    session: AsyncSession,
    settings: Settings,
    purge_id: str,
    *,
    plan_digest: str,
    confirm: bool,
    actor: str,
    now: datetime | None,
    state_store: Any,
) -> RetentionPurge:
    mode = purge_mode(settings)
    if mode == "off":
        raise RetentionError("retention_disabled", "AUDIT_RETENTION_PURGE_MODE is off.", 409)
    if mode != "apply":
        raise RetentionError("retention_apply_disabled", "Set AUDIT_RETENTION_PURGE_MODE=apply to apply purge plans.", 409)
    if confirm is not True:
        raise RetentionError("retention_confirm_required", "Applying a purge plan requires confirm: true.", 422)
    now = _aware(now) or _now()
    purge = (await session.execute(select(RetentionPurge).where(RetentionPurge.purge_id == purge_id))).scalar_one_or_none()
    if purge is None:
        raise RetentionError("retention_plan_not_found", f"Purge plan {purge_id} not found.", 404)
    if purge.status == "expired" or (purge.status == "planned" and _aware(purge.expires_at) <= now):
        if purge.status == "planned":
            purge.status = "expired"
            purge.updated_at = now
            await session.commit()
        raise RetentionError("retention_plan_expired", "The purge plan expired; create a new plan.", 409)
    if purge.status != "planned":
        raise RetentionError("retention_plan_already_applied", "This plan was already applied or is being applied.", 409)
    if plan_digest != purge.plan_digest:
        raise RetentionError("retention_plan_digest_mismatch", "plan_digest does not match the stored plan.", 409)

    bind = session.bind
    if _is_postgres(session) and isinstance(bind, AsyncEngine):
        # Session-level advisory lock on a dedicated connection: per-run commits may return the session's
        # connection to the pool, so the lock cannot live there.
        async with bind.connect() as lock_conn:
            got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY})).scalar()
            if not got:
                raise RetentionError("retention_purge_in_progress", "Another retention purge is running.", 409)
            try:
                return await _apply_locked(session, settings, purge, actor=actor, now=now, state_store=state_store)
            finally:
                await lock_conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY})
                await lock_conn.commit()
    return await _apply_locked(session, settings, purge, actor=actor, now=now, state_store=state_store)


async def latest_applicable_plan(session: AsyncSession, *, created_by: str, now: datetime | None = None) -> RetentionPurge | None:
    """D16 ``--apply-latest``: the newest ``planned`` plan of this creator that is still inside its TTL."""
    now = _aware(now) or _now()
    rows = await session.execute(
        select(RetentionPurge)
        .where(RetentionPurge.created_by == created_by, RetentionPurge.status == "planned")
        .order_by(RetentionPurge.created_at.desc(), RetentionPurge.id.desc())
        .limit(1)
    )
    purge = rows.scalar_one_or_none()
    if purge is None or _aware(purge.expires_at) <= now:
        return None
    return purge


# ---------------------------------------------------------------------------
# D17: tombstone lookup (410 run_purged in P5)
# ---------------------------------------------------------------------------


async def tombstone_for(session: AsyncSession, run_id: str) -> RetentionTombstone | None:
    return (await session.execute(select(RetentionTombstone).where(RetentionTombstone.run_id == run_id))).scalar_one_or_none()


def tombstone_summary(tombstone: RetentionTombstone) -> dict[str, Any]:
    return {
        "tombstone_id": tombstone.tombstone_id,
        "run_id": tombstone.run_id,
        "tenant_id": tombstone.tenant_id,
        "purge_id": tombstone.purge_id,
        "purged_at": _iso(tombstone.purged_at),
        "run_created_at": _iso(tombstone.run_created_at),
        "final_status": tombstone.final_status,
        "record_counts": dict(tombstone.record_counts or {}),
        "content_digest": tombstone.content_digest,
        "policy_version": (tombstone.policy_snapshot or {}).get("policy_version", ""),
        # v1.6.1 P5 (feature eight): written back by the audit chain in the purge transaction
        "chain_version": tombstone.chain_version or "",
        "prev_hash": tombstone.prev_hash,
        "entry_hash": tombstone.entry_hash,
    }


def purge_to_dict(purge: RetentionPurge) -> dict[str, Any]:
    return {
        "purge_id": purge.purge_id,
        "status": purge.status,
        "plan_digest": purge.plan_digest,
        "cutoff_at": _iso(purge.cutoff_at),
        "expires_at": _iso(purge.expires_at),
        "settings_snapshot": dict(purge.settings_snapshot or {}),
        "candidate_run_ids": list(purge.candidate_run_ids or []),
        "excluded_summary": dict(purge.excluded_summary or {}),
        "estimated_counts": dict(purge.estimated_counts or {}),
        "scan_summary": dict(purge.scan_summary or {}),
        "created_by": purge.created_by,
        "created_at": _iso(purge.created_at),
        "applied_by": purge.applied_by,
        "applied_at": _iso(purge.applied_at),
        "applied_counts": dict(purge.applied_counts or {}),
        "skipped_at_apply": list(purge.skipped_at_apply or []),
        "failures": list(purge.failures or []),
        "receipt_digest": purge.receipt_digest,
    }


# ---------------------------------------------------------------------------
# D15: legal holds (roles are enforced here; the API maps its principal to actor_role in P5)
# ---------------------------------------------------------------------------


async def create_hold(
    session: AsyncSession,
    *,
    scope_type: str,
    scope_id: str,
    reason: str,
    actor: str,
    actor_role: str,
    actor_tenant_id: str | None = None,
) -> LegalHold:
    if actor_role not in HOLD_CREATE_ROLES:
        raise RetentionError("legal_hold_forbidden", "Only admin or tenant_admin can create legal holds.", 403)
    if scope_type not in HOLD_SCOPES:
        raise RetentionError("legal_hold_scope_invalid", "scope_type must be 'tenant' or 'run'.", 422)
    scope_id = (scope_id or "").strip()
    reason = (reason or "").strip()
    if not scope_id:
        raise RetentionError("legal_hold_scope_invalid", "scope_id is required.", 422)
    if not reason:
        raise RetentionError("legal_hold_reason_required", "A legal hold needs a reason.", 422)
    if scope_type == "tenant":
        tenant_id = scope_id
    else:
        run = (await session.execute(select(AgentRun).where(AgentRun.run_id == scope_id))).scalar_one_or_none()
        if run is None:
            raise RetentionError("run_not_found", f"Run {scope_id} not found.", 404)
        tenant_id = run.tenant_id
    if actor_role == "tenant_admin" and tenant_id != actor_tenant_id:
        raise RetentionError("legal_hold_forbidden", "tenant_admin can only hold its own tenant.", 403)
    hold = LegalHold(
        hold_id=_new_id("lh_"),
        scope_type=scope_type,
        scope_id=scope_id,
        tenant_id=tenant_id,
        reason=reason,
        status="active",
        created_by=actor,
    )
    session.add(hold)
    await session.commit()
    await persist_governance_event(
        session,
        tenant_id=tenant_id,
        user_id=actor,
        event_type="retention.legal_hold_created",
        decision="active",
        reason="Legal hold created.",
        details={"hold_id": hold.hold_id, "scope_type": scope_type, "scope_id": scope_id},
    )
    return hold


async def release_hold(
    session: AsyncSession, hold_id: str, *, actor: str, actor_role: str, release_reason: str
) -> LegalHold:
    if actor_role != "admin":
        raise RetentionError("legal_hold_forbidden", "Only admin can release legal holds.", 403)
    release_reason = (release_reason or "").strip()
    if not release_reason:
        raise RetentionError("legal_hold_release_reason_required", "Releasing a legal hold needs a release_reason.", 422)
    hold = (await session.execute(select(LegalHold).where(LegalHold.hold_id == hold_id))).scalar_one_or_none()
    if hold is None:
        raise RetentionError("legal_hold_not_found", f"Legal hold {hold_id} not found.", 404)
    if hold.status != "active":
        raise RetentionError("legal_hold_not_active", "This legal hold is already released.", 409)
    hold.status = "released"
    hold.released_by = actor
    hold.released_at = _now()
    hold.release_reason = release_reason
    await session.commit()
    await persist_governance_event(
        session,
        tenant_id=hold.tenant_id,
        user_id=actor,
        event_type="retention.legal_hold_released",
        decision="released",
        reason="Legal hold released.",
        details={"hold_id": hold.hold_id, "scope_type": hold.scope_type, "scope_id": hold.scope_id},
    )
    return hold


async def list_holds(
    session: AsyncSession, *, tenant_id: str | None = None, status: str | None = None, limit: int = 100
) -> list[LegalHold]:
    stmt = select(LegalHold).order_by(LegalHold.id.desc()).limit(max(1, min(limit, 500)))
    if tenant_id:
        stmt = stmt.where(LegalHold.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(LegalHold.status == status)
    return list((await session.execute(stmt)).scalars())


def hold_to_dict(hold: LegalHold) -> dict[str, Any]:
    return {
        "hold_id": hold.hold_id,
        "scope_type": hold.scope_type,
        "scope_id": hold.scope_id,
        "tenant_id": hold.tenant_id,
        "reason": hold.reason,
        "status": hold.status,
        "created_by": hold.created_by,
        "created_at": _iso(hold.created_at),
        "released_by": hold.released_by,
        "released_at": _iso(hold.released_at),
        "release_reason": hold.release_reason,
    }


# ---------------------------------------------------------------------------
# P5: read helpers for the API and Dashboard
# ---------------------------------------------------------------------------


async def get_purge(session: AsyncSession, purge_id: str) -> RetentionPurge | None:
    return (await session.execute(select(RetentionPurge).where(RetentionPurge.purge_id == purge_id))).scalar_one_or_none()


async def list_purges(session: AsyncSession, *, status: str | None = None, limit: int = 50) -> list[RetentionPurge]:
    stmt = select(RetentionPurge).order_by(RetentionPurge.id.desc()).limit(max(1, min(limit, 200)))
    if status:
        stmt = stmt.where(RetentionPurge.status == status)
    return list((await session.execute(stmt)).scalars())


async def list_tombstones(
    session: AsyncSession, *, tenant_id: str | None = None, purge_id: str | None = None, limit: int = 100
) -> list[RetentionTombstone]:
    stmt = select(RetentionTombstone).order_by(RetentionTombstone.id.desc()).limit(max(1, min(limit, 500)))
    if tenant_id:
        stmt = stmt.where(RetentionTombstone.tenant_id == tenant_id)
    if purge_id:
        stmt = stmt.where(RetentionTombstone.purge_id == purge_id)
    return list((await session.execute(stmt)).scalars())


async def active_holds_for_run(session: AsyncSession, run_id: str, tenant_id: str) -> list[LegalHold]:
    """Active holds that protect this run: run-scoped ones and those on its tenant."""
    stmt = select(LegalHold).where(
        LegalHold.status == "active",
        ((LegalHold.scope_type == "run") & (LegalHold.scope_id == run_id))
        | ((LegalHold.scope_type == "tenant") & (LegalHold.scope_id == tenant_id)),
    )
    return list((await session.execute(stmt.order_by(LegalHold.id.asc()))).scalars())


def policy_view(settings: Settings) -> dict[str, Any]:
    """Effective retention policy for ``GET /v1/retention/policy`` and the Dashboard header."""
    mode = purge_mode(settings)
    return {
        "purge_mode": mode,
        "deletes_enabled": mode == "apply",
        "retention_days": int(settings.audit_retention_days),
        "batch_size": _batch_size(settings),
        "plan_ttl_minutes": int(settings.audit_retention_plan_ttl_minutes),
        "finops_ledger_retention_days": int(settings.finops_ledger_retention_days),
        "finops_ledger_policy": "report_only",
        "policy_version": RETENTION_POLICY_VERSION,
        "exclusion_reasons": list(EXCLUSION_REASONS),
    }


async def policy_health(session: AsyncSession, settings: Settings) -> dict[str, Any]:
    """Facts for the ``retention_policy`` operations check: mode, newest plan and active hold count."""
    latest = (
        await session.execute(select(RetentionPurge).order_by(RetentionPurge.id.desc()).limit(1))
    ).scalar_one_or_none()
    active_holds = int(
        (await session.execute(select(func.count()).select_from(LegalHold).where(LegalHold.status == "active"))).scalar_one()
    )
    return {
        **policy_view(settings),
        "latest_plan_at": _iso(latest.created_at) if latest is not None else None,
        "latest_plan_status": latest.status if latest is not None else None,
        "active_holds": active_holds,
    }


# ---------------------------------------------------------------------------
# D14: FinOps ledger report (never deletes)
# ---------------------------------------------------------------------------


async def ledger_report(session: AsyncSession, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    now = _aware(now) or _now()
    days = int(settings.finops_ledger_retention_days)
    cutoff = now - timedelta(days=days)
    tables: dict[str, Any] = {}
    for label, model in LEDGER_TABLES:
        row = (await session.execute(select(func.count(), func.min(model.created_at)).where(model.created_at < cutoff))).one()
        tables[label] = {"expired_count": int(row[0]), "oldest_expired_at": _iso(row[1])}
    return {
        "retention_days": days,
        "cutoff": _iso(cutoff),
        "policy": "report_only",
        "deletes": False,
        "tables": tables,
    }


__all__ = [
    "DELETED_TABLES",
    "EXCLUSION_REASONS",
    "JOB_ACTOR",
    "RetentionError",
    "apply_plan",
    "build_plan",
    "canonical_json",
    "compute_plan_digest",
    "content_digest",
    "create_hold",
    "active_holds_for_run",
    "exclusion_reasons",
    "get_purge",
    "hold_to_dict",
    "latest_applicable_plan",
    "ledger_report",
    "list_holds",
    "list_purges",
    "list_tombstones",
    "policy_health",
    "policy_view",
    "purge_mode",
    "purge_to_dict",
    "receipt_digest",
    "release_hold",
    "row_digest",
    "tombstone_for",
    "tombstone_summary",
]
