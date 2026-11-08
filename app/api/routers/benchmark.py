"""v1.5.9 labeled benchmark, detector measurement and honeypot endpoints.

Router split rule (AGENTS.md 2.1). Every response is monitor-only (``signal_usage``); benchmark
runs have no side effects beyond storing the run itself. Benchmark data and honeypot details are
never visible to the ``operator`` role used by agent harnesses.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_control_plane_planner, get_event_publisher, get_orchestrator, get_state_store
from app.api.routers.evaluations import _scoped_tenant
from app.core.config import get_settings
from app.infra.postgres import get_session
from app.schemas.agent import AgentConstraints, AgentRequest
from app.schemas.benchmark import (
    BenchmarkRunRequest,
    BulkReviewExportRequest,
    HoneypotEnableRequest,
    HoneypotLaunchRequest,
    InspectImportRequest,
    ReviewExportRequest,
    SampleRelabelRequest,
    SampleRetireRequest,
)
from app.services import benchmark as bench
from app.services import honeypot as hp
from app.services.auth import AuthContext, get_auth_context, require_role
from app.services.cheat_rates import agent_identity
from app.services.inspect_import import import_inspect_log
from app.services.control_plane import ControlPlanePlanner
from app.services.events import EventPublisher
from app.services.orchestrator import AgentOrchestrator
from app.services.run_state import RunStateStore

router = APIRouter(prefix="/v1", tags=["benchmark"])

READ_ROLES = ("reviewer", "tenant_admin", "admin")
WRITE_ROLES = ("tenant_admin", "admin")
HONEYPOT_TOOLS = ["read_repo", "run_tests"]


def _is_admin(auth_context: AuthContext) -> bool:
    return (not auth_context.enabled) or auth_context.role in {"admin", "super_admin"}


def _http(exc: bench.BenchmarkError | hp.HoneypotError) -> HTTPException:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.detail}
    detail.update(getattr(exc, "extra", {}) or {})
    return HTTPException(status_code=exc.status_code, detail=detail)


def _actor(explicit: str, auth_context: AuthContext) -> str:
    return explicit or (auth_context.key_id if auth_context.enabled else "") or "dashboard"


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------


@router.get("/benchmark/samples")
async def get_benchmark_samples(
    corpus: str | None = None,
    label: str | None = None,
    category: str | None = None,
    source: str | None = None,
    split: str = Query(default="all", pattern="^(all|dev|holdout)$"),
    status: str = Query(default="active", pattern="^(active|retired|all)$"),
    tenant_id: str | None = None,
    include_evidence: bool = False,
    limit: int = Query(default=200, ge=1, le=2000),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *READ_ROLES)
    tenant = _scoped_tenant(tenant_id, auth_context) or "default"
    await bench.ensure_seed_corpus(session)
    corpora = [corpus] if corpus else [bench.SEED_CORPUS, "global", f"tenant:{tenant}"]
    rows = await bench.select_samples(
        session, tenant_id=tenant, corpora=corpora, split=split, label=label, category=category, source=source, status=status, limit=limit
    )
    await bench.refresh_sample_gauge(session)
    return {
        "tenant_id": tenant,
        "count": len(rows),
        "samples": [bench.sample_to_dict(row, include_evidence=include_evidence) for row in rows],
        "signal_usage": "monitor_only",
    }


async def _visible_sample(session: AsyncSession, sample_id: str, auth_context: AuthContext):
    sample = await bench.get_sample(session, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail={"code": "benchmark_sample_not_found", "message": "Sample not found."})
    tenant = _scoped_tenant(None, auth_context)
    if sample.scope == "tenant" and tenant and sample.tenant_id != tenant:
        raise HTTPException(status_code=404, detail={"code": "benchmark_sample_not_found", "message": "Sample not found."})
    return sample


@router.get("/benchmark/samples/{sample_id}")
async def get_benchmark_sample(
    sample_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *READ_ROLES)
    return bench.sample_to_dict(await _visible_sample(session, sample_id, auth_context))


@router.post("/reviews/{review_id}/export-benchmark", status_code=201)
async def post_review_export(
    review_id: str,
    payload: ReviewExportRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *WRITE_ROLES)
    from app.services.review_queue import get_review

    review = await get_review(session, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail={"code": "review_not_found", "message": "Review not found."})
    _scoped_tenant(review.tenant_id, auth_context)
    try:
        sample = await bench.export_review(
            session,
            review_id=review_id,
            scope=payload.scope,
            allow_global=_is_admin(auth_context),
            label_categories=payload.label_categories or None,
            notes=payload.notes,
            actor=_actor(payload.exported_by, auth_context),
        )
    except bench.BenchmarkError as exc:
        raise _http(exc) from exc
    return bench.sample_to_dict(sample)


@router.post("/benchmark/samples/export-reviews")
async def post_bulk_review_export(
    payload: BulkReviewExportRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *WRITE_ROLES)
    tenant = _scoped_tenant(payload.tenant_id, auth_context)
    if payload.scope == "global" and not _is_admin(auth_context):
        raise HTTPException(status_code=403, detail={"code": "benchmark_scope_forbidden", "message": "Only admins can export to the global corpus."})
    try:
        return await bench.export_reviews(
            session,
            tenant_id=tenant,
            label=payload.label,
            decided_after=payload.decided_after,
            scope=payload.scope,
            allow_global=_is_admin(auth_context),
            actor=_actor(payload.exported_by, auth_context),
            limit=payload.limit,
        )
    except bench.BenchmarkError as exc:
        raise _http(exc) from exc


@router.post("/benchmark/samples/{sample_id}/relabel")
async def post_sample_relabel(
    sample_id: str,
    payload: SampleRelabelRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    await _visible_sample(session, sample_id, auth_context)
    try:
        sample = await bench.relabel_sample(
            session,
            sample_id=sample_id,
            label=payload.label,
            label_categories=payload.label_categories,
            reason=payload.reason,
            actor=_actor(payload.relabeled_by, auth_context),
        )
    except bench.BenchmarkError as exc:
        raise _http(exc) from exc
    return bench.sample_to_dict(sample)


@router.post("/benchmark/samples/{sample_id}/retire")
async def post_sample_retire(
    sample_id: str,
    payload: SampleRetireRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *WRITE_ROLES)
    await _visible_sample(session, sample_id, auth_context)
    try:
        sample = await bench.retire_sample(
            session,
            sample_id=sample_id,
            tenant_id=_scoped_tenant(None, auth_context),
            is_admin=_is_admin(auth_context),
            actor=_actor(payload.retired_by, auth_context),
        )
    except bench.BenchmarkError as exc:
        raise _http(exc) from exc
    return bench.sample_to_dict(sample, include_evidence=False)


@router.post("/benchmark/imports/inspect")
async def post_inspect_import(
    payload: InspectImportRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    """Optional: import an Inspect JSON eval log as tenant-scoped samples (dry run by default)."""
    require_role(auth_context, "admin")
    if (payload.log is None) == (payload.log_text is None):
        raise HTTPException(status_code=422, detail={"code": "inspect_import_invalid_log", "message": "Provide exactly one of 'log' or 'log_text'."})
    tenant = _scoped_tenant(payload.tenant_id, auth_context) or "default"
    try:
        return await import_inspect_log(
            session,
            raw=payload.log if payload.log is not None else payload.log_text or "",
            tenant_id=tenant,
            max_bytes=get_settings().evaluation_inspect_import_max_bytes,
            label_from=payload.label_from,
            score_threshold=payload.score_threshold,
            scorer=payload.scorer,
            metadata_key=payload.metadata_key,
            hacking_category=payload.hacking_category,
            dry_run=payload.dry_run,
            actor=_actor(payload.imported_by, auth_context),
        )
    except bench.BenchmarkError as exc:
        raise _http(exc) from exc


# ---------------------------------------------------------------------------
# Benchmark runs
# ---------------------------------------------------------------------------


def _uses_llm_profile(names: list[str] | None) -> bool:
    """Unknown names count as LLM profiles here (admin required); the service then rejects them with 422."""
    from app.services.evaluation.judges.profiles import get_profile

    settings = get_settings()
    for name in names or []:
        profile = get_profile(settings, name)
        if profile is None or profile.provider != "deterministic":
            return True
    return False


@router.post("/benchmark/runs", status_code=201)
async def post_benchmark_run(
    payload: BenchmarkRunRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *WRITE_ROLES)
    if payload.judge_provider == "openai_compatible" or _uses_llm_profile(payload.judge_profiles):
        # v1.5.10 (D23): sending benchmark evidence to an LLM judge costs money and is not reproducible.
        require_role(auth_context, "admin")
    tenant = _scoped_tenant(payload.tenant_id, auth_context) or "default"
    try:
        run = await bench.run_benchmark(
            session,
            tenant_id=tenant,
            settings=get_settings(),
            corpora=payload.corpora,
            split=payload.split,
            exclude_accepted_behavior=payload.exclude_accepted_behavior,
            created_by=_actor(payload.created_by, auth_context),
            judge_provider=payload.judge_provider,
            max_judge_samples=payload.max_judge_samples,
            judge_profiles=payload.judge_profiles,
        )
    except bench.BenchmarkError as exc:
        raise _http(exc) from exc
    return bench.benchmark_run_to_dict(run)


@router.get("/benchmark/runs")
async def get_benchmark_runs(
    tenant_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=200),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *READ_ROLES)
    tenant = _scoped_tenant(tenant_id, auth_context)
    runs = await bench.list_benchmark_runs(session, tenant_id=tenant, limit=limit)
    return {"runs": [bench.benchmark_run_to_dict(run, include_per_sample=False) for run in runs], "signal_usage": "monitor_only"}


async def _visible_run(session: AsyncSession, benchmark_run_id: str, auth_context: AuthContext):
    run = await bench.get_benchmark_run(session, benchmark_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "benchmark_run_not_found", "message": "Benchmark run not found."})
    _scoped_tenant(run.tenant_id, auth_context)
    return run


@router.get("/benchmark/runs/{benchmark_run_id}")
async def get_benchmark_run_detail(
    benchmark_run_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *READ_ROLES)
    return bench.benchmark_run_to_dict(await _visible_run(session, benchmark_run_id, auth_context))


@router.get("/benchmark/runs/{benchmark_run_id}/export")
async def get_benchmark_run_export(
    benchmark_run_id: str,
    format: str = Query(default="json", pattern="^(json|csv)$"),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *READ_ROLES)
    run = await _visible_run(session, benchmark_run_id, auth_context)
    if format == "csv":
        return PlainTextResponse(
            bench.per_sample_csv(run),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{run.benchmark_run_id}.csv"'},
        )
    return {
        "benchmark_run_id": run.benchmark_run_id,
        "evaluator_version": run.evaluator_version,
        "corpus_digest": run.corpus_digest,
        "per_sample": list(run.per_sample or []),
        "signal_usage": "monitor_only",
    }


# ---------------------------------------------------------------------------
# Honeypot probes
# ---------------------------------------------------------------------------


@router.get("/honeypots")
async def get_honeypot_probes(
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *READ_ROLES)
    probes = await hp.list_probes(session)
    return {"probes": [hp.probe_to_dict(row) for row in probes], "signal_usage": "monitor_only"}


@router.get("/honeypots/results")
async def get_honeypot_results(
    tenant_id: str | None = None,
    probe_id: str | None = None,
    agent_id: str | None = None,
    model_name: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *READ_ROLES)
    tenant = _scoped_tenant(tenant_id, auth_context)
    return await hp.honeypot_results(session, tenant_id=tenant, probe_id=probe_id, agent_id=agent_id, model_name=model_name, limit=limit)


@router.get("/honeypots/{probe_id}/package")
async def get_honeypot_package(
    probe_id: str,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, *WRITE_ROLES)
    probe = await hp.get_probe(session, probe_id)
    if probe is None:
        raise HTTPException(status_code=404, detail={"code": "honeypot_probe_not_found", "message": "Probe not found."})
    return hp.task_package(probe)


@router.post("/honeypots/{probe_id}/enabled")
async def post_honeypot_enabled(
    probe_id: str,
    payload: HoneypotEnableRequest,
    auth_context: AuthContext = Depends(get_auth_context),
    session: AsyncSession = Depends(get_session),
):
    require_role(auth_context, "admin")
    try:
        probe = await hp.set_probe_enabled(session, probe_id, payload.enabled)
    except hp.HoneypotError as exc:
        raise _http(exc) from exc
    return hp.probe_to_dict(probe)


@router.post("/honeypots/{probe_id}/launch", status_code=201)
async def post_honeypot_launch(
    probe_id: str,
    payload: HoneypotLaunchRequest,
    request: Request,
    auth_context: AuthContext = Depends(get_auth_context),
    orchestrator: AgentOrchestrator = Depends(get_orchestrator),
    planner: ControlPlanePlanner = Depends(get_control_plane_planner),
    session: AsyncSession = Depends(get_session),
    state_store: RunStateStore = Depends(get_state_store),
    event_publisher: EventPublisher = Depends(get_event_publisher),
):
    """Create a probe run through the normal submit path (D17); the probe mapping stays server-side (D15)."""
    require_role(auth_context, *WRITE_ROLES)
    from app.api.routes import submit_agent_request

    probe = await hp.get_probe(session, probe_id)
    if probe is None:
        raise HTTPException(status_code=404, detail={"code": "honeypot_probe_not_found", "message": "Probe not found."})
    if not probe.enabled:
        raise HTTPException(status_code=409, detail={"code": "honeypot_probe_disabled", "message": "Probe is disabled."})
    tenant = _scoped_tenant(payload.tenant_id, auth_context) or "default"
    identity = agent_identity({"agent_id": payload.agent_id, "agent_version": payload.agent_version, "model_name": payload.model_name})
    agent_request = AgentRequest(
        tenant_id=tenant,
        user_id=payload.user_id,
        task_type=probe.task_type if probe.task_type in {"code_fix", "analysis", "data_query", "workflow", "other"} else "code_fix",
        prompt=probe.prompt,
        goal=probe.goal,
        allowed_tools=list(HONEYPOT_TOOLS),
        constraints=AgentConstraints(max_tool_calls=4),
        metadata={key: value for key, value in identity.items() if value},
    )
    # Launch is authorised above (tenant_admin/admin); the run itself is submitted exactly like an
    # operator would submit it, on the same tenant, so every pre-execution guard applies unchanged.
    submit_context = replace(auth_context, role="operator") if auth_context.enabled else auth_context
    response = await submit_agent_request(
        request=request,
        agent_request=agent_request,
        auth_context=submit_context,
        orchestrator=orchestrator,
        planner=planner,
        session=session,
        state_store=state_store,
        event_publisher=event_publisher,
    )
    if isinstance(response, Response):
        return response  # quota or MCP rejection from the normal submit path; nothing was launched
    data = response.model_dump()
    launch = await hp.record_launch(
        session,
        run_id=data["run_id"],
        probe=probe,
        tenant_id=tenant,
        identity=identity,
        launched_by=_actor(payload.launched_by, auth_context),
    )
    return {
        "run_id": launch.run_id,
        "status": data.get("status"),
        "probe_id": probe.probe_id,
        "tenant_id": tenant,
        "package_url": f"/v1/honeypots/{probe.probe_id}/package",
        "signal_usage": "monitor_only",
    }
