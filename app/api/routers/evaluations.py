"""v1.5.8 evaluation, outcome evidence and post-execution review endpoints.

Router split rule (AGENTS.md 2.1): new endpoints live here instead of ``app/api/routes.py``.
All evaluation and review responses carry ``signal_usage: "monitor_only"``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.observability.metrics import EVALUATION_REVIEWS_TOTAL
from app.schemas.audit import EvaluationRecord
from app.schemas.evaluation import (
    EvaluationPolicyRecord,
    EvaluationPolicyUpdateRequest,
    EvaluationPreviewRequest,
    EvaluationPreviewResponse,
    OutcomeRecord,
    OutcomeSubmitRequest,
    OutcomeSubmitResponse,
    ReviewDecisionRequest,
    ReviewRecord,
)
from app.schemas.benchmark import AttestationKeyCreateRequest, AttestationKeyRevokeRequest
from app.services.attestation_keys import (
    AttestationKeyServiceError,
    create_attestation_key,
    key_to_dict,
    list_attestation_keys,
    revoke_attestation_key,
)
from app.services.audit_log import get_run, list_evaluation_results
from app.services.auth import AuthContext, auth_error, get_auth_context, require_role
from app.services.cheat_rates import compute_cheat_rates
from app.services.honeypot import can_see_honeypots
from app.services.evaluation.evidence import EvidenceError
from app.services.judge_triage import (
    assessment_to_dict,
    default_order,
    effective_priority,
    judge_status,
    list_assessments,
    triage_review,
)
from app.services.outcomes import list_outcomes, preview_evaluation, refresh_review_queue_gauge, submit_outcome
from app.services.review_queue import (
    CONCEALED_SAMPLE_REASONS,
    ReviewError,
    decide_review,
    get_evaluation_policy,
    get_review,
    list_reviews,
    policy_to_dict,
    upsert_evaluation_policy,
)
from app.services.review_sampling import decision_to_dict, list_decisions, sampling_summary

router = APIRouter(prefix="/v1", tags=["evaluation"])


def _scoped_tenant(tenant_id: str | None, auth_context: AuthContext) -> str | None:
    if auth_context.enabled:
        auth_tenant = auth_context.tenant_id or "default"
        if tenant_id and tenant_id != auth_tenant:
            raise auth_error(403, "tenant_mismatch", "Query tenant_id does not match API key tenant.")
        return auth_tenant
    return tenant_id


def _key_identity(auth_context: AuthContext) -> str:
    """Stable per-key identity for separation of duties (legacy env keys share key_id='legacy')."""
    return auth_context.key_id if auth_context.enabled and auth_context.source == "db" else ""


def _evidence_http_error(exc: EvidenceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.detail})


def _review_view(review, auth_context: AuthContext) -> ReviewRecord:
    """Roles that submit agent work never learn that a review came from a honeypot (v1.5.9, D15) or from
    sampling (v1.5.10, D20), and never see judge triage, which would teach an agent how to evade the judge."""
    record = ReviewRecord.model_validate(review)
    record = record.model_copy(update={"effective_priority": effective_priority(review)})
    if not can_see_honeypots(auth_context.role, auth_context.enabled):
        update: dict = {
            "triage_priority": "",
            "triage_score": None,
            "triage_source": "",
            "judge_assessment_id": "",
            "effective_priority": record.priority,
        }
        if record.sample_reason in CONCEALED_SAMPLE_REASONS:
            update["sample_reason"] = "rule_review"
        record = record.model_copy(update=update)
    return record


def _require_reviewer_view(auth_context: AuthContext) -> None:
    require_role(auth_context, "reviewer", "tenant_admin", "admin")


@router.get("/evaluations", response_model=list[EvaluationRecord])
async def get_evaluations(
    task_type: str | None = None,
    verdict: str | None = None,
    tenant_id: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    tenant_id = _scoped_tenant(tenant_id, auth_context)
    return await list_evaluation_results(session, task_type=task_type, verdict=verdict, tenant_id=tenant_id)


@router.post("/runs/{run_id}/outcome", response_model=OutcomeSubmitResponse)
async def post_run_outcome(
    run_id: str,
    payload: OutcomeSubmitRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "operator", "admin")
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    _scoped_tenant(run.tenant_id, auth_context)
    try:
        outcome, evaluation, review, run = await submit_outcome(
            session,
            run=run,
            payload=payload,
            settings=get_settings(),
            submitted_by_key_id=_key_identity(auth_context),
        )
    except EvidenceError as exc:
        raise _evidence_http_error(exc) from exc
    return OutcomeSubmitResponse(
        outcome_id=outcome.outcome_id,
        run_id=run.run_id,
        run_status=run.status,
        submission_seq=outcome.submission_seq,
        evaluation=evaluation,
        review=_review_view(review, auth_context) if review is not None else None,
    )


@router.get("/runs/{run_id}/outcomes", response_model=list[OutcomeRecord])
async def get_run_outcomes(
    run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    _scoped_tenant(run.tenant_id, auth_context)
    return await list_outcomes(session, run_id)


@router.post("/evaluations/preview", response_model=EvaluationPreviewResponse)
async def post_evaluation_preview(
    payload: EvaluationPreviewRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _scoped_tenant(payload.tenant_id, auth_context) or "default"
    try:
        evaluation = await preview_evaluation(session, payload=payload, tenant_id=tenant_id, settings=get_settings())
    except EvidenceError as exc:
        raise _evidence_http_error(exc) from exc
    return EvaluationPreviewResponse(evaluation=evaluation, persisted=False)


@router.get("/reviews", response_model=list[ReviewRecord])
async def get_reviews(
    status: str | None = Query(default=None, description="open | needs_info | closed | active"),
    priority: str | None = None,
    category: str | None = None,
    tenant_id: str | None = None,
    run_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    order: str | None = Query(default=None, pattern="^(rule|triage)$", description="rule (v1.5.8 order) | triage (v1.5.10 advisory order)"),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _scoped_tenant(tenant_id, auth_context)
    if not can_see_honeypots(auth_context.role, auth_context.enabled):
        order = "rule"  # triage order would leak judge output to roles that submit agent work
    rows = await list_reviews(
        session,
        status=status,
        priority=priority,
        category=category,
        tenant_id=tenant_id,
        run_id=run_id,
        limit=limit,
        order=order or default_order(get_settings()),
    )
    await refresh_review_queue_gauge(session)
    return [_review_view(row, auth_context) for row in rows]


@router.get("/reviews/{review_id}", response_model=ReviewRecord)
async def get_review_detail(
    review_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    review = await get_review(session, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    _scoped_tenant(review.tenant_id, auth_context)
    return _review_view(review, auth_context)


@router.post("/reviews/{review_id}/decision", response_model=ReviewRecord)
async def post_review_decision(
    review_id: str,
    decision: ReviewDecisionRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "reviewer", "tenant_admin", "admin")
    review = await get_review(session, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    _scoped_tenant(review.tenant_id, auth_context)
    try:
        result = await decide_review(session, review_id, decision, reviewer_key_id=_key_identity(auth_context))
    except ReviewError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.detail}) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    EVALUATION_REVIEWS_TOTAL.labels(label=decision.label).inc()
    await refresh_review_queue_gauge(session)
    return _review_view(result[0], auth_context)


@router.get("/tenants/{tenant_id}/evaluation-policy", response_model=EvaluationPolicyRecord)
async def get_tenant_evaluation_policy(
    tenant_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "viewer", "operator", "admin")
    tenant_id = _scoped_tenant(tenant_id, auth_context) or tenant_id
    return policy_to_dict(tenant_id, await get_evaluation_policy(session, tenant_id))


@router.put("/tenants/{tenant_id}/evaluation-policy", response_model=EvaluationPolicyRecord)
async def put_tenant_evaluation_policy(
    tenant_id: str,
    payload: EvaluationPolicyUpdateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "tenant_admin", "admin")
    tenant_id = _scoped_tenant(tenant_id, auth_context) or tenant_id
    policy = await upsert_evaluation_policy(
        session,
        tenant_id=tenant_id,
        protected_paths=payload.protected_paths,
        allow_default_protected_paths=payload.allow_default_protected_paths,
        updated_by=payload.updated_by,
        allow_external_judge=payload.allow_external_judge,
        external_judge_profiles=payload.external_judge_profiles,
    )
    return policy_to_dict(tenant_id, policy)


# ---------------------------------------------------------------------------
# v1.5.9 cheat rates (monitor-only) and CI attestation trust roots
# ---------------------------------------------------------------------------


@router.get("/evaluations/cheat-rates")
async def get_cheat_rates(
    group_by: str = Query(default="agent_id", pattern="^(agent_id|model_name|agent_version)$"),
    tenant_id: str | None = None,
    window_days: int = Query(default=30, ge=1, le=365),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "reviewer", "tenant_admin", "admin")
    tenant_id = _scoped_tenant(tenant_id, auth_context)
    return await compute_cheat_rates(
        session,
        group_by=group_by,
        tenant_id=tenant_id,
        window_days=window_days,
        min_samples=get_settings().evaluation_cheat_rate_min_samples,
    )


def _key_http_error(exc: AttestationKeyServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.detail})


@router.get("/tenants/{tenant_id}/attestation-keys")
async def get_attestation_keys(
    tenant_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "reviewer", "tenant_admin", "admin")
    tenant_id = _scoped_tenant(tenant_id, auth_context) or tenant_id
    rows = await list_attestation_keys(session, tenant_id)
    return {"tenant_id": tenant_id, "keys": [key_to_dict(row) for row in rows]}


@router.post("/tenants/{tenant_id}/attestation-keys", status_code=201)
async def post_attestation_key(
    tenant_id: str,
    payload: AttestationKeyCreateRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "tenant_admin", "admin")
    tenant_id = _scoped_tenant(tenant_id, auth_context) or tenant_id
    try:
        row = await create_attestation_key(
            session,
            tenant_id=tenant_id,
            name=payload.name,
            algorithm=payload.algorithm,
            public_key_pem=payload.public_key_pem,
            allowed_subjects=payload.allowed_subjects,
            created_by=payload.created_by or (auth_context.key_id if auth_context.enabled else "") or "api",
        )
    except AttestationKeyServiceError as exc:
        raise _key_http_error(exc) from exc
    return key_to_dict(row)


@router.post("/tenants/{tenant_id}/attestation-keys/{key_id}/revoke")
async def post_attestation_key_revoke(
    tenant_id: str,
    key_id: str,
    payload: AttestationKeyRevokeRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "tenant_admin", "admin")
    tenant_id = _scoped_tenant(tenant_id, auth_context) or tenant_id
    try:
        row = await revoke_attestation_key(
            session, tenant_id=tenant_id, key_id=key_id, actor=payload.revoked_by or (auth_context.key_id if auth_context.enabled else "") or "api"
        )
    except AttestationKeyServiceError as exc:
        raise _key_http_error(exc) from exc
    return key_to_dict(row)


# ---------------------------------------------------------------------------
# v1.5.10 advisory judge triage and quota-capped review sampling (monitor-only)
# ---------------------------------------------------------------------------

JUDGE_SAFETY_STATEMENT = "Judge output only orders review work; it never changes verdicts, priorities or review status."


@router.get("/reviews/{review_id}/judge-assessments")
async def get_review_judge_assessments(
    review_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_reviewer_view(auth_context)
    review = await get_review(session, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    _scoped_tenant(review.tenant_id, auth_context)
    rows = await list_assessments(session, review_id=review_id)
    return {
        "review_id": review_id,
        "rule_priority": review.priority,
        "triage_priority": review.triage_priority,
        "assessments": [assessment_to_dict(row) for row in rows],
        "safety_statement": JUDGE_SAFETY_STATEMENT,
        "signal_usage": "monitor_only",
    }


@router.post("/reviews/{review_id}/judge-assessments")
async def post_review_judge_assessment(
    review_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "tenant_admin", "admin")
    review = await get_review(session, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    _scoped_tenant(review.tenant_id, auth_context)
    settings = get_settings()
    if settings.evaluation_judge_mode == "off":
        raise HTTPException(status_code=409, detail={"code": "judge_disabled", "message": "EVALUATION_JUDGE_MODE is off."})
    assessment, deduplicated = await triage_review(session, review, settings)
    if assessment is None:
        raise HTTPException(status_code=503, detail={"code": "judge_unavailable", "message": "The judge assessment could not be stored."})
    body = {"assessment": assessment_to_dict(assessment), "deduplicated": deduplicated, "safety_statement": JUDGE_SAFETY_STATEMENT, "signal_usage": "monitor_only"}
    return JSONResponse(status_code=200 if deduplicated else 201, content=jsonable_encoder(body))


@router.get("/evaluations/judge/status")
async def get_judge_status(
    tenant_id: str | None = None,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_reviewer_view(auth_context)
    tenant_id = _scoped_tenant(tenant_id, auth_context)
    return {**(await judge_status(session, get_settings(), tenant_id=tenant_id)), "signal_usage": "monitor_only"}


@router.get("/evaluations/sampling/summary")
async def get_sampling_summary(
    tenant_id: str | None = None,
    window_days: int = Query(default=7, ge=1, le=90),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_reviewer_view(auth_context)
    tenant_id = _scoped_tenant(tenant_id, auth_context)
    return await sampling_summary(session, get_settings(), tenant_id=tenant_id, window_days=window_days)


@router.get("/evaluations/sampling/decisions")
async def get_sampling_decisions(
    tenant_id: str | None = None,
    decision: str | None = Query(default=None, pattern="^(sampled|skipped|not_selected)$"),
    slice_name: str | None = Query(default=None, alias="slice", pattern="^(threshold|random|none)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    _require_reviewer_view(auth_context)
    tenant_id = _scoped_tenant(tenant_id, auth_context)
    rows = await list_decisions(session, tenant_id=tenant_id, decision=decision, slice_name=slice_name, limit=limit)
    return {"decisions": [decision_to_dict(row) for row in rows], "signal_usage": "monitor_only"}
