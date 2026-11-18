"""v1.6.2 P4b: dry-run replay with a sandbox re-run and the platform verification block on Run and Review Detail (no Docker)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings
from app.models.agent_run import AgentRun
from app.models.checkpoint import ReplayAttempt
from app.models.control_plane import ExecutionPlan
from app.models.evaluation_review import EvaluationReview, RunOutcome
from app.models.sandbox import SandboxJob, VerificationRun
from app.schemas.checkpoint import ReplayRunRequest
from app.services import finops
from app.services.finops import leases
from app.services.verification import demo, engine, sampling
from tests.openapi_contract import current_contract
from tests.test_v162_verification import FAILING, FakeBackend
from tests.test_v162_verification_flow import DOCKER_VERSION, IMAGES, _count, _in_session, _keys, _open_gate, _run, _suite

ROOT = Path(__file__).resolve().parents[1]
NOTE_EN = "it is not tamper-proof evidence"
NOTE_ZH = "報告由平台在 sandbox 內、用平台保存的測試重跑產生，不表示 agent 的程式碰不到報告，也不是防竄改證明。"
BLOCK = 'data-testid="platform-verification"'
BUTTON = 'data-testid="platform-request-rerun"'
CHECKBOX = 'data-testid="replay-sandbox-rerun"'


# ---------------------------------------------------------------------------
# D33: dry-run replay with a sandbox re-run
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_env(monkeypatch):
    monkeypatch.setenv("SANDBOX_BACKEND", "docker")
    monkeypatch.setenv("SANDBOX_EXECUTION_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_MIN_ISOLATION_HIGH_RISK", "container")
    monkeypatch.setenv("SANDBOX_IMAGES", IMAGES)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _plan(session_factory, run_id="run_1", tenant_id="t-1") -> None:
    async def add(session):
        session.add(ExecutionPlan(plan_id=f"plan_{run_id}", run_id=run_id, tenant_id=tenant_id, workflow_id="wf", workflow_version="1", status="completed"))
        await session.commit()

    _in_session(session_factory, add)


def _replayable(session_factory, *, gate_open=True, bound=True, evidence=True):
    settings = get_settings()
    suite = _suite(session_factory, settings=settings)
    if gate_open:
        _open_gate(session_factory, settings)
    _run(session_factory, suite_id=suite.suite_id if bound else "", evidence=demo.scenario_evidence("fake_report") if evidence else None)
    _plan(session_factory)
    return suite


def _forbid_side_effects(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("a replay must not touch a token lease")

    for name in ("acquire_run_budget_lease", "release_run_budget_lease"):
        monkeypatch.setattr(leases, name, forbidden)
        monkeypatch.setattr(finops, name, forbidden)  # callers import these names from the package


def _replay(client, **body):
    return client.post("/v1/runs/run_1/replay", json={"mode": "dry_run", **body})


def _attempt(session_factory) -> ReplayAttempt:
    return _in_session(session_factory, lambda s: s.scalar(select(ReplayAttempt)))


def _run_job(session_factory, backend) -> None:
    job = _in_session(session_factory, lambda s: s.scalar(select(SandboxJob)))
    asyncio.run(engine.handle_job(job, settings=get_settings(), session_factory=session_factory, backend_factory=backend, docker_server_version=DOCKER_VERSION))


def test_a_sandbox_replay_queues_a_replay_verification(client, session_factory, sandbox_env, monkeypatch):
    _forbid_side_effects(monkeypatch)
    _replayable(session_factory)
    answer = _replay(client, sandbox_rerun=True)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["status"] == "queued" and body["result_payload"]["automatic_external_execution"] is False
    assert body["limitations"]["automatic_external_execution"] is False and body["limitations"]["external_tool_calls"] is False
    row = _in_session(session_factory, lambda s: s.scalar(select(VerificationRun)))
    assert (row.trigger, row.status, row.verification_id) == ("replay", "queued", body["result_payload"]["verification_id"])


def test_a_sandbox_replay_dispatches_nothing(client, session_factory, sandbox_env, monkeypatch):
    _forbid_side_effects(monkeypatch)
    _replayable(session_factory)
    before = _in_session(session_factory, lambda s: s.scalar(select(AgentRun)))
    assert _replay(client, sandbox_rerun=True).status_code == 200
    after = _in_session(session_factory, lambda s: s.scalar(select(AgentRun)))
    assert client.event_publisher.events == [] and (after.status, after.updated_at) == (before.status, before.updated_at)


def test_the_replay_ends_when_the_rerun_ends(client, session_factory, sandbox_env):
    _replayable(session_factory)
    _replay(client, sandbox_rerun=True)
    _run_job(session_factory, FakeBackend(FAILING, FAILING, FAILING, FAILING))
    attempt = _attempt(session_factory)
    assert attempt.status == "completed"
    assert (attempt.result_payload["verification_status"], attempt.result_payload["assurance_level"]) == ("completed", "platform_verified")


def test_a_failed_rerun_fails_the_replay(client, session_factory, sandbox_env):
    _replayable(session_factory)
    _replay(client, sandbox_rerun=True)
    _run_job(session_factory, FakeBackend(("timeout", None, {})))
    attempt = _attempt(session_factory)
    assert (attempt.status, attempt.result_payload["verification_status"]) == ("failed", "failed")


def test_a_rerun_held_back_by_the_cap_completes_the_replay_with_the_reason(client, session_factory, sandbox_env, monkeypatch):
    monkeypatch.setenv("SANDBOX_VERIFY_DAILY_CAP_PER_TENANT", "0")
    get_settings.cache_clear()
    _replayable(session_factory)
    body = _replay(client, sandbox_rerun=True).json()
    assert (body["status"], body["result_payload"]["verification_status"], body["result_payload"]["skip_reason"]) == ("completed", "skipped", "daily_cap")


def test_joining_a_rerun_locks_it_until_the_attempt_is_committed():
    locked = str(sampling.active_statement("outcome_1", lock=True).compile(dialect=postgresql.dialect()))
    plain = str(sampling.active_statement("outcome_1").compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in locked and "FOR UPDATE" not in plain


def test_a_replay_joins_a_rerun_that_is_already_running(client, session_factory, sandbox_env):
    _replayable(session_factory)
    first = _replay(client, sandbox_rerun=True).json()["result_payload"]["verification_id"]
    second = _replay(client, sandbox_rerun=True).json()["result_payload"]["verification_id"]
    assert first == second and _count(session_factory, VerificationRun) == 1
    _run_job(session_factory, FakeBackend(FAILING, FAILING, FAILING, FAILING))
    attempts = _in_session(session_factory, lambda s: s.scalars(select(ReplayAttempt)))
    assert {item.status for item in attempts.all()} == {"completed"}


@pytest.mark.parametrize(
    "state", [{"gate_open": False}, {"bound": False}, {"evidence": False}], ids=["gate_closed", "no_suite", "no_outcome"]
)
def test_an_unavailable_rerun_answers_409(client, session_factory, sandbox_env, state):
    _replayable(session_factory, **state)
    answer = _replay(client, sandbox_rerun=True)
    assert (answer.status_code, answer.json()["error"]["code"]) == (409, "sandbox_rerun_unavailable")
    assert _count(session_factory, ReplayAttempt) == 0 and _count(session_factory, VerificationRun) == 0


def test_a_plain_replay_is_unchanged(client, session_factory, sandbox_env):
    _replayable(session_factory)
    body = _replay(client).json()
    assert body["status"] == "completed" and "verification_id" not in body["result_payload"]
    assert _count(session_factory, VerificationRun) == 0


def test_the_replay_api_keeps_its_roles_and_tenant_scope(client, session_factory, sandbox_env, monkeypatch):
    keys = _keys(monkeypatch)
    _replayable(session_factory)
    assert client.post("/v1/runs/run_1/replay", json={"sandbox_rerun": True}, headers=keys["reviewer"]).status_code == 403
    assert client.post("/v1/runs/run_1/replay", json={"sandbox_rerun": True}, headers=keys["other"]).status_code == 403
    assert client.post("/v1/runs/run_1/replay", json={"sandbox_rerun": True}, headers=keys["operator"]).status_code == 200
    row = _in_session(session_factory, lambda s: s.scalar(select(VerificationRun)))
    assert row.requested_by != "system"


def test_the_replay_request_keeps_the_published_schema():
    assert "sandbox_rerun" not in ReplayRunRequest.model_json_schema()["properties"]
    baseline = json.loads((ROOT / "tests" / "fixtures" / "v160" / "openapi_v1512.json").read_text(encoding="utf-8"))
    current = current_contract()
    for key in ("POST /v1/runs/{run_id}/replay", "POST /dashboard/runs/{run_id}/replay", "GET /v1/runs/{run_id}/outcomes"):
        assert current[key] == baseline[key]


# ---------------------------------------------------------------------------
# D39: Run Detail and Review Detail
# ---------------------------------------------------------------------------


def _review(session_factory) -> None:
    async def add(session):
        session.add(EvaluationReview(review_id="review_1", run_id="run_1", tenant_id="t-1", status="open", priority="P2"))
        await session.commit()

    _in_session(session_factory, add)


def _with_result(session_factory) -> VerificationRun:
    _replayable(session_factory)
    _review(session_factory)
    settings = get_settings()

    async def enqueue(session):
        run = await session.scalar(select(AgentRun))
        outcome = await session.scalar(select(RunOutcome))
        return await sampling.enqueue(session, settings, run=run, outcome=outcome, suite_id=sampling.bound_suite_id(run), trigger="all", requested_by="test")

    row = _in_session(session_factory, enqueue)
    _run_job(session_factory, FakeBackend(FAILING, FAILING, FAILING, FAILING))
    return row


@pytest.mark.parametrize("path", ["/dashboard/runs/run_1", "/dashboard/reviews/review_1"])
def test_the_block_shows_the_result_and_what_it_does_not_prove(client, session_factory, signed_in, sandbox_env, path):
    row = _with_result(session_factory)
    signed_in(role="reviewer", tenant_id="t-1")
    page = client.get(path)
    assert page.status_code == 200 and BLOCK in page.text and NOTE_EN in page.text and NOTE_ZH in page.text
    assert 'data-testid="platform-assurance">platform_verified' in page.text and "RV-001" in page.text
    assert row.verification_id in page.text and BUTTON in page.text


@pytest.mark.parametrize("path", ["/dashboard/runs/run_1", "/dashboard/reviews/review_1"])
def test_the_auditor_reads_the_block_without_the_button(client, session_factory, signed_in, sandbox_env, path):
    _with_result(session_factory)
    signed_in(role="auditor")
    page = client.get(path)
    assert BLOCK in page.text and BUTTON not in page.text
    assert client.post("/dashboard/verification/request", data={"run_id": "run_1"}).status_code == 403


def test_the_verification_page_carries_the_same_note(client, session_factory, signed_in, sandbox_env):
    row = _with_result(session_factory)
    signed_in(role="auditor")
    page = client.get(f"/dashboard/verification/runs/{row.verification_id}").text
    assert NOTE_EN in page and NOTE_ZH in page and "RV-001" in page


def test_an_operator_does_not_see_the_results(client, session_factory, signed_in, sandbox_env):
    _with_result(session_factory)
    signed_in(role="operator")
    page = client.get("/dashboard/runs/run_1")
    assert page.status_code == 200 and BLOCK not in page.text and "RV-001" not in page.text


def test_a_run_without_a_suite_shows_no_block(client, session_factory, signed_in, sandbox_env):
    _replayable(session_factory, bound=False)
    signed_in(role="reviewer", tenant_id="t-1")
    assert BLOCK not in client.get("/dashboard/runs/run_1").text


def test_a_bound_run_without_results_offers_the_first_rerun(client, session_factory, signed_in, sandbox_env):
    _replayable(session_factory)
    signed_in(role="reviewer", tenant_id="t-1")
    page = client.get("/dashboard/runs/run_1").text
    assert 'data-testid="platform-empty"' in page and BUTTON in page


@pytest.mark.parametrize(("form", "target"), [({"return_to": "run"}, "/dashboard/runs/run_1?"), ({"return_to": "review", "review_id": "review_1"}, "/dashboard/reviews/review_1?")])
def test_the_button_returns_to_the_page_it_was_pressed_on(client, session_factory, signed_in, sandbox_env, form, target):
    _replayable(session_factory)
    _review(session_factory)
    signed_in(role="reviewer", tenant_id="t-1")
    answer = client.post("/dashboard/verification/request", data={"run_id": "run_1", **form}, follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"].startswith(target) and "verification_queued" in answer.headers["location"]
    assert _in_session(session_factory, lambda s: s.scalar(select(VerificationRun))).trigger == "manual"


def test_the_return_target_cannot_leave_the_site(client, session_factory, signed_in, sandbox_env):
    _replayable(session_factory)
    signed_in(role="reviewer", tenant_id="t-1")
    form = {"run_id": "run_1", "return_to": "review", "review_id": "//evil.example/x"}
    location = client.post("/dashboard/verification/request", data=form, follow_redirects=False).headers["location"]
    assert location.startswith("/dashboard/reviews/%2F%2Fevil.example%2Fx?")


def test_the_replay_form_offers_the_sandbox_only_when_it_can_run(client, session_factory, signed_in, sandbox_env):
    _replayable(session_factory)
    signed_in(role="operator", tenant_id="t-1")
    assert CHECKBOX in client.get("/dashboard/runs/run_1").text
    answer = client.post("/dashboard/runs/run_1/replay", data={"sandbox_rerun": "1"}, follow_redirects=False)
    assert answer.status_code == 303 and "replay_queued" in answer.headers["location"]
    assert _attempt(session_factory).status == "queued"
    assert 'data-testid="replay-attempt-status">queued' in client.get("/dashboard/runs/run_1").text


def test_the_replay_form_hides_the_sandbox_when_the_gate_is_closed(client, session_factory, signed_in, sandbox_env):
    _replayable(session_factory, gate_open=False)
    signed_in(role="operator", tenant_id="t-1")
    assert CHECKBOX not in client.get("/dashboard/runs/run_1").text
    answer = client.post("/dashboard/runs/run_1/replay", data={"sandbox_rerun": "1"}, follow_redirects=False)
    assert answer.status_code == 303 and "error=sandbox_rerun_unavailable" in answer.headers["location"]
    assert _count(session_factory, ReplayAttempt) == 0


def test_a_plain_dashboard_replay_still_works(client, session_factory, signed_in, sandbox_env):
    _replayable(session_factory)
    signed_in(role="operator", tenant_id="t-1")
    assert client.post("/dashboard/runs/run_1/replay", data={}).status_code == 200
    assert _attempt(session_factory).status == "completed" and _count(session_factory, VerificationRun) == 0


def test_a_tenant_user_cannot_replay_another_tenants_run(client, session_factory, signed_in, sandbox_env):
    _replayable(session_factory)
    signed_in(role="operator", tenant_id="t-2")
    assert client.post("/dashboard/runs/run_1/replay", data={"sandbox_rerun": "1"}).status_code == 403
    assert _in_session(session_factory, lambda s: s.scalar(select(func.count()).select_from(ReplayAttempt))) == 0
