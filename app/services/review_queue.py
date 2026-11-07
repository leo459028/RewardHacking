"""v1.5.8 post-execution review queue and tenant evaluation policy.

Invariants (AGENTS.md 2.3):
- Post-execution review judges what already happened. A review decision never re-dispatches a run,
  never publishes to Kafka and never acquires or releases token leases.
- Monotonic safety: only a human review decision can move a ``review_required`` run back to
  ``completed``; automated components may only open reviews or raise their priority.
- The submitter of outcome evidence may not decide the review of the same run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.evaluation_review import EvaluationReview, RunOutcome, TenantEvaluationPolicy
from app.models.governance_event import GovernanceEvent
from app.models.incident import Incident
from app.observability.metrics import INCIDENTS_TOTAL
from app.schemas.evaluation import ReviewDecisionRequest
from app.services.cheat_rates import agent_identity
from app.services.evaluation.evidence import DEFAULT_PROTECTED_PATHS, effective_protected_paths, normalize_path
from app.services.evaluation.taxonomy import SEVERITY_RANK, higher_priority

RUN_REVIEW_REQUIRED = "review_required"
RUN_REJECTED = "rejected"
OPEN_REVIEW_STATUSES = ("open", "needs_info")
LABEL_TO_RUN_STATUS = {
    "confirmed_hacking": RUN_REJECTED,
    "false_positive": "completed",
    "accepted": "completed",
    "needs_info": RUN_REVIEW_REQUIRED,
}


class ReviewError(Exception):
    def __init__(self, code: str, detail: str, status_code: int = 409):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def new_review_id() -> str:
    return f"review_{uuid4().hex[:24]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def review_relevant_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforced findings that drive review (severity >= medium) plus legacy signals when nothing else exists."""
    enforced = [item for item in findings if item.get("mode", "enforce") == "enforce"]
    relevant = [item for item in enforced if SEVERITY_RANK.get(str(item.get("severity")), 0) >= SEVERITY_RANK["medium"]]
    if not relevant:
        relevant = [item for item in enforced if item.get("category") == "legacy_text_signal"]
    return relevant


async def get_open_review(session: AsyncSession, run_id: str) -> EvaluationReview | None:
    return await session.scalar(
        select(EvaluationReview)
        .where(EvaluationReview.run_id == run_id, EvaluationReview.status.in_(OPEN_REVIEW_STATUSES))
        .order_by(EvaluationReview.id.desc())
    )


async def get_review(session: AsyncSession, review_id: str) -> EvaluationReview | None:
    return await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))


async def open_or_merge_review(
    session: AsyncSession,
    *,
    run_id: str,
    tenant_id: str,
    evaluation: dict[str, Any],
    sample_reason: str = "rule_review",
    submitted_by: str = "",
    submitted_by_key_id: str = "",
) -> EvaluationReview:
    """Open a review for a run, or merge new findings into its open review (one open review per run)."""
    findings = review_relevant_findings(list(evaluation.get("findings") or []))
    priority = str(evaluation.get("review_priority") or "P3")
    existing = await get_open_review(session, run_id)
    if existing is None:
        run = await session.scalar(select(AgentRun).where(AgentRun.run_id == run_id))
        identity = agent_identity(((run.request_payload if run is not None else {}) or {}).get("metadata"))
        review = EvaluationReview(
            review_id=new_review_id(),
            run_id=run_id,
            tenant_id=tenant_id or "default",
            status="open",
            priority=priority,
            categories=sorted({str(item.get("category")) for item in findings}),
            finding_ids=[str(item.get("finding_id")) for item in findings],
            findings=findings,
            sample_reason=sample_reason,
            evaluator_version=str(evaluation.get("evaluator_version") or ""),
            submitted_by=submitted_by,
            submitted_by_key_id=submitted_by_key_id,
            label="",
            label_categories=[],
            reviewer="",
            reviewer_key_id="",
            reason="",
            agent_id=identity["agent_id"],
            agent_version=identity["agent_version"],
            model_name=identity["model_name"],
            finding_labels={},
            exported_sample_id="",
        )
        session.add(review)
        await session.flush()
        return review

    if sample_reason == "honeypot":
        existing.sample_reason = "honeypot"

    known = {str(item.get("finding_id")): item for item in list(existing.findings or [])}
    for item in findings:
        known.setdefault(str(item.get("finding_id")), item)
    merged = list(known.values())
    existing.findings = merged
    existing.finding_ids = list(known.keys())
    existing.categories = sorted({str(item.get("category")) for item in merged})
    existing.priority = higher_priority(existing.priority or "P3", priority)
    existing.evaluator_version = str(evaluation.get("evaluator_version") or existing.evaluator_version)
    if existing.status == "needs_info":
        existing.status = "open"
    if submitted_by_key_id:
        existing.submitted_by_key_id = submitted_by_key_id
    if submitted_by:
        existing.submitted_by = submitted_by
    await session.flush()
    return existing


