from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.models.approval_event import ApprovalEvent
from app.models.evaluation_review import EvaluationReview
from app.models.finops import BudgetViolation, PricingCatalog
from app.models.incident import Incident
from app.models.operations import OperationsEvidence, ReleaseGateItem, ReleaseGateRun
from app.models.tenant import TenantApiKey
from app.models.worker_attempt import WorkerAttempt
from app.services.finops import ensure_default_pricing


@dataclass(frozen=True)
class OperationsCheck:
    check_key: str
    status: str
    title: str
    detail: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class OperationsReadiness:
    status: str
    summary: str
    checks: list[OperationsCheck]


def summarize_status(checks: list[OperationsCheck]) -> str:
    # v1.5.9: advisory checks are shown with their real status but do not decide the overall gate.
    statuses = {check.status for check in checks if not (check.metadata or {}).get("advisory")}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


def _status_summary(status: str) -> str:
    if status == "pass":
        return "Release gate prerequisites are currently satisfied."
    if status == "warn":
        return "Release gate has warnings that should be reviewed before release."
    return "Release gate has blocking failures."


async def latest_evidence(
    session: AsyncSession,
    *,
    evidence_type: str,
) -> OperationsEvidence | None:
    return await session.scalar(
        select(OperationsEvidence)
        .where(OperationsEvidence.evidence_type == evidence_type)
        .order_by(OperationsEvidence.created_at.desc())
        .limit(1)
    )


def _fresh_evidence_check(
    evidence: OperationsEvidence | None,
    *,
    check_key: str,
    title: str,
    max_age_days: int,
    missing_status: str = "warn",
) -> OperationsCheck:
    if evidence is None:
        return OperationsCheck(
            check_key=check_key,
            status=missing_status,
            title=title,
            detail="No operations evidence has been recorded yet.",
            metadata={},
        )
    created_at = evidence.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - created_at).days
    if evidence.status == "fail":
        status = "fail"
    elif age_days > max_age_days:
        status = "warn"
    else:
        status = evidence.status if evidence.status in {"pass", "warn"} else "warn"
    return OperationsCheck(
        check_key=check_key,
        status=status,
        title=title,
        detail=evidence.summary,
        metadata={"age_days": age_days, "reference_url": evidence.reference_url},
    )


async def _evaluation_benchmark_gate_check(session: AsyncSession, *, settings: Settings) -> OperationsCheck:
    """v1.5.9: reward-tampering TPR at the configured FPR on the latest benchmark for the current evaluator.

    Reads stored benchmark runs only (never runs a benchmark). Advisory unless
    ``EVALUATION_BENCHMARK_GATE_ENFORCED`` is true; when enforced, a stale benchmark warns and a TPR
    below ``EVALUATION_BENCHMARK_MIN_TPR`` fails in ``APP_ENV=prod``.
    """
    from app.services.benchmark import gate_evaluation, latest_benchmark_run
    from app.services.evaluation.engine import EvaluationEngine
    from app.services.evaluation.metrics import fpr_key

    evaluator_version = EvaluationEngine().version()
    run = await latest_benchmark_run(session, evaluator_version=evaluator_version)
    status, reason = gate_evaluation(run.metrics if run is not None else None, settings)
    cell: dict[str, Any] = {}
    auroc = None
    if run is not None:
        block = (run.metrics or {}).get("as_configured", {}).get("by_kind", {}).get("reward_tampering", {})
        cell = dict(block.get("tpr_at_fpr", {}).get(fpr_key(settings.evaluation_benchmark_gate_fpr)) or {})
        auroc = block.get("auroc")
    tpr = cell.get("tpr")
    detail = (
        f"No benchmark run for evaluator {evaluator_version}; run POST /v1/benchmark/runs."
        if run is None
        else (
            f"Benchmark {run.benchmark_run_id}: reward_tampering TPR@FPR{settings.evaluation_benchmark_gate_fpr:g}="
            f"{(tpr if tpr is not None else 0):.2f} (min {settings.evaluation_benchmark_min_tpr:.2f}), AUROC="
            f"{(auroc if auroc is not None else 0):.2f}; {reason}."
        )
    )
    enforced = bool(settings.evaluation_benchmark_gate_enforced)
    if not enforced:
        detail = "Advisory (not enforced): " + detail
    return OperationsCheck(
        check_key="evaluation_benchmark_gate",
        status=status,
        title="Detector benchmark gate",
        detail=detail,
        metadata={
            "advisory": not enforced,
            "reason": reason,
            "evaluator_version": evaluator_version,
            "benchmark_run_id": run.benchmark_run_id if run is not None else "",
            "corpus_digest": run.corpus_digest if run is not None else "",
            "reward_tampering_tpr": tpr,
            "reward_tampering_auroc": auroc,
            "insufficient_samples": bool(cell.get("insufficient_samples")) if cell else None,
            "min_tpr": settings.evaluation_benchmark_min_tpr,
            "fpr": settings.evaluation_benchmark_gate_fpr,
        },
    )


