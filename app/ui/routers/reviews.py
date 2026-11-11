"""v1.5.8 Dashboard: post-execution review queue, evidence preview, outcome submission and evaluation policy."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.infra.postgres import get_session
from app.models.evaluation_review import TenantEvaluationPolicy
from app.observability.metrics import EVALUATION_REVIEWS_TOTAL
from app.schemas.evaluation import EvaluationPreviewRequest, OutcomeSubmitRequest, ReviewDecisionRequest
from app.services.audit_log import get_run
from app.services.evaluation.evidence import EvidenceError
from app.services.evaluation.taxonomy import CATEGORIES
from app.services.judge_triage import default_order, effective_priority, list_assessments, triage_review
from app.services.outcomes import list_outcomes, preview_evaluation, refresh_review_queue_gauge, submit_outcome
from app.services.review_queue import ReviewError, decide_review, get_review, list_reviews, policy_relaxes, upsert_evaluation_policy
from app.services.review_sampling import sampling_summary
from app.ui.routes import dashboard_auth, templates
from app.ui.security import actor_for, csrf_protect, ensure_permission, ensure_tenant_access, needs, principal_of, scoped_tenant

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth), Depends(csrf_protect)])

REVIEW_LABELS = ("confirmed_hacking", "false_positive", "accepted", "needs_info")


def _int(value: Any) -> int:
    try:
        return max(0, int(str(value or "0").strip() or "0"))
    except ValueError:
        return 0


def _first_line(exc: Exception) -> str:
    text = str(exc)
    return text.splitlines()[0] if text else exc.__class__.__name__


def _evidence_from_form(form: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "final_message": str(form.get("final_message", "")),
        "diff": str(form.get("diff", "")),
        "provenance": str(form.get("provenance", "self_reported") or "self_reported"),
    }
    test_format = str(form.get("test_format", "summary") or "summary")
    if test_format == "junit_xml" and str(form.get("junit_xml", "")).strip():
        evidence["test_report"] = {"format": "junit_xml", "content": str(form.get("junit_xml", ""))}
    elif test_format == "summary":
        counts = {key: _int(form.get(f"tests_{key}")) for key in ("passed", "failed", "skipped", "errors")}
        if any(counts.values()) or str(form.get("tests_reported", "")).lower() in {"on", "true", "1"}:
            evidence["test_report"] = {"format": "summary", "summary": counts}
    lint_errors = str(form.get("lint_errors", "")).strip()
    if lint_errors:
        evidence["lint_report"] = {"tool": str(form.get("lint_tool", "lint") or "lint"), "errors": _int(lint_errors)}
    attestation_json = str(form.get("attestation_json", "")).strip()
    if attestation_json:
        try:
            attestation = json.loads(attestation_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Attestation is not valid JSON: {exc.msg}") from exc
        if not isinstance(attestation, dict):
            raise ValueError("Attestation must be a JSON object (DSSE envelope).")
        evidence["attestation"] = attestation
    return evidence


async def _render_reviews(
    request: Request,
    session: AsyncSession,
    *,
    preview: Any = None,
    preview_input: dict[str, Any] | None = None,
    error: str = "",
) -> HTMLResponse:
    params = request.query_params
    settings = get_settings()
    status = params.get("status", "active")
    priority = params.get("priority", "")
    category = params.get("category", "")
    tenant_id = await scoped_tenant(request, session, params.get("tenant_id", "")) or ""
    order = params.get("order", "")
    order = order if order in {"rule", "triage"} else default_order(settings)
    reviews = await list_reviews(
        session,
        status=status or None,
        priority=priority or None,
        category=category or None,
        tenant_id=tenant_id or None,
        limit=200,
        order=order,
    )
    own_tenant = None if principal_of(request).is_platform else tenant_id
    depth = await refresh_review_queue_gauge(session, tenant_id=own_tenant)
    sampling = await sampling_summary(session, settings, tenant_id=tenant_id or None, window_days=7)
    return templates.TemplateResponse(
        request,
        "reviews.html",
        {
            "reviews": reviews,
            "depth": depth,
            "order": order,
            "judge_mode": settings.evaluation_judge_mode,
            "judge_provider": settings.effective_judge_provider,
            "sampling": sampling,
            "effective_priority": effective_priority,
            "filters": {"status": status, "priority": priority, "category": category, "tenant_id": tenant_id},
            "categories": sorted(CATEGORIES.keys()),
            "preview": preview,
            "preview_input": preview_input or {},
            "message": params.get("message", ""),
            "error": error or params.get("error", ""),
            "active": "reviews",
        },
    )


@router.get("/dashboard/reviews", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="reviews"))
async def dashboard_reviews(request: Request, session: AsyncSession = Depends(get_session)):
    return await _render_reviews(request, session)


@router.post("/dashboard/reviews/preview", response_class=HTMLResponse, dependencies=needs("evaluations.preview", view="reviews"))
async def dashboard_reviews_preview(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    preview_input = {key: str(form.get(key, "")) for key in form.keys()}
    preview_tenant = await scoped_tenant(request, session, str(form.get("tenant_id", "")), permission="evaluations.preview")
    try:
        payload = EvaluationPreviewRequest(
            task_type=str(form.get("task_type", "code_fix") or "code_fix"),
            goal=str(form.get("goal", "")),
            tenant_id=preview_tenant,
            **_evidence_from_form(form),
        )
        evaluation = await preview_evaluation(session, payload=payload, tenant_id=payload.tenant_id or "default", settings=get_settings())
    except (ValidationError, ValueError, EvidenceError) as exc:
        message = exc.detail if isinstance(exc, EvidenceError) else _first_line(exc)
        return await _render_reviews(request, session, preview_input=preview_input, error=f"Preview rejected: {message}")
    return await _render_reviews(request, session, preview=evaluation, preview_input=preview_input)


@router.get("/dashboard/reviews/{review_id}", response_class=HTMLResponse, dependencies=needs("dashboard.view", view="review_detail"))
async def dashboard_review_detail(request: Request, review_id: str, session: AsyncSession = Depends(get_session)):
    review = await get_review(session, review_id)
    if review is None:
        if not principal_of(request).is_platform:
            await ensure_tenant_access(request, session, None)  # no existence oracle for tenant-bound users
        raise HTTPException(status_code=404, detail="Review not found.")
    await ensure_tenant_access(request, session, review.tenant_id)
    run = await get_run(session, review.run_id)
    outcomes = await list_outcomes(session, review.run_id)
    assessments = await list_assessments(session, review_id=review_id)
    return templates.TemplateResponse(
        request,
        "review_detail.html",
        {
            "review": review,
            "run": run,
            "outcomes": outcomes,
            "assessments": assessments,
            "judge_mode": get_settings().evaluation_judge_mode,
            "labels": REVIEW_LABELS,
            "categories": sorted(CATEGORIES.keys()),
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
            "active": "reviews",
        },
    )


@router.post("/dashboard/reviews/{review_id}/decision", response_class=HTMLResponse, dependencies=needs("reviews.decide"))
async def dashboard_review_decision(request: Request, review_id: str, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    target = await get_review(session, review_id)
    if target is not None:
        await ensure_tenant_access(request, session, target.tenant_id, permission="reviews.decide")
    try:
        decision = ReviewDecisionRequest(
            label=str(form.get("label", "")),
            # D23: in session mode the reviewer is the signed-in user; a forged form field is ignored.
            reviewer=actor_for(request, str(form.get("reviewer", "")).strip() or "dashboard-reviewer"),
            reason=str(form.get("reason", "")),
            label_categories=[str(item) for item in form.getlist("label_categories") if str(item).strip()],
            finding_labels={
                key.removeprefix("finding_label__"): str(form.get(key))
                for key in form.keys()
                if key.startswith("finding_label__") and str(form.get(key)) in {"true_positive", "false_positive"}
            },
        )
        result = await decide_review(session, review_id, decision)
    except ReviewError as exc:
        return RedirectResponse(f"/dashboard/reviews/{review_id}?error={quote(exc.detail)}", status_code=303)
    except (ValidationError, ValueError) as exc:
        return RedirectResponse(f"/dashboard/reviews/{review_id}?error={quote(_first_line(exc))}", status_code=303)
    if result is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    EVALUATION_REVIEWS_TOTAL.labels(label=decision.label).inc()
    review, run = result
    status = run.status if run is not None else "n/a"
    message = f"Review decided: {decision.label}. Run status is now {status}."
    return RedirectResponse(f"/dashboard/reviews/{review_id}?message={quote(message)}", status_code=303)


@router.post("/dashboard/reviews/{review_id}/judge-assessments", response_class=HTMLResponse, dependencies=needs("reviews.retriage"))
async def dashboard_review_reassess(request: Request, review_id: str, session: AsyncSession = Depends(get_session)):
    """v1.5.10: re-run advisory judge triage (deduplicated by evidence digest and judge version)."""
    review = await get_review(session, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    await ensure_tenant_access(request, session, review.tenant_id, permission="reviews.retriage")
    settings = get_settings()
    if settings.evaluation_judge_mode == "off":
        return RedirectResponse(f"/dashboard/reviews/{review_id}?error={quote('Judge mode is off.')}#judge-assessments", status_code=303)
    assessment, deduplicated = await triage_review(session, review, settings)
    if assessment is None:
        return RedirectResponse(f"/dashboard/reviews/{review_id}?error={quote('Judge assessment could not be stored.')}#judge-assessments", status_code=303)
    message = (
        f"Already assessed | 已評估: {assessment.assessment_id} (same evidence and judge version)."
        if deduplicated
        else f"Judge assessment {assessment.assessment_id} recorded ({assessment.status}, triage {assessment.triage_priority})."
    )
    return RedirectResponse(f"/dashboard/reviews/{review_id}?message={quote(message)}#judge-assessments", status_code=303)


@router.post("/dashboard/runs/{run_id}/outcome", response_class=HTMLResponse, dependencies=needs("outcomes.submit"))
async def dashboard_run_outcome(request: Request, run_id: str, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    await ensure_tenant_access(request, session, run.tenant_id, permission="outcomes.submit")
    # D23: the submitter is the signed-in user in session mode, so review_queue's self-review rule applies to it.
    submitted_by = actor_for(request, str(form.get("submitted_by", "")).strip())
    try:
        payload = OutcomeSubmitRequest(submitted_by=submitted_by, **_evidence_from_form(form))
        outcome, evaluation, review, run = await submit_outcome(session, run=run, payload=payload, settings=get_settings())
    except EvidenceError as exc:
        return RedirectResponse(f"/dashboard/runs/{run_id}?error={quote(exc.detail)}", status_code=303)
    except (ValidationError, ValueError) as exc:
        return RedirectResponse(f"/dashboard/runs/{run_id}?error={quote(_first_line(exc))}", status_code=303)
    message = (
        f"Outcome {outcome.outcome_id} evaluated: verdict={evaluation.verdict}"
        f"{' (' + evaluation.review_priority + ')' if evaluation.review_priority else ''}; run status {run.status}."
    )
    return RedirectResponse(f"/dashboard/runs/{run_id}?message={quote(message)}#outcome-evidence", status_code=303)


@router.post("/dashboard/governance/evaluation-policy", response_class=HTMLResponse, dependencies=needs("evaluation_policy.write"))
async def dashboard_evaluation_policy(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    tenant_id = await scoped_tenant(request, session, str(form.get("tenant_id", "")), permission="evaluation_policy.write") or "default"
    raw = str(form.get("protected_paths", ""))
    paths = [item.strip() for chunk in raw.splitlines() for item in chunk.split(",") if item.strip()]
    allow_defaults = str(form.get("allow_default_protected_paths", "")).lower() in {"on", "true", "1", "yes"}
    if await policy_relaxes(session, tenant_id=tenant_id, protected_paths=paths, allow_default_protected_paths=allow_defaults):
        # v1.6.1 feature seven: fewer protected paths needs evaluation_policy.relax and a recent re-authentication.
        await ensure_permission(request, session, "evaluation_policy.relax")
    await upsert_evaluation_policy(
        session,
        tenant_id=tenant_id,
        protected_paths=paths,
        allow_default_protected_paths=allow_defaults,
        updated_by=actor_for(request, str(form.get("updated_by", "dashboard-admin")).strip() or "dashboard-admin"),
    )
    message = f"Evaluation policy saved for {tenant_id} ({len(paths)} custom protected path(s))."
    return RedirectResponse(f"/dashboard/governance?tenant_id={quote(tenant_id)}&message={quote(message)}#evaluation-policy", status_code=303)


async def list_evaluation_policies(session: AsyncSession) -> list[TenantEvaluationPolicy]:
    return list((await session.scalars(select(TenantEvaluationPolicy).order_by(TenantEvaluationPolicy.tenant_id.asc()))).all())
