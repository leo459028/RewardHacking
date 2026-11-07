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
from app.services.outcomes import list_outcomes, preview_evaluation, refresh_review_queue_gauge, submit_outcome
from app.services.review_queue import ReviewError, decide_review, get_review, list_reviews, upsert_evaluation_policy
from app.ui.routes import dashboard_auth, templates

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth)])

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
    status = params.get("status", "active")
    priority = params.get("priority", "")
    category = params.get("category", "")
    tenant_id = params.get("tenant_id", "")
    reviews = await list_reviews(
        session,
        status=status or None,
        priority=priority or None,
        category=category or None,
        tenant_id=tenant_id or None,
        limit=200,
    )
    depth = await refresh_review_queue_gauge(session)
    return templates.TemplateResponse(
        request,
        "reviews.html",
        {
            "reviews": reviews,
            "depth": depth,
            "filters": {"status": status, "priority": priority, "category": category, "tenant_id": tenant_id},
            "categories": sorted(CATEGORIES.keys()),
            "preview": preview,
            "preview_input": preview_input or {},
            "message": params.get("message", ""),
            "error": error or params.get("error", ""),
            "active": "reviews",
        },
    )


@router.get("/dashboard/reviews", response_class=HTMLResponse)
async def dashboard_reviews(request: Request, session: AsyncSession = Depends(get_session)):
    return await _render_reviews(request, session)


@router.post("/dashboard/reviews/preview", response_class=HTMLResponse)
async def dashboard_reviews_preview(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    preview_input = {key: str(form.get(key, "")) for key in form.keys()}
    try:
        payload = EvaluationPreviewRequest(
            task_type=str(form.get("task_type", "code_fix") or "code_fix"),
            goal=str(form.get("goal", "")),
            tenant_id=str(form.get("tenant_id", "")).strip() or None,
            **_evidence_from_form(form),
        )
        evaluation = await preview_evaluation(session, payload=payload, tenant_id=payload.tenant_id or "default", settings=get_settings())
    except (ValidationError, ValueError, EvidenceError) as exc:
        message = exc.detail if isinstance(exc, EvidenceError) else _first_line(exc)
        return await _render_reviews(request, session, preview_input=preview_input, error=f"Preview rejected: {message}")
    return await _render_reviews(request, session, preview=evaluation, preview_input=preview_input)


@router.get("/dashboard/reviews/{review_id}", response_class=HTMLResponse)
async def dashboard_review_detail(request: Request, review_id: str, session: AsyncSession = Depends(get_session)):
    review = await get_review(session, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found.")
    run = await get_run(session, review.run_id)
    outcomes = await list_outcomes(session, review.run_id)
    return templates.TemplateResponse(
        request,
        "review_detail.html",
        {
            "review": review,
            "run": run,
            "outcomes": outcomes,
            "labels": REVIEW_LABELS,
            "categories": sorted(CATEGORIES.keys()),
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
            "active": "reviews",
        },
    )


@router.post("/dashboard/reviews/{review_id}/decision", response_class=HTMLResponse)
async def dashboard_review_decision(request: Request, review_id: str, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    try:
        decision = ReviewDecisionRequest(
            label=str(form.get("label", "")),
            reviewer=str(form.get("reviewer", "")).strip() or "dashboard-reviewer",
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


@router.post("/dashboard/runs/{run_id}/outcome", response_class=HTMLResponse)
async def dashboard_run_outcome(request: Request, run_id: str, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    run = await get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    try:
        payload = OutcomeSubmitRequest(submitted_by=str(form.get("submitted_by", "")).strip(), **_evidence_from_form(form))
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


@router.post("/dashboard/governance/evaluation-policy", response_class=HTMLResponse)
async def dashboard_evaluation_policy(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    raw = str(form.get("protected_paths", ""))
    paths = [item.strip() for chunk in raw.splitlines() for item in chunk.split(",") if item.strip()]
    allow_defaults = str(form.get("allow_default_protected_paths", "")).lower() in {"on", "true", "1", "yes"}
    await upsert_evaluation_policy(
        session,
        tenant_id=tenant_id,
        protected_paths=paths,
        allow_default_protected_paths=allow_defaults,
        updated_by=str(form.get("updated_by", "dashboard-admin")).strip() or "dashboard-admin",
    )
    message = f"Evaluation policy saved for {tenant_id} ({len(paths)} custom protected path(s))."
    return RedirectResponse(f"/dashboard/governance?tenant_id={quote(tenant_id)}&message={quote(message)}#evaluation-policy", status_code=303)


async def list_evaluation_policies(session: AsyncSession) -> list[TenantEvaluationPolicy]:
    return list((await session.scalars(select(TenantEvaluationPolicy).order_by(TenantEvaluationPolicy.tenant_id.asc()))).all())