async def _evaluation_judge_health_check(session: AsyncSession, *, settings: Settings, now: datetime) -> OperationsCheck:
    """v1.5.10 (advisory): judge configuration, recent failure ratio, same-family warnings, latest triage benchmark."""
    from app.models.benchmark import BenchmarkRun
    from app.models.judge import JudgeAssessment
    from app.services.evaluation.judges import get_judge

    from app.services.evaluation.judges.profiles import active_profile_known

    judge = get_judge(settings)
    issues: list[str] = []
    status = "pass"
    if not active_profile_known(settings):
        status = "fail"
        issues.append(f"EVALUATION_JUDGE_PROFILE '{settings.evaluation_judge_profile}' is not defined (falling back to deterministic)")
    if settings.evaluation_judge_mode == "off":
        status = "warn"
        issues.append("judge mode is off")
    if judge.name == "openai_compatible":
        if not settings.evaluation_judge_base_url or not settings.evaluation_judge_model:
            status = "fail"
            issues.append("openai_compatible needs EVALUATION_JUDGE_BASE_URL and EVALUATION_JUDGE_MODEL")
        elif getattr(judge, "is_external", False) and not settings.evaluation_judge_allow_external:
            status = "warn" if status == "pass" else status
            issues.append("external judge endpoint is blocked (EVALUATION_JUDGE_ALLOW_EXTERNAL=false)")
    since = now - timedelta(hours=24)
    rows = (
        await session.execute(
            select(JudgeAssessment.status, func.count())
            .where(JudgeAssessment.created_at >= since, JudgeAssessment.benchmark_run_id == "")
            .group_by(JudgeAssessment.status)
        )
    ).all()
    counts = {str(key): int(value) for key, value in rows}
    total = sum(counts.values())
    failures = sum(counts.get(key, 0) for key in ("timeout", "error", "invalid_output"))
    failure_ratio = (failures / total) if total else 0.0
    if total and failure_ratio > 0.2:
        status = "warn" if status == "pass" else status
        issues.append(f"{failures}/{total} judge calls failed in 24h (fallback used)")
    same_family = int(
        await session.scalar(
            select(func.count()).select_from(JudgeAssessment).where(JudgeAssessment.created_at >= since, JudgeAssessment.same_family_warning.is_(True))
        )
        or 0
    )
    if same_family and settings.app_env.lower() == "prod":
        status = "warn" if status == "pass" else status
        issues.append(f"{same_family} assessments used a judge from the monitored agent's model family")
    if settings.judge_uses_legacy_alias:
        issues.append("LLM_JUDGE_ENABLED is deprecated; set EVALUATION_JUDGE_PROVIDER")
    from app.services.judge_background import pending_summary

    pending = await pending_summary(session, settings, now=now)
    if pending["stale"]:
        status = "warn" if status == "pass" else status
        issues.append(
            f"{pending['stale']} review(s) pending judge triage longer than {int(pending['sweep_threshold_seconds'])}s "
            "(run POST /v1/evaluations/judge/sweep)"
        )
    latest = None
    for candidate in (await session.scalars(select(BenchmarkRun).order_by(BenchmarkRun.id.desc()).limit(20))).all():
        if (candidate.metrics or {}).get("triage"):
            latest = candidate
            break
    triage = (latest.metrics or {}).get("triage", {}) if latest is not None else {}
    return OperationsCheck(
        check_key="evaluation_judge_health",
        status=status,
        title="Evaluation judge triage health",
        detail=(
            f"Advisory: judge {judge.version()} in {settings.evaluation_judge_mode} mode (ordering only, never decides verdicts); "
            f"24h assessments {total}, failures {failures}; " + ("; ".join(issues) if issues else "no issues") + "."
        ),
        metadata={
            "advisory": True,
            "mode": settings.evaluation_judge_mode,
            "profile": getattr(judge, "profile_label", ""),
            "configured_profile": settings.evaluation_judge_profile,
            "profile_known": active_profile_known(settings),
            "provider": judge.name,
            "judge_version": judge.version(),
            "status_counts_24h": counts,
            "failure_ratio_24h": round(failure_ratio, 4),
            "same_family_warnings_24h": same_family,
            "legacy_alias_in_use": settings.judge_uses_legacy_alias,
            "latest_triage_benchmark_run_id": latest.benchmark_run_id if latest is not None else "",
            "delta_vs_rule_only": triage.get("delta_vs_rule_only", {}),
            "promotion_criteria_met": triage.get("promotion_criteria_met"),
            "promotion_blockers": triage.get("promotion_blockers", []),
            "pending_triage": pending,
            "used_for_verdict": False,
        },
    )


