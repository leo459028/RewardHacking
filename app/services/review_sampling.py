"""v1.5.10 quota-capped ``sample_for_review`` for rule-passed runs (plan D14–D21).

Only outcome submissions whose deterministic verdict is ``pass`` are candidates (honeypot runs and the
simulated worker path are excluded), and each run gets exactly one decision.

- **Random audit slice**: ``HMAC-SHA256(secret, run_id)`` mapped to [0, 1) below
  ``EVALUATION_SAMPLE_RANDOM_RATE``. The secret stays server-side, so an agent cannot predict whether it
  will be audited. It has its own daily cap and no share cap (a share cap would favour later runs and
  break uniformity). It yields the unbiased miss-rate estimate that v1.5.9 cheat rates lacked.
- **Threshold slice**: ``max(suspicion_score incl. shadow findings, pre-execution risk_score)`` at or above
  ``EVALUATION_SAMPLE_MIN_SCORE``. It pulls in shadow-detector hits (for example ``special_casing``) so
  humans label them, which is the evidence a shadow → enforce promotion needs. Capped per tenant per
  UTC day and by a maximum share of the day's candidates. The judge score is never used here.

Sampling only makes a run stricter (``completed`` → ``review_required``, review priority ``P3``). It opens
no incident, publishes nothing and never touches token leases. Every candidate is recorded as
``sampled``, ``skipped`` (with a reason) or ``not_selected``: nothing is silently dropped.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.models.evaluation_review import EvaluationReview
from app.models.judge import ReviewSamplingDecision
from app.observability.metrics import EVALUATION_REVIEW_SAMPLING_TOTAL
from app.schemas.evaluation import EvaluationResult
from app.services.evaluation.aggregator import suspicion_score
from app.services.evaluation.taxonomy import SEVERITY_RANK

POLICY_NAME = "sampling:1.0.0"
SAMPLE_REASON_BY_SLICE = {"threshold": "threshold_sample", "random": "random_audit"}
SKIP_REASONS = ("daily_cap", "share_cap", "random_daily_cap")

# Used only when EVALUATION_SAMPLE_SECRET is unset: draws stay unpredictable but are not reproducible
# across restarts (reported by the ``evaluation_sampling_health`` readiness check).
_EPHEMERAL_SECRET = secrets.token_bytes(32)


def new_decision_id() -> str:
    return f"smp_{uuid4().hex[:24]}"


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")


def sample_secret(settings: Settings) -> bytes:
    configured = settings.evaluation_sample_secret.get_secret_value()
    return configured.encode("utf-8") if configured else _EPHEMERAL_SECRET


def random_draw(run_id: str, secret: bytes) -> float:
    digest = hmac.new(secret, run_id.encode("utf-8"), hashlib.sha256).hexdigest()
    return int(digest[:8], 16) / 2**32


def sampling_score(findings: list[dict[str, Any]], risk_score: float) -> float:
    return round(max(suspicion_score(findings, include_shadow=True), float(risk_score or 0.0)), 6)


def policy_settings(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": bool(settings.evaluation_sampling_enabled),
        "min_score": float(settings.evaluation_sample_min_score),
        "daily_cap_per_tenant": int(settings.evaluation_sample_daily_cap_per_tenant),
        "max_share": float(settings.evaluation_sample_max_share),
        "random_rate": float(settings.evaluation_sample_random_rate),
        "random_daily_cap_per_tenant": int(settings.evaluation_sample_random_daily_cap_per_tenant),
        "secret_configured": bool(settings.evaluation_sample_secret.get_secret_value()),
    }


def policy_version(settings: Settings) -> str:
    values = {key: value for key, value in policy_settings(settings).items() if key != "secret_configured"}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    return f"{POLICY_NAME}:{digest}"


async def _count(session: AsyncSession, tenant_id: str, day: str, **filters: str) -> int:
    stmt = select(func.count()).select_from(ReviewSamplingDecision).where(
        ReviewSamplingDecision.tenant_id == tenant_id, ReviewSamplingDecision.day == day
    )
    for name, value in filters.items():
        stmt = stmt.where(getattr(ReviewSamplingDecision, name) == value)
    return int(await session.scalar(stmt) or 0)


def _review_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sampled reviews show every non-disabled finding (incl. shadow) so reviewers can label them per finding."""
    rows = [item for item in findings if item.get("mode", "enforce") != "disabled" and item.get("detector") != "legacy_text"]
    return sorted(rows, key=lambda item: -SEVERITY_RANK.get(str(item.get("severity")), 0))


