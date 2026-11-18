"""Reusable sandbox conformance cases (v1.6.0 D24, v1.6.2 D15). No pytest dependency.

Every case takes a backend factory and returns a ``CaseResult``. A backend must pass **all** cases before it
may be enabled. ``SMOKE_CASES`` is the fast subset for health checks.

Specs carry both a real ``argv`` and a ``simulation`` block, so the same case drives a real backend (which
ignores ``simulation``) and the simulated one (which ignores ``argv``). ``CONFORMANCE_IMAGE`` is a placeholder
digest: a backend that really runs containers must pass ``image=`` (a digest-pinned image that has ``echo``,
``sleep``, ``printf``, ``sh``, ``cat``, ``id``, ``nc`` and ``nslookup``) to ``run_cases`` / ``run_smoke`` or to each
case.

``HARDENING_CASES`` prove real isolation, so they apply only to backends at isolation ``container`` or above; a lower
backend gets ``not_applicable``. Each case prints a marker first, so a container that never started cannot pass.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from dataclasses import dataclass, replace
from typing import Any, Callable

from app.services.sandbox.base import RESULT_STATUSES, BackendCapabilities, ExecutionResult, IsolationLevel, SandboxBackend
from app.services.sandbox.spec import ExecutionSpec, Limits, Mount, spec_digest

BackendFactory = Callable[[], SandboxBackend]
Case = Callable[..., "CaseResult"]
# Digest-pinned so the same spec is valid at every isolation level (the digest itself is a placeholder).
CONFORMANCE_IMAGE = "registry.local/rh/conformance@sha256:" + "0" * 64


@dataclass(frozen=True)
class CaseResult:
    name: str
    passed: bool
    detail: str = ""
    not_applicable: bool = False

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "passed": self.passed, "detail": self.detail}
        if self.not_applicable:
            data["not_applicable"] = True
        return data


def base_spec(caps: BackendCapabilities, *, image: str = CONFORMANCE_IMAGE, **overrides: Any) -> ExecutionSpec:
    cap = caps.max_limits
    limits = Limits(
        timeout_seconds=min(5, cap.get("timeout_seconds", 5)),
        cpu_millis=min(500, cap.get("cpu_millis", 500)),
        memory_mb=min(128, cap.get("memory_mb", 128)),
        pids=min(32, cap.get("pids", 32)),
        max_output_bytes=min(4096, cap.get("max_output_bytes", 4096)),
        max_output_files_bytes=min(65536, cap.get("max_output_files_bytes", 65536)),
    )
    spec = ExecutionSpec(
        tool_name="conformance",
        argv=("echo", "ok"),
        limits=limits,
        image=image,
        mounts=(Mount(source_ref="workspace", target="/workspace"),),
        labels={"run_id": "conformance"},
        simulation={"stdout": "ok\n"},
    )
    return replace(spec, **overrides)


def _check(name: str, condition: bool, detail: str) -> CaseResult:
    return CaseResult(name, bool(condition), "" if condition else detail)


def _rejected(name: str, backend: SandboxBackend, spec: ExecutionSpec, reason: str) -> CaseResult:
    result = backend.run(spec)
    ok = result.status == "rejected" and reason in result.rejection_reasons
    return _check(name, ok, f"expected rejected with {reason}, got {result.status} {list(result.rejection_reasons)}")


def rejects_invalid_spec(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    return _rejected("rejects_invalid_spec", backend, base_spec(backend.capabilities(), image=image, argv="echo ok; rm -rf /"), "argv_shell_string")


def timeout_enforced(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    spec = base_spec(backend.capabilities(), image=image)
    spec = replace(spec, argv=("sleep", "30"), limits=replace(spec.limits, timeout_seconds=1), simulation={"duration_ms": 30_000})
    result = backend.run(spec)
    return _check("timeout_enforced", result.status == "timeout", f"expected timeout, got {result.status}")


def output_truncated_with_digest(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    payload = "x" * 1000
    spec = base_spec(backend.capabilities(), image=image)
    spec = replace(spec, argv=("printf", payload), limits=replace(spec.limits, max_output_bytes=64), simulation={"stdout": payload})
    result = backend.run(spec)
    ok = (
        result.status == "ok"
        and result.truncated
        and len(result.stdout.encode("utf-8")) <= 64
        and result.stdout_sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    )
    return _check("output_truncated_with_digest", ok, f"status={result.status} truncated={result.truncated} len={len(result.stdout)}")


def output_file_digests(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    content = "report-1\n"
    spec = base_spec(
        backend.capabilities(),
        image=image,
        argv=("sh", "-c", "printf 'report-1\\n' > /workspace/out/report.txt"),
        mounts=(Mount(source_ref="workspace", target="/workspace", read_only=False),),
        simulation={"output_files": [{"path": "out/report.txt", "content": content}]},
    )
    result = backend.run(spec)
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    found = {item.path: item for item in result.output_files}
    item = found.get("out/report.txt")
    ok = result.status == "ok" and item is not None and item.sha256 == expected and item.bytes == len(content)
    return _check("output_file_digests", ok, f"status={result.status} files={sorted(found)}")


def network_none_default(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    spec = base_spec(backend.capabilities(), image=image)
    ok = spec.network == "none" and "none" in backend.capabilities().supports_network and backend.run(spec).status == "ok"
    return _check("network_none_default", ok, "the default spec (network none) must be supported and run")


def unsupported_network_rejected(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    invalid = _rejected("unsupported_network_rejected", backend, base_spec(caps, image=image, network="host"), "network_invalid")
    if not invalid.passed or "egress_proxy" in caps.supports_network:
        return invalid
    return _rejected("unsupported_network_rejected", backend, base_spec(caps, image=image, network="egress_proxy"), "network_unsupported")


def limits_required(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    missing = _rejected("limits_required", backend, base_spec(caps, image=image, limits=None), "limits_required")
    if not missing.passed:
        return missing
    spec = base_spec(caps, image=image)
    return _rejected("limits_required", backend, replace(spec, limits=replace(spec.limits, memory_mb=0)), "limit_memory_mb_required")


def deterministic_same_spec(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    spec = base_spec(caps, image=image)
    first, second = backend.run(spec), factory().run(replace(spec, labels={"run_id": "other"}))
    same_digest = first.spec_digest == second.spec_digest == spec_digest(spec)
    if not caps.deterministic:
        return _check("deterministic_same_spec", same_digest, "spec_digest must not depend on labels")
    return _check("deterministic_same_spec", same_digest and first == second, "same spec_digest must give the same result")


def result_identifies_backend(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    spec = base_spec(caps, image=image)
    result = backend.run(spec)
    ok = (
        result.status in RESULT_STATUSES
        and result.backend_id == caps.backend_id
        and result.backend_version == caps.backend_version
        and result.isolation_level == caps.isolation_level.label
        and result.spec_digest == spec_digest(spec)
    )
    return _check("result_identifies_backend", ok, f"{result.backend_id}/{result.isolation_level}/{result.status}")


def env_secret_names_rejected(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    for name in ("OPENAI_API_KEY", "DB_PASSWORD", "GITHUB_TOKEN", "CLIENT_SECRET"):
        result = _rejected("env_secret_names_rejected", backend, base_spec(caps, image=image, env={name: "x"}), "env_secret_name")
        if not result.passed:
            return result
    return CaseResult("env_secret_names_rejected", True)


CASES: tuple[Case, ...] = (
    rejects_invalid_spec,
    timeout_enforced,
    output_truncated_with_digest,
    output_file_digests,
    network_none_default,
    unsupported_network_rejected,
    limits_required,
    deterministic_same_spec,
    result_identifies_backend,
    env_secret_names_rejected,
)
CASE_NAMES = tuple(case.__name__ for case in CASES)
SMOKE_CASES = ("rejects_invalid_spec", "network_none_default", "limits_required", "result_identifies_backend")


def run_cases(
    factory: BackendFactory, names: tuple[str, ...] | None = None, *, image: str = CONFORMANCE_IMAGE
) -> list[CaseResult]:
    """Run the named cases (default: all). A case that raises is reported as failed, never propagated."""
    results = []
    for case in CASES:
        if names is not None and case.__name__ not in names:
            continue
        try:
            results.append(case(factory, image))
        except Exception as exc:  # noqa: BLE001 - a crashing backend fails the case
            results.append(CaseResult(case.__name__, False, f"raised {type(exc).__name__}"))
    return results


def run_smoke(factory: BackendFactory, *, image: str = CONFORMANCE_IMAGE) -> dict[str, Any]:
    results = run_cases(factory, SMOKE_CASES, image=image)
    return {"passed": all(item.passed for item in results), "cases": [item.to_dict() for item in results]}


# ---------------------------------------------------------------------------
# v1.6.2 D15: hardening cases (real isolation, backends at ``container`` or above)
# ---------------------------------------------------------------------------

STARTED = "rh-started"
_CAP_EFF = re.compile(r"^CapEff:\s*([0-9a-fA-F]+)\s*$", re.MULTILINE)
_NO_NEW_PRIVS = re.compile(r"^NoNewPrivs:\s*(\d)\s*$", re.MULTILINE)
_HOST_ENV_NAMES = ("POSTGRES_DSN", "REDIS_URL", "KAFKA_BOOTSTRAP_SERVERS")
_FORK_REFUSED = re.compile(r"can't fork|resource temporarily unavailable", re.IGNORECASE)
PIDS_LIMIT = 16
PIDS_ATTEMPTS = 64
PIDS_CONTROL_LIMIT = 64
PIDS_CONTROL_COUNT = 8


def _expectations(backend: SandboxBackend) -> dict[str, str]:
    probe = getattr(backend, "hardening_expectations", None)
    return dict(probe()) if callable(probe) else {}


def _host_config(backend: SandboxBackend) -> dict[str, Any]:
    probe = getattr(backend, "last_host_config", None)
    return dict(probe() or {}) if callable(probe) else {}


def _shell(backend: SandboxBackend, image: str, script: str, *, timeout: int = 10, **limits: int) -> ExecutionResult:
    spec = base_spec(backend.capabilities(), image=image, argv=("sh", "-c", f"echo {STARTED}; {script}"), simulation={})
    cap = spec.limits
    allowed = backend.capabilities().max_limits.get("timeout_seconds", timeout)
    return backend.run(replace(spec, limits=replace(cap, timeout_seconds=min(timeout, allowed), **limits)))


def _ran(result: ExecutionResult) -> bool:
    return result.status in {"ok", "nonzero_exit"} and STARTED in result.stdout


def _describe(result: ExecutionResult) -> str:
    return (
        f"status={result.status} exit={result.exit_code} reasons={list(result.rejection_reasons)} "
        f"stdout={result.stdout[:200]!r} stderr={result.stderr[:200]!r}"
    )


def root_fs_read_only(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    result = _shell(factory(), image, "if touch /etc/rh-probe 2>/dev/null; then echo probe-writable; else echo probe-readonly; fi")
    ok = _ran(result) and "probe-readonly" in result.stdout and "probe-writable" not in result.stdout
    return _check("root_fs_read_only", ok, _describe(result))


def network_unreachable(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    script = (
        "command -v nc >/dev/null 2>&1 && command -v nslookup >/dev/null 2>&1 || echo tool-missing; "
        "if nc -w 2 1.1.1.1 53 </dev/null >/dev/null 2>&1; then echo tcp-open; else echo tcp-blocked; fi; "
        "if nslookup example.com >/dev/null 2>&1; then echo dns-open; else echo dns-blocked; fi; "
        "echo interfaces-begin; cat /proc/net/dev; echo interfaces-end"
    )
    result = _shell(factory(), image, script, timeout=20)
    section = result.stdout.partition("interfaces-begin")[2].partition("interfaces-end")[0]
    interfaces = {line.split(":", 1)[0].strip() for line in section.splitlines() if ":" in line}
    ok = (
        _ran(result)
        and "tool-missing" not in result.stdout
        and "tcp-blocked" in result.stdout
        and "dns-blocked" in result.stdout
        and "interfaces-end" in result.stdout
        and interfaces <= {"lo"}
    )
    return _check("network_unreachable", ok, f"interfaces={sorted(interfaces)} {_describe(result)}")


def runs_as_non_root(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    expected = _expectations(backend).get("uid", "")
    result = _shell(backend, image, "id -u")
    uid = result.stdout.replace(STARTED, "").strip()
    return _check("runs_as_non_root", _ran(result) and uid not in {"", "0"} and uid == expected, f"uid={uid!r} expected={expected!r}")


def capabilities_dropped(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    result = _shell(factory(), image, "cat /proc/self/status")
    match = _CAP_EFF.search(result.stdout)
    ok = _ran(result) and match is not None and int(match.group(1), 16) == 0
    return _check("capabilities_dropped", ok, f"CapEff={match.group(1) if match else None}")


def no_new_privileges(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    result = _shell(backend, image, "cat /proc/self/status")
    match = _NO_NEW_PRIVS.search(result.stdout)
    if match is not None:
        return _check("no_new_privileges", _ran(result) and match.group(1) == "1", f"NoNewPrivs={match.group(1)}")
    # gVisor's /proc does not report the flag, so the container's own configuration is the evidence there.
    host = _host_config(backend)
    ok = _ran(result) and host.get("Runtime") == "runsc" and "no-new-privileges:true" in (host.get("SecurityOpt") or [])
    return _check("no_new_privileges", ok, "NoNewPrivs is not reported and the container configuration does not prove it")


def _spawn_script(count: int) -> str:
    return (
        f"i=0; while [ $i -lt {count} ]; do sleep 8 & i=$((i+1)); echo spawned=$i; done; "
        "echo all-spawned; echo procs=$(ls /proc | grep -c '^[0-9]')"
    )


def pids_limit_enforced(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    """Needs proof from both sides: the script works with room to spare, and the limit is what stops it."""
    control = _shell(factory(), image, _spawn_script(PIDS_CONTROL_COUNT), timeout=15, pids=PIDS_CONTROL_LIMIT)
    counted = re.search(r"procs=(\d+)", control.stdout)
    if not (_ran(control) and "all-spawned" in control.stdout and counted and int(counted.group(1)) >= PIDS_CONTROL_COUNT):
        return CaseResult("pids_limit_enforced", False, f"control run did not spawn its processes: {_describe(control)}")
    limited = _shell(factory(), image, _spawn_script(PIDS_ATTEMPTS), timeout=15, pids=PIDS_LIMIT)
    spawned = [int(value) for value in re.findall(r"spawned=(\d+)", limited.stdout)]
    ok = (
        _ran(limited)
        and "all-spawned" not in limited.stdout
        and _FORK_REFUSED.search(limited.stderr) is not None
        and max(spawned, default=0) <= PIDS_LIMIT
    )
    return _check("pids_limit_enforced", ok, f"spawned={max(spawned, default=0)} {_describe(limited)}")


def memory_limit_enforced(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    # The shell must outlive dd: when the container's main process is the one killed, Docker can handle the exit first
    # and never record the OOM. The pass condition stays Docker's own report.
    script = "dd if=/dev/zero of=/dev/null bs=256M count=1; echo dd-finished; sleep 1"
    result = _shell(factory(), image, script, timeout=30, memory_mb=32)
    return _check("memory_limit_enforced", result.status == "oom" and STARTED in result.stdout, _describe(result))


def host_env_not_visible(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    name, value = "RH_SANDBOX_CONFORMANCE_CANARY", secrets.token_hex(16)
    os.environ[name] = value
    try:
        result = _shell(factory(), image, "env")
    finally:
        os.environ.pop(name, None)
    leaked = [item for item in (name, value, *_HOST_ENV_NAMES) if item in result.stdout]
    ok = _ran(result) and not result.truncated and not leaked
    return _check("host_env_not_visible", ok, f"leaked={leaked} status={result.status} truncated={result.truncated}")


def workspace_isolated(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    script = (
        "for path in /var/run/docker.sock /run/docker.sock /app/app/main.py /app/alembic.ini /app/config; do "
        "if [ -e \"$path\" ]; then echo \"visible:$path\"; fi; done; "
        "if grep -q docker.sock /proc/mounts; then echo visible:mounts; fi; echo checked-paths"
    )
    result = _shell(factory(), image, script)
    return _check("workspace_isolated", _ran(result) and "checked-paths" in result.stdout and "visible:" not in result.stdout, _describe(result))


def runtime_matches_config(factory: BackendFactory, image: str = CONFORMANCE_IMAGE) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    expected = _expectations(backend)
    result = backend.run(base_spec(caps, image=image))
    actual = _host_config(backend).get("Runtime")
    ok = (
        result.status == "ok"
        and actual == expected.get("runtime")
        and result.isolation_level == expected.get("isolation_level") == caps.isolation_level.label
    )
    return _check("runtime_matches_config", ok, f"runtime={actual!r} expected={expected} isolation={result.isolation_level}")


HARDENING_CASES: tuple[Case, ...] = (
    root_fs_read_only,
    network_unreachable,
    runs_as_non_root,
    capabilities_dropped,
    no_new_privileges,
    pids_limit_enforced,
    memory_limit_enforced,
    host_env_not_visible,
    workspace_isolated,
    runtime_matches_config,
)
HARDENING_CASE_NAMES = tuple(case.__name__ for case in HARDENING_CASES)
ALL_CASE_NAMES = CASE_NAMES + HARDENING_CASE_NAMES


def run_hardening_cases(factory: BackendFactory, *, image: str = CONFORMANCE_IMAGE) -> list[CaseResult]:
    """``not_applicable`` below isolation ``container``: nothing runs there, so nothing can be proven."""
    if factory().capabilities().isolation_level < IsolationLevel.CONTAINER:
        return [CaseResult(name, False, "backend does not execute containers", not_applicable=True) for name in HARDENING_CASE_NAMES]
    results = []
    for case in HARDENING_CASES:
        try:
            results.append(case(factory, image))
        except Exception as exc:  # noqa: BLE001 - a crashing backend fails the case
            results.append(CaseResult(case.__name__, False, f"raised {type(exc).__name__}"))
    return results


def run_full_suite(factory: BackendFactory, *, image: str) -> list[dict[str, Any]]:
    """``CASES`` then ``HARDENING_CASES``, each with its group and duration (stored on the conformance run)."""
    report: list[dict[str, Any]] = []
    for group, cases in (("base", CASES), ("hardening", HARDENING_CASES)):
        for case in cases:
            started = time.perf_counter()
            try:
                result = case(factory, image)
            except Exception as exc:  # noqa: BLE001 - a crashing backend fails the case
                result = CaseResult(case.__name__, False, f"raised {type(exc).__name__}")
            report.append({**result.to_dict(), "group": group, "duration_ms": int((time.perf_counter() - started) * 1000)})
    return report


def suite_passed(report: list[dict[str, Any]]) -> bool:
    """D14: every case of both lists must have run and passed."""
    return {item.get("name") for item in report} == set(ALL_CASE_NAMES) and all(item.get("passed") is True for item in report)