async def _evaluation_sampling_health_check(session: AsyncSession, *, settings: Settings, now: datetime) -> OperationsCheck:
    """v1.5.10 (advisory): sampling enabled, secret configured, no skipped candidates, random slice active."""
    from app.services.review_sampling import policy_settings, policy_version, sampling_summary

    summary = await sampling_summary(session, settings, window_days=7, now=now)
    totals = summary["totals"]
    issues: list[str] = []
    status = "pass"
    if not settings.evaluation_sampling_enabled:
        status = "warn"
        issues.append("sampling disabled: no unbiased miss-rate estimate")
    if not settings.evaluation_sample_secret.get_secret_value():
        status = "warn"
        issues.append("EVALUATION_SAMPLE_SECRET unset: random draws are not reproducible across restarts")
    if summary["skipped_total"]:
        status = "warn"
        issues.append(f"{summary['skipped_total']} candidates skipped by caps in 7 days ({totals['skipped']})")
    if totals["candidates"] >= 100 and totals["sampled"]["random"] == 0 and settings.evaluation_sample_random_rate > 0:
        status = "warn"
        issues.append("random audit slice selected nothing from 100+ candidates")
    return OperationsCheck(
        check_key="evaluation_sampling_health",
        status=status,
        title="Review sampling health",
        detail=(
            f"Advisory: 7-day candidates {totals['candidates']}, sampled {totals['sampled']}, skipped {summary['skipped_total']}, "
            f"not selected {totals['not_selected']}; " + ("; ".join(issues) if issues else "no issues") + "."
        ),
        metadata={
            "advisory": True,
            "totals_7d": totals,
            "conserved": summary["conserved"],
            "random_inclusion_rate": summary["random_inclusion_rate"],
            "settings": policy_settings(settings),
            "policy_version": policy_version(settings),
        },
    )


async def _evaluation_review_queue_check(session: AsyncSession, *, settings: Settings, now: datetime) -> OperationsCheck:
    """v1.5.8: P0 reviews waiting beyond the SLA warn; unknown detector mode overrides fail."""
    from app.services.evaluation.registry import unknown_detector_overrides

    sla_cutoff = now - timedelta(hours=settings.evaluation_review_p0_sla_hours)
    open_rows = (
        await session.execute(
            select(EvaluationReview.priority, func.count())
            .where(EvaluationReview.status.in_(("open", "needs_info")))
            .group_by(EvaluationReview.priority)
        )
    ).all()
    open_by_priority = {str(priority): int(count) for priority, count in open_rows}
    overdue_p0 = (
        await session.scalar(
            select(func.count())
            .select_from(EvaluationReview)
            .where(
                EvaluationReview.status.in_(("open", "needs_info")),
                EvaluationReview.priority == "P0",
                EvaluationReview.opened_at < sla_cutoff,
            )
        )
        or 0
    )
    unknown = unknown_detector_overrides(settings.evaluation_detector_modes)
    status = "fail" if unknown else ("warn" if overdue_p0 else "pass")
    return OperationsCheck(
        check_key="evaluation_review_queue_health",
        status=status,
        title="Evaluation review queue health",
        detail=(
            f"Open reviews by priority: {open_by_priority or 'none'}; P0 reviews older than "
            f"{settings.evaluation_review_p0_sla_hours}h: {overdue_p0}; unknown detector overrides: {', '.join(unknown) or 'none'}."
        ),
        metadata={
            "open_by_priority": open_by_priority,
            "overdue_p0": int(overdue_p0),
            "p0_sla_hours": settings.evaluation_review_p0_sla_hours,
            "unknown_detector_overrides": unknown,
            "detector_modes": dict(settings.evaluation_detector_modes or {}),
        },
    )


