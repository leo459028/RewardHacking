"""v1.5.8 agent outcome ingestion: bound, redact, evaluate and persist agent-reported evidence.

Evidence submitted by an agent is ``self_reported`` (a claim). Submissions are append-only and
capped per run, and monotonic safety applies: a run under review can never be returned to
``completed`` by submitting new evidence; only a human review decision can do that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.models.evaluation_result import EvaluationResultRecord
from app.models.evaluation_review import EvaluationReview, RunOutcome
from app.models.incident import Incident
from app.observability.metrics import (
    EVALUATION_ATTESTATIONS_TOTAL,
    EVALUATION_FINDINGS_TOTAL,
    EVALUATION_HONEYPOT_RESULTS_TOTAL,
    EVALUATION_OUTCOMES_TOTAL,
    EVALUATION_REVIEW_QUEUE_DEPTH,
    INCIDENTS_TOTAL,
)
from app.schemas.agent import AgentRequest
from app.schemas.evaluation import EvaluationPreviewRequest, EvaluationResult, OutcomeSubmitRequest
from app.services.attestation_keys import trusted_keys_for
from app.services.cheat_rates import agent_identity
from app.services.evaluation.taxonomy import higher_priority
from app.services.honeypot import get_launch, judge_honeypot
from app.services.judge_triage import judge_signal, triage_review
from app.services.review_sampling import decide_sampling
from app.services.evaluation.attestation import AttestationResult, verify_attestation
from app.services.evaluation.evidence import (
    EvaluationContext,
    EvidenceError,
    apply_attestation_result,
    context_from_outcome,
    evidence_digest,
)
from app.services.evaluator import Evaluator
from app.services.review_queue import RUN_REVIEW_REQUIRED, open_or_merge_review, protected_paths_for

OUTCOME_ACCEPTING_STATUSES = ("completed", RUN_REVIEW_REQUIRED)
_EVIDENCE_FIELDS = (
    "final_message",
    "diff",
    "changed_files",
    "test_report",
    "tests_before",
    "lint_report",
    "tool_transcript",
    "provenance",
    "attestation",
)


def new_outcome_id() -> str:
    return f"outcome_{uuid4().hex[:24]}"


def _evidence_dict(payload: OutcomeSubmitRequest | EvaluationPreviewRequest) -> dict[str, Any]:
    data = payload.model_dump(mode="json")
    return {key: data.get(key) for key in _EVIDENCE_FIELDS}


def record_finding_metrics(evaluation: EvaluationResult) -> None:
    for item in evaluation.findings:
        EVALUATION_FINDINGS_TOTAL.labels(
            category=str(item.get("category", "")),
            detector=str(item.get("detector", "")),
            severity=str(item.get("severity", "")),
            mode=str(item.get("mode", "")),
        ).inc()


async def refresh_review_queue_gauge(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(EvaluationReview.priority, func.count())
            .where(EvaluationReview.status.in_(("open", "needs_info")))
            .group_by(EvaluationReview.priority)
        )
    ).all()
    depth = {priority: 0 for priority in ("P0", "P1", "P2", "P3")}
    for priority, count in rows:
        depth[str(priority)] = int(count)
    for priority, count in depth.items():
        EVALUATION_REVIEW_QUEUE_DEPTH.labels(priority=priority).set(count)
    return depth


async def verify_ctx_attestation(
    session: AsyncSession,
    ctx: EvaluationContext,
    *,
    redacted: dict[str, Any],
    raw_attestation: dict[str, Any] | None,
    raw_test_report: dict[str, Any] | None,
    tenant_id: str,
    run_id: str,
    settings: Settings,
    check_replay: bool = True,
) -> AttestationResult:
    """Verify ``ci_attested`` evidence (D18-D20) and attach the result to the context.

    The signature and subject digest are checked against the evidence as submitted (before
    redaction), because that is what the CI signed; the persisted copy stays redacted.
    """
    if ctx.provenance != "ci_attested":
        return AttestationResult("none")
    result = verify_attestation(
        raw_attestation or {},
        test_report=raw_test_report if raw_test_report is not None else redacted.get("test_report"),
        run_id=run_id,
        keys=await trusted_keys_for(session, tenant_id),
        max_age_hours=settings.evaluation_attestation_max_age_hours,
        max_bytes=settings.evaluation_attestation_max_bytes,
        sigstore_enabled=settings.evaluation_sigstore_enabled,
    )
    if check_replay and result.digest and result.status in {"verified", "result_mismatch"}:
        reused = await session.scalar(
            select(RunOutcome.outcome_id).where(RunOutcome.tenant_id == tenant_id, RunOutcome.attestation_digest == result.digest).limit(1)
        )
        if reused:
            result = AttestationResult("replayed", {**result.detail, "reason": "attestation_already_used", "previous_outcome_id": reused}, result.digest)
    apply_attestation_result(ctx, result.status, result.detail)
    EVALUATION_ATTESTATIONS_TOTAL.labels(status=result.status).inc()
    return result


async def outcome_count(session: AsyncSession, run_id: str) -> int:
    return int(await session.scalar(select(func.count()).select_from(RunOutcome).where(RunOutcome.run_id == run_id)) or 0)


async def list_outcomes(session: AsyncSession, run_id: str) -> list[RunOutcome]:
    return list((await session.scalars(select(RunOutcome).where(RunOutcome.run_id == run_id).order_by(RunOutcome.submission_seq.asc()))).all())


async def submit_outcome(
    session: AsyncSession,
    *,
    run: AgentRun,
    payload: OutcomeSubmitRequest,
    settings: Settings,
    submitted_by_key_id: str = "",
) -> tuple[RunOutcome, EvaluationResult, EvaluationReview | None, AgentRun]:
    if run.status not in OUTCOME_ACCEPTING_STATUSES:
        raise EvidenceError(
            "outcome_run_state_invalid",
            f"Outcome evidence is accepted only for runs in {', '.join(OUTCOME_ACCEPTING_STATUSES)} (current: {run.status}).",
            409,
        )
    existing = await outcome_count(session, run.run_id)
    if existing >= settings.evaluation_outcome_max_submissions:
        raise EvidenceError(
            "outcome_submission_limit_reached",
            f"At most {settings.evaluation_outcome_max_submissions} outcome submissions are accepted per run.",
            429,
        )

    request = AgentRequest.model_validate(run.request_payload)
    protected = await protected_paths_for(session, run.tenant_id, request.metadata)
    ctx, redacted = context_from_outcome(
        task_type=request.task_type,
        goal=request.goal,
        metadata=request.metadata,
        policy_flags=list(run.policy_flags or []),
        evidence=_evidence_dict(payload),
        protected_paths=protected,
        settings=settings,
    )
    attestation = await verify_ctx_attestation(
        session,
        ctx,
        redacted=redacted,
        raw_attestation=payload.attestation,
        raw_test_report=_evidence_dict(payload).get("test_report"),
        tenant_id=run.tenant_id,
        run_id=run.run_id,
        settings=settings,
    )
    evaluation = Evaluator().evaluate_context(ctx)
    evaluation.run_id = run.run_id
    evaluation.judge_signal = judge_signal(settings)
    outcome_id = new_outcome_id()
    submitted_by = payload.submitted_by or run.user_id
    identity = agent_identity(request.metadata)

    # Honeypot probes (D16): judged server-side; a cheated outcome can only make the verdict stricter.
    launch = await get_launch(session, run.run_id)
    honeypot_outcome = ""
    sample_reason = "rule_review"
    if launch is not None:
        honeypot_outcome = judge_honeypot(evaluation, ctx)
        EVALUATION_HONEYPOT_RESULTS_TOTAL.labels(outcome=honeypot_outcome).inc()
        if honeypot_outcome == "cheated":
            sample_reason = "honeypot"
            evaluation.verdict = "review"
            evaluation.review_priority = higher_priority(evaluation.review_priority or "P1", "P1")

    outcome = RunOutcome(
        outcome_id=outcome_id,
        run_id=run.run_id,
        tenant_id=run.tenant_id,
        submission_seq=existing + 1,
        provenance=str(redacted.get("provenance") or "self_reported"),
        submitted_by=submitted_by,
        submitted_by_key_id=submitted_by_key_id,
        evidence=redacted,
        evidence_digest=evidence_digest(redacted),
        verdict=evaluation.verdict,
        review_priority=evaluation.review_priority,
        assurance_level=evaluation.assurance_level,
        evaluator_version=evaluation.evaluator_version,
        findings=evaluation.findings,
        attestation_status=attestation.status,
        # Only verified or contradicting attestations are recorded for replay detection.
        attestation_digest=attestation.digest if attestation.status in {"verified", "result_mismatch"} else "",
        attestation_detail=dict(attestation.detail),
        agent_id=identity["agent_id"],
        agent_version=identity["agent_version"],
        model_name=identity["model_name"],
        honeypot_probe_id=launch.probe_id if launch is not None else "",
        honeypot_outcome=honeypot_outcome,
    )
    session.add(outcome)
    session.add(
        EvaluationResultRecord(
            run_id=run.run_id,
            task_type=request.task_type,
            verdict=evaluation.verdict,
            final_score=evaluation.final_score,
            rubric=dict(evaluation.rubric),
            judge_signal=dict(evaluation.judge_signal),
            explanation=evaluation.explanation,
            tenant_id=run.tenant_id,
            evaluator_version=evaluation.evaluator_version,
            evaluation_source="outcome",
            assurance_level=evaluation.assurance_level,
            findings=evaluation.findings,
            review_priority=evaluation.review_priority,
            outcome_id=outcome_id,
            suspicion_score=evaluation.suspicion_score,
        )
    )

    now = datetime.now(timezone.utc).isoformat()
    previous_status = run.status
    review: EvaluationReview | None = None
    payload_dict = evaluation.model_dump()
    if evaluation.verdict == "review":
        # Monotonic safety: evidence can only make the run stricter.
        run.status = RUN_REVIEW_REQUIRED
        run.requires_human_review = True
        run.decision_reason = f"Outcome evidence requires review ({evaluation.review_priority})."
        review = await open_or_merge_review(
            session,
            run_id=run.run_id,
            tenant_id=run.tenant_id,
            evaluation=payload_dict,
            sample_reason=sample_reason,
            submitted_by=submitted_by,
            submitted_by_key_id=submitted_by_key_id,
        )
        if previous_status != RUN_REVIEW_REQUIRED:
            session.add(
                Incident(
                    run_id=run.run_id,
                    severity="high" if evaluation.review_priority in {"P0", "P1"} else "medium",
                    code="eval_review_required",
                    detail=evaluation.explanation,
                )
            )
            INCIDENTS_TOTAL.labels(
                severity="high" if evaluation.review_priority in {"P0", "P1"} else "medium", code="eval_review_required"
            ).inc()
    elif launch is None and previous_status == "completed":
        # v1.5.10 quota-capped sample_for_review: a rule-passed run may only become stricter (D14–D19).
        _decision, sampled_review = await decide_sampling(
            session,
            run=run,
            outcome_id=outcome_id,
            evaluation=evaluation,
            settings=settings,
            submitted_by=submitted_by,
            submitted_by_key_id=submitted_by_key_id,
        )
        if sampled_review is not None:
            review = sampled_review
    elif previous_status == RUN_REVIEW_REQUIRED:
        review = await session.scalar(
            select(EvaluationReview).where(EvaluationReview.run_id == run.run_id, EvaluationReview.status.in_(("open", "needs_info")))
        )
        if review is not None and review.status == "needs_info":
            review.status = "open"

    summary = dict(run.evaluation_summary or {})
    summary["latest_outcome"] = {
        "outcome_id": outcome_id,
        "submission_seq": existing + 1,
        "verdict": evaluation.verdict,
        "review_priority": evaluation.review_priority,
        "assurance_level": evaluation.assurance_level,
        "evaluator_version": evaluation.evaluator_version,
        "findings_count": len([f for f in evaluation.findings if f.get("detector") != "legacy_text"]),
        "submitted_at": now,
    }
    summary["run_state"] = {"status": run.status, "updated_at": now}
    timeline = list(summary.get("event_timeline", []))
    timeline.append({"event_type": "outcome.evaluated", "status": evaluation.verdict, "ts": now})
    summary["event_timeline"] = timeline
    run.evaluation_summary = summary
    result = dict(run.result or {})
    response = dict(result.get("response") or {})
    if response:
        response["status"] = run.status
        response["decision_reason"] = run.decision_reason
        result["response"] = response
    result["evaluation_summary"] = summary
    run.result = result

    await session.commit()
    await session.refresh(outcome)
    await session.refresh(run)
    if review is not None:
        await session.refresh(review)
        # v1.5.10: advisory judge triage runs only after the verdict and review are committed (D6).
        await triage_review(session, review, settings)
        await session.refresh(review)
        await session.refresh(run)
    EVALUATION_OUTCOMES_TOTAL.labels(provenance=outcome.provenance, verdict=evaluation.verdict).inc()
    record_finding_metrics(evaluation)
    await refresh_review_queue_gauge(session)
    return outcome, evaluation, review, run


async def preview_evaluation(
    session: AsyncSession,
    *,
    payload: EvaluationPreviewRequest,
    tenant_id: str,
    settings: Settings,
) -> EvaluationResult:
    protected = await protected_paths_for(session, tenant_id, payload.metadata)
    ctx, redacted = context_from_outcome(
        task_type=payload.task_type,
        goal=payload.goal,
        metadata=payload.metadata,
        policy_flags=[],
        evidence=_evidence_dict(payload),
        protected_paths=protected,
        settings=settings,
    )
    if ctx.provenance == "ci_attested":
        # Preview has no run: verification cannot pass run binding, so it reports the reason.
        await verify_ctx_attestation(
            session,
            ctx,
            redacted=redacted,
            raw_attestation=payload.attestation,
            raw_test_report=_evidence_dict(payload).get("test_report"),
            tenant_id=tenant_id,
            run_id=str(payload.metadata.get("run_id") or ""),
            settings=settings,
            check_replay=False,
        )
    return Evaluator().evaluate_context(ctx)
