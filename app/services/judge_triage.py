"""v1.5.10 advisory judge triage of the post-execution review queue.

Invariants (AGENTS.md 2.3, plan D1–D11):
- A judge writes only ``judge_assessments`` and the four ``triage_*`` columns of a review. It never
  writes verdicts, review priority, review status, run status or incidents, and never publishes events.
- Triage runs only after the outcome / review transaction has committed (the verdict is already fixed).
  Any failure is recorded on the assessment and swallowed; evaluation never fails because of a judge.
- Judge tokens are recorded in the FinOps ledger (``step_id="evaluation_judge"``) without acquiring or
  releasing run token leases. A per-tenant daily token cap falls back to the deterministic provider.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.models.evaluation_review import EvaluationReview, RunOutcome
from app.models.finops import PricingCatalog
from app.models.judge import JudgeAssessment
from app.observability.metrics import (
    EVALUATION_JUDGE_ASSESSMENTS_TOTAL,
    EVALUATION_JUDGE_LATENCY_SECONDS,
    EVALUATION_JUDGE_TOKENS_TOTAL,
)
from app.services.evaluation.aggregator import suspicion_score
from app.services.evaluation.evidence import redact_text
from app.services.evaluation.judges import JudgeProvider, JudgeResult, assess_with_fallback, get_judge
from app.services.evaluation.judges.base import JudgeInput
from app.services.evaluation.judges.family import same_family
from app.services.evaluation.judges.inputs import build_judge_input
from app.services.evaluation.judges.profiles import active_profile_known, get_profile
from app.services.evaluation.taxonomy import PRIORITY_RANK

logger = logging.getLogger(__name__)

JUDGE_STEP_ID = "evaluation_judge"


def new_assessment_id() -> str:
    return f"jdg_{uuid4().hex[:24]}"


def judge_signal(settings: Settings) -> dict[str, Any]:
    """Value stored in ``EvaluationResult.judge_signal``: the judge never decides verdicts or hard denies."""
    return {
        "enabled": settings.evaluation_judge_mode != "off",
        "provider": settings.effective_judge_provider,
        "mode": settings.evaluation_judge_mode,
        "used_for_verdict": False,
        "used_for_hard_deny": False,
    }


def _utc_day_start(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


async def tokens_used_today(session: AsyncSession, tenant_id: str, *, now: datetime | None = None) -> int:
    start = _utc_day_start(now)
    total = await session.scalar(
        select(func.coalesce(func.sum(JudgeAssessment.input_tokens + JudgeAssessment.output_tokens), 0)).where(
            JudgeAssessment.tenant_id == tenant_id,
            JudgeAssessment.created_at >= start,
            JudgeAssessment.created_at < start + timedelta(days=1),
        )
    )
    return int(total or 0)


async def catalog_family(session: AsyncSession, model_name: str) -> str:
    if not model_name:
        return ""
    family = await session.scalar(
        select(PricingCatalog.model_family).where(PricingCatalog.model_name == model_name, PricingCatalog.model_family != "").limit(1)
    )
    return str(family or "")


async def external_judge_consent(session: AsyncSession, tenant_id: str, profile_name: str) -> bool:
    """v1.5.11 (D15): has this tenant agreed to send (redacted) evidence to this external judge profile?"""
    from app.models.evaluation_review import TenantEvaluationPolicy

    policy = await session.scalar(select(TenantEvaluationPolicy).where(TenantEvaluationPolicy.tenant_id == (tenant_id or "default")))
    if policy is None or not policy.allow_external_judge:
        return False
    allowed = [str(item) for item in list(policy.external_judge_profiles or [])]
    return not allowed or profile_name in allowed


async def latest_outcome(session: AsyncSession, run_id: str) -> RunOutcome | None:
    return await session.scalar(select(RunOutcome).where(RunOutcome.run_id == run_id).order_by(RunOutcome.submission_seq.desc()).limit(1))


async def judge_input_for_review(session: AsyncSession, review: EvaluationReview, settings: Settings) -> JudgeInput:
    run = await session.scalar(select(AgentRun).where(AgentRun.run_id == review.run_id))
    payload = dict((run.request_payload if run is not None else {}) or {})
    metadata = dict(payload.get("metadata") or {})
    outcome = await latest_outcome(session, review.run_id)
    if outcome is not None:
        evidence = dict(outcome.evidence or {})
        findings = list(outcome.findings or [])
    else:
        # Worker path: only the simulated executor output exists.
        evidence = {"final_message": str(getattr(run, "execution_output", "") or "")}
        findings = list(review.findings or [])
    if review.model_name and not metadata.get("model_name"):
        metadata["model_name"] = review.model_name
    return build_judge_input(
        evidence=evidence,
        task_type=str(payload.get("task_type") or "code_fix"),
        goal=str(payload.get("goal") or ""),
        metadata=metadata,
        findings=findings,
        max_chars=settings.evaluation_judge_max_input_chars,
    )


async def _record_judge_usage(
    session: AsyncSession, *, settings: Settings, tenant_id: str, run_id: str, result: JudgeResult, extra: dict[str, Any]
) -> str:
    """Record judge tokens in the ledger. ``record_usage_event`` commits, so this runs after the assessment commit."""
    primary = result.primary
    if not (primary.input_tokens or primary.output_tokens):
        return ""
    from app.schemas.finops import UsageEventCreateRequest
    from app.services.finops import record_usage_event

    status = {"ok": "success", "timeout": "timeout"}.get(primary.status, "failed")
    # v1.5.11 (D13): attribute cost to the profile's real provider / model so catalog prices match
    # (e.g. provider "google" for Gemini, a zero-cost "qa_local" row for a local model).
    profile = get_profile(settings, primary.profile) if primary.profile else None
    pricing_provider = profile.pricing_provider if profile is not None else "custom"
    pricing_model = (profile.effective_pricing_model if profile is not None else "") or primary.model
    metadata = {
        "cost_kind": "evaluation_judge",
        "judge_version": primary.judge_version,
        "judge_profile": primary.profile,
        "source": "evaluation_judge",
        **extra,
    }
    usage_id = ""
    for unit, quantity in (("input_tokens", primary.input_tokens), ("output_tokens", primary.output_tokens)):
        if not quantity:
            continue
        usage = await record_usage_event(
            session,
            UsageEventCreateRequest(
                tenant_id=tenant_id,
                run_id=run_id,
                step_id=JUDGE_STEP_ID,
                provider=pricing_provider,
                model_name=pricing_model,
                resource_type="model",
                resource_subtype="chat_completion",
                usage_unit=unit,
                quantity=quantity,
                input_tokens=primary.input_tokens if unit == "input_tokens" else 0,
                output_tokens=primary.output_tokens if unit == "output_tokens" else 0,
                phase="actual",
                status=status,
                latency_ms=primary.latency_ms,
                metadata=metadata,
            ),
        )
        usage_id = usage_id or usage.usage_id
    return usage_id


def _observe(result: JudgeResult) -> None:
    primary = result.primary
    EVALUATION_JUDGE_ASSESSMENTS_TOTAL.labels(provider=primary.provider, status=primary.status).inc()
    if primary.latency_ms:
        EVALUATION_JUDGE_LATENCY_SECONDS.labels(provider=primary.provider).observe(primary.latency_ms / 1000.0)
    if primary.input_tokens:
        EVALUATION_JUDGE_TOKENS_TOTAL.labels(provider=primary.provider, direction="input").inc(primary.input_tokens)
    if primary.output_tokens:
        EVALUATION_JUDGE_TOKENS_TOTAL.labels(provider=primary.provider, direction="output").inc(primary.output_tokens)


async def run_judge(
    session: AsyncSession,
    judge_input: JudgeInput,
    settings: Settings,
    *,
    tenant_id: str,
    judge: JudgeProvider | None = None,
    require_tenant_consent: bool = True,
) -> tuple[JudgeResult, bool]:
    """Assess with tenant consent and the token cap applied; returns the result and the same-family flag.

    An external (cloud) judge needs the global ``EVALUATION_JUDGE_ALLOW_EXTERNAL`` switch (checked by the provider)
    **and** the tenant's consent (D15). Without consent no request is sent and the deterministic judge is used.
    """
    judge = judge or get_judge(settings)
    skip = ""
    if require_tenant_consent and getattr(judge, "is_external", False):
        if not await external_judge_consent(session, tenant_id, getattr(judge, "profile_label", "")):
            skip = "blocked_external"
    if not skip and judge.name != "deterministic":
        cap = int(settings.evaluation_judge_daily_token_cap_per_tenant)
        if await tokens_used_today(session, tenant_id) >= cap:
            skip = "budget_exceeded"
    result = await assess_with_fallback(judge_input, settings, judge=judge, skip_primary_status=skip)
    family_warning = False
    if judge.name != "deterministic" and getattr(judge, "model", ""):
        family_warning = same_family(
            judge.model,
            judge_input.agent_model_name,
            judge_catalog_family=await catalog_family(session, judge.model),
            agent_catalog_family=await catalog_family(session, judge_input.agent_model_name),
        )
    _observe(result)
    return result, family_warning


def build_assessment(
    *,
    result: JudgeResult,
    judge_input: JudgeInput,
    settings: Settings,
    family_warning: bool,
    tenant_id: str,
    run_id: str = "",
    review_id: str = "",
    outcome_id: str = "",
    benchmark_run_id: str = "",
    sample_id: str = "",
) -> JudgeAssessment:
    primary = result.primary
    effective = primary if primary.ok else (result.fallback or primary)
    return JudgeAssessment(
        assessment_id=new_assessment_id(),
        tenant_id=tenant_id or "default",
        run_id=run_id,
        review_id=review_id,
        outcome_id=outcome_id,
        benchmark_run_id=benchmark_run_id,
        sample_id=sample_id,
        provider=primary.provider,
        profile=primary.profile,
        model=primary.model,
        judge_version=primary.judge_version,
        prompt_version=primary.prompt_version,
        mode=settings.evaluation_judge_mode,
        status=primary.status,
        suspicion=primary.suspicion if primary.ok else None,
        triage_score=result.triage_score,
        triage_priority=result.triage_priority,
        triage_source=result.triage_source,
        categories=list(effective.categories),
        evidence_refs=[redact_text(item) for item in effective.evidence_refs],
        rationale=redact_text(effective.rationale)[:1000],
        fallback=_fallback_payload(result),
        injection_suspected=result.injection_suspected,
        injection_markers=list(judge_input.injection_markers),
        same_family_warning=family_warning,
        input_digest=judge_input.digest(),
        input_truncated=dict(judge_input.truncated),
        input_tokens=primary.input_tokens,
        output_tokens=primary.output_tokens,
        latency_ms=primary.latency_ms,
    )


def _fallback_payload(result: JudgeResult) -> dict[str, Any]:
    """Fallback summary plus (v1.5.11) how the primary request was made: format actually used and HTTP attempts."""
    payload: dict[str, Any] = result.fallback.summary() if result.fallback is not None else {}
    primary = result.primary
    if primary.provider != "deterministic" and (primary.structured_output or primary.attempts):
        payload["request"] = {"structured_output": primary.structured_output, "attempts": primary.attempts}
    return payload


def _apply_to_review(review: EvaluationReview, assessment: JudgeAssessment) -> None:
    """The only review fields a judge may touch (D1)."""
    review.triage_priority = assessment.triage_priority
    review.triage_score = assessment.triage_score
    review.triage_source = assessment.triage_source
    review.judge_assessment_id = assessment.assessment_id


async def triage_review(
    session: AsyncSession,
    review: EvaluationReview,
    settings: Settings,
    *,
    judge: JudgeProvider | None = None,
) -> tuple[JudgeAssessment | None, bool]:
    """Assess one review. Returns ``(assessment, deduplicated)``; never raises."""
    if settings.evaluation_judge_mode == "off":
        return None, False
    started = time.perf_counter()
    try:
        judge = judge or get_judge(settings)
        judge_input = await judge_input_for_review(session, review, settings)
        existing = await session.scalar(
            select(JudgeAssessment)
            .where(
                JudgeAssessment.run_id == review.run_id,
                JudgeAssessment.input_digest == judge_input.digest(),
                JudgeAssessment.judge_version == judge.version(),
                JudgeAssessment.benchmark_run_id == "",
            )
            .order_by(JudgeAssessment.id.desc())
            .limit(1)
        )
        if existing is not None:
            # Re-apply when the review points elsewhere or still carries a v1.5.11 "pending:" marker.
            if review.judge_assessment_id != existing.assessment_id or review.triage_source != existing.triage_source:
                _apply_to_review(review, existing)
                await session.commit()
            return existing, True
        outcome = await latest_outcome(session, review.run_id)
        result, family_warning = await run_judge(session, judge_input, settings, tenant_id=review.tenant_id, judge=judge)
        assessment = build_assessment(
            result=result,
            judge_input=judge_input,
            settings=settings,
            family_warning=family_warning,
            tenant_id=review.tenant_id,
            run_id=review.run_id,
            review_id=review.review_id,
            outcome_id=outcome.outcome_id if outcome is not None else "",
        )
        session.add(assessment)
        _apply_to_review(review, assessment)
        await session.commit()
        usage_id = await _record_judge_usage(
            session, settings=settings, tenant_id=review.tenant_id, run_id=review.run_id, result=result, extra={"review_id": review.review_id}
        )
        if usage_id:
            assessment.usage_id = usage_id
            await session.commit()
        await session.refresh(assessment)
        await session.refresh(review)
        return assessment, False
    except Exception:  # noqa: BLE001 - a judge failure must never break evaluation (D6)
        logger.exception("judge triage failed for review %s after %.0f ms", review.review_id, (time.perf_counter() - started) * 1000)
        await session.rollback()
        return None, False


# ---------------------------------------------------------------------------
# Queue ordering (D3)
# ---------------------------------------------------------------------------


def review_sort_key(review: EvaluationReview) -> tuple:
    """``(min(rule, triage), rule, -triage_score, opened_at, id)``: rule P0 always first; a judge can only move items earlier."""
    rule_rank = PRIORITY_RANK.get(review.priority or "P3", 3)
    triage_rank = PRIORITY_RANK.get(review.triage_priority, rule_rank) if review.triage_priority else rule_rank
    score = review.triage_score if review.triage_score is not None else suspicion_score(list(review.findings or []))
    opened = review.opened_at
    opened_key = opened.timestamp() if opened is not None and hasattr(opened, "timestamp") else 0.0
    return (min(rule_rank, triage_rank), rule_rank, -float(score or 0.0), opened_key, review.id or 0)


def effective_priority(review: EvaluationReview) -> str:
    rule = review.priority or "P3"
    if review.triage_priority and PRIORITY_RANK.get(review.triage_priority, 9) < PRIORITY_RANK.get(rule, 9):
        return review.triage_priority
    return rule


def default_order(settings: Settings) -> str:
    return "triage" if settings.evaluation_judge_mode == "triage" else "rule"


async def list_assessments(session: AsyncSession, *, review_id: str = "", run_id: str = "", limit: int = 50) -> list[JudgeAssessment]:
    stmt = select(JudgeAssessment).where(JudgeAssessment.benchmark_run_id == "")
    if review_id:
        stmt = stmt.where(JudgeAssessment.review_id == review_id)
    if run_id:
        stmt = stmt.where(JudgeAssessment.run_id == run_id)
    return list((await session.scalars(stmt.order_by(JudgeAssessment.id.desc()).limit(limit))).all())


def assessment_to_dict(item: JudgeAssessment) -> dict[str, Any]:
    return {
        "assessment_id": item.assessment_id,
        "tenant_id": item.tenant_id,
        "run_id": item.run_id,
        "review_id": item.review_id,
        "outcome_id": item.outcome_id,
        "benchmark_run_id": item.benchmark_run_id,
        "sample_id": item.sample_id,
        "provider": item.provider,
        "profile": item.profile,
        "model": item.model,
        "judge_version": item.judge_version,
        "prompt_version": item.prompt_version,
        "mode": item.mode,
        "status": item.status,
        "suspicion": item.suspicion,
        "triage_score": item.triage_score,
        "triage_priority": item.triage_priority,
        "triage_source": item.triage_source,
        "categories": list(item.categories or []),
        "evidence_refs": list(item.evidence_refs or []),
        "rationale": item.rationale,
        "fallback": dict(item.fallback or {}),
        "injection_suspected": bool(item.injection_suspected),
        "injection_markers": list(item.injection_markers or []),
        "same_family_warning": bool(item.same_family_warning),
        "input_digest": item.input_digest,
        "input_truncated": dict(item.input_truncated or {}),
        "input_tokens": item.input_tokens,
        "output_tokens": item.output_tokens,
        "latency_ms": item.latency_ms,
        "usage_id": item.usage_id,
        "used_for_verdict": False,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


async def judge_status(session: AsyncSession, settings: Settings, *, tenant_id: str | None = None) -> dict[str, Any]:
    judge = get_judge(settings)
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    stmt = select(JudgeAssessment.status, func.count()).where(JudgeAssessment.created_at >= since).group_by(JudgeAssessment.status)
    if tenant_id:
        stmt = stmt.where(JudgeAssessment.tenant_id == tenant_id)
    counts = {str(status): int(count) for status, count in (await session.execute(stmt)).all()}
    external = bool(getattr(judge, "is_external", False))
    return {
        "mode": settings.evaluation_judge_mode,
        "profile": getattr(judge, "profile_label", "") or settings.evaluation_judge_profile,
        "configured_profile": settings.evaluation_judge_profile,
        "profile_known": active_profile_known(settings),
        "provider": judge.name,
        "model": getattr(judge, "model", ""),
        "judge_version": judge.version(),
        "external_endpoint": external,
        "external_allowed": bool(settings.evaluation_judge_allow_external),
        "legacy_alias_in_use": settings.judge_uses_legacy_alias,
        "timeout_seconds": settings.evaluation_judge_timeout_seconds,
        "daily_token_cap_per_tenant": settings.evaluation_judge_daily_token_cap_per_tenant,
        "tokens_used_today": await tokens_used_today(session, tenant_id) if tenant_id else None,
        "status_counts_24h": counts,
        "default_order": default_order(settings),
        "used_for_verdict": False,
        "used_for_hard_deny": False,
    }
