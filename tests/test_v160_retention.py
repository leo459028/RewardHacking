"""v1.6.0 P3: audit retention purge (plan -> apply), legal holds, tombstones and the FinOps ledger report.

Service-level tests on in-memory SQLite (D9-D17). HTTP endpoints, the 410 ``run_purged`` response and the
Dashboard views are P5 / P6 and are tested there. Postgres-only behaviour (``SELECT ... FOR UPDATE``,
``pg_try_advisory_lock``) is not exercised on SQLite.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.jobs import retention as retention_job
from app.models.agent_run import AgentRun
from app.models.approval_event import ApprovalEvent
from app.models.benchmark import BenchmarkSample, HoneypotLaunch
from app.models.checkpoint import ReplayAttempt, RunCheckpoint
from app.models.control_plane import ExecutionPlan, ExecutionPlanStep
from app.models.evaluation_result import EvaluationResultRecord
from app.models.evaluation_review import EvaluationReview, RunOutcome
from app.models.finops import BudgetViolation, CostEvent, UsageEvent
from app.models.governance_event import GovernanceEvent
from app.models.incident import Incident
from app.models.judge import JudgeAssessment, ReviewSamplingDecision
from app.models.retention import LegalHold, RetentionPurge, RetentionTombstone
from app.models.tool_execution import ToolExecution
from app.models.worker_attempt import WorkerAttempt
from app.services import retention
from app.services.run_state import InMemoryRunStateStore

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=200)
RECENT = NOW - timedelta(days=5)
SECRET_PROMPT = "PROMPT-CONTENT-zx81"
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"

# Run-scoped tables that a purge deletes (D10), listed independently of the service's own spec.
RUN_TABLES = (
    ("approval_events", ApprovalEvent, ApprovalEvent.run_id),
    ("worker_attempts", WorkerAttempt, WorkerAttempt.run_id),
    ("tool_executions", ToolExecution, ToolExecution.run_id),
    ("evaluation_results", EvaluationResultRecord, EvaluationResultRecord.run_id),
    ("review_sampling_decisions", ReviewSamplingDecision, ReviewSamplingDecision.run_id),
    ("evaluation_reviews", EvaluationReview, EvaluationReview.run_id),
    ("run_outcomes", RunOutcome, RunOutcome.run_id),
    ("execution_plan_steps", ExecutionPlanStep, ExecutionPlanStep.run_id),
    ("execution_plans", ExecutionPlan, ExecutionPlan.run_id),
    ("run_checkpoints", RunCheckpoint, RunCheckpoint.run_id),
    ("replay_attempts", ReplayAttempt, ReplayAttempt.source_run_id),
    ("agent_runs", AgentRun, AgentRun.run_id),
)


def _settings(**overrides) -> Settings:
    values = {
        "audit_retention_purge_mode": "apply",
        "audit_retention_days": 90,
        "audit_retention_purge_batch_size": 500,
        "audit_retention_plan_ttl_minutes": 60,
        "finops_ledger_retention_days": 2555,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def run(coro):
    return asyncio.run(coro)


async def _count(session, model, *where) -> int:
    return int((await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one())


def _add_run(session, run_id: str, *, tenant: str = "t1", status: str = "completed", when: datetime = OLD) -> AgentRun:
    agent_run = AgentRun(
        run_id=run_id,
        tenant_id=tenant,
        user_id="u1",
        task_type="analysis",
        prompt=SECRET_PROMPT,
        goal="goal text",
        status=status,
        decision_reason="ok",
        created_at=when,
        updated_at=when,
    )
    session.add(agent_run)
    return agent_run


def _add_full_children(session, run_id: str, *, when: datetime = OLD) -> None:
    """One row in every D10 table plus rows that must survive (ledger, benchmark judge, admin audit)."""
    session.add_all(
        [
            ApprovalEvent(run_id=run_id, status="approved", created_at=when),
            WorkerAttempt(run_id=run_id, attempt_number=1, status="completed", created_at=when),
            ToolExecution(run_id=run_id, tool_name="read_data", status="ok", created_at=when),
            EvaluationResultRecord(run_id=run_id, task_type="analysis", verdict="pass", final_score=1.0, created_at=when),
            RunOutcome(outcome_id=f"outcome_{run_id}", run_id=run_id, created_at=when),
            EvaluationReview(review_id=f"review_{run_id}", run_id=run_id, status="closed", label="false_positive", exported_sample_id="bms_x", updated_at=when),
            Incident(run_id=run_id, severity="low", code="note", detail=SECRET_PROMPT, created_at=when),
            ExecutionPlan(plan_id=f"plan_{run_id}", run_id=run_id, workflow_id="wf", workflow_version="1", status="completed", created_at=when, updated_at=when),
            ExecutionPlanStep(step_id=f"step_{run_id}", plan_id=f"plan_{run_id}", run_id=run_id, step_type="tool", status="completed", created_at=when),
            RunCheckpoint(checkpoint_id=f"ckpt_{run_id}", run_id=run_id, checkpoint_type="pre", status="created", created_at=when),
            ReplayAttempt(replay_id=f"replay_{run_id}", source_run_id=run_id, created_at=when),
            GovernanceEvent(tenant_id="t1", run_id=run_id, event_type="policy.decision", decision="allow", created_at=when),
            JudgeAssessment(assessment_id=f"jdg_{run_id}", run_id=run_id, created_at=when),
            ReviewSamplingDecision(decision_id=f"smp_{run_id}", run_id=run_id, day="2026-04-23", created_at=when),
            # Survivors (D10 / D14):
            JudgeAssessment(assessment_id=f"jdg_bm_{run_id}", run_id=run_id, benchmark_run_id="bmr_1", created_at=when),
            GovernanceEvent(tenant_id="t1", run_id="", event_type="tenant.updated", decision="ok", created_at=when),
            UsageEvent(usage_id=f"usage_{run_id}", run_id=run_id, resource_type="model", usage_unit="token", quantity=Decimal("10"), created_at=when),
            CostEvent(cost_id=f"cost_{run_id}", run_id=run_id, usage_event_id=f"usage_{run_id}", resource_type="model", created_at=when),
            BudgetViolation(violation_id=f"bv_{run_id}", run_id=run_id, violation_type="over_budget", created_at=when),
        ]
    )


async def _seed(factory, *run_ids: str, full: bool = True, **kwargs) -> None:
    async with factory() as session:
        for run_id in run_ids:
            _add_run(session, run_id, **kwargs)
            if full:
                _add_full_children(session, run_id)
        await session.commit()


async def _plan(factory, settings=None, **kwargs) -> RetentionPurge:
    async with factory() as session:
        return await retention.build_plan(session, settings or _settings(), now=NOW, **kwargs)


async def _apply(factory, purge: RetentionPurge, settings=None, **kwargs) -> RetentionPurge:
    kwargs.setdefault("plan_digest", purge.plan_digest)
    kwargs.setdefault("confirm", True)
    async with factory() as session:
        return await retention.apply_plan(session, settings or _settings(), purge.purge_id, now=NOW, **kwargs)


def _load_migration(name: str):
    spec = importlib.util.spec_from_file_location(name[:-3], MIGRATIONS_DIR / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# migration, models, settings
# ---------------------------------------------------------------------------


def test_v160_migration_is_single_head_after_v1512():
    module = _load_migration("202611120002_add_v160_runtime_foundations.py")
    assert module.revision == "202611120002" and module.down_revision == "202611120001"
    revisions = {}
    for path in MIGRATIONS_DIR.glob("*.py"):
        mod = _load_migration(path.name)
        revisions[mod.revision] = mod.down_revision
    heads = set(revisions) - {down for down in revisions.values() if down}
    assert len(heads) == 1 and (heads == {"202611120002"} or "202611120002" in revisions.values())
    columns = {c.name: c for c in RetentionTombstone.__table__.columns}
    assert columns["run_id"].unique and columns["prev_hash"].nullable and columns["entry_hash"].nullable
    assert {"legal_holds", "retention_purges", "retention_tombstones"} <= set(RetentionPurge.metadata.tables)


def test_v160_retention_settings_defaults_and_mode_fail_closed():
    settings = Settings(_env_file=None)
    assert settings.audit_retention_purge_mode == "plan"
    assert settings.audit_retention_purge_batch_size == 500
    assert settings.audit_retention_plan_ttl_minutes == 60
    assert settings.finops_ledger_retention_days == 2555
    assert retention.purge_mode(_settings(audit_retention_purge_mode="APPLY")) == "apply"
    assert retention.purge_mode(_settings(audit_retention_purge_mode="aply")) == "off"


# ---------------------------------------------------------------------------
# D9: candidates and exclusion reasons
# ---------------------------------------------------------------------------


def test_v160_plan_selects_expired_terminal_runs_only(session_factory):
    async def scenario():
        await _seed(session_factory, "run_old")
        await _seed(session_factory, "run_new", when=RECENT)
        async with session_factory() as session:  # old run with recent worker activity is not expired yet
            _add_run(session, "run_active")
            session.add(WorkerAttempt(run_id="run_active", attempt_number=2, status="completed", created_at=RECENT))
            await session.commit()
        purge = await _plan(session_factory)
        assert purge.status == "planned" and purge.candidate_run_ids == ["run_old"]
        assert purge.scan_summary["recent_activity_runs"] == 1
        assert purge.estimated_counts["agent_runs"] == 1 and purge.estimated_counts["judge_assessments"] == 1
        assert purge.estimated_counts["incidents"] == 1
        assert retention.purge_to_dict(purge)["expires_at"] == (NOW + timedelta(minutes=60)).isoformat()

    run(scenario())


def _cond_hold_run(session, run_id):
    session.add(LegalHold(hold_id="lh_r", scope_type="run", scope_id=run_id, tenant_id="t1", reason="litigation"))


def _cond_hold_tenant(session, run_id):
    session.add(LegalHold(hold_id="lh_t", scope_type="tenant", scope_id="t1", tenant_id="t1", reason="audit"))


EXCLUSION_CASES = {
    "legal_hold_run": ("legal_hold", _cond_hold_run),
    "legal_hold_tenant": ("legal_hold", _cond_hold_tenant),
    "run_not_terminal": ("run_not_terminal", None),
    "dlq_unresolved": ("dlq_unresolved", lambda s, r: s.add(WorkerAttempt(run_id=r, attempt_number=3, status="dlq", created_at=OLD))),
    "review_open": ("review_open", lambda s, r: s.add(EvaluationReview(review_id="review_o", run_id=r, status="needs_info", updated_at=OLD))),
    "unexported_label": (
        "unexported_label",
        lambda s, r: s.add(EvaluationReview(review_id="review_l", run_id=r, status="closed", label="confirmed_hacking", updated_at=OLD)),
    ),
    "benchmark_reference": (
        "benchmark_reference",
        lambda s, r: s.add(BenchmarkSample(sample_id="bms_a", corpus="tenant", label="hacking", evidence_digest="d", origin_run_id=r)),
    ),
    "high_severity_incident": ("high_severity_incident", lambda s, r: s.add(Incident(run_id=r, severity="critical", code="x", detail="y", created_at=OLD))),
    "checkpoint_reference": (
        "checkpoint_reference",
        lambda s, r: s.add(RunCheckpoint(checkpoint_id="ckpt_n", run_id=r, checkpoint_type="pre", status="created", created_at=RECENT)),
    ),
    "replay_reference": ("replay_reference", lambda s, r: s.add(ReplayAttempt(replay_id="replay_n", source_run_id=r, created_at=RECENT))),
    "honeypot_measurement": ("honeypot_measurement", lambda s, r: s.add(HoneypotLaunch(run_id=r, probe_id="hp_1", created_at=OLD))),
}


@pytest.mark.parametrize("case", sorted(EXCLUSION_CASES))
def test_v160_each_exclusion_reason_blocks_purge(session_factory, case):
    reason, condition = EXCLUSION_CASES[case]

    async def scenario():
        async with session_factory() as session:
            _add_run(session, "run_x", status="review_required" if case == "run_not_terminal" else "completed")
            if condition:
                condition(session, "run_x")
            await session.commit()
            assert await retention.exclusion_reasons(session, "run_x", settings=_settings(), now=NOW) == [reason]
        purge = await _plan(session_factory)
        assert purge.candidate_run_ids == []
        assert purge.excluded_summary[reason] == {"count": 1, "examples": ["run_x"]}

    run(scenario())


NEGATIVE_CASES = {
    "released_hold": lambda s, r: s.add(
        LegalHold(hold_id="lh_old", scope_type="run", scope_id=r, tenant_id="t1", reason="done", status="released")
    ),
    "exported_label": lambda s, r: s.add(
        EvaluationReview(review_id="review_e", run_id=r, status="closed", label="accepted", exported_sample_id="bms_e", updated_at=OLD)
    ),
    "retired_benchmark_sample": lambda s, r: s.add(
        BenchmarkSample(sample_id="bms_r", corpus="tenant", label="hacking", evidence_digest="d", origin_run_id=r, status="retired")
    ),
    "low_incident": lambda s, r: s.add(Incident(run_id=r, severity="medium", code="x", detail="y", created_at=OLD)),
    "resolved_dlq": lambda s, r: s.add(WorkerAttempt(run_id=r, attempt_number=4, status="dlq_resolved", created_at=OLD)),
    "expired_checkpoint_and_replay": lambda s, r: s.add_all(
        [
            RunCheckpoint(checkpoint_id="ckpt_o", run_id=r, checkpoint_type="pre", status="created", created_at=OLD),
            ReplayAttempt(replay_id="replay_o", source_run_id=r, created_at=OLD),
        ]
    ),
}


@pytest.mark.parametrize("case", sorted(NEGATIVE_CASES))
def test_v160_legitimate_history_does_not_block_purge(session_factory, case):
    async def scenario():
        async with session_factory() as session:
            _add_run(session, "run_ok")
            NEGATIVE_CASES[case](session, "run_ok")
            await session.commit()
        purge = await _plan(session_factory)
        assert purge.candidate_run_ids == ["run_ok"] and purge.excluded_summary == {}

    run(scenario())


def test_v160_all_exclusion_reasons_are_recorded_together(session_factory):
    async def scenario():
        async with session_factory() as session:
            _add_run(session, "run_multi", status="queued")
            _cond_hold_tenant(session, "run_multi")
            EXCLUSION_CASES["honeypot_measurement"][1](session, "run_multi")
            EXCLUSION_CASES["unexported_label"][1](session, "run_multi")
            await session.commit()
            reasons = await retention.exclusion_reasons(session, "run_multi", settings=_settings(), now=NOW)
        assert reasons == ["legal_hold", "run_not_terminal", "unexported_label", "honeypot_measurement"]
        assert set(retention.EXCLUSION_REASONS) >= set(reasons)

    run(scenario())


# ---------------------------------------------------------------------------
# D11: plan digest and modes
# ---------------------------------------------------------------------------


def test_v160_plan_digest_is_deterministic(session_factory):
    async def scenario():
        await _seed(session_factory, "run_b", "run_a", full=False)
        first = await _plan(session_factory)
        second = await _plan(session_factory)
        assert first.purge_id != second.purge_id and first.plan_digest == second.plan_digest
        expected = retention.compute_plan_digest(NOW - timedelta(days=90), retention.policy_snapshot(_settings()), ["run_a", "run_b"])
        assert first.plan_digest == expected and first.candidate_run_ids == ["run_a", "run_b"]
        await _seed(session_factory, "run_c", full=False)
        assert (await _plan(session_factory)).plan_digest != first.plan_digest
        assert (await _plan(session_factory, _settings(audit_retention_days=91))).plan_digest != first.plan_digest

    run(scenario())


def test_v160_plan_respects_batch_size(session_factory):
    async def scenario():
        await _seed(session_factory, "run_1", "run_2", "run_3", full=False)
        purge = await _plan(session_factory, _settings(audit_retention_purge_batch_size=2))
        assert purge.candidate_run_ids == ["run_1", "run_2"] and purge.scan_summary["truncated"] is True

    run(scenario())


def test_v160_mode_off_refuses_plan_and_apply(session_factory):
    async def scenario():
        await _seed(session_factory, "run_a", full=False)
        purge = await _plan(session_factory)
        with pytest.raises(retention.RetentionError) as plan_err:
            await _plan(session_factory, _settings(audit_retention_purge_mode="off"))
        with pytest.raises(retention.RetentionError) as apply_err:
            await _apply(session_factory, purge, _settings(audit_retention_purge_mode="off"))
        assert plan_err.value.code == apply_err.value.code == "retention_disabled"

    run(scenario())


def test_v160_default_plan_mode_refuses_apply(session_factory):
    async def scenario():
        await _seed(session_factory, "run_a", full=False)
        purge = await _plan(session_factory, _settings(audit_retention_purge_mode="plan"))
        with pytest.raises(retention.RetentionError) as err:
            await _apply(session_factory, purge, _settings(audit_retention_purge_mode="plan"))
        assert (err.value.code, err.value.status_code) == ("retention_apply_disabled", 409)
        async with session_factory() as session:
            assert await _count(session, AgentRun) == 1

    run(scenario())


@pytest.mark.parametrize(
    ("change", "code", "status"),
    [
        ({"plan_digest": "0" * 64}, "retention_plan_digest_mismatch", 409),
        ({"confirm": False}, "retention_confirm_required", 422),
        ({"now_offset": timedelta(minutes=61)}, "retention_plan_expired", 409),
    ],
)
def test_v160_apply_refuses_stale_or_unconfirmed_plans(session_factory, change, code, status):
    async def scenario():
        await _seed(session_factory, "run_a", full=False)
        purge = await _plan(session_factory)
        kwargs = {"plan_digest": purge.plan_digest, "confirm": True}
        offset = change.pop("now_offset", timedelta())
        kwargs.update(change)
        async with session_factory() as session:
            with pytest.raises(retention.RetentionError) as err:
                await retention.apply_plan(session, _settings(), purge.purge_id, now=NOW + offset, **kwargs)
        assert (err.value.code, err.value.status_code) == (code, status)
        async with session_factory() as session:
            assert await _count(session, AgentRun) == 1 and await _count(session, RetentionTombstone) == 0

    run(scenario())


def test_v160_apply_twice_and_unknown_plan_are_refused(session_factory):
    async def scenario():
        await _seed(session_factory, "run_a", full=False)
        purge = await _plan(session_factory)
        assert (await _apply(session_factory, purge)).status == "applied"
        with pytest.raises(retention.RetentionError) as again:
            await _apply(session_factory, purge)
        assert again.value.code == "retention_plan_already_applied"
        async with session_factory() as session:
            with pytest.raises(retention.RetentionError) as missing:
                await retention.apply_plan(session, _settings(), "purge_nope", plan_digest="x", confirm=True, now=NOW)
        assert (missing.value.code, missing.value.status_code) == ("retention_plan_not_found", 404)

    run(scenario())


# ---------------------------------------------------------------------------
# D10 / D13: apply, delete scope, tombstone, receipt
# ---------------------------------------------------------------------------


def test_v160_apply_deletes_run_scope_and_keeps_ledger_benchmark_and_admin_audit(session_factory):
    async def scenario():
        await _seed(session_factory, "run_a", "run_b")
        async with session_factory() as session:
            session.add(BenchmarkSample(sample_id="bms_keep", corpus="tenant", label="hacking", evidence_digest="d", origin_run_id="run_gone", status="retired"))
            await session.commit()
            ledger_before = [await _count(session, model) for model in (UsageEvent, CostEvent, BudgetViolation)]
        purge = await _plan(session_factory)
        result = await _apply(session_factory, purge, actor="ops@example")
        assert result.status == "applied" and result.failures == [] and result.skipped_at_apply == []
        assert result.applied_counts["runs"] == 2 and result.applied_counts["agent_runs"] == 2
        assert result.applied_counts["governance_events"] == 2 and result.applied_counts["judge_assessments"] == 2
        async with session_factory() as session:
            for label, model, column in RUN_TABLES:
                assert await _count(session, model, column.in_(["run_a", "run_b"])) == 0, label
            assert await _count(session, Incident) == 0
            assert await _count(session, GovernanceEvent, GovernanceEvent.run_id.in_(["run_a", "run_b"])) == 0
            # Survivors: FinOps ledger (run_id kept for traceability), benchmark judge rows, admin audit, benchmark.
            assert [await _count(session, model) for model in (UsageEvent, CostEvent, BudgetViolation)] == ledger_before
            assert await _count(session, UsageEvent, UsageEvent.run_id == "run_a") == 1
            assert await _count(session, JudgeAssessment, JudgeAssessment.benchmark_run_id == "bmr_1") == 2
            assert await _count(session, GovernanceEvent, GovernanceEvent.event_type == "tenant.updated") == 2
            assert await _count(session, BenchmarkSample) == 1
            tombstones = list((await session.execute(select(RetentionTombstone).order_by(RetentionTombstone.run_id))).scalars())
        assert [t.run_id for t in tombstones] == ["run_a", "run_b"]
        assert tombstones[0].record_counts["agent_runs"] == 1 and tombstones[0].record_counts["incidents"] == 1
        assert tombstones[0].final_status == "completed" and tombstones[0].purge_id == purge.purge_id

    run(scenario())


def test_v160_tombstone_digest_is_recomputable_and_hash_chain_columns_reserved(session_factory):
    async def scenario():
        await _seed(session_factory, "run_a")
        entries = []
        async with session_factory() as session:
            specs = RUN_TABLES + (
                ("incidents", Incident, Incident.run_id),
                ("governance_events", GovernanceEvent, GovernanceEvent.run_id),
            )
            for label, model, column in specs:
                for row in (await session.execute(select(model).where(column == "run_a"))).scalars():
                    entries.append((label, row.id, retention.row_digest(row)))
            for row in (await session.execute(select(JudgeAssessment).where(JudgeAssessment.run_id == "run_a", JudgeAssessment.benchmark_run_id == ""))).scalars():
                entries.append(("judge_assessments", row.id, retention.row_digest(row)))
        purge = await _plan(session_factory)
        result = await _apply(session_factory, purge)
        async with session_factory() as session:
            tombstone = await retention.tombstone_for(session, "run_a")
        assert tombstone.content_digest == retention.content_digest(entries)
        assert len(entries) == sum(tombstone.record_counts.values())
        # v1.6.1 P5 (feature eight): the audit chain fills the reserved columns in the purge transaction
        assert tombstone.chain_version == "rh-chain/1" and len(tombstone.prev_hash) == 64 and len(tombstone.entry_hash) == 64
        assert tombstone.tombstone_id.startswith("tomb_") and tombstone.policy_snapshot["retention_days"] == 90
        assert result.receipt_digest == retention.receipt_digest([("run_a", tombstone.content_digest)])

    run(scenario())


def test_v160_receipt_tombstone_and_governance_events_carry_no_content(session_factory):
    async def scenario():
        await _seed(session_factory, "run_a")
        purge = await _plan(session_factory, actor="ops@example")
        result = await _apply(session_factory, purge, actor="ops@example")
        async with session_factory() as session:
            events = list(
                (await session.execute(select(GovernanceEvent).where(GovernanceEvent.event_type.like("retention.%")).order_by(GovernanceEvent.id))).scalars()
            )
            tombstone = await retention.tombstone_for(session, "run_a")
        assert [e.event_type for e in events] == ["retention.plan_created", "retention.purge_applied"]
        assert all(e.run_id == "" and e.tenant_id == retention.SYSTEM_TENANT for e in events)
        assert events[1].details["receipt_digest"] == result.receipt_digest and events[1].details["purged_runs"] == 1
        blob = json.dumps(
            [retention.purge_to_dict(result), retention.tombstone_summary(tombstone), [e.details for e in events], tombstone.policy_snapshot],
            default=str,
        )
        assert SECRET_PROMPT not in blob and "goal text" not in blob

    run(scenario())


def test_v160_hold_created_after_plan_is_skipped_at_apply(session_factory):
    async def scenario():
        await _seed(session_factory, "run_a", "run_b")
        purge = await _plan(session_factory)
        async with session_factory() as session:
            await retention.create_hold(session, scope_type="run", scope_id="run_b", reason="litigation", actor="admin1", actor_role="admin")
        result = await _apply(session_factory, purge)
        assert result.status == "applied"
        assert result.skipped_at_apply == [{"run_id": "run_b", "reasons": ["legal_hold"]}]
        async with session_factory() as session:
            assert await _count(session, AgentRun, AgentRun.run_id == "run_b") == 1
            assert await _count(session, WorkerAttempt, WorkerAttempt.run_id == "run_b") == 1
            assert await retention.tombstone_for(session, "run_b") is None
            assert await retention.tombstone_for(session, "run_a") is not None

    run(scenario())


def test_v160_partial_failure_rolls_back_the_run_and_is_resumable(session_factory, monkeypatch):
    original = retention._delete_run_records

    async def flaky(session, run_id):
        result = await original(session, run_id)
        if run_id == "run_b":
            raise RuntimeError("disk full")
        return result

    async def scenario():
        await _seed(session_factory, "run_a", "run_b", "run_c")
        monkeypatch.setattr(retention, "_delete_run_records", flaky)
        result = await _apply(session_factory, await _plan(session_factory))
        assert result.status == "partial"
        assert result.failures == [{"run_id": "run_b", "error": "RuntimeError"}]
        assert result.applied_counts["runs"] == 2
        async with session_factory() as session:  # the failed run's transaction rolled back completely
            for label, model, column in RUN_TABLES:
                assert await _count(session, model, column == "run_b") == 1, label
            assert await retention.tombstone_for(session, "run_b") is None
        monkeypatch.setattr(retention, "_delete_run_records", original)
        retry = await _plan(session_factory)
        assert retry.candidate_run_ids == ["run_b"]
        assert (await _apply(session_factory, retry)).status == "applied"
        async with session_factory() as session:
            assert await _count(session, AgentRun) == 0 and await _count(session, RetentionTombstone) == 3

    run(scenario())


def test_v160_apply_drops_run_state_keys_but_not_leases(session_factory):
    async def scenario():
        store = InMemoryRunStateStore()
        await store.set_run_state("run_a", "completed")
        await store.mark_idempotency_key("run:run_a:worker:1")
        await store.mark_idempotency_key("run:run_other:worker:1")
        store.token_leases["tenant:t1:lease:run_a"] = {"run_id": "run_a"}
        await _seed(session_factory, "run_a", full=False)
        await _apply(session_factory, await _plan(session_factory), state_store=store)
        assert "run_a" not in store.run_states
        assert store.idempotency_keys == {"run:run_other:worker:1"}
        assert "tenant:t1:lease:run_a" in store.token_leases

    run(scenario())


def test_v160_tombstone_summary_for_purged_run(session_factory):
    async def scenario():
        await _seed(session_factory, "run_a", full=False)
        purge = await _plan(session_factory)
        await _apply(session_factory, purge)
        async with session_factory() as session:
            assert await retention.tombstone_for(session, "run_missing") is None
            summary = retention.tombstone_summary(await retention.tombstone_for(session, "run_a"))
        assert summary["run_id"] == "run_a" and summary["purge_id"] == purge.purge_id
        assert summary["purged_at"] == NOW.isoformat() and summary["run_created_at"] == OLD.isoformat()
        assert summary["record_counts"]["agent_runs"] == 1 and summary["policy_version"] == "v1.6.0"

    run(scenario())


# ---------------------------------------------------------------------------
# D15: legal holds
# ---------------------------------------------------------------------------


def test_v160_hold_creation_roles_and_tenant_scope(session_factory):
    async def scenario():
        await _seed(session_factory, "run_t1", full=False)
        await _seed(session_factory, "run_t2", tenant="t2", full=False)
        async with session_factory() as session:
            own = await retention.create_hold(
                session, scope_type="tenant", scope_id="t1", reason="audit", actor="ta", actor_role="tenant_admin", actor_tenant_id="t1"
            )
            own_run = await retention.create_hold(
                session, scope_type="run", scope_id="run_t1", reason="case 7", actor="ta", actor_role="tenant_admin", actor_tenant_id="t1"
            )
            admin_hold = await retention.create_hold(session, scope_type="run", scope_id="run_t2", reason="case 8", actor="root", actor_role="admin")
            assert own.hold_id.startswith("lh_") and own.status == "active" and own_run.tenant_id == "t1"
            assert admin_hold.tenant_id == "t2"
            refused = [
                dict(scope_type="tenant", scope_id="t2", reason="x", actor_role="tenant_admin", actor_tenant_id="t1"),
                dict(scope_type="run", scope_id="run_t2", reason="x", actor_role="tenant_admin", actor_tenant_id="t1"),
                dict(scope_type="tenant", scope_id="t1", reason="x", actor_role="viewer", actor_tenant_id="t1"),
                dict(scope_type="tenant", scope_id="t1", reason="  ", actor_role="admin"),
                dict(scope_type="workflow", scope_id="t1", reason="x", actor_role="admin"),
                dict(scope_type="run", scope_id="run_nope", reason="x", actor_role="admin"),
            ]
            codes = []
            for kwargs in refused:
                with pytest.raises(retention.RetentionError) as err:
                    await retention.create_hold(session, actor="someone", **kwargs)
                codes.append((err.value.code, err.value.status_code))
            events = await _count(session, GovernanceEvent, GovernanceEvent.event_type == "retention.legal_hold_created")
        assert codes == [
            ("legal_hold_forbidden", 403),
            ("legal_hold_forbidden", 403),
            ("legal_hold_forbidden", 403),
            ("legal_hold_reason_required", 422),
            ("legal_hold_scope_invalid", 422),
            ("run_not_found", 404),
        ]
        assert events == 3

    run(scenario())


def test_v160_hold_release_requires_admin_and_reason_and_all_holds_released(session_factory):
    async def scenario():
        await _seed(session_factory, "run_a", full=False)
        async with session_factory() as session:
            first = await retention.create_hold(session, scope_type="run", scope_id="run_a", reason="case 1", actor="root", actor_role="admin")
            second = await retention.create_hold(session, scope_type="tenant", scope_id="t1", reason="case 2", actor="root", actor_role="admin")
            for kwargs, code in (
                ({"actor_role": "tenant_admin", "release_reason": "done"}, "legal_hold_forbidden"),
                ({"actor_role": "admin", "release_reason": " "}, "legal_hold_release_reason_required"),
            ):
                with pytest.raises(retention.RetentionError) as err:
                    await retention.release_hold(session, first.hold_id, actor="x", **kwargs)
                assert err.value.code == code
            released = await retention.release_hold(session, first.hold_id, actor="root", actor_role="admin", release_reason="case closed")
            assert released.status == "released" and released.release_reason == "case closed" and released.released_by == "root"
            with pytest.raises(retention.RetentionError) as again:
                await retention.release_hold(session, first.hold_id, actor="root", actor_role="admin", release_reason="again")
            assert again.value.code == "legal_hold_not_active"
            assert await retention.exclusion_reasons(session, "run_a", settings=_settings(), now=NOW) == ["legal_hold"]
            await retention.release_hold(session, second.hold_id, actor="root", actor_role="admin", release_reason="audit over")
            assert await retention.exclusion_reasons(session, "run_a", settings=_settings(), now=NOW) == []
            assert [h.hold_id for h in await retention.list_holds(session, tenant_id="t1", status="released")] == [second.hold_id, first.hold_id]
            assert await _count(session, GovernanceEvent, GovernanceEvent.event_type == "retention.legal_hold_released") == 2
            assert retention.hold_to_dict(released)["released_at"] is not None

    run(scenario())


# ---------------------------------------------------------------------------
# D14: FinOps ledger report
# ---------------------------------------------------------------------------


def test_v160_ledger_report_counts_expired_rows_and_never_deletes(session_factory):
    async def scenario():
        ancient = NOW - timedelta(days=3000)
        async with session_factory() as session:
            session.add_all(
                [
                    UsageEvent(usage_id="usage_1", resource_type="model", usage_unit="token", quantity=Decimal("1"), created_at=ancient),
                    UsageEvent(usage_id="usage_2", resource_type="model", usage_unit="token", quantity=Decimal("1"), created_at=RECENT),
                    CostEvent(cost_id="cost_1", usage_event_id="usage_1", resource_type="model", created_at=ancient),
                    BudgetViolation(violation_id="bv_1", violation_type="over_budget", created_at=RECENT),
                ]
            )
            await session.commit()
            report = await retention.ledger_report(session, _settings(), now=NOW)
            assert [await _count(session, model) for model in (UsageEvent, CostEvent, BudgetViolation)] == [2, 1, 1]
        assert report["retention_days"] == 2555 and report["deletes"] is False and report["policy"] == "report_only"
        assert report["tables"]["usage_events"] == {"expired_count": 1, "oldest_expired_at": ancient.isoformat()}
        assert report["tables"]["cost_events"]["expired_count"] == 1
        assert report["tables"]["budget_violations"] == {"expired_count": 0, "oldest_expired_at": None}
        assert not {"usage_events", "cost_events", "budget_violations"} & set(retention.DELETED_TABLES)

    run(scenario())


# ---------------------------------------------------------------------------
# D16: CLI job
# ---------------------------------------------------------------------------


def test_v160_cli_plan_then_apply_latest(session_factory, capsys):
    async def seed():
        await _seed(session_factory, "run_a", full=False)
        async with session_factory() as session:  # a newer plan by someone else is never applied by the job
            other = await retention.build_plan(session, _settings(), actor="api:admin")
        return other

    other = run(seed())
    settings = _settings()
    assert retention_job.main(["--plan"], settings=settings, session_factory=session_factory) == 0
    planned = json.loads(capsys.readouterr().out)
    assert planned["action"] == "plan" and planned["candidate_count"] == 1 and planned["status"] == "planned"
    assert retention_job.main(["--apply-latest"], settings=settings, session_factory=session_factory) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["purge_id"] == planned["purge_id"] and applied["status"] == "applied"
    assert applied["applied_counts"]["runs"] == 1 and applied["receipt_digest"]
    assert retention_job.main(["--apply-latest"], settings=settings, session_factory=session_factory) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "retention_no_applicable_plan"

    async def check():
        async with session_factory() as session:
            stored = (await session.execute(select(RetentionPurge).where(RetentionPurge.purge_id == other.purge_id))).scalar_one()
            return stored.status, await _count(session, AgentRun)

    assert run(check()) == ("planned", 0)


def test_v160_cli_refuses_apply_in_default_plan_mode(session_factory, capsys):
    settings = _settings(audit_retention_purge_mode="plan")
    run(_seed(session_factory, "run_a", full=False))
    assert retention_job.main(["--plan"], settings=settings, session_factory=session_factory) == 0
    capsys.readouterr()
    assert retention_job.main(["--apply-latest"], settings=settings, session_factory=session_factory) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "retention_apply_disabled"

    async def latest_after_ttl():
        async with session_factory() as session:
            return await retention.latest_applicable_plan(session, created_by=retention.JOB_ACTOR, now=datetime.now(timezone.utc) + timedelta(hours=2))

    assert run(latest_after_ttl()) is None