async def compute_operations_readiness(
    session: AsyncSession,
    *,
    settings: Settings,
    state_store: object | None = None,
) -> OperationsReadiness:
    now = datetime.now(timezone.utc)
    queued_cutoff = now - timedelta(minutes=settings.release_gate_max_queued_age_minutes)
    stale_queued = (
        await session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.status == "queued", AgentRun.created_at < queued_cutoff)
        )
        or 0
    )
    dlq_count = (
        await session.scalar(
            select(func.count()).select_from(WorkerAttempt).where(WorkerAttempt.status == "dlq")
        )
        or 0
    )
    blocking_incidents = (
        await session.scalar(
            select(func.count())
            .select_from(Incident)
            .where(Incident.severity.in_(["high", "critical"]))
        )
        or 0
    )
    expired_approvals = (
        await session.scalar(
            select(func.count())
            .select_from(ApprovalEvent)
            .where(
                ApprovalEvent.status == "pending",
                (ApprovalEvent.expired.is_(True)) | (ApprovalEvent.expires_at < now),
            )
        )
        or 0
    )
    active_keys = (
        await session.scalar(
            select(func.count())
            .select_from(TenantApiKey)
            .where(TenantApiKey.status == "active", TenantApiKey.revoked.is_(False))
        )
        or 0
    )
    total_pricing_rows = (
        await session.scalar(select(func.count()).select_from(PricingCatalog)) or 0
    )
    if total_pricing_rows == 0:
        await ensure_default_pricing(session)
    active_pricing = (
        await session.scalar(
            select(func.count())
            .select_from(PricingCatalog)
            .where(PricingCatalog.is_active.is_(True))
        )
        or 0
    )
    open_critical_violations = (
        await session.scalar(
            select(func.count())
            .select_from(BudgetViolation)
            .where(
                BudgetViolation.resolution_status == "open",
                BudgetViolation.severity.in_(["high", "critical"]),
            )
        )
        or 0
    )
    violation_status = (
        ("fail" if settings.app_env.lower() == "prod" else "warn")
        if open_critical_violations
        else "pass"
    )
    from app.models.finops import BudgetPolicy
    from app.services.finops import compute_tenant_burn_rate_forecast

    active_policy_tenants = list(
        (
            await session.scalars(
                select(BudgetPolicy.tenant_id)
                .where(BudgetPolicy.is_active.is_(True))
                .distinct()
            )
        ).all()
    )
    elevated_burn_policies = 0
    critical_burn_policies = 0
    for b_tenant in active_policy_tenants:
        if not b_tenant:
            continue
        forecast_resp = await compute_tenant_burn_rate_forecast(session, tenant_id=b_tenant)
        for pf in forecast_resp.burn_rate_forecasts:
            if pf.burn_status == "critical_burn":
                critical_burn_policies += 1
            elif pf.burn_status == "elevated_burn":
                elevated_burn_policies += 1
    burn_health_status = "warn" if (critical_burn_policies or elevated_burn_policies) else "pass"

    from app.services.mcp_gateway import CORE_MCP_SERVER_IDS, list_mcp_servers

    mcp_servers = await list_mcp_servers(session, settings)
    mcp_index = {item["mcp_server_id"]: item for item in mcp_servers}
    unhealthy_core = [
        server_id
        for server_id in CORE_MCP_SERVER_IDS
        if server_id not in mcp_index
        or mcp_index[server_id]["status"] != "active"
        or mcp_index[server_id]["trust_tier"] == "blocked"
    ]
    open_mcp_breakers: dict = {}
    if state_store is not None and hasattr(state_store, "list_open_mcp_breakers"):
        open_mcp_breakers = await state_store.list_open_mcp_breakers()
    open_core_breakers = [server_id for server_id in CORE_MCP_SERVER_IDS if server_id in open_mcp_breakers]
    mcp_health_status = "warn" if (unhealthy_core or open_core_breakers or open_mcp_breakers) else "pass"

    checks = [
        OperationsCheck(
            check_key="audit_retention_config",
            status="pass" if settings.audit_retention_days > 0 else "fail",
            title="Audit retention configured",
            detail=f"Audit retention is set to {settings.audit_retention_days} days.",
            metadata={"audit_retention_days": settings.audit_retention_days},
        ),
        OperationsCheck(
            check_key="stale_queued_runs",
            status="fail" if stale_queued else "pass",
            title="No stale queued runs",
            detail=f"{stale_queued} queued runs are older than the release gate threshold.",
            metadata={"count": stale_queued},
        ),
        OperationsCheck(
            check_key="worker_dlq",
            status="fail" if dlq_count else "pass",
            title="Worker DLQ is empty",
            detail=f"{dlq_count} worker attempts are in DLQ.",
            metadata={"count": dlq_count},
        ),
        OperationsCheck(
            check_key="blocking_incidents",
            status="fail" if blocking_incidents else "pass",
            title="No high severity incidents",
            detail=f"{blocking_incidents} high or critical incidents are open in audit history.",
            metadata={"count": blocking_incidents},
        ),
        OperationsCheck(
            check_key="expired_approvals",
            status="warn" if expired_approvals else "pass",
            title="No expired approvals",
            detail=f"{expired_approvals} approval records are expired.",
            metadata={"count": expired_approvals},
        ),
        OperationsCheck(
            check_key="tenant_api_keys",
            status="pass" if active_keys else "warn",
            title="Tenant API keys available",
            detail=f"{active_keys} active DB-managed tenant API keys are available.",
            metadata={"active_keys": active_keys},
        ),
        OperationsCheck(
            check_key="finops_pricing_catalog_ready",
            status="pass" if active_pricing > 0 else "warn",
            title="FinOps pricing catalog ready",
            detail=f"{active_pricing} active pricing catalog rows are available.",
            metadata={"active_pricing": active_pricing},
        ),
        OperationsCheck(
            check_key="finops_open_critical_violations",
            status=violation_status,
            title="No open critical budget violations",
            detail=f"{open_critical_violations} high or critical FinOps budget violations are open.",
            metadata={"open_critical_violations": open_critical_violations},
        ),
        OperationsCheck(
            check_key="finops_burn_rate_health",
            status=burn_health_status,
            title="FinOps burn-rate forecast health",
            detail=(
                f"{critical_burn_policies} critical and {elevated_burn_policies} elevated burn-rate policies detected."
                if (critical_burn_policies or elevated_burn_policies)
                else "All active FinOps budget policies are within normal burn-rate trajectory."
            ),
            metadata={
                "critical_burn_policies": critical_burn_policies,
                "elevated_burn_policies": elevated_burn_policies,
                "evaluated_tenants": len(active_policy_tenants),
            },
        ),
        OperationsCheck(
            check_key="mcp_gateway_registry_health",
            status=mcp_health_status,
            title="Safe MCP Gateway registry health",
            detail=(
                f"{len(mcp_servers)} MCP servers registered; unhealthy core servers: {', '.join(unhealthy_core) or 'none'}; "
                f"open MCP breakers: {', '.join(sorted(open_mcp_breakers)) or 'none'}."
            ),
            metadata={
                "registered_servers": len(mcp_servers),
                "unhealthy_core_servers": unhealthy_core,
                "open_core_breakers": open_core_breakers,
                "open_mcp_breakers": sorted(open_mcp_breakers),
            },
        ),
        await _evaluation_review_queue_check(session, settings=settings, now=now),
        await _evaluation_benchmark_gate_check(session, settings=settings),
        await _evaluation_judge_health_check(session, settings=settings, now=now),
        await _evaluation_sampling_health_check(session, settings=settings, now=now),
    ]

    if settings.app_env.lower() == "prod" and settings.release_gate_fail_on_bootstrap_keys_in_prod:
        checks.append(
            OperationsCheck(
                check_key="bootstrap_keys_in_prod",
                status="fail" if settings.api_keys else "pass",
                title="No bootstrap API keys in production",
                detail="APP_ENV=prod should not rely on API_KEYS bootstrap fallback.",
                metadata={"api_keys_configured": bool(settings.api_keys)},
            )
        )

    backup = await latest_evidence(session, evidence_type="backup_restore_drill")
    secret = await latest_evidence(session, evidence_type="secret_rotation")
    safety = await latest_evidence(session, evidence_type="safety_regression")
    safety_missing_status = "fail" if settings.app_env.lower() == "prod" else "warn"
    checks.extend(
        [
            _fresh_evidence_check(
                backup,
                check_key="backup_restore_evidence",
                title="Recent backup/restore evidence",
                max_age_days=settings.operations_evidence_max_age_days,
            ),
            _fresh_evidence_check(
                secret,
                check_key="secret_rotation_evidence",
                title="Recent secret rotation evidence",
                max_age_days=settings.secret_rotation_max_age_days,
            ),
            _fresh_evidence_check(
                safety,
                check_key="safety_regression_evidence",
                title="Recent safety regression evidence",
                max_age_days=settings.operations_evidence_max_age_days,
                missing_status=safety_missing_status,
            ),
        ]
    )
    status = summarize_status(checks)
    return OperationsReadiness(status=status, summary=_status_summary(status), checks=checks)