async def decide_sampling(
    session: AsyncSession,
    *,
    run: AgentRun,
    outcome_id: str,
    evaluation: EvaluationResult,
    settings: Settings,
    submitted_by: str = "",
    submitted_by_key_id: str = "",
    now: datetime | None = None,
) -> tuple[ReviewSamplingDecision | None, EvaluationReview | None]:
    """Record one sampling decision for a rule-passed run (inside the caller's transaction; no commit)."""
    if not settings.evaluation_sampling_enabled or evaluation.verdict != "pass":
        return None, None
    existing = await session.scalar(select(ReviewSamplingDecision).where(ReviewSamplingDecision.run_id == run.run_id))
    if existing is not None:
        return None, None

    tenant_id = run.tenant_id or "default"
    day = utc_day(now)
    findings = list(evaluation.findings or [])
    score = sampling_score(findings, run.risk_score)
    draw = random_draw(run.run_id, sample_secret(settings))
    threshold_hit = score >= float(settings.evaluation_sample_min_score)
    random_hit = draw < float(settings.evaluation_sample_random_rate)
    candidates_including_this = await _count(session, tenant_id, day) + 1

    slice_name, decision, skip_reason = "none", "not_selected", ""
    random_capped = False
    if random_hit:
        if await _count(session, tenant_id, day, slice="random", decision="sampled") < int(settings.evaluation_sample_random_daily_cap_per_tenant):
            slice_name, decision = "random", "sampled"
        else:
            random_capped = True
    if decision != "sampled" and threshold_hit:
        slice_name = "threshold"
        sampled_today = await _count(session, tenant_id, day, slice="threshold", decision="sampled")
        share_limit = max(1, math.floor(float(settings.evaluation_sample_max_share) * candidates_including_this))
        if sampled_today >= int(settings.evaluation_sample_daily_cap_per_tenant):
            decision, skip_reason = "skipped", "daily_cap"
        elif sampled_today + 1 > share_limit:
            decision, skip_reason = "skipped", "share_cap"
        else:
            decision = "sampled"
    if decision == "not_selected" and random_capped:
        slice_name, decision, skip_reason = "random", "skipped", "random_daily_cap"

    record = ReviewSamplingDecision(
        decision_id=new_decision_id(),
        tenant_id=tenant_id,
        run_id=run.run_id,
        outcome_id=outcome_id,
        day=day,
        slice=slice_name,
        decision=decision,
        skip_reason=skip_reason,
        sampling_score=score,
        random_draw=round(draw, 8),
        also_threshold=bool(slice_name == "random" and threshold_hit),
        policy_version=policy_version(settings),
    )
    session.add(record)
    EVALUATION_REVIEW_SAMPLING_TOTAL.labels(slice=slice_name, decision=decision, reason=skip_reason or "none").inc()

    review: EvaluationReview | None = None
    if decision == "sampled":
        from app.services.review_queue import RUN_REVIEW_REQUIRED, open_or_merge_review

        run.status = RUN_REVIEW_REQUIRED
        run.requires_human_review = True
        run.decision_reason = f"Sampled for human review ({SAMPLE_REASON_BY_SLICE[slice_name]}); rule verdict was pass."
        review = await open_or_merge_review(
            session,
            run_id=run.run_id,
            tenant_id=tenant_id,
            evaluation={"findings": [], "review_priority": "P3", "evaluator_version": evaluation.evaluator_version},
            sample_reason=SAMPLE_REASON_BY_SLICE[slice_name],
            submitted_by=submitted_by,
            submitted_by_key_id=submitted_by_key_id,
        )
        shown = _review_findings(findings)
        review.findings = shown
        review.finding_ids = [str(item.get("finding_id")) for item in shown]
        review.categories = sorted({str(item.get("category")) for item in shown})
        record.review_id = review.review_id
        await session.flush()
    return record, review