async def list_reviews(
    session: AsyncSession,
    *,
    status: str | None = None,
    priority: str | None = None,
    category: str | None = None,
    tenant_id: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
) -> list[EvaluationReview]:
    stmt = select(EvaluationReview)
    if status == "active":
        stmt = stmt.where(EvaluationReview.status.in_(OPEN_REVIEW_STATUSES))
    elif status:
        stmt = stmt.where(EvaluationReview.status == status)
    if priority:
        stmt = stmt.where(EvaluationReview.priority == priority)
    if tenant_id:
        stmt = stmt.where(EvaluationReview.tenant_id == tenant_id)
    if run_id:
        stmt = stmt.where(EvaluationReview.run_id == run_id)
    stmt = stmt.order_by(EvaluationReview.priority.asc(), EvaluationReview.opened_at.asc(), EvaluationReview.id.asc())
    rows = list((await session.scalars(stmt.limit(max(limit, 1) * (5 if category else 1)))).all())
    if category:
        rows = [row for row in rows if category in list(row.categories or [])]
    return rows[:limit]


async def _submitter_keys(session: AsyncSession, review: EvaluationReview) -> tuple[set[str], set[str]]:
    rows = list((await session.scalars(select(RunOutcome).where(RunOutcome.run_id == review.run_id))).all())
    keys = {row.submitted_by_key_id for row in rows if row.submitted_by_key_id}
    names = {row.submitted_by.strip().lower() for row in rows if row.submitted_by.strip()}
    if review.submitted_by_key_id:
        keys.add(review.submitted_by_key_id)
    if review.submitted_by.strip():
        names.add(review.submitted_by.strip().lower())
    return keys, names


async def decide_review(
    session: AsyncSession,
    review_id: str,
    decision: ReviewDecisionRequest,
    *,
    reviewer_key_id: str = "",
) -> tuple[EvaluationReview, AgentRun | None] | None:
    review = await get_review(session, review_id)
    if review is None:
        return None
    if review.status not in OPEN_REVIEW_STATUSES:
        raise ReviewError("review_already_closed", "This review has already been decided.")
    keys, names = await _submitter_keys(session, review)
    if (reviewer_key_id and reviewer_key_id in keys) or (not reviewer_key_id and decision.reviewer.strip().lower() in names):
        raise ReviewError(
            "review_self_approval_forbidden",
            "The submitter of outcome evidence cannot decide the review of the same run.",
            403,
        )
    finding_labels = dict(decision.finding_labels or {})
    unknown = sorted(set(finding_labels) - set(review.finding_ids or []))
    if unknown:
        raise ReviewError(
            "finding_labels_invalid",
            f"finding_labels contains finding IDs that are not part of this review: {', '.join(unknown[:5])}.",
            422,
        )

    now = _now()
    if finding_labels:
        review.finding_labels = {**dict(review.finding_labels or {}), **finding_labels}
    review.label = decision.label
    review.label_categories = [str(item) for item in decision.label_categories] or list(review.categories or [])
    review.reviewer = decision.reviewer
    review.reviewer_key_id = reviewer_key_id
    review.reason = decision.reason
    review.decided_at = now
    review.status = "needs_info" if decision.label == "needs_info" else "closed"

    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == review.run_id))
    if run is not None:
        new_status = LABEL_TO_RUN_STATUS[decision.label]
        run.status = new_status
        run.requires_human_review = decision.label == "needs_info"
        run.decision_reason = f"Review {decision.label} by {decision.reviewer}: {decision.reason}".strip()
        summary = dict(run.evaluation_summary or {})
        summary["review"] = {
            "review_id": review.review_id,
            "status": review.status,
            "label": decision.label,
            "reviewer": decision.reviewer,
            "reason": decision.reason,
            "priority": review.priority,
            "decided_at": now.isoformat(),
        }
        summary["run_state"] = {"status": new_status, "updated_at": now.isoformat()}
        timeline = list(summary.get("event_timeline", []))
        timeline.append(
            {
                "event_type": "evaluation.review.decided",
                "status": decision.label,
                "reviewer": decision.reviewer,
                "ts": now.isoformat(),
            }
        )
        summary["event_timeline"] = timeline
        run.evaluation_summary = summary
        result = dict(run.result or {})
        response = dict(result.get("response") or {})
        if response:
            response["status"] = new_status
            response["decision_reason"] = run.decision_reason
            result["response"] = response
        result["evaluation_summary"] = summary
        run.result = result

    if decision.label == "confirmed_hacking":
        session.add(
            Incident(
                run_id=review.run_id,
                severity="high",
                code="reward_hacking_confirmed",
                detail=f"Confirmed by {decision.reviewer}: {', '.join(review.label_categories)}. {decision.reason}".strip(),
            )
        )
        INCIDENTS_TOTAL.labels(severity="high", code="reward_hacking_confirmed").inc()

    session.add(
        GovernanceEvent(
            tenant_id=review.tenant_id,
            user_id=getattr(run, "user_id", "") if run is not None else "",
            run_id=review.run_id,
            event_type="evaluation.review.decided",
            decision=decision.label,
            reason=decision.reason or decision.label,
            details={
                "review_id": review.review_id,
                "priority": review.priority,
                "categories": list(review.categories or []),
                "label_categories": list(review.label_categories or []),
                "reviewer": decision.reviewer,
                "finding_ids": list(review.finding_ids or []),
                "finding_labels": dict(review.finding_labels or {}),
            },
        )
    )
    await session.commit()
    await session.refresh(review)
    if run is not None:
        await session.refresh(run)
    return review, run


