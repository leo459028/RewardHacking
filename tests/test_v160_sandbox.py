"""v1.6.0 P4: sandbox execution spec, simulated backend, isolation policy, conformance suite, executor wiring.

``GET /v1/sandbox/status`` and the ``sandbox_backend_health`` operations check are P5 and are tested there.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.schemas.agent import AgentRequest
from app.services import sandbox
from app.services.executor import BoundedToolExecutor
from app.services.mcp_gateway import resolve_tool_mcp_server, validate_tool_output
from app.services.sandbox import ExecutionSpec, IsolationLevel, Limits, Mount, SimulatedBackend, spec_digest, validate_spec
from app.services.sandbox import conformance
from app.services.sandbox.base import BackendCapabilities
from app.workers.consumer import process_run_by_id

PINNED = "ghcr.io/acme/runner@sha256:" + "a" * 64
LIMITS = Limits(timeout_seconds=10, cpu_millis=500, memory_mb=256, pids=32, max_output_bytes=1024, max_output_files_bytes=4096)
SIM_CAPS = SimulatedBackend().capabilities()
CONTAINER_CAPS = BackendCapabilities(
    backend_id="fake-container",
    backend_version="test",
    isolation_level=IsolationLevel.CONTAINER,
    supports_network=frozenset({"none"}),
    max_limits=dict(SIM_CAPS.max_limits),
    deterministic=False,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _spec(**overrides) -> ExecutionSpec:
    spec = ExecutionSpec(
        tool_name="run_tests",
        argv=("pytest", "-q"),
        limits=LIMITS,
        image=PINNED,
        mounts=(Mount(source_ref="workspace", target="/workspace"),),
        env={"PYTHONUNBUFFERED": "1"},
    )
    return replace(spec, **overrides)


# ---------------------------------------------------------------------------
# D19: validate_spec
# ---------------------------------------------------------------------------


def test_v160_valid_spec_passes_at_simulated_and_container_levels():
    assert validate_spec(_spec(), SIM_CAPS) == []
    assert validate_spec(_spec(), CONTAINER_CAPS) == []
    assert validate_spec(_spec(mounts=(Mount(source_ref="artifact:sha256:" + "b" * 64, target="/inputs/data"),)), CONTAINER_CAPS) == []
    assert validate_spec(_spec(image="localhost:5000/team/runner@sha256:" + "d" * 64), CONTAINER_CAPS) == []  # registry port
    assert validate_spec(_spec(image=""), SIM_CAPS) == []  # no image needed when nothing is executed


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"argv": "pytest -q && curl evil"}, "argv_shell_string"),
        ({"argv": ()}, "argv_required"),
        ({"argv": ("pytest", 3)}, "argv_invalid"),
        ({"tool_name": " "}, "tool_name_required"),
        ({"image": "python:3.12"}, "image_not_digest_pinned"),
        ({"image": "python:3.12@sha256:" + "a" * 64}, "image_not_digest_pinned"),
        ({"image": "ghcr.io/acme/runner@sha256:abc"}, "image_not_digest_pinned"),
        ({"image": "python:3@sha256:" + "a" * 64}, "image_not_digest_pinned"),
        ({"image": ""}, "image_not_digest_pinned"),
        ({"workdir": "workspace"}, "workdir_invalid"),
        ({"workdir": "/workspace/../etc"}, "workdir_invalid"),
        ({"mounts": (Mount(source_ref="/var/run/docker.sock", target="/workspace/sock"),)}, "mount_source_not_allowed"),
        ({"mounts": (Mount(source_ref="C:\\Users\\me", target="/workspace/home"),)}, "mount_source_not_allowed"),
        ({"mounts": (Mount(source_ref="workspace", target="/var/run/docker.sock"),)}, "mount_target_forbidden"),
        ({"mounts": (Mount(source_ref="workspace", target="/proc/self"),)}, "mount_target_forbidden"),
        ({"mounts": (Mount(source_ref="workspace", target="/sys"),)}, "mount_target_forbidden"),
        ({"mounts": (Mount(source_ref="workspace", target="relative/path"),)}, "mount_target_invalid"),
        ({"network": "host"}, "network_invalid"),
        ({"network": "egress_proxy"}, "network_unsupported"),
        ({"env": {"lower_case": "1"}}, "env_name_invalid"),
        ({"env": {"OPENAI_API_KEY": "x"}}, "env_secret_name"),
        ({"env": {"CLIENT_SECRET_VALUE": "x"}}, "env_secret_name"),
        ({"env": {"GITHUB_TOKEN": "x"}}, "env_secret_name"),
        ({"env": {"DBPASSWORD": "x"}}, "env_secret_name"),
        ({"env": {"DEBUG": 1}}, "env_value_invalid"),
        ({"limits": None}, "limits_required"),
        ({"limits": replace(LIMITS, timeout_seconds=0)}, "limit_timeout_seconds_required"),
        ({"limits": replace(LIMITS, pids=-1)}, "limit_pids_required"),
        ({"limits": replace(LIMITS, memory_mb=True)}, "limit_memory_mb_required"),
        ({"limits": replace(LIMITS, timeout_seconds=100_000)}, "limit_timeout_seconds_exceeds_max"),
        ({"limits": replace(LIMITS, max_output_bytes=10**9)}, "limit_max_output_bytes_exceeds_max"),
    ],
)
def test_v160_validate_spec_rejects(overrides, reason):
    assert reason in validate_spec(_spec(**overrides), CONTAINER_CAPS)


def test_v160_validate_spec_reports_every_reason_once():
    spec = _spec(argv="rm -rf /", limits=None, env={"A_TOKEN": "x", "B_TOKEN": "y"}, network="host")
    reasons = validate_spec(spec, CONTAINER_CAPS)
    assert reasons == ["argv_shell_string", "network_invalid", "env_secret_name", "limits_required"]


def test_v160_spec_digest_ignores_labels_only():
    base = _spec(labels={"run_id": "r1", "tenant_id": "t1", "trace_id": "x"})
    assert spec_digest(base) == spec_digest(replace(base, labels={"run_id": "r2"})) and len(spec_digest(base)) == 64
    changed = [
        replace(base, argv=("pytest",)),
        replace(base, image="ghcr.io/acme/runner@sha256:" + "c" * 64),
        replace(base, env={}),
        replace(base, network="egress_proxy"),
        replace(base, limits=replace(LIMITS, memory_mb=257)),
        replace(base, mounts=(Mount(source_ref="workspace", target="/workspace", read_only=False),)),
        replace(base, simulation={"stdout": "x"}),
    ]
    assert len({spec_digest(item) for item in changed} | {spec_digest(base)}) == len(changed) + 1


# ---------------------------------------------------------------------------
# D20 / D23: simulated backend
# ---------------------------------------------------------------------------


def test_v160_simulated_backend_result_shapes():
    backend = SimulatedBackend()
    ok = backend.run(_spec(simulation={"stdout": "3 passed", "output_files": [{"path": "junit.xml", "content": "<x/>"}]}))
    assert (ok.status, ok.exit_code, ok.stdout, ok.truncated) == ("ok", 0, "3 passed", False)
    assert ok.stdout_sha256 == hashlib.sha256(b"3 passed").hexdigest()
    assert ok.output_files[0].sha256 == hashlib.sha256(b"<x/>").hexdigest() and ok.output_files[0].bytes == 4
    assert (ok.backend_id, ok.isolation_level, ok.started_at, ok.finished_at) == ("simulated", "simulated", None, None)
    assert ok.to_dict()["output_files"] == [{"path": "junit.xml", "sha256": ok.output_files[0].sha256, "bytes": 4}]
    assert backend.run(_spec(simulation={"exit_code": 2, "stderr": "boom"})).status == "nonzero_exit"
    assert backend.run(_spec(simulation={"duration_ms": 10_001})).status == "timeout"
    assert backend.run(_spec(simulation={"duration_ms": 10_000})).status == "ok"
    assert backend.run(_spec(simulation={"oom": True})).status == "oom"
    big = backend.run(_spec(simulation={"output_files": [{"path": "big.bin", "content": "x" * 5000}]}))
    assert big.status == "ok" and big.truncated and big.output_files == ()
    rejected = backend.run(_spec(network="egress_proxy", simulation="not a dict"))
    assert rejected.status == "rejected" and set(rejected.rejection_reasons) == {"network_unsupported", "simulation_invalid"}
    assert rejected.stdout == "" and rejected.exit_code is None


def test_v160_get_backend_default_and_unknown_backend_fails_closed():
    backend = sandbox.get_backend(_settings(sandbox_max_timeout_seconds=30, sandbox_max_output_bytes=2048))
    caps = backend.capabilities()
    assert isinstance(backend, sandbox.SandboxBackend)
    assert (caps.backend_id, caps.isolation_level, caps.deterministic) == ("simulated", IsolationLevel.SIMULATED, True)
    assert caps.supports_network == frozenset({"none"})
    assert caps.max_limits["timeout_seconds"] == 30 and caps.max_limits["max_output_bytes"] == 2048
    assert caps.to_dict()["isolation_level"] == "simulated"
    with pytest.raises(sandbox.SandboxConfigError):
        sandbox.get_backend(_settings(sandbox_backend="docker"))


# ---------------------------------------------------------------------------
# D24: conformance suite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", conformance.CASES, ids=lambda case: case.__name__)
def test_v160_simulated_backend_passes_conformance(case):
    result = case(SimulatedBackend)
    assert result.passed, result.detail
    assert result.name == case.__name__


def test_v160_conformance_case_list_and_smoke():
    assert conformance.CASE_NAMES == (
        "rejects_invalid_spec",
        "timeout_enforced",
        "output_truncated_with_digest",
        "output_file_digests",
        "network_none_default",
        "unsupported_network_rejected",
        "limits_required",
        "deterministic_same_spec",
        "result_identifies_backend",
        "env_secret_names_rejected",
    )
    assert set(conformance.SMOKE_CASES) <= set(conformance.CASE_NAMES)
    smoke = conformance.run_smoke(SimulatedBackend)
    assert smoke["passed"] is True and [c["name"] for c in smoke["cases"]] == list(conformance.SMOKE_CASES)


def test_v160_conformance_detects_a_broken_backend():
    class AcceptsEverything(SimulatedBackend):
        """Skips validation and ignores limits: must fail the suite."""

        def run(self, spec):
            return self._result("ok", spec_digest(spec), exit_code=0, stdout="x" * 1000)

    class Crashes(SimulatedBackend):
        def run(self, spec):
            raise RuntimeError("backend down")

    failed = {item.name for item in conformance.run_cases(AcceptsEverything) if not item.passed}
    assert {"rejects_invalid_spec", "timeout_enforced", "output_truncated_with_digest", "limits_required", "env_secret_names_rejected"} <= failed
    crashed = conformance.run_cases(Crashes)
    assert all(not item.passed and item.detail == "raised RuntimeError" for item in crashed) and len(crashed) == len(conformance.CASES)
    assert conformance.run_smoke(Crashes)["passed"] is False


# ---------------------------------------------------------------------------
# D21: isolation policy
# ---------------------------------------------------------------------------


def test_v160_required_isolation_by_risk_and_tool_override():
    settings = _settings()
    assert sandbox.required_isolation(0.10, None, settings) == IsolationLevel.CONTAINER
    assert sandbox.required_isolation(0.65, None, settings) == IsolationLevel.GVISOR  # DEFAULT_RISK_THRESHOLD
    assert sandbox.required_isolation(None, None, settings) == IsolationLevel.GVISOR  # unknown risk -> high
    assert sandbox.required_isolation(0.10, {"metadata": {"min_isolation": "microvm"}}, settings) == IsolationLevel.MICROVM
    assert sandbox.required_isolation(0.90, {"min_isolation": "process"}, settings) == IsolationLevel.GVISOR  # never lowers
    assert sandbox.required_isolation(0.10, {"min_isolation": "typo"}, settings) == IsolationLevel.MICROVM
    custom = _settings(sandbox_high_risk_threshold=0.3, sandbox_min_isolation_default="process", sandbox_min_isolation_high_risk="microvm")
    assert sandbox.required_isolation(0.29, None, custom) == IsolationLevel.PROCESS
    assert sandbox.required_isolation(0.30, None, custom) == IsolationLevel.MICROVM
    typo = _settings(sandbox_min_isolation_default="contaner", sandbox_min_isolation_high_risk="")
    assert sandbox.required_isolation(0.1, None, typo) == IsolationLevel.CONTAINER
    assert sandbox.required_isolation(0.9, None, typo) == IsolationLevel.GVISOR


def test_v160_isolation_levels_are_ordered_and_satisfaction_labels():
    assert [level.label for level in sorted(IsolationLevel)] == ["simulated", "process", "container", "gvisor", "microvm"]
    assert sandbox.isolation_satisfied("simulated", IsolationLevel.CONTAINER) == "n/a"
    assert sandbox.isolation_satisfied("container", IsolationLevel.CONTAINER) == "yes"
    assert sandbox.isolation_satisfied(IsolationLevel.MICROVM, IsolationLevel.GVISOR) == "yes"
    assert sandbox.isolation_satisfied("process", IsolationLevel.CONTAINER) == "no"
    with pytest.raises(ValueError):
        IsolationLevel.parse("vm")


# ---------------------------------------------------------------------------
# D22 / D23: executor wiring keeps the v1.5.12 transcript
# ---------------------------------------------------------------------------


def _code_fix_request() -> AgentRequest:
    return AgentRequest(
        user_id="u1",
        tenant_id="t1",
        task_type="code_fix",
        prompt="Fix the failing unit test.",
        goal="Make the parser test pass",
        allowed_tools=["read_repo", "write_patch", "deploy_service"],
        constraints={"no_external_network": True, "max_tool_calls": 4},
    )


def test_v160_executor_adds_sandbox_execution_and_keeps_v1512_transcript():
    request = _code_fix_request()
    result = BoundedToolExecutor().execute(request, risk_score=0.2, run_id="run_1")
    server = resolve_tool_mcp_server(None)
    policy = {
        "network": "blocked",
        "filesystem": "bounded_workspace",
        "process": "no_child_processes",
        "secret": "redacted",
        "timeout_seconds": 60,
        "resource_limit": {"max_tool_calls": 3},
        "cost_limit": {"max_units": 3},
        "mcp_server_id": server,
    }
    meta = {"mcp_server_id": server, "mcp_transport": "", "mcp_trust_tier": ""}

    def simulated(tool: str) -> dict:
        output = {"status": "simulated", "summary": f"Bounded executor simulated {tool} via {server} without external side effects."}
        schema_status, _ = validate_tool_output(output, None)
        return {
            "tool_name": tool,
            "status": "simulated",
            "detail": "Bounded v0.3 executor recorded the tool action without external side effects.",
            "sandbox_policy": {**policy, "schema_validation": schema_status},
            "artifact_refs": [],
            "schema_validation": schema_status,
            **meta,
        }

    blocked = {
        "tool_name": "deploy_service",
        "status": "blocked",
        "detail": "Tool is not allowed for task type code_fix.",
        "sandbox_policy": policy,
        "artifact_refs": [],
        **meta,
    }
    # Every v1.5.12 field is byte-for-byte unchanged; only ``sandbox_execution`` is new, and only on executed tools.
    stripped = [{key: value for key, value in entry.items() if key != "sandbox_execution"} for entry in result.tool_transcript]
    assert stripped == [simulated("read_repo"), simulated("write_patch"), blocked]
    assert result.output == (
        "Executed code_fix with 3 bounded tool transcript entries. Goal: Make the parser test pass. "
        "No destructive or external-network actions were used."
    )
    assert "sandbox_execution" not in result.tool_transcript[2]
    first, second = (entry["sandbox_execution"] for entry in result.tool_transcript[:2])
    assert set(first) == {
        "backend_id", "backend_version", "isolation_level", "status", "spec_digest", "output_sha256", "required_isolation", "isolation_satisfied",
    }
    assert (first["backend_id"], first["isolation_level"], first["status"]) == ("simulated", "simulated", "ok")
    assert (first["required_isolation"], first["isolation_satisfied"]) == ("container", "n/a")
    expected_stdout = json.dumps(
        {"status": "simulated", "summary": f"Bounded executor simulated read_repo via {server} without external side effects."}, sort_keys=True
    )
    assert first["output_sha256"] == hashlib.sha256(expected_stdout.encode()).hexdigest()
    assert first["spec_digest"] != second["spec_digest"] and len(first["spec_digest"]) == 64


def test_v160_executor_required_isolation_follows_risk_and_digest_ignores_run():
    executor = BoundedToolExecutor()
    low = executor.execute(_code_fix_request(), risk_score=0.2, run_id="run_a").tool_transcript[0]["sandbox_execution"]
    high = executor.execute(_code_fix_request(), risk_score=0.9, run_id="run_b").tool_transcript[0]["sandbox_execution"]
    unknown = executor.execute(_code_fix_request()).tool_transcript[0]["sandbox_execution"]
    assert (low["required_isolation"], high["required_isolation"], unknown["required_isolation"]) == ("container", "gvisor", "gvisor")
    assert low["isolation_satisfied"] == high["isolation_satisfied"] == "n/a"
    assert low["spec_digest"] == high["spec_digest"]  # labels (run_id) are not part of the digest
    tool_map = {"read_repo": {"status": "active", "task_types": ["code_fix"], "metadata": {"min_isolation": "microvm"}}}
    override = executor.execute(_code_fix_request(), allowed_tool_map=tool_map, risk_score=0.2).tool_transcript[0]["sandbox_execution"]
    assert override["required_isolation"] == "microvm"


def test_v160_worker_run_records_sandbox_execution_without_raising_provenance(client, low_risk_payload, session_factory):
    run_id = client.post("/v1/agent/submit", json=low_risk_payload).json()["run_id"]

    async def work():
        async with session_factory() as session:
            await process_run_by_id(session, run_id=run_id, state_store=client.state_store, event_publisher=client.event_publisher)
        async with session_factory() as session:
            return (await session.execute(select(AgentRun).where(AgentRun.run_id == run_id))).scalar_one()

    run = asyncio.run(work())
    transcript = run.evaluation_summary["execution_event"]["payload"]["tool_transcript"]
    assert run.status == "completed" and transcript[0]["status"] == "simulated"
    execution = transcript[0]["sandbox_execution"]
    assert (execution["backend_id"], execution["isolation_level"], execution["isolation_satisfied"]) == ("simulated", "simulated", "n/a")
    assert execution["required_isolation"] in {"container", "gvisor"}
    # D22: the simulated backend executes nothing, so no evidence level is raised anywhere in the run record.
    blob = json.dumps([run.evaluation_summary, run.result], default=str)
    assert "platform_verified" not in blob and "ci_attested" not in blob
    rows = client.get(f"/v1/runs/{run_id}/tool-executions").json()
    assert rows[0]["status"] == "simulated" and "sandbox_execution" not in rows[0]["sandbox_policy"]
