"""v1.6.2 P3b: which outcomes are re-run, FinOps, retention, metrics, dual control, API and Dashboard (no Docker)."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import Settings, get_settings
from app.models.agent_run import AgentRun
from app.models.evaluation_review import RunOutcome
from app.models.finops import BudgetViolation, CostEvent, UsageEvent
from app.models.governance_event import GovernanceEvent
from app.models.sandbox import SandboxArtifact, SandboxConformanceRun, SandboxJob, VerificationRun, VerificationSuite
from app.observability.metrics import SANDBOX_CONFORMANCE_PASSED, SANDBOX_JOBS, VERIFICATION_RUNS
from app.services import change_requests, retention
from app.services.access import permissions
from app.services.sandbox import docker_config, gate, jobs
from app.services.sandbox.base import ExecutionResult
from app.services.verification import artifacts, demo, engine, metrics, patching, sampling, suites
from app.services.verification.errors import VerificationError

IMAGE = "ghcr.io/acme/python@sha256:" + "a" * 64
IMAGES = json.dumps({"python": IMAGE})
DOCKER_VERSION = "29.9.2"
EVIDENCE = {"changed_files": [{"path": "tax.py", "after": "x = 1\n"}]}
PAGE = "/dashboard/verification"


def _settings(**overrides) -> Settings:
    values = {"sandbox_backend": "docker", "sandbox_execution_enabled": True, "sandbox_images": {"python": IMAGE}, "sandbox_min_isolation_high_risk": "container"}
    return Settings(_env_file=None, **{**values, **overrides})


def _in_session(session_factory, function):
    async def scenario():
        async with session_factory() as session:
            return await function(session)

    return asyncio.run(scenario())


def _open_gate(session_factory, settings=None) -> None:
    settings = settings or _settings()

    async def add(session):
        session.add(
            SandboxConformanceRun(
                run_id="sbc_1", backend_id="docker", backend_version=docker_config.BACKEND_VERSION, runtime="runc",
                config_fingerprint=gate.config_fingerprint(settings, docker_server_version=DOCKER_VERSION), passed=True, cases=[],
                docker_server_version=DOCKER_VERSION, triggered_by="test", created_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    _in_session(session_factory, add)


def _suite(session_factory, *, tenant_id="t-1", name="demo", settings=None) -> VerificationSuite:
    normalized = artifacts.normalize_upload(demo.suite_archive(), max_bytes=1_048_576)

    async def add(session):
        await artifacts.store_artifact(session, normalized)
        return await suites.publish(session, settings or _settings(), tenant_id=tenant_id, name=name, artifact_digest=normalized.digest, actor="user:alice")

    return _in_session(session_factory, add)


def _run(session_factory, *, run_id="run_1", tenant_id="t-1", suite_id="", status="completed", evidence=None, risk=0.2):
    async def add(session):
        metadata = {suites.METADATA_KEY: suite_id} if suite_id else {}
        run = AgentRun(
            run_id=run_id, tenant_id=tenant_id, user_id="u1", task_type="code_fix", status=status, risk_score=risk, decision_reason="", goal="Fix it",
            prompt="Fix it", request_payload={"tenant_id": tenant_id, "user_id": "u1", "task_type": "code_fix", "prompt": "Fix it", "goal": "Fix it", "metadata": metadata},
        )
        session.add(run)
        outcome = None
        if evidence is not None:
            outcome = RunOutcome(outcome_id=f"outcome_{run_id}", run_id=run_id, tenant_id=tenant_id, submission_seq=1, evidence=evidence)
            session.add(outcome)
        await session.commit()
        return run, outcome

    return _in_session(session_factory, add)


def _auto(session_factory, run, outcome, settings=None):
    return _in_session(session_factory, lambda s: sampling.maybe_enqueue_for_outcome(s, settings or _settings(sandbox_verify_mode="all"), run=run, outcome=outcome))


def _count(session_factory, model) -> int:
    return _in_session(session_factory, lambda s: s.scalar(select(func.count()).select_from(model)))


def _ready(session_factory, **run_args):
    suite = _suite(session_factory)
    _open_gate(session_factory)
    run, outcome = _run(session_factory, suite_id=suite.suite_id, evidence=EVIDENCE, **run_args)
    return suite, run, outcome


# ---------------------------------------------------------------------------
# D32: selection
# ---------------------------------------------------------------------------


def test_mode_all_queues_a_job_for_the_runner(session_factory):
    suite, run, outcome = _ready(session_factory, risk=0.7)
    row = _auto(session_factory, run, outcome)
    assert (row.status, row.trigger, row.suite_id, row.artifact_digest) == ("queued", "all", suite.suite_id, suite.artifact_digest)
    assert row.verification_id.startswith("vrf_") and row.requested_by == "system:sampling"
    job = _in_session(session_factory, lambda s: s.scalar(select(SandboxJob)))
    assert (job.kind, job.ref_id, job.run_id, job.tenant_id) == ("verification", row.verification_id, "run_1", "t-1")
    assert job.payload == {"verification_id": row.verification_id, "risk_score": 0.7}


@pytest.mark.parametrize("mode", ["off", "", "everything"])
def test_mode_off_or_a_typo_queues_nothing(session_factory, mode):
    _suite_row, run, outcome = _ready(session_factory)
    assert _auto(session_factory, run, outcome, _settings(sandbox_verify_mode=mode)) is None
    assert _count(session_factory, VerificationRun) == 0


def test_a_run_without_a_suite_is_not_a_candidate(session_factory):
    _open_gate(session_factory)
    run, outcome = _run(session_factory, evidence=EVIDENCE)
    assert _auto(session_factory, run, outcome) is None


@pytest.mark.parametrize(("rate", "selected"), [(1.0, True), (0.0, False)])
def test_sampling_follows_the_rate(session_factory, rate, selected):
    settings = _settings(sandbox_verify_mode="sampled", sandbox_verify_sample_rate=rate, sandbox_verify_sample_secret="s" * 32)
    suite = _suite(session_factory, settings=settings)
    _open_gate(session_factory, settings)
    run, outcome = _run(session_factory, suite_id=suite.suite_id, evidence=EVIDENCE)
    row = _auto(session_factory, run, outcome, settings)
    assert (row is not None) is selected and (row is None or row.trigger == "sampled")


def test_the_draw_depends_on_the_secret_and_repeats():
    first = _settings(sandbox_verify_sample_rate=0.5, sandbox_verify_sample_secret="a" * 32)
    second = _settings(sandbox_verify_sample_rate=0.5, sandbox_verify_sample_secret="b" * 32)
    ids = [f"run_{index}" for index in range(64)]
    draws = [sampling.is_sampled(run_id, first) for run_id in ids]
    assert draws == [sampling.is_sampled(run_id, first) for run_id in ids]
    assert draws != [sampling.is_sampled(run_id, second) for run_id in ids] and 0 < sum(draws) < 64


def _skip_reason(session_factory, run, outcome, settings=None):
    row = _auto(session_factory, run, outcome, settings)
    assert row.status == "skipped" and row.finished_at is not None
    assert _count(session_factory, SandboxJob) == 0
    return row.skip_reason


def test_a_closed_gate_is_recorded_not_silent(session_factory):
    suite = _suite(session_factory)
    run, outcome = _run(session_factory, suite_id=suite.suite_id, evidence=EVIDENCE)
    assert _skip_reason(session_factory, run, outcome) == gate.NOT_CONFORMANT


def test_a_disabled_suite_is_recorded(session_factory):
    suite, run, outcome = _ready(session_factory)

    async def disable(session):
        return await suites.disable(session, await suites.get_suite(session, suite.suite_id), actor="user:alice")

    _in_session(session_factory, disable)
    assert _skip_reason(session_factory, run, outcome) == sampling.SUITE_UNAVAILABLE


def test_a_suite_of_another_tenant_is_recorded(session_factory):
    other = _suite(session_factory, tenant_id="t-2")
    _open_gate(session_factory)
    run, outcome = _run(session_factory, suite_id=other.suite_id, evidence=EVIDENCE)
    assert _skip_reason(session_factory, run, outcome) == sampling.SUITE_UNAVAILABLE


@pytest.mark.parametrize(
    ("evidence", "reason"),
    [({}, patching.NO_APPLICABLE_CHANGES), ({"changed_files": [{"path": "a.py"}]}, patching.NO_APPLICABLE_CHANGES), ({"diff": "x", "evidence_limits": ["evidence_truncated"]}, patching.EVIDENCE_TRUNCATED)],
)
def test_evidence_that_cannot_be_rerun_is_recorded(session_factory, evidence, reason):
    suite = _suite(session_factory)
    _open_gate(session_factory)
    run, outcome = _run(session_factory, suite_id=suite.suite_id, evidence=evidence)
    assert _skip_reason(session_factory, run, outcome) == reason


def test_the_daily_cap_is_per_tenant_and_skipped_records_do_not_count(session_factory):
    settings = _settings(sandbox_verify_mode="all", sandbox_verify_daily_cap_per_tenant=1)
    suite = _suite(session_factory, settings=settings)
    other = _suite(session_factory, tenant_id="t-2", settings=settings)
    _open_gate(session_factory, settings)
    empty_run, empty = _run(session_factory, run_id="run_0", suite_id=suite.suite_id, evidence={})
    assert _auto(session_factory, empty_run, empty, settings).status == "skipped"
    first_run, first = _run(session_factory, run_id="run_1", suite_id=suite.suite_id, evidence=EVIDENCE)
    second_run, second = _run(session_factory, run_id="run_2", suite_id=suite.suite_id, evidence=EVIDENCE)
    third_run, third = _run(session_factory, run_id="run_3", tenant_id="t-2", suite_id=other.suite_id, evidence=EVIDENCE)
    assert _auto(session_factory, first_run, first, settings).status == "queued"
    capped = _auto(session_factory, second_run, second, settings)
    assert (capped.status, capped.skip_reason) == ("skipped", sampling.DAILY_CAP)
    assert _auto(session_factory, third_run, third, settings).status == "queued"


def test_yesterdays_runs_do_not_count_against_today(session_factory):
    settings = _settings(sandbox_verify_mode="all", sandbox_verify_daily_cap_per_tenant=1)
    suite = _suite(session_factory, settings=settings)
    _open_gate(session_factory, settings)
    run, outcome = _run(session_factory, suite_id=suite.suite_id, evidence=EVIDENCE)

    async def add(session):
        session.add(
            VerificationRun(
                verification_id="vrf_old", run_id="run_old", tenant_id="t-1", suite_id=suite.suite_id, status="completed",
                created_at=datetime.now(timezone.utc) - timedelta(days=1, hours=1),
            )
        )
        await session.commit()

    _in_session(session_factory, add)
    assert _auto(session_factory, run, outcome, settings).status == "queued"


def test_queueing_changes_neither_the_run_nor_the_verdict(session_factory):
    _suite_row, run, outcome = _ready(session_factory, status="review_required")
    _auto(session_factory, run, outcome)
    stored = _in_session(session_factory, lambda s: s.scalar(select(AgentRun).where(AgentRun.run_id == "run_1")))
    assert stored.status == "review_required"


# ---------------------------------------------------------------------------
# Manual requests
# ---------------------------------------------------------------------------


def _manual(session_factory, run, settings=None):
    return _in_session(session_factory, lambda s: sampling.request_manual(s, settings or _settings(), run=run, requested_by="user:rita"))


def test_a_reviewer_can_request_a_rerun_whatever_the_mode(session_factory):
    _suite_row, run, _outcome = _ready(session_factory)
    row, created = _manual(session_factory, run)
    assert (row.status, row.trigger, row.requested_by, created) == ("queued", "manual", "user:rita", True)


def test_an_active_rerun_is_returned_instead_of_a_second_one(session_factory):
    _suite_row, run, _outcome = _ready(session_factory)
    first, _created = _manual(session_factory, run)
    second, created = _manual(session_factory, run)
    assert second.verification_id == first.verification_id and created is False and _count(session_factory, SandboxJob) == 1


def test_a_manual_request_counts_against_the_cap(session_factory):
    settings = _settings(sandbox_verify_daily_cap_per_tenant=0)
    suite = _suite(session_factory, settings=settings)
    _open_gate(session_factory, settings)
    run, _outcome = _run(session_factory, suite_id=suite.suite_id, evidence=EVIDENCE)
    row, _created = _manual(session_factory, run, settings)
    assert (row.status, row.skip_reason) == ("skipped", sampling.DAILY_CAP)


@pytest.mark.parametrize(("suite_bound", "code"), [(False, "verification_suite_missing"), (True, "verification_outcome_missing")])
def test_a_manual_request_needs_a_suite_and_an_outcome(session_factory, suite_bound, code):
    suite_id = _suite(session_factory).suite_id if suite_bound else ""
    run, _outcome = _run(session_factory, suite_id=suite_id)
    with pytest.raises(VerificationError) as caught:
        _manual(session_factory, run)
    assert (caught.value.code, caught.value.status_code) == (code, 409)


def test_the_latest_outcome_is_the_one_rerun(session_factory):
    _suite_row, run, _outcome = _ready(session_factory)

    async def add(session):
        session.add(RunOutcome(outcome_id="outcome_new", run_id="run_1", tenant_id="t-1", submission_seq=2, evidence=EVIDENCE))
        await session.commit()

    _in_session(session_factory, add)
    assert _manual(session_factory, run)[0].outcome_id == "outcome_new"


# ---------------------------------------------------------------------------
# Outcome submission
# ---------------------------------------------------------------------------


@pytest.fixture
def live_settings(monkeypatch):
    for name, value in {
        "SANDBOX_BACKEND": "docker", "SANDBOX_EXECUTION_ENABLED": "true", "SANDBOX_IMAGES": IMAGES, "SANDBOX_VERIFY_MODE": "all",
        "SANDBOX_MIN_ISOLATION_HIGH_RISK": "container",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def test_submitting_an_outcome_queues_the_rerun_and_keeps_the_response(client, session_factory, live_settings):
    suite = _suite(session_factory, settings=live_settings)
    _open_gate(session_factory, live_settings)
    _run(session_factory, suite_id=suite.suite_id)
    response = client.post("/v1/runs/run_1/outcome", json=demo.scenario_evidence("honest"))
    assert response.status_code == 200, response.text
    row = _in_session(session_factory, lambda s: s.scalar(select(VerificationRun)))
    assert (row.status, row.outcome_id) == ("queued", response.json()["outcome_id"])
    assert client.event_publisher.events == []


def test_a_queueing_failure_does_not_fail_the_submission(client, session_factory, live_settings, monkeypatch):
    suite = _suite(session_factory, settings=live_settings)
    _run(session_factory, suite_id=suite.suite_id)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("queue down")

    monkeypatch.setattr(sampling, "maybe_enqueue_for_outcome", broken)
    response = client.post("/v1/runs/run_1/outcome", json=demo.scenario_evidence("honest"))
    assert response.status_code == 200 and response.json()["outcome_id"]


def test_cut_evidence_is_marked_by_the_platform(client, session_factory, monkeypatch):
    monkeypatch.setenv("EVALUATION_MAX_DIFF_BYTES", "32")
    get_settings.cache_clear()
    _run(session_factory)
    response = client.post("/v1/runs/run_1/outcome", json={"final_message": "done", "diff": "--- a/a.py\n+++ b/a.py\n" + "+x = 1\n" * 40})
    assert response.status_code == 200, response.text
    outcome = _in_session(session_factory, lambda s: s.scalar(select(RunOutcome)))
    assert outcome.evidence["evidence_limits"] == ["evidence_truncated"]


def test_an_agent_cannot_mark_or_unmark_its_own_evidence(client, session_factory):
    _run(session_factory)
    response = client.post("/v1/runs/run_1/outcome", json={"final_message": "done", "evidence_limits": ["none"], "verification_suite_id": "vsu_weak"})
    assert response.status_code == 200
    assert "evidence_limits" not in _in_session(session_factory, lambda s: s.scalar(select(RunOutcome))).evidence


# ---------------------------------------------------------------------------
# D32: FinOps
# ---------------------------------------------------------------------------


class _Backend:
    def __call__(self):
        return self

    def run_with_outputs(self, spec, *, artifacts=None):
        report = b'<testsuite><testcase classname="t" name="a"/></testsuite>'
        result = ExecutionResult(
            status="ok", backend_id="docker", backend_version="v1.6.2", isolation_level="container", spec_digest="d", exit_code=0,
            usage={"wall_ms": 2500, "cpu_ms": None, "max_rss_mb": None},
        )
        return result, {"out/junit.xml": report}


def _complete(session_factory, settings, evidence=None, backend=None):
    suite = _suite(session_factory, settings=settings)
    _open_gate(session_factory, settings)
    run, outcome = _run(session_factory, suite_id=suite.suite_id, evidence=demo.scenario_evidence("honest") if evidence is None else evidence)
    row = _in_session(session_factory, lambda s: sampling.enqueue(s, settings, run=run, outcome=outcome, suite_id=suite.suite_id, trigger="all", requested_by="test"))
    job = _in_session(session_factory, lambda s: s.scalar(select(SandboxJob)))
    asyncio.run(engine.handle_job(job, settings=settings, session_factory=session_factory, backend_factory=backend or _Backend(), docker_server_version=DOCKER_VERSION))
    return row


def test_a_finished_rerun_records_its_compute_without_charging_by_default(session_factory):
    row = _complete(session_factory, _settings())
    usage = _in_session(session_factory, lambda s: s.scalar(select(UsageEvent)))
    cost = _in_session(session_factory, lambda s: s.scalar(select(CostEvent)))
    assert (usage.step_id, usage.run_id, usage.tenant_id, usage.usage_unit, usage.resource_type) == ("sandbox_verification", "run_1", "t-1", "cpu_seconds", "container")
    assert usage.quantity == Decimal("5") and usage.container_seconds == Decimal("5") and usage.idempotency_key == row.verification_id
    assert cost.usage_event_id == usage.usage_id and cost.net_cost == 0 and cost.calculation_method == "sandbox_compute"
    assert _count(session_factory, BudgetViolation) == 0


def test_the_configured_price_is_charged_per_cpu_second(session_factory):
    _complete(session_factory, _settings(sandbox_compute_cost_per_cpu_second=Decimal("0.01")))
    cost = _in_session(session_factory, lambda s: s.scalar(select(CostEvent)))
    assert (cost.unit_price, cost.net_cost, cost.actual_cost) == (Decimal("0.01"), Decimal("0.05"), Decimal("0.05"))


def test_recording_the_same_rerun_twice_keeps_one_event(session_factory):
    from app.services.finops import ledger

    async def twice(session):
        for _attempt in range(2):
            await ledger.record_sandbox_compute(
                session, tenant_id="t-1", run_id="run_1", reference_id="vrf_1", wall_seconds=Decimal("1"), cpu_seconds=Decimal("1"), unit_cost=Decimal("0"), status="success"
            )
        await session.commit()

    _in_session(session_factory, twice)
    assert _count(session_factory, UsageEvent) == 1 and _count(session_factory, CostEvent) == 1


class _KilledAfterTheChange(_Backend):
    """gVisor can end a run with exit 137 and no OOM report: the sandbox says ``nonzero_exit`` and leaves no test report."""

    def __init__(self) -> None:
        self.calls = 0

    def run_with_outputs(self, spec, *, artifacts=None):
        self.calls += 1
        if self.calls == 1:
            return super().run_with_outputs(spec, artifacts=artifacts)
        result = ExecutionResult(
            status="nonzero_exit", backend_id="docker", backend_version="v1.6.2", isolation_level="gvisor", spec_digest="d", exit_code=137,
            usage={"wall_ms": 2500, "cpu_ms": None, "max_rss_mb": None},
        )
        return result, {}


def test_a_run_killed_without_an_oom_report_fails_and_still_records_its_compute(session_factory):
    row = _complete(session_factory, _settings(), backend=_KilledAfterTheChange())
    stored = _in_session(session_factory, lambda s: sampling.get_verification(s, row.verification_id))
    usage = _in_session(session_factory, lambda s: s.scalar(select(UsageEvent)))
    assert (stored.status, stored.skip_reason, stored.after["status"], stored.after["exit_code"]) == ("failed", "after_no_report", "nonzero_exit", 137)
    assert stored.classification == {} and (usage.status, usage.quantity) == ("failed", Decimal("5"))


def test_a_skipped_rerun_records_no_compute(session_factory):
    _complete(session_factory, _settings(), evidence={"diff": "--- a/missing.py\n+++ b/missing.py\n@@ -1 +1 @@\n-x\n+y\n"})
    assert _count(session_factory, UsageEvent) == 0


# ---------------------------------------------------------------------------
# D34: retention
# ---------------------------------------------------------------------------


def _verification(session_factory, status) -> None:
    async def add(session):
        session.add(VerificationRun(verification_id="vrf_1", run_id="run_1", tenant_id="t-1", suite_id="vsu_1", status=status))
        await session.commit()

    _in_session(session_factory, add)


@pytest.mark.parametrize(("status", "excluded"), [("queued", True), ("running", True), ("completed", False), ("failed", False), ("skipped", False)])
def test_a_run_with_an_active_rerun_is_not_purged(session_factory, status, excluded):
    _run(session_factory)
    _verification(session_factory, status)
    reasons = _in_session(session_factory, lambda s: retention.exclusion_reasons(s, "run_1", settings=Settings(_env_file=None)))
    assert ("verification_in_progress" in reasons) is excluded


def test_a_rerun_abandoned_before_the_cutoff_does_not_block_the_purge(session_factory):
    _run(session_factory)
    settings = Settings(_env_file=None)

    async def add(session):
        created = datetime.now(timezone.utc) - timedelta(days=int(settings.audit_retention_days) + 1)
        session.add(VerificationRun(verification_id="vrf_old", run_id="run_1", tenant_id="t-1", suite_id="vsu_1", status="queued", created_at=created))
        await session.commit()

    _in_session(session_factory, add)
    reasons = _in_session(session_factory, lambda s: retention.exclusion_reasons(s, "run_1", settings=settings))
    assert "verification_in_progress" not in reasons


def test_reruns_and_their_jobs_follow_the_run_and_suites_stay(session_factory):
    suite = _suite(session_factory)
    _run(session_factory)
    _verification(session_factory, "completed")

    async def scenario(session):
        await jobs.enqueue(session, jobs.KIND_VERIFICATION, tenant_id="t-1", run_id="run_1", ref_id="vrf_1")
        await jobs.enqueue(session, jobs.KIND_CONFORMANCE)
        await session.commit()
        counts, _entries = await retention._delete_run_records(session, "run_1")
        await session.commit()
        return counts

    counts = _in_session(session_factory, scenario)
    assert (counts["verification_runs"], counts["sandbox_jobs"]) == (1, 1)
    assert _count(session_factory, VerificationRun) == 0 and _count(session_factory, SandboxJob) == 1
    assert _count(session_factory, VerificationSuite) == 1 and _count(session_factory, SandboxArtifact) == 1 and suite.suite_id


def test_suites_and_artifacts_are_never_purge_targets():
    assert not {"verification_suites", "sandbox_artifacts", "sandbox_conformance_runs"} & set(retention.DELETED_TABLES)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_are_read_from_the_database(session_factory, client):
    _verification(session_factory, "completed")
    _open_gate(session_factory)
    _in_session(session_factory, metrics.refresh)
    assert VERIFICATION_RUNS.labels(status="completed")._value.get() == 1
    assert SANDBOX_CONFORMANCE_PASSED._value.get() == 1
    assert SANDBOX_JOBS is not None
    text = client.get("/v1/metrics").text
    assert 'verification_runs{status="completed"} 1.0' in text and "sandbox_conformance_passed 1.0" in text


def test_a_database_problem_does_not_break_the_metrics(session_factory):
    class Broken:
        async def execute(self, *_args):
            raise RuntimeError("database down")

        async def rollback(self):
            return None

    asyncio.run(metrics.refresh(Broken()))


# ---------------------------------------------------------------------------
# D40: permissions
# ---------------------------------------------------------------------------


def test_the_permission_table_matches_the_plan():
    assert permissions.PERMISSIONS["verification.view"] == {"admin", "tenant_admin", "reviewer", "auditor"}
    assert permissions.PERMISSIONS["verification.request"] == {"admin", "tenant_admin", "reviewer"}
    assert permissions.PERMISSIONS["verification_suites.manage"] == {"admin", "tenant_admin"}
    assert "verification_suites.manage" in permissions.STEP_UP_PERMISSIONS and "verification.view" in permissions.READ_PERMISSIONS
    assert "verification" in permissions.TENANT_VIEW_ALLOWLIST


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def _files(archive=None):
    return {"file": ("suite.tar.gz", archive if archive is not None else demo.suite_archive(), "application/gzip")}


def _symlink_archive() -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as output:
        info = tarfile.TarInfo("link.py")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        output.addfile(info)
    return gzip.compress(raw.getvalue())


def _keys(monkeypatch, images=True) -> dict[str, dict[str, str]]:
    roles = {"admin": "admin", "tenant": "tenant_admin", "reviewer": "reviewer", "auditor": "auditor", "operator": "operator"}
    keys = ",".join(f"key-{name}:t-1:{role}" for name, role in roles.items()) + ",key-other:t-2:tenant_admin"
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_KEYS", keys)
    if images:
        monkeypatch.setenv("SANDBOX_IMAGES", IMAGES)
    get_settings.cache_clear()
    return {name: {"X-API-Key": f"key-{name}"} for name in [*roles, "other"]}


def test_uploading_listing_and_disabling_a_suite(client, monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGES", IMAGES)
    get_settings.cache_clear()
    created = client.post("/v1/verification-suites", data={"name": "demo", "tenant_id": "t-1", "base_ref": "v1"}, files=_files())
    assert created.status_code == 201, created.text
    suite = created.json()
    assert (suite["version"], suite["tenant_id"], suite["base_ref"], suite["status"]) == (1, "t-1", "v1", "active")
    assert client.post("/v1/verification-suites", data={"name": "demo", "tenant_id": "t-1"}, files=_files()).json()["version"] == 2
    assert client.get("/v1/verification-suites", params={"tenant_id": "t-1"}).json()["count"] == 2
    assert client.get(f"/v1/verification-suites/{suite['suite_id']}").json()["suite_id"] == suite["suite_id"]
    assert client.post(f"/v1/verification-suites/{suite['suite_id']}/default").json()["default_verification_suite_id"] == suite["suite_id"]
    assert client.get("/v1/verification-suites", params={"tenant_id": "t-1"}).json()["default_suite_id"] == suite["suite_id"]
    assert client.post(f"/v1/verification-suites/{suite['suite_id']}/disable").json()["status"] == "disabled"
    assert client.post(f"/v1/verification-suites/{suite['suite_id']}/disable").status_code == 409
    assert client.get("/v1/verification-suites", params={"tenant_id": "t-1"}).json()["default_suite_id"] == ""


@pytest.mark.parametrize(
    ("data", "archive", "status", "code"),
    [
        ({"name": "demo"}, _symlink_archive(), 422, "artifact_symlink_rejected"),
        ({"name": "demo"}, b"not a gzip", 422, "artifact_invalid_archive"),
        ({"name": "bad name"}, None, 422, "suite_name_invalid"),
        ({"name": "demo", "image_name": "nope"}, None, 422, "suite_image_unknown"),
        ({"name": "demo", "test_command": "not json"}, None, 422, "suite_test_command_invalid"),
        ({"name": "demo", "test_paths": '["nothing/**"]'}, None, 422, "suite_no_tests"),
    ],
)
def test_bad_uploads_answer_with_their_code(client, monkeypatch, data, archive, status, code):
    monkeypatch.setenv("SANDBOX_IMAGES", IMAGES)
    get_settings.cache_clear()
    response = client.post("/v1/verification-suites", data=data, files=_files(archive))
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)


def test_an_oversized_upload_is_refused_before_it_is_read(client, monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGES", IMAGES)
    monkeypatch.setenv("SANDBOX_ARTIFACT_MAX_BYTES", "16")
    get_settings.cache_clear()
    response = client.post("/v1/verification-suites", data={"name": "demo"}, files=_files(b"x" * 200_000))
    assert (response.status_code, response.json()["error"]["code"]) == (413, "artifact_too_large")


def test_api_roles_and_tenant_scope(client, session_factory, monkeypatch):
    keys = _keys(monkeypatch)
    created = client.post("/v1/verification-suites", data={"name": "demo"}, files=_files(), headers=keys["tenant"])
    assert created.status_code == 201 and created.json()["tenant_id"] == "t-1"
    suite_id = created.json()["suite_id"]
    for role in ("reviewer", "auditor", "operator"):
        assert client.post("/v1/verification-suites", data={"name": "demo"}, files=_files(), headers=keys[role]).status_code == 403
        assert client.post(f"/v1/verification-suites/{suite_id}/disable", headers=keys[role]).status_code == 403
    for role in ("admin", "tenant", "reviewer", "auditor"):
        assert client.get("/v1/verification-suites", headers=keys[role]).status_code == 200
    assert client.get("/v1/verification-suites", headers=keys["operator"]).status_code == 403
    assert client.get(f"/v1/verification-suites/{suite_id}", headers=keys["other"]).status_code == 403
    assert client.post(f"/v1/verification-suites/{suite_id}/disable", headers=keys["other"]).status_code == 403
    assert client.get("/v1/verification-suites", params={"tenant_id": "t-1"}, headers=keys["other"]).status_code == 403
    assert client.get("/v1/verification-suites", headers=keys["other"]).json()["count"] == 0


def test_run_verifications_api(client, session_factory, monkeypatch):
    keys = _keys(monkeypatch)
    suite = _suite(session_factory, settings=get_settings())
    _run(session_factory, suite_id=suite.suite_id, evidence=EVIDENCE)
    assert client.post("/v1/runs/run_1/verifications", headers=keys["auditor"]).status_code == 403
    assert client.post("/v1/runs/run_1/verifications", headers=keys["other"]).status_code == 403
    requested = client.post("/v1/runs/run_1/verifications", headers=keys["reviewer"])
    assert requested.status_code == 202 and requested.json()["signal_usage"] == "monitor_only"
    verification = requested.json()["verification"]
    assert (verification["status"], verification["skip_reason"], verification["signal_usage"]) == ("skipped", gate.NOT_CONFORMANT, "monitor_only")
    listed = client.get("/v1/runs/run_1/verifications", headers=keys["auditor"]).json()
    assert listed["count"] == 1 and listed["verifications"][0]["verification_id"] == verification["verification_id"]
    assert client.get(f"/v1/verifications/{verification['verification_id']}", headers=keys["auditor"]).json()["run_id"] == "run_1"
    assert client.get(f"/v1/verifications/{verification['verification_id']}", headers=keys["other"]).status_code == 403
    assert client.get("/v1/verifications/vrf_missing", headers=keys["admin"]).status_code == 404
    assert client.get("/v1/runs/run_missing/verifications", headers=keys["admin"]).status_code == 404


def test_a_run_can_be_bound_through_the_api(client, session_factory, monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGES", IMAGES)
    get_settings.cache_clear()
    suite = client.post("/v1/verification-suites", data={"name": "demo", "tenant_id": "t-1"}, files=_files()).json()
    payload = {"tenant_id": "t-1", "user_id": "u1", "task_type": "code_fix", "prompt": "Fix the parser.", "goal": "Fix it", "metadata": {suites.METADATA_KEY: suite["suite_id"]}}
    response = client.post("/v1/agent/submit", json=payload)
    assert response.status_code == 200, response.text
    run = _in_session(session_factory, lambda s: s.scalar(select(AgentRun)))
    assert run.request_payload["metadata"][suites.METADATA_KEY] == suite["suite_id"]


# ---------------------------------------------------------------------------
# D22: dual control
# ---------------------------------------------------------------------------


def test_publishing_can_need_a_second_person(client, session_factory, signed_in, monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGES", IMAGES)
    monkeypatch.setenv("DUAL_CONTROL_ACTIONS", change_requests.VERIFICATION_SUITE_PUBLISH)
    get_settings.cache_clear()
    pending = client.post("/v1/verification-suites", data={"name": "demo", "tenant_id": "t-1"}, files=_files())
    assert pending.status_code == 202 and pending.json()["status"] == "pending_second_approval"
    assert _count(session_factory, VerificationSuite) == 0
    change_id = pending.json()["change_request"]["change_id"]
    signed_in(role="admin")
    decided = client.post(f"/dashboard/change-requests/{change_id}/approve", data={"reason": "checked"}, follow_redirects=False)
    assert decided.status_code == 303, decided.text
    suite = _in_session(session_factory, lambda s: s.scalar(select(VerificationSuite)))
    assert (suite.name, suite.version, suite.tenant_id) == ("demo", 1, "t-1") and suite.created_by.startswith("user:")


def test_the_longest_tenant_and_name_fit_the_change_request_target(session_factory):
    params = {"tenant_id": "t" * 128, "name": "n" * 128}
    first = asyncio.run(change_requests._prepare_suite_publish(None, None, params, None)).target_ref
    other = asyncio.run(change_requests._prepare_suite_publish(None, None, {**params, "tenant_id": "u" * 128}, None)).target_ref
    assert len(first) <= 255 and first != other


def test_the_publish_action_is_listed_for_dual_control():
    assert change_requests.VERIFICATION_SUITE_PUBLISH in change_requests.ACTIONS
    assert change_requests.ACTION_PERMISSIONS[change_requests.VERIFICATION_SUITE_PUBLISH] == "verification_suites.manage"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@pytest.fixture
def images(monkeypatch):
    monkeypatch.setenv("SANDBOX_IMAGES", IMAGES)
    get_settings.cache_clear()


def _upload(client, *, archive=None, **data):
    return client.post(f"{PAGE}/suites", data={"name": "demo", **data}, files=_files(archive), follow_redirects=False)


def test_an_admin_publishes_and_sees_the_suite(client, session_factory, signed_in, images):
    signed_in(role="admin")
    answer = _upload(client, tenant_id="t-1")
    assert answer.status_code == 303 and "suite_published" in answer.headers["location"]
    page = client.get(PAGE).text
    assert 'data-testid="verification-suite-row"' in page and "Platform Verification" in page and "平台重跑驗證" in page
    assert _in_session(session_factory, lambda s: s.scalar(select(VerificationSuite))).created_by.startswith("user:admin")


def test_a_symlink_archive_shows_its_error_code(client, signed_in, images):
    signed_in(role="admin")
    answer = _upload(client, archive=_symlink_archive())
    assert answer.status_code == 303 and "error=artifact_symlink_rejected" in answer.headers["location"]
    assert "artifact_symlink_rejected" in client.get(answer.headers["location"]).text


def test_disabling_and_choosing_the_default_on_the_page(client, session_factory, signed_in, images):
    signed_in(role="admin")
    suite = _suite(session_factory, settings=get_settings())
    assert "suite_default_set" in client.post(f"{PAGE}/suites/{suite.suite_id}/default", data={"enabled": "1"}, follow_redirects=False).headers["location"]
    assert 'data-testid="verification-suite-default"' in client.get(PAGE).text
    assert "suite_disabled" in client.post(f"{PAGE}/suites/{suite.suite_id}/disable", follow_redirects=False).headers["location"]
    assert "error=suite_already_disabled" in client.post(f"{PAGE}/suites/{suite.suite_id}/disable", follow_redirects=False).headers["location"]


def test_the_auditor_reads_but_changes_nothing(client, session_factory, signed_in, images):
    suite = _suite(session_factory, settings=get_settings())
    signed_in(role="auditor")
    page = client.get(PAGE)
    assert page.status_code == 200 and suite.suite_id in page.text
    assert "verification-upload-form" not in page.text and "verification-request-form" not in page.text and "verification-suite-disable" not in page.text
    assert _upload(client).status_code == 403
    assert client.post(f"{PAGE}/suites/{suite.suite_id}/disable").status_code == 403
    assert client.post(f"{PAGE}/request", data={"run_id": "run_1"}).status_code == 403


def test_an_operator_has_no_access(client, signed_in):
    signed_in(role="operator")
    assert client.get(PAGE).status_code == 403


def test_a_reviewer_requests_a_rerun_but_cannot_publish(client, session_factory, signed_in, images):
    suite = _suite(session_factory, settings=get_settings())
    _run(session_factory, suite_id=suite.suite_id, evidence=EVIDENCE)
    signed_in(role="reviewer")
    page = client.get(PAGE).text
    assert "verification-request-form" in page and "verification-upload-form" not in page
    answer = client.post(f"{PAGE}/request", data={"run_id": "run_1"}, follow_redirects=False)
    assert answer.status_code == 303 and gate.NOT_CONFORMANT in answer.headers["location"]
    assert "error=run_not_found" in client.post(f"{PAGE}/request", data={"run_id": "nope"}, follow_redirects=False).headers["location"]
    assert _upload(client).status_code == 403


def test_a_tenant_admin_only_reaches_its_own_tenant(client, session_factory, signed_in, images):
    own, other = _suite(session_factory, settings=get_settings()), _suite(session_factory, tenant_id="t-2", settings=get_settings())
    _run(session_factory, run_id="run_other", tenant_id="t-2", suite_id=other.suite_id, evidence=EVIDENCE)
    _verification_other = VerificationRun(verification_id="vrf_other", run_id="run_other", tenant_id="t-2", suite_id=other.suite_id, status="completed")

    async def add(session):
        session.add(_verification_other)
        await session.commit()

    _in_session(session_factory, add)
    signed_in(role="tenant_admin", tenant_id="t-1")
    page = client.get(PAGE).text
    assert own.suite_id in page and other.suite_id not in page
    assert client.get(PAGE, params={"tenant_id": "t-2"}).status_code == 403
    assert client.post(f"{PAGE}/suites/{other.suite_id}/disable").status_code == 403
    assert client.post(f"{PAGE}/request", data={"run_id": "run_other"}).status_code == 403
    assert client.get(f"{PAGE}/runs/vrf_other").status_code == 403
    assert _upload(client, tenant_id="t-2").status_code == 303
    newest = _in_session(session_factory, lambda s: s.scalar(select(VerificationSuite).order_by(VerificationSuite.id.desc())))
    assert newest.tenant_id == "t-1"


def test_unknown_ids_look_like_another_tenants_to_tenant_users(client, signed_in, images):
    signed_in(role="tenant_admin", tenant_id="t-1")
    assert client.get(f"{PAGE}/runs/vrf_missing").status_code == 403
    assert client.post(f"{PAGE}/suites/vsu_missing/disable").status_code == 403
    assert client.post(f"{PAGE}/request", data={"run_id": "run_missing"}).status_code == 403


def test_an_oversized_dashboard_upload_is_refused(client, signed_in, monkeypatch, images):
    monkeypatch.setenv("SANDBOX_ARTIFACT_MAX_BYTES", "16")
    get_settings.cache_clear()
    signed_in(role="admin")
    answer = _upload(client, archive=b"x" * 4096)
    assert answer.status_code == 303 and "error=artifact_too_large" in answer.headers["location"]


def test_the_detail_page_shows_the_result(client, session_factory, signed_in, images):
    settings = _settings()
    row = _complete(session_factory, settings)
    signed_in(role="auditor")
    page = client.get(f"{PAGE}/runs/{row.verification_id}")
    assert page.status_code == 200 and 'data-testid="verification-detail-status">completed' in page.text
    assert "verification-detail-classes" in page.text and "Before and after" in page.text
    assert client.get(f"{PAGE}/runs/vrf_missing").status_code == 404


def test_a_stale_session_must_reauthenticate_before_publishing(client, session_factory, signed_in, images):
    from sqlalchemy import update

    from app.models.access import DashboardSession

    who = signed_in(role="admin")

    async def age(session):
        await session.execute(
            update(DashboardSession).where(DashboardSession.session_id == who.session_id).values(reauth_at=datetime.now(timezone.utc) - timedelta(days=1))
        )
        await session.commit()

    _in_session(session_factory, age)
    answer = _upload(client)
    assert answer.status_code == 303 and "/dashboard/reauth" in answer.headers["location"]
    assert _count(session_factory, VerificationSuite) == 0


def test_every_change_writes_an_audit_event(client, session_factory, signed_in, images):
    signed_in(role="admin")
    _upload(client, tenant_id="t-1")
    suite = _in_session(session_factory, lambda s: s.scalar(select(VerificationSuite)))
    client.post(f"{PAGE}/suites/{suite.suite_id}/disable")
    events = _in_session(session_factory, lambda s: s.scalars(select(GovernanceEvent.event_type)))
    assert {"verification_suite.published", "verification_suite.disabled"} <= set(events.all())