# ---------------------------------------------------------------------------
# Tenant evaluation policy (protected paths)
# ---------------------------------------------------------------------------


async def get_evaluation_policy(session: AsyncSession, tenant_id: str) -> TenantEvaluationPolicy | None:
    return await session.scalar(select(TenantEvaluationPolicy).where(TenantEvaluationPolicy.tenant_id == tenant_id))


def policy_to_dict(tenant_id: str, policy: TenantEvaluationPolicy | None) -> dict[str, Any]:
    paths = list(policy.protected_paths or []) if policy is not None else []
    allow_defaults = bool(policy.allow_default_protected_paths) if policy is not None else True
    return {
        "tenant_id": tenant_id,
        "protected_paths": paths,
        "allow_default_protected_paths": allow_defaults,
        "default_protected_paths": list(DEFAULT_PROTECTED_PATHS),
        "effective_protected_paths": effective_protected_paths(tenant_paths=paths, allow_defaults=allow_defaults, request_paths=None),
        "updated_by": policy.updated_by if policy is not None else "",
    }


async def upsert_evaluation_policy(
    session: AsyncSession,
    *,
    tenant_id: str,
    protected_paths: list[str],
    allow_default_protected_paths: bool,
    updated_by: str = "",
) -> TenantEvaluationPolicy:
    cleaned = list(dict.fromkeys(normalize_path(item) for item in protected_paths if normalize_path(item)))
    policy = await get_evaluation_policy(session, tenant_id)
    if policy is None:
        policy = TenantEvaluationPolicy(tenant_id=tenant_id)
        session.add(policy)
    policy.protected_paths = cleaned
    policy.allow_default_protected_paths = bool(allow_default_protected_paths)
    policy.updated_by = updated_by
    session.add(
        GovernanceEvent(
            tenant_id=tenant_id,
            event_type="evaluation.policy.updated",
            decision="updated",
            reason=f"Protected paths updated by {updated_by or 'unknown'}",
            details={"protected_paths": cleaned, "allow_default_protected_paths": bool(allow_default_protected_paths)},
        )
    )
    await session.commit()
    await session.refresh(policy)
    return policy


async def protected_paths_for(session: AsyncSession, tenant_id: str, request_metadata: dict[str, Any] | None) -> list[str]:
    policy = await get_evaluation_policy(session, tenant_id)
    meta = dict(request_metadata or {})
    request_paths = meta.get("protected_paths") if isinstance(meta.get("protected_paths"), list) else None
    return effective_protected_paths(
        tenant_paths=list(policy.protected_paths or []) if policy is not None else [],
        allow_defaults=bool(policy.allow_default_protected_paths) if policy is not None else True,
        request_paths=request_paths,
    )