async def persist_operations_evidence(
    session: AsyncSession,
    *,
    evidence_type: str,
    status: str,
    summary: str,
    reference_url: str,
    metadata: dict[str, Any],
    created_by: str,
) -> OperationsEvidence:
    evidence = OperationsEvidence(
        evidence_type=evidence_type,
        status=status,
        summary=summary,
        reference_url=reference_url,
        metadata_payload=metadata,
        created_by=created_by,
    )
    session.add(evidence)
    await session.commit()
    await session.refresh(evidence)
    return evidence


async def list_operations_evidence(
    session: AsyncSession,
    *,
    evidence_type: str | None = None,
    limit: int = 100,
) -> list[OperationsEvidence]:
    stmt = select(OperationsEvidence).order_by(OperationsEvidence.created_at.desc()).limit(limit)
    if evidence_type:
        stmt = stmt.where(OperationsEvidence.evidence_type == evidence_type)
    return list((await session.scalars(stmt)).all())


async def persist_release_gate(
    session: AsyncSession,
    *,
    release_version: str,
    created_by: str,
    trace_id: str,
    readiness: OperationsReadiness,
) -> ReleaseGateRun:
    gate = ReleaseGateRun(
        gate_id=f"gate_{uuid.uuid4().hex[:12]}",
        release_version=release_version,
        status=readiness.status,
        summary=readiness.summary,
        created_by=created_by,
        trace_id=trace_id,
    )
    session.add(gate)
    await session.flush()
    for check in readiness.checks:
        session.add(
            ReleaseGateItem(
                gate_id=gate.gate_id,
                check_key=check.check_key,
                status=check.status,
                title=check.title,
                detail=check.detail,
                metadata_payload=check.metadata,
            )
        )
    await session.commit()
    await session.refresh(gate)
    return gate


async def list_release_gates(session: AsyncSession, *, limit: int = 50) -> list[ReleaseGateRun]:
    stmt = select(ReleaseGateRun).order_by(ReleaseGateRun.created_at.desc()).limit(limit)
    return list((await session.scalars(stmt)).all())


async def get_release_gate(session: AsyncSession, gate_id: str) -> ReleaseGateRun | None:
    return await session.scalar(select(ReleaseGateRun).where(ReleaseGateRun.gate_id == gate_id))


async def list_release_gate_items(session: AsyncSession, gate_id: str) -> list[ReleaseGateItem]:
    stmt = (
        select(ReleaseGateItem)
        .where(ReleaseGateItem.gate_id == gate_id)
        .order_by(ReleaseGateItem.id.asc())
    )
    return list((await session.scalars(stmt)).all())
