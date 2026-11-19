"""v1.6.2 P2b: work queue, sandbox-runner, API, Dashboard, operations checks and delivery files (no Docker needed)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.models.governance_event import GovernanceEvent
from app.models.sandbox import SandboxConformanceRun, SandboxJob
from app.services.access import permissions
from app.services.run_state import InMemoryRunStateStore
from app.services.sandbox import conformance, docker_config, gate, jobs
from app.workers import sandbox_runner
from app.workers.sandbox_runner import SandboxRunner

ROOT = Path(__file__).resolve().parents[1]
VERSION = "29.9.2"
START = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, sandbox_backend="docker", **overrides)


def _row(settings, **overrides) -> SandboxConformanceRun:
    values = {
        "run_id": "sbc_1",
        "backend_id": "docker",
        "backend_version": docker_config.BACKEND_VERSION,
        "runtime": "runc",
        "config_fingerprint": gate.config_fingerprint(settings, docker_server_version=VERSION),
        "passed": True,
        "cases": [],
        "docker_server_version": VERSION,
        "triggered_by": "test",
        "created_at": datetime.now(timezone.utc),
    }
    return SandboxConformanceRun(**{**values, **overrides})


def _in_session(session_factory, function):
    async def scenario():
        async with session_factory() as session:
            return await function(session)

    return asyncio.run(scenario())


def _job(session_factory, job_id):
    async def load(session):
        return (await session.execute(select(SandboxJob).where(SandboxJob.job_id == job_id))).scalar_one()

    return _in_session(session_factory, load)


def _enqueue(session_factory, kind=jobs.KIND_CONFORMANCE, **kwargs):
    async def add(session):
        job = await jobs.enqueue(session, kind, **kwargs)
        await session.commit()
        return job.job_id

    return _in_session(session_factory, add)


def _claim(session_factory, *, runner="r1", kinds=(jobs.KIND_CONFORMANCE,), now=None, lease_seconds=jobs.LEASE_SECONDS):
    return _in_session(session_factory, lambda s: jobs.claim_next(s, runner_id=runner, kinds=kinds, now=now, lease_seconds=lease_seconds))


# ---------------------------------------------------------------------------
# D21: work queue
# ---------------------------------------------------------------------------


def test_job_ids_use_the_documented_prefix(session_factory):
    assert _enqueue(session_factory).startswith("sbj_")


def test_claim_takes_the_oldest_job_and_starts_a_lease(session_factory):
    first, _second = _enqueue(session_factory), _enqueue(session_factory)
    job = _claim(session_factory, now=START)
    assert job.job_id == first and (job.status, job.claimed_by, job.attempts) == ("running", "r1", 1)
    assert job.lease_expires_at == START + timedelta(seconds=jobs.LEASE_SECONDS)


def test_claim_filters_by_kind(session_factory):
    _enqueue(session_factory, jobs.KIND_VERIFICATION)
    assert _claim(session_factory) is None
    assert _claim(session_factory, kinds=()) is None
    assert _claim(session_factory, kinds=(jobs.KIND_VERIFICATION,)) is not None


def test_a_claimed_job_is_not_claimed_again(session_factory):
    _enqueue(session_factory)
    assert _claim(session_factory) is not None and _claim(session_factory, runner="r2") is None


def test_claiming_skips_rows_locked_by_another_runner():
    text = (ROOT / "app/services/sandbox/jobs.py").read_text(encoding="utf-8")
    assert text.count("with_for_update(skip_locked=True)") == 2


def test_expired_lease_is_requeued_once_then_expires(session_factory):
    job_id = _enqueue(session_factory)
    _claim(session_factory, now=START, lease_seconds=10)
    later = START + timedelta(seconds=11)
    assert _in_session(session_factory, lambda s: jobs.reclaim_expired(s, now=later)) == {"requeued": 1, "expired": 0}
    row = _job(session_factory, job_id)
    assert (row.status, row.claimed_by, row.lease_expires_at) == ("queued", "", None)
    assert _claim(session_factory, runner="r2", now=later, lease_seconds=10).attempts == 2
    final = later + timedelta(seconds=11)
    assert _in_session(session_factory, lambda s: jobs.reclaim_expired(s, now=final)) == {"requeued": 0, "expired": 1}
    row = _job(session_factory, job_id)
    assert (row.status, row.error_code) == ("expired", jobs.LEASE_EXPIRED) and row.finished_at is not None


def test_a_live_lease_is_not_reclaimed(session_factory):
    job_id = _enqueue(session_factory)
    _claim(session_factory, now=START, lease_seconds=60)
    counts = _in_session(session_factory, lambda s: jobs.reclaim_expired(s, now=START + timedelta(seconds=30)))
    assert counts == {"requeued": 0, "expired": 0} and _job(session_factory, job_id).status == "running"


def test_renewing_extends_the_lease_for_the_holder_only(session_factory):
    job_id = _enqueue(session_factory)
    _claim(session_factory, now=START, lease_seconds=10)
    assert not _in_session(session_factory, lambda s: jobs.renew_lease(s, job_id=job_id, runner_id="other", now=START))
    renewed = _in_session(session_factory, lambda s: jobs.renew_lease(s, job_id=job_id, runner_id="r1", now=START + timedelta(seconds=9), lease_seconds=60))
    assert renewed
    assert _in_session(session_factory, lambda s: jobs.reclaim_expired(s, now=START + timedelta(seconds=30))) == {"requeued": 0, "expired": 0}


def test_a_runner_that_lost_the_lease_cannot_write_a_result(session_factory):
    job_id = _enqueue(session_factory)
    _claim(session_factory, now=START, lease_seconds=10)
    _in_session(session_factory, lambda s: jobs.reclaim_expired(s, now=START + timedelta(seconds=11)))
    _claim(session_factory, runner="r2", now=START + timedelta(seconds=12))
    assert not _in_session(session_factory, lambda s: jobs.finish(s, job_id=job_id, runner_id="r1", status="completed", result={"stale": True}))
    assert _in_session(session_factory, lambda s: jobs.finish(s, job_id=job_id, runner_id="r2", status="completed", result={"ok": True}))
    row = _job(session_factory, job_id)
    assert (row.status, row.result) == ("completed", {"ok": True}) and row.lease_expires_at is None


def test_gated_work_is_refused_while_the_gate_is_closed(session_factory):
    gated, conformance_job = _enqueue(session_factory, jobs.KIND_VERIFICATION), _enqueue(session_factory)
    assert _in_session(session_factory, lambda s: jobs.fail_queued(s, kinds=jobs.GATED_KINDS, error_code=gate.NOT_CONFORMANT)) == 1
    row = _job(session_factory, gated)
    assert (row.status, row.error_code) == ("failed", "sandbox_not_conformant")
    assert _job(session_factory, conformance_job).status == "queued"


def test_conformance_request_is_deduplicated_and_audited(session_factory):
    async def request(session):
        job, created = await jobs.enqueue_conformance(session, requested_by="user:alice")
        await session.commit()
        return job.job_id, created

    async def events(session):
        stmt = select(GovernanceEvent).where(GovernanceEvent.event_type == "sandbox.conformance_requested")
        return (await session.execute(stmt)).scalars().all()

    first = _in_session(session_factory, request)
    second = _in_session(session_factory, request)
    assert first[1] is True and second == (first[0], False)
    rows = _in_session(session_factory, events)
    assert len(rows) == 1 and rows[0].details == {"job_id": first[0]} and rows[0].user_id == "user:alice"


def test_only_recent_conformance_runs_are_kept(session_factory):
    settings = _settings()

    async def scenario(session):
        for index in range(5):
            session.add(_row(settings, run_id=f"sbc_{index}", created_at=START + timedelta(minutes=index)))
        await session.commit()
        removed = await jobs.prune_conformance(session, keep=2)
        return removed, [run.run_id for run in await jobs.list_conformance(session)]

    assert _in_session(session_factory, scenario) == (3, ["sbc_4", "sbc_3"])


# ---------------------------------------------------------------------------
# D19: runner
# ---------------------------------------------------------------------------


def _passing_report():
    return [{"name": name, "passed": True, "detail": "", "group": "base", "duration_ms": 1} for name in conformance.ALL_CASE_NAMES]


class RunnerBackend:
    def __init__(self, fail_connect=False):
        self.fail_connect = fail_connect
        self.version = VERSION
        self.swept = 0

    def server_version(self):
        if self.fail_connect:
            raise ConnectionError("no docker")
        return self.version

    def sweep_orphans(self):
        self.swept += 1
        return 0


def _runner(session_factory, monkeypatch, *, report=None, backend=None, **settings):
    values = {"sandbox_execution_enabled": True, **settings}
    backend = backend or RunnerBackend()
    monkeypatch.setattr(conformance, "run_full_suite", lambda factory, image: report if report is not None else _passing_report())
    store = InMemoryRunStateStore()
    runner = SandboxRunner(_settings(**values), session_factory=session_factory, state_store=store, backend_factory=lambda: backend, runner_id="r1")
    return runner, store, backend


def _gate(runner, session_factory):
    return _in_session(session_factory, lambda s: gate.evaluate(s, runner.settings, docker_server_version=VERSION))


def test_runner_proves_the_backend_at_startup(session_factory, monkeypatch):
    runner, _, backend = _runner(session_factory, monkeypatch)
    assert asyncio.run(runner.prepare()) is True and backend.swept == 1 and runner.state == "ready"
    runs = _in_session(session_factory, jobs.list_conformance)
    assert len(runs) == 1 and runs[0].passed is True and runs[0].triggered_by == "runner_startup"
    assert runs[0].run_id.startswith("sbc_") and runs[0].docker_server_version == VERSION
    assert _gate(runner, session_factory).ready


def test_runner_stays_idle_while_execution_is_off(session_factory, monkeypatch):
    runner, _, backend = _runner(session_factory, monkeypatch, sandbox_execution_enabled=False)
    assert asyncio.run(runner.prepare()) is False and runner.state == "disabled" and backend.swept == 0
    assert _in_session(session_factory, jobs.list_conformance) == []


def test_runner_reports_an_unreachable_docker(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch, backend=RunnerBackend(fail_connect=True))
    assert asyncio.run(runner.prepare()) is False and runner.state == "docker_unreachable"


def test_failed_conformance_closes_the_gate(session_factory, monkeypatch):
    bad = [{**item, "passed": item["name"] != "network_unreachable"} for item in _passing_report()]
    runner, _, _ = _runner(session_factory, monkeypatch, report=bad)
    asyncio.run(runner.prepare())
    assert _gate(runner, session_factory).reasons == (gate.CONFORMANCE_FAILED,)


def test_runner_handles_a_conformance_job(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch)
    asyncio.run(runner.prepare())
    job_id = _enqueue(session_factory, payload={"requested_by": "user:alice"})
    assert asyncio.run(runner.run_once()) is True
    row = _job(session_factory, job_id)
    assert row.status == "completed" and row.result["passed"] is True
    assert _in_session(session_factory, jobs.list_conformance)[0].triggered_by == "user:alice"
    assert asyncio.run(runner.run_once()) is False


def test_runner_refuses_gated_work_while_the_gate_is_closed(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch)
    calls = []

    async def handler(job):
        calls.append(job.job_id)
        return {}

    runner.handlers[jobs.KIND_VERIFICATION] = handler
    job_id = _enqueue(session_factory, jobs.KIND_VERIFICATION, payload={"risk_score": 0.1})
    assert asyncio.run(runner.run_once()) is False
    row = _job(session_factory, job_id)
    assert (row.status, row.error_code) == ("failed", gate.NOT_CONFORMANT) and calls == []


def _open_gate(runner, session_factory):
    asyncio.run(runner.prepare())
    assert _gate(runner, session_factory).ready


def test_runner_refuses_work_that_needs_more_isolation(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch)
    _open_gate(runner, session_factory)
    calls = []

    async def handler(job):
        calls.append(job.job_id)
        return {}

    runner.handlers[jobs.KIND_VERIFICATION] = handler
    high = _enqueue(session_factory, jobs.KIND_VERIFICATION, payload={"risk_score": 0.9})
    assert asyncio.run(runner.run_once()) is True
    row = _job(session_factory, high)
    assert (row.status, row.error_code) == ("failed", "isolation_insufficient") and calls == []
    low = _enqueue(session_factory, jobs.KIND_VERIFICATION, payload={"risk_score": 0.1})
    assert asyncio.run(runner.run_once()) is True and _job(session_factory, low).status == "completed" and calls == [low]


def test_work_without_a_risk_score_counts_as_high_risk(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch)
    _open_gate(runner, session_factory)

    async def handler(job):
        return {}

    runner.handlers[jobs.KIND_VERIFICATION] = handler
    job_id = _enqueue(session_factory, jobs.KIND_VERIFICATION)
    asyncio.run(runner.run_once())
    assert _job(session_factory, job_id).error_code == "isolation_insufficient"


def test_relaxed_high_risk_minimum_lets_high_risk_work_run(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch, sandbox_min_isolation_high_risk="container")
    _open_gate(runner, session_factory)

    async def handler(job):
        return {}

    runner.handlers[jobs.KIND_VERIFICATION] = handler
    job_id = _enqueue(session_factory, jobs.KIND_VERIFICATION, payload={"risk_score": 0.9})
    assert asyncio.run(runner.run_once()) is True and _job(session_factory, job_id).status == "completed"


def test_a_crashing_handler_fails_the_job_and_keeps_the_runner_alive(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch)
    _open_gate(runner, session_factory)

    async def handler(job):
        raise RuntimeError("boom")

    runner.handlers[jobs.KIND_VERIFICATION] = handler
    job_id = _enqueue(session_factory, jobs.KIND_VERIFICATION, payload={"risk_score": 0.1})
    assert asyncio.run(runner.run_once()) is True
    row = _job(session_factory, job_id)
    assert (row.status, row.error_code) == ("failed", "runner_error:RuntimeError") and runner.state == "ready"


def test_runner_reclaims_abandoned_jobs_before_claiming(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch)
    asyncio.run(runner.prepare())
    job_id = _enqueue(session_factory)
    _claim(session_factory, runner="dead", now=datetime.now(timezone.utc) - timedelta(hours=1), lease_seconds=10)
    assert asyncio.run(runner.run_once()) is True
    row = _job(session_factory, job_id)
    assert (row.status, row.claimed_by, row.attempts) == ("completed", "r1", 2)


def test_heartbeat_reports_state_and_expires(session_factory, monkeypatch):
    runner, store, _ = _runner(session_factory, monkeypatch)
    asyncio.run(runner.prepare())
    asyncio.run(runner.heartbeat_once())
    beats = asyncio.run(jobs.runner_heartbeats(store))
    assert [beat["runner_id"] for beat in beats] == ["r1"] and beats[0]["state"] == "ready" and beats[0]["age_seconds"] == 0
    assert beats[0]["docker_server_version"] == VERSION and beats[0]["runtime"] == "runc"
    _expiry, payload = store.sandbox_runners["r1"]
    store.sandbox_runners["r1"] = (datetime.now(timezone.utc) - timedelta(seconds=1), payload)
    assert asyncio.run(jobs.runner_heartbeats(store)) == []


def test_heartbeat_renews_the_running_job(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch)
    job_id = _enqueue(session_factory)
    claimed = _claim(session_factory, now=datetime.now(timezone.utc) - timedelta(seconds=290))
    runner.current_job_id = job_id
    before = claimed.lease_expires_at
    asyncio.run(runner.heartbeat_once())
    assert _job(session_factory, job_id).lease_expires_at.replace(tzinfo=timezone.utc) > before


def test_a_redis_outage_does_not_end_the_job_lease(session_factory, monkeypatch):
    runner, store, _ = _runner(session_factory, monkeypatch)
    job_id = _enqueue(session_factory)
    claimed = _claim(session_factory, now=datetime.now(timezone.utc) - timedelta(seconds=290))
    runner.current_job_id = job_id

    async def redis_down(*args, **kwargs):
        raise ConnectionError("redis down")

    store.set_sandbox_runner_heartbeat = redis_down
    with pytest.raises(ConnectionError):
        asyncio.run(runner.heartbeat_once())
    assert _job(session_factory, job_id).lease_expires_at.replace(tzinfo=timezone.utc) > claimed.lease_expires_at


def test_orphans_are_swept_again_while_the_runner_lives(session_factory, monkeypatch):
    runner, _, backend = _runner(session_factory, monkeypatch)
    asyncio.run(runner.prepare())
    asyncio.run(runner.run_once())
    assert backend.swept == 1
    runner.last_sweep -= sandbox_runner.SWEEP_SECONDS
    asyncio.run(runner.run_once())
    assert backend.swept == 2


def test_a_docker_upgrade_makes_the_runner_prove_the_backend_again(session_factory, monkeypatch):
    runner, _, backend = _runner(session_factory, monkeypatch)
    asyncio.run(runner.prepare())
    backend.version = "30.0.0"
    asyncio.run(runner.run_once())
    latest = _in_session(session_factory, jobs.list_conformance)[0]
    assert (latest.docker_server_version, latest.triggered_by) == ("30.0.0", "runner_refresh") and runner.gate_ready is True


def test_a_stale_conformance_run_is_refreshed_by_the_runner(session_factory, monkeypatch):
    runner, _, _ = _runner(session_factory, monkeypatch)
    old = datetime.now(timezone.utc) - timedelta(hours=169)

    async def add(session):
        session.add(_row(runner.settings, created_at=old))
        await session.commit()

    _in_session(session_factory, add)
    asyncio.run(runner.run_once())
    assert _in_session(session_factory, jobs.list_conformance)[0].triggered_by == "runner_refresh" and runner.gate_ready is True


def test_a_failed_conformance_run_is_not_repeated_in_a_loop(session_factory, monkeypatch):
    bad = [{**item, "passed": False} for item in _passing_report()]
    runner, _, _ = _runner(session_factory, monkeypatch, report=bad)
    asyncio.run(runner.prepare())
    asyncio.run(runner.run_once())
    asyncio.run(runner.run_once())
    assert len(_in_session(session_factory, jobs.list_conformance)) == 1


def test_losing_docker_closes_the_runner_until_it_is_proven_again(session_factory, monkeypatch):
    runner, _, backend = _runner(session_factory, monkeypatch)
    asyncio.run(runner.prepare())
    backend.fail_connect = True
    job_id = _enqueue(session_factory)
    assert asyncio.run(runner.run_once()) is False
    assert (runner.state, runner.prepared, runner.gate_ready) == ("docker_unreachable", False, False)
    assert _job(session_factory, job_id).status == "queued"
    backend.fail_connect = False
    assert asyncio.run(runner.prepare()) is True and runner.state == "ready"


def test_a_redis_outage_reads_as_no_runner():
    class Broken:
        async def list_sandbox_runners(self):
            raise ConnectionError("redis down")

    assert asyncio.run(jobs.runner_heartbeats(Broken())) == []
    assert asyncio.run(jobs.runner_heartbeats(None)) == []


def test_serve_stops_cleanly(session_factory, monkeypatch):
    runner, store, _ = _runner(session_factory, monkeypatch)
    runner.poll_seconds = 0.01

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(runner.serve(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert runner.prepared is True and "r1" in store.sandbox_runners


# ---------------------------------------------------------------------------
# D19, D38, D52: delivery files
# ---------------------------------------------------------------------------


def test_compose_gives_docker_access_to_the_runner_only():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    runner = services["sandbox-runner"]
    assert runner["profiles"] == ["sandbox"] and runner["command"] == ["python", "-m", "app.workers.sandbox_runner"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in runner["volumes"]
    assert "user" not in runner and "privileged" not in runner and "ports" not in runner
    for name, service in services.items():
        if name != "sandbox-runner":
            assert not any("docker.sock" in str(volume) for volume in service.get("volumes", [])), name


def test_sandbox_python_image_is_pinned_and_not_root():
    text = (ROOT / "docker/sandbox-python/Dockerfile").read_text(encoding="utf-8")
    pins = (ROOT / "docker/sandbox-python/requirements.txt").read_text(encoding="utf-8")
    assert "@sha256:" in text and "USER 65534:65534" in text and "pytest==" in pins and "coverage==" in pins
    assert "build-essential" not in text and "gcc" not in text


def test_ci_runs_both_runtimes_and_the_postgres_migration():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))["jobs"]
    assert workflow["sandbox"]["strategy"]["matrix"]["runtime"] == ["runc", "runsc"]
    steps = workflow["sandbox"]["steps"]
    install = next(step for step in steps if step.get("name") == "Install gVisor")
    assert "sha512sum -c" in install["run"] and len(install["env"]["GVISOR_SHA512"]) == 128
    live = next(step for step in steps if step.get("name") == "Real Docker conformance")
    assert live["env"]["RH_SANDBOX_LIVE"] == "1" and "continue-on-error" not in live
    runs = [step.get("run", "") for step in workflow["migrations"]["steps"]]
    assert runs.index("alembic upgrade head") < runs.index("alembic downgrade -1")
    assert runs.count("alembic upgrade head") == 2 and "postgres" in workflow["migrations"]["services"]


def test_release_workflow_scans_the_sandbox_image_before_pushing_it():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-image.yml").read_text(encoding="utf-8"))["jobs"]
    assert any("rewardhacking-sandbox-python:scan" in step.get("run", "") for step in workflow["verify"]["steps"])
    steps = workflow["publish"]["steps"]
    pushed = [step for step in steps if step.get("with", {}).get("context") == "docker/sandbox-python" and step["with"]["push"] is True]
    assert len(pushed) == 1 and "-sandbox-python:" in pushed[0]["with"]["tags"]
    scan = next(step for step in steps if "rewardhacking-sandbox-python:publish" in step.get("run", "") and "image_content_check.py" in step["run"])
    assert steps.index(scan) < steps.index(pushed[0])


def test_dependabot_upgrades_opentelemetry_packages_together():
    updates = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))["updates"]
    pip = next(item for item in updates if item["package-ecosystem"] == "pip")
    assert pip["groups"]["opentelemetry"]["patterns"] == ["opentelemetry-*"]


# ---------------------------------------------------------------------------
# D39-D41: API, Dashboard and operations
# ---------------------------------------------------------------------------


def _docker_env(monkeypatch, **extra):
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    monkeypatch.setenv("SANDBOX_EXECUTION_ENABLED", "true")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def _store_passing_run(session_factory, **overrides):
    settings = get_settings()

    async def add(session):
        session.add(_row(settings, **overrides))
        await session.commit()

    _in_session(session_factory, add)


def _heartbeat(client, state="ready"):
    payload = {"runner_id": "r1", "state": state, "runtime": "runc", "at": datetime.now(timezone.utc).isoformat()}
    asyncio.run(client.state_store.set_sandbox_runner_heartbeat("r1", payload))


def test_permissions_are_the_documented_ones():
    assert permissions.PERMISSIONS["sandbox.view"] == frozenset({"admin", "operator", "auditor"})
    assert permissions.PERMISSIONS["sandbox.manage"] == frozenset({"admin"})
    assert "sandbox.view" in permissions.READ_PERMISSIONS and "sandbox.manage" not in permissions.READ_PERMISSIONS
    assert "sandbox.manage" not in permissions.STEP_UP_PERMISSIONS and "sandbox" not in permissions.TENANT_VIEW_ALLOWLIST


def test_status_api_reports_a_closed_gate_by_default(client):
    body = client.get("/v1/sandbox/status").json()
    assert body["gate"]["ready"] is False and body["gate"]["reasons"] == ["execution_disabled", "backend_not_docker"]
    assert body["runners"] == [] and body["configured_backend"] == "simulated"


def test_status_api_reports_an_open_gate_and_runners(client, session_factory, monkeypatch):
    _docker_env(monkeypatch)
    _store_passing_run(session_factory)
    _heartbeat(client)
    body = client.get("/v1/sandbox/status").json()
    assert body["gate"]["ready"] is True and body["capabilities"]["backend_id"] == "docker"
    assert body["runners"][0]["runner_id"] == "r1" and body["executes_tools"] is False


def test_conformance_api_is_refused_while_execution_is_off(client):
    response = client.post("/v1/sandbox/conformance")
    assert response.status_code == 409 and response.json()["error"]["code"] == "sandbox_execution_disabled"


def test_conformance_api_queues_one_job(client, monkeypatch):
    _docker_env(monkeypatch)
    first = client.post("/v1/sandbox/conformance")
    second = client.post("/v1/sandbox/conformance")
    assert first.status_code == 202 and first.json()["created"] is True and first.json()["job"]["job_id"].startswith("sbj_")
    assert second.status_code == 202 and second.json()["created"] is False
    assert second.json()["job"]["job_id"] == first.json()["job"]["job_id"]


def test_conformance_list_api_returns_cases(client, session_factory, monkeypatch):
    _docker_env(monkeypatch)
    _store_passing_run(session_factory)
    body = client.get("/v1/sandbox/conformance").json()
    assert body["count"] == 1 and body["runs"][0]["passed"] is True and body["runs"][0]["run_id"] == "sbc_1"


def test_dashboard_page_shows_the_gate_reasons(client):
    html = client.get("/dashboard/sandbox").text
    assert "execution_disabled" in html and "backend_not_docker" in html and 'data-testid="sandbox-gate"' in html
    assert 'id="sandbox-conformance-button"' not in html and 'data-testid="sandbox-no-runner"' in html


def test_dashboard_page_shows_cases_runner_and_relaxation(client, session_factory, monkeypatch):
    _docker_env(monkeypatch, SANDBOX_MIN_ISOLATION_HIGH_RISK="container")
    _store_passing_run(session_factory, cases=_passing_report())
    _heartbeat(client)
    html = client.get("/dashboard/sandbox").text
    total = len(conformance.ALL_CASE_NAMES)
    assert 'data-testid="sandbox-relaxed"' in html and "runtime_matches_config" in html and "r1" in html
    assert f"{total}/{total}" in html and 'id="sandbox-conformance-button"' in html


def test_dashboard_conformance_button_queues_a_job(client, monkeypatch):
    _docker_env(monkeypatch)
    response = client.post("/dashboard/sandbox/conformance", follow_redirects=False)
    assert response.status_code == 303 and "conformance_queued:sbj_" in response.headers["location"]
    assert 'data-testid="sandbox-job-row"' in client.get("/dashboard/sandbox").text


def test_dashboard_conformance_is_refused_while_execution_is_off(client):
    response = client.post("/dashboard/sandbox/conformance", follow_redirects=False)
    assert response.status_code == 303 and "error=sandbox_execution_disabled" in response.headers["location"]


@pytest.mark.parametrize(
    ("role", "tenant", "page", "post"),
    [
        ("admin", None, 200, 303),
        ("operator", None, 200, 403),
        ("auditor", None, 200, 403),
        ("reviewer", None, 403, 403),
        ("tenant_admin", "t1", 403, 403),
    ],
)
def test_dashboard_permissions(client, signed_in, monkeypatch, role, tenant, page, post):
    _docker_env(monkeypatch)
    signed_in(role=role, tenant_id=tenant)
    assert client.get("/dashboard/sandbox").status_code == page
    assert client.post("/dashboard/sandbox/conformance", follow_redirects=False).status_code == post


def test_dashboard_auditor_sees_no_button(client, signed_in, monkeypatch):
    _docker_env(monkeypatch)
    signed_in(role="auditor")
    html = client.get("/dashboard/sandbox").text
    assert "Sandbox Runtime" in html and 'id="sandbox-conformance-button"' not in html


def test_dashboard_post_needs_a_csrf_token(client, signed_in, monkeypatch):
    _docker_env(monkeypatch)
    signed_in(role="admin")
    signed_in.auto_csrf["on"] = False
    assert client.post("/dashboard/sandbox/conformance", follow_redirects=False).status_code == 403


def _checks(client):
    return {item["check_key"]: item for item in client.get("/v1/operations/readiness").json()["checks"]}


def test_operations_checks_pass_while_execution_is_off(client):
    checks = _checks(client)
    assert checks["sandbox_backend_health"]["status"] == "pass" and checks["sandbox_runner_health"]["status"] == "pass"


def test_operations_fail_when_the_flag_is_on_and_the_gate_is_closed(client, monkeypatch):
    _docker_env(monkeypatch)
    checks = _checks(client)
    assert checks["sandbox_backend_health"]["status"] == "fail" and "conformance_missing" in checks["sandbox_backend_health"]["detail"]
    assert checks["sandbox_runner_health"]["status"] == "fail" and "no runner heartbeat" in checks["sandbox_runner_health"]["detail"]


def test_operations_pass_with_an_open_gate_and_a_live_runner(client, session_factory, monkeypatch):
    _docker_env(monkeypatch)
    _store_passing_run(session_factory)
    _heartbeat(client)
    checks = _checks(client)
    assert checks["sandbox_backend_health"]["status"] == "pass" and checks["sandbox_runner_health"]["status"] == "pass"


def test_operations_fail_when_the_runner_cannot_reach_docker(client, session_factory, monkeypatch):
    _docker_env(monkeypatch)
    _store_passing_run(session_factory)
    _heartbeat(client, state="docker_unreachable")
    assert _checks(client)["sandbox_runner_health"]["status"] == "fail"


def test_operations_warn_on_runc_in_production(client, session_factory, monkeypatch):
    _docker_env(monkeypatch, APP_ENV="prod")
    _store_passing_run(session_factory)
    check = _checks(client)["sandbox_backend_health"]
    assert check["status"] == "warn" and "gVisor" in check["detail"]