# ---------------------------------------------------------------------------
# Reporting (D18, D21)
# ---------------------------------------------------------------------------


def decision_to_dict(item: ReviewSamplingDecision) -> dict[str, Any]:
    return {
        "decision_id": item.decision_id,
        "tenant_id": item.tenant_id,
        "run_id": item.run_id,
        "outcome_id": item.outcome_id,
        "day": item.day,
        "slice": item.slice,
        "decision": item.decision,
        "skip_reason": item.skip_reason,
        "sampling_score": item.sampling_score,
        "random_draw": item.random_draw,
        "also_threshold": bool(item.also_threshold),
        "policy_version": item.policy_version,
        "review_id": item.review_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


async def list_decisions(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    decision: str | None = None,
    slice_name: str | None = None,
    limit: int = 100,
) -> list[ReviewSamplingDecision]:
    stmt = select(ReviewSamplingDecision)
    if tenant_id:
        stmt = stmt.where(ReviewSamplingDecision.tenant_id == tenant_id)
    if decision:
        stmt = stmt.where(ReviewSamplingDecision.decision == decision)
    if slice_name:
        stmt = stmt.where(ReviewSamplingDecision.slice == slice_name)
    return list((await session.scalars(stmt.order_by(ReviewSamplingDecision.id.desc()).limit(max(1, min(limit, 1000))))).all())


def _empty_counts() -> dict[str, Any]:
    return {
        "candidates": 0,
        "sampled": {"threshold": 0, "random": 0},
        "skipped": {reason: 0 for reason in SKIP_REASONS},
        "not_selected": 0,
    }


def _add(counts: dict[str, Any], row: ReviewSamplingDecision) -> None:
    counts["candidates"] += 1
    if row.decision == "sampled":
        counts["sampled"][row.slice] = counts["sampled"].get(row.slice, 0) + 1
    elif row.decision == "skipped":
        counts["skipped"][row.skip_reason] = counts["skipped"].get(row.skip_reason, 0) + 1
    else:
        counts["not_selected"] += 1


async def sampling_summary(
    session: AsyncSession,
    settings: Settings,
    *,
    tenant_id: str | None = None,
    window_days: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    days = max(1, min(int(window_days), 90))
    first_day = utc_day(current - timedelta(days=days - 1))
    stmt = select(ReviewSamplingDecision).where(ReviewSamplingDecision.day >= first_day)
    if tenant_id:
        stmt = stmt.where(ReviewSamplingDecision.tenant_id == tenant_id)
    rows = list((await session.scalars(stmt)).all())
    totals = _empty_counts()
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        _add(totals, row)
        _add(by_day.setdefault(row.day, _empty_counts()), row)
    sampled_total = sum(totals["sampled"].values())
    skipped_total = sum(totals["skipped"].values())
    candidates = totals["candidates"]
    return {
        "tenant_id": tenant_id,
        "window_days": days,
        "totals": totals,
        "sampled_total": sampled_total,
        "skipped_total": skipped_total,
        "conserved": sampled_total + skipped_total + totals["not_selected"] == candidates,
        "random_inclusion_rate": (totals["sampled"]["random"] / candidates) if candidates else None,
        "by_day": [{"day": day, **by_day[day]} for day in sorted(by_day, reverse=True)],
        "settings": policy_settings(settings),
        "policy_version": policy_version(settings),
        "signal_usage": "monitor_only",
    }
