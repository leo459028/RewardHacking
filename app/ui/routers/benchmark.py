"""v1.5.9 Dashboard: labeled benchmark, detector measurement, honeypot probes, cheat rates and CI attestation keys.

All signals shown here are monitor-only. Like the API, the page is open to admin, tenant_admin and reviewer (plus the
read-only auditor, v1.6.1 D21) and not to operator, because it shows honeypot provenance. It is a platform view in
v1.6.1 (cross-tenant runs, honeypot results and cheat rates), so tenant-bound users use the API instead.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_control_plane_planner, get_event_publisher, get_orchestrator, get_state_store
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.benchmark import HoneypotLaunchRequest
from app.services import benchmark as bench
from app.services import honeypot as hp
from app.services.attestation_keys import (
    AttestationKeyServiceError,
    create_attestation_key,
    list_attestation_keys,
    revoke_attestation_key,
)
from app.services.auth import AuthContext
from app.services.cheat_rates import GROUP_BY_FIELDS, compute_cheat_rates
from app.services.control_plane import ControlPlanePlanner
from app.services.evaluation.engine import EvaluationEngine
from app.services.evaluation.metrics import fpr_key
from app.services.evaluation.taxonomy import CATEGORIES
from app.services.events import EventPublisher
from app.services.judge_background import pending_summary, sweep_pending_triage
from app.services.evaluation.judges.profiles import resolve_active_profile
from app.services.judge_profiles_admin import JudgeProfileAdminError, ensure_zero_cost_pricing, probe_profile, profiles_view
from app.services.orchestrator import AgentOrchestrator
from app.services.review_queue import get_evaluation_policy, policy_to_dict, upsert_evaluation_policy
from app.services.run_state import RunStateStore
from app.ui.routes import dashboard_auth, templates
from app.ui.security import actor_for, ensure_tenant_access, needs, principal_of

router = APIRouter(tags=["dashboard"], dependencies=[Depends(dashboard_auth)])
CURVE_WIDTH = 320
CURVE_HEIGHT = 160
DASHBOARD_ADMIN = AuthContext(enabled=False)


def _redirect(path: str, *, message: str = "", error: str = "", anchor: str = "") -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    query = f"message={quote(message)}" if message else f"error={quote(error)}"
    return RedirectResponse(f"{path}{separator}{query}{('#' + anchor) if anchor else ''}", status_code=303)


def audit_curve_points(curve: list[dict[str, Any]]) -> dict[str, Any]:
    """Server-side SVG polyline for the audit-budget curve (x = budget 0..max, y = catch rate 0..1)."""
    if not curve:
        return {"points": "", "dots": [], "max_budget": 0}
    max_budget = max(float(point["budget"]) for point in curve) or 1.0
    coords = [(0.0, float(CURVE_HEIGHT))]
    dots = []
    for point in curve:
        x = float(point["budget"]) / max_budget * CURVE_WIDTH
        rate = point.get("catch_rate")
        y = CURVE_HEIGHT - (float(rate) if rate is not None else 0.0) * CURVE_HEIGHT
        coords.append((x, y))
        dots.append({"x": round(x, 1), "y": round(y, 1), "budget": point["budget"], "rate": rate})
    return {"points": " ".join(f"{x:.1f},{y:.1f}" for x, y in coords), "dots": dots, "max_budget": max_budget}


@router.get("/dashboard/benchmark", response_class=HTMLResponse, dependencies=needs("benchmark.view", view="benchmark"))
async def dashboard_benchmark(request: Request, session: AsyncSession = Depends(get_session)):
    params = request.query_params
    settings = get_settings()
    tenant_id = params.get("tenant_id", "default") or "default"
    corpus = params.get("corpus", bench.SEED_CORPUS)
    label = params.get("label", "")
    category = params.get("category", "")
    source = params.get("source", "")
    split = params.get("split", "all")
    mode = params.get("mode", "as_configured")
    mode = mode if mode in {"as_configured", "all_enforced"} else "as_configured"
    group_by = params.get("group_by", "agent_id")
    group_by = group_by if group_by in GROUP_BY_FIELDS else "agent_id"

    await bench.ensure_seed_corpus(session)
    corpora = [corpus] if corpus else [bench.SEED_CORPUS, "global", f"tenant:{tenant_id}"]
    samples = await bench.select_samples(
        session, tenant_id=tenant_id, corpora=corpora, split=split, label=label or None, category=category or None, source=source or None, limit=300
    )
    sample_counts = await bench.refresh_sample_gauge(session)
    runs = await bench.list_benchmark_runs(session, limit=10)
    selected = None
    if params.get("run"):
        selected = await bench.get_benchmark_run(session, params["run"])
    if selected is None and runs:
        selected = runs[0]
    metrics = (selected.metrics or {}) if selected is not None else {}
    mode_metrics = metrics.get(mode) or {}
    current_version = EvaluationEngine().version()
    gate_status, gate_reason = bench.gate_evaluation(
        metrics if selected is not None and selected.evaluator_version == current_version else None, settings
    )
    probes = await hp.list_probes(session)
    honeypots = await hp.honeypot_results(session, limit=50)
    rates = await compute_cheat_rates(
        session, group_by=group_by, window_days=30, min_samples=settings.evaluation_cheat_rate_min_samples
    )
    keys = await list_attestation_keys(session, params.get("key_tenant_id", tenant_id) or tenant_id)
    consent_tenant = params.get("consent_tenant_id", tenant_id) or tenant_id
    consent = policy_to_dict(consent_tenant, await get_evaluation_policy(session, consent_tenant))
    return templates.TemplateResponse(
        request,
        "benchmark.html",
        {
            "active": "benchmark",
            "tenant_id": tenant_id,
            "filters": {"corpus": corpus, "label": label, "category": category, "source": source, "split": split},
            "categories": sorted(CATEGORIES.keys()),
            "samples": samples,
            "sample_counts": sample_counts,
            "runs": runs,
            "selected": selected,
            "metrics": metrics,
            "mode": mode,
            "mode_metrics": mode_metrics,
            "fpr_keys": metrics.get("fpr_targets") or [fpr_key(0.01), fpr_key(settings.evaluation_benchmark_gate_fpr)],
            "triage_keys": ["5", "10", "20", "budget_0.01", "budget_0.05", "budget_0.10"],
            # v1.5.11 judge profiles, external-judge consent and pending background triage
            "judge_profiles": profiles_view(settings),
            "active_judge_profile": resolve_active_profile(settings).name,  # v1.5.12: what live triage really uses
            "judge_mode": settings.evaluation_judge_mode,
            "consent": consent,
            "pending_triage": await pending_summary(session, settings),
            "gate_fpr_key": fpr_key(settings.evaluation_benchmark_gate_fpr),
            "curve": audit_curve_points(mode_metrics.get("audit_budget_curve") or []),
            "curve_width": CURVE_WIDTH,
            "curve_height": CURVE_HEIGHT,
            "current_version": current_version,
            "gate": {
                "status": gate_status,
                "reason": gate_reason,
                "enforced": settings.evaluation_benchmark_gate_enforced,
                "min_tpr": settings.evaluation_benchmark_min_tpr,
                "fpr": settings.evaluation_benchmark_gate_fpr,
            },
            "probes": probes,
            "honeypots": honeypots,
            "rates": rates,
            "group_by": group_by,
            "group_by_fields": GROUP_BY_FIELDS,
            "attestation_keys": keys,
            "key_tenant_id": params.get("key_tenant_id", tenant_id) or tenant_id,
            "message": params.get("message", ""),
            "error": params.get("error", ""),
        },
    )


def _bounded_int(value: object, *, default: int, low: int, high: int) -> int:
    text = str(value or "").strip()
    return max(low, min(high, int(text))) if text.isdigit() else default


@router.post("/dashboard/benchmark/runs", response_class=HTMLResponse, dependencies=needs("benchmark.manage", view="benchmark"))
async def dashboard_benchmark_run(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    corpora = [str(item) for item in form.getlist("corpora") if str(item).strip()] or [bench.SEED_CORPUS]
    split = str(form.get("split", "all") or "all")
    try:
        run = await bench.run_benchmark(
            session,
            tenant_id=tenant_id,
            settings=get_settings(),
            corpora=corpora,
            split=split if split in {"all", "dev", "holdout"} else "all",
            exclude_accepted_behavior=str(form.get("exclude_accepted_behavior", "")).lower() in {"on", "true", "1"},
            created_by=actor_for(request, str(form.get("created_by", "dashboard-admin")).strip() or "dashboard-admin"),
            judge_provider=str(form.get("judge_provider", "none") or "none"),
            max_judge_samples=_bounded_int(form.get("max_judge_samples"), default=100, low=1, high=5000),
            judge_profiles=[str(item) for item in form.getlist("judge_profiles") if str(item).strip()] or None,
        )
    except bench.BenchmarkError as exc:
        return _redirect(f"/dashboard/benchmark?tenant_id={quote(tenant_id)}", error=exc.detail, anchor="benchmark-runs")
    message = f"Benchmark {run.benchmark_run_id} measured {run.sample_count} samples ({run.positive_count} hacking / {run.negative_count} benign); gate {run.gate_status}."
    if run.judge_config:
        labels = [c.get("profile") or c.get("provider") for c in run.judge_config.get("profiles", [run.judge_config])]
        message += f" Judge triage measured with {run.judge_config.get('judge_version')} (profiles: {', '.join(labels)})."
    return _redirect(f"/dashboard/benchmark?tenant_id={quote(tenant_id)}&run={run.benchmark_run_id}", message=message, anchor="benchmark-runs")


@router.post("/dashboard/reviews/{review_id}/export-benchmark", response_class=HTMLResponse, dependencies=needs("benchmark.manage"))
async def dashboard_review_export(request: Request, review_id: str, session: AsyncSession = Depends(get_session)):
    from app.services.review_queue import get_review

    form = await request.form()
    scope = str(form.get("scope", "tenant") or "tenant")
    target = await get_review(session, review_id)
    if target is not None:
        await ensure_tenant_access(request, session, target.tenant_id, permission="benchmark.manage")
    try:
        sample = await bench.export_review(
            session,
            review_id=review_id,
            scope=scope,
            allow_global=principal_of(request).is_platform,  # D22: tenant-bound users export to their tenant only
            actor=actor_for(request, str(form.get("exported_by", "dashboard-admin")).strip() or "dashboard-admin"),
        )
    except bench.BenchmarkError as exc:
        suffix = f" ({exc.extra['sample_id']})" if exc.extra.get("sample_id") else ""
        return _redirect(f"/dashboard/reviews/{review_id}", error=f"{exc.detail}{suffix}")
    return _redirect(f"/dashboard/reviews/{review_id}", message=f"Exported to benchmark as {sample.sample_id} ({sample.label}, {sample.corpus}).")


@router.post("/dashboard/honeypots/launch", response_class=HTMLResponse, dependencies=needs("benchmark.manage", view="benchmark"))
async def dashboard_honeypot_launch(
    request: Request,
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    planner: ControlPlanePlanner = Depends(get_control_plane_planner),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    from fastapi import HTTPException

    from app.api.routers.benchmark import post_honeypot_launch

    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    probe_id = str(form.get("probe_id", "")).strip()
    try:
        payload = HoneypotLaunchRequest(
            tenant_id=tenant_id,
            user_id=str(form.get("user_id", "")).strip() or "agent-demo",
            agent_id=str(form.get("agent_id", "")).strip(),
            agent_version=str(form.get("agent_version", "")).strip(),
            model_name=str(form.get("model_name", "")).strip(),
            launched_by=actor_for(request, str(form.get("launched_by", "dashboard-admin")).strip() or "dashboard-admin"),
        )
        result = await post_honeypot_launch(
            probe_id,
            payload,
            request,
            auth_context=DASHBOARD_ADMIN,
            orchestrator=orchestrator,
            planner=planner,
            session=session,
            state_store=state_store,
            event_publisher=event_publisher,
        )
    except HTTPException as exc:
        detail = exc.detail.get("message") if isinstance(exc.detail, dict) else str(exc.detail)
        return _redirect(f"/dashboard/benchmark?tenant_id={quote(tenant_id)}", error=f"Launch failed: {detail}", anchor="honeypots")
    except ValidationError as exc:
        return _redirect(f"/dashboard/benchmark?tenant_id={quote(tenant_id)}", error=str(exc).splitlines()[0], anchor="honeypots")
    if isinstance(result, Response):
        return _redirect(f"/dashboard/benchmark?tenant_id={quote(tenant_id)}", error="Launch rejected by the submit path (quota or MCP gate).", anchor="honeypots")
    message = f"Launched probe {probe_id} as run {result['run_id']} ({result['status']})."
    return _redirect(f"/dashboard/benchmark?tenant_id={quote(tenant_id)}&launched={result['run_id']}", message=message, anchor="honeypots")


@router.get("/dashboard/honeypots/{probe_id}/package", response_class=HTMLResponse, dependencies=needs("honeypots.package", view="benchmark"))
async def dashboard_honeypot_package(request: Request, probe_id: str, session: AsyncSession = Depends(get_session)):
    probe = await hp.get_probe(session, probe_id)
    if probe is None:
        return _redirect("/dashboard/benchmark", error=f"Probe {probe_id} not found.", anchor="honeypots")
    return templates.TemplateResponse(
        request,
        "honeypot_package.html",
        {"active": "benchmark", "probe": probe, "package": hp.task_package(probe)},
    )


@router.post("/dashboard/benchmark/imports/inspect", response_class=HTMLResponse, dependencies=needs("benchmark.import", view="benchmark"))
async def dashboard_inspect_import(request: Request, session: AsyncSession = Depends(get_session)):
    from app.services.inspect_import import import_inspect_log

    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    dry_run = str(form.get("dry_run", "")).lower() in {"on", "true", "1"}
    target = f"/dashboard/benchmark?tenant_id={quote(tenant_id)}"
    try:
        result = await import_inspect_log(
            session,
            raw=str(form.get("log_text", "")),
            tenant_id=tenant_id,
            max_bytes=get_settings().evaluation_inspect_import_max_bytes,
            label_from=str(form.get("label_from", "score") or "score"),
            score_threshold=float(str(form.get("score_threshold", "1") or "1")),
            hacking_category=str(form.get("hacking_category", "special_casing") or "special_casing"),
            dry_run=dry_run,
            actor=actor_for(request, "dashboard-admin"),
        )
    except bench.BenchmarkError as exc:
        return _redirect(target, error=f"Inspect import rejected: {exc.detail}", anchor="inspect-import")
    except ValueError as exc:
        return _redirect(target, error=f"Inspect import rejected: {exc}", anchor="inspect-import")
    counts = result["counts"]
    verb = "Dry run" if dry_run else "Imported"
    message = (
        f"{verb} {result['corpus']}: {counts['hacking']} hacking / {counts['benign']} benign, "
        f"{counts['unlabelled']} unlabelled, {counts['duplicates']} duplicate(s), {counts['imported']} stored."
    )
    return _redirect(target, message=message, anchor="inspect-import")


@router.post("/dashboard/benchmark/attestation-keys", response_class=HTMLResponse, dependencies=needs("benchmark.manage", view="benchmark"))
async def dashboard_attestation_key_create(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    subjects = [item.strip() for item in str(form.get("allowed_subjects", "")).replace(",", "\n").splitlines() if item.strip()]
    target = f"/dashboard/benchmark?tenant_id={quote(tenant_id)}&key_tenant_id={quote(tenant_id)}"
    try:
        row = await create_attestation_key(
            session,
            tenant_id=tenant_id,
            name=str(form.get("name", "")),
            algorithm=str(form.get("algorithm", "ed25519") or "ed25519"),
            public_key_pem=str(form.get("public_key_pem", "")),
            allowed_subjects=subjects,
            created_by=actor_for(request, str(form.get("created_by", "dashboard-admin")).strip() or "dashboard-admin"),
        )
    except AttestationKeyServiceError as exc:
        return _redirect(target, error=exc.detail, anchor="attestation-keys")
    return _redirect(target, message=f"Attestation key {row.key_id} registered for {tenant_id}.", anchor="attestation-keys")


@router.post("/dashboard/benchmark/attestation-keys/{key_id}/revoke", response_class=HTMLResponse, dependencies=needs("benchmark.manage", view="benchmark"))
async def dashboard_attestation_key_revoke(request: Request, key_id: str, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    target = f"/dashboard/benchmark?tenant_id={quote(tenant_id)}&key_tenant_id={quote(tenant_id)}"
    try:
        await revoke_attestation_key(session, tenant_id=tenant_id, key_id=key_id, actor=actor_for(request, "dashboard-admin"))
    except AttestationKeyServiceError as exc:
        return _redirect(target, error=exc.detail, anchor="attestation-keys")
    return _redirect(target, message=f"Attestation key {key_id} revoked.", anchor="attestation-keys")


# ---------------------------------------------------------------------------
# v1.5.11 judge profiles: probe, local zero-cost pricing, external-judge consent and pending sweep
# ---------------------------------------------------------------------------


@router.post("/dashboard/judge/profiles/{name}/probe", response_class=HTMLResponse, dependencies=needs("judge.profile.operate", view="judge_profiles"))
async def dashboard_judge_probe(request: Request, name: str, session: AsyncSession = Depends(get_session)):
    try:
        result = await probe_profile(session, get_settings(), name)
    except JudgeProfileAdminError as exc:
        return _redirect("/dashboard/benchmark", error=exc.detail, anchor="judge-profiles")
    message = (
        f"Probe {name}: {result['status']}"
        f"{' (' + result['error'] + ')' if result['error'] else ''}, structured output "
        f"{result['structured_output'] or 'n/a'}, {result['attempts']} request(s), {result['latency_ms']} ms, "
        f"{result['input_tokens']}/{result['output_tokens']} tokens. No assessment was written."
    )
    return _redirect("/dashboard/benchmark", message=message, anchor="judge-profiles")


@router.post("/dashboard/judge/profiles/{name}/zero-cost-pricing", response_class=HTMLResponse, dependencies=needs("judge.profile.operate", view="judge_profiles"))
async def dashboard_judge_zero_cost_pricing(request: Request, name: str, session: AsyncSession = Depends(get_session)):
    try:
        result = await ensure_zero_cost_pricing(session, get_settings(), name)
    except JudgeProfileAdminError as exc:
        return _redirect("/dashboard/benchmark", error=exc.detail, anchor="judge-profiles")
    message = (
        f"Zero-cost pricing for {result['provider']}/{result['model']}: created {len(result['created'])} row(s)"
        f"{', already present: ' + ', '.join(result['already_present']) if result['already_present'] else ''}."
    )
    return _redirect("/dashboard/benchmark", message=message, anchor="judge-profiles")


@router.post("/dashboard/judge/consent", response_class=HTMLResponse, dependencies=needs("judge.external_consent", view="benchmark"))
async def dashboard_judge_consent(request: Request, session: AsyncSession = Depends(get_session)):
    form = await request.form()
    tenant_id = str(form.get("tenant_id", "default")).strip() or "default"
    current = policy_to_dict(tenant_id, await get_evaluation_policy(session, tenant_id))
    allow = str(form.get("allow_external_judge", "")).lower() in {"on", "true", "1", "yes"}
    profiles = [str(item).strip() for item in form.getlist("external_judge_profiles") if str(item).strip()]
    # Protected paths are kept as they are; only the consent fields change.
    await upsert_evaluation_policy(
        session,
        tenant_id=tenant_id,
        protected_paths=list(current["protected_paths"]),
        allow_default_protected_paths=bool(current["allow_default_protected_paths"]),
        updated_by=actor_for(request, str(form.get("updated_by", "dashboard-admin")).strip() or "dashboard-admin"),
        allow_external_judge=allow,
        external_judge_profiles=profiles,
    )
    scope = ", ".join(profiles) if profiles else "all external profiles"
    message = f"External judge consent for {tenant_id}: {'allowed (' + scope + ')' if allow else 'not allowed'}."
    return _redirect(f"/dashboard/benchmark?tenant_id={quote(tenant_id)}&consent_tenant_id={quote(tenant_id)}", message=message, anchor="external-judge-consent")


@router.post("/dashboard/judge/sweep", response_class=HTMLResponse, dependencies=needs("judge.profile.operate", view="judge_profiles"))
async def dashboard_judge_sweep(request: Request, session: AsyncSession = Depends(get_session)):
    swept = await sweep_pending_triage(session, get_settings())
    message = f"Re-scheduled {len(swept)} stale pending judge triage task(s)." if swept else "No stale pending judge triage."
    return _redirect("/dashboard/benchmark", message=message, anchor="pending-triage")
