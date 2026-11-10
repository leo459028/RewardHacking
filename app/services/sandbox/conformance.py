"""Reusable sandbox conformance cases (v1.6.0 D24). No pytest dependency.

Every case takes a backend factory and returns a ``CaseResult``. A backend must pass **all** cases before it
may be enabled (the v1.6.1 Docker backend included). ``SMOKE_CASES`` is the fast subset for health checks.

Specs carry both a real ``argv`` and a ``simulation`` block, so the same case drives a real backend (which
ignores ``simulation``) and the simulated one (which ignores ``argv``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import Any, Callable

from app.services.sandbox.base import RESULT_STATUSES, BackendCapabilities, SandboxBackend
from app.services.sandbox.spec import ExecutionSpec, Limits, Mount, spec_digest

BackendFactory = Callable[[], SandboxBackend]
# Digest-pinned so the same spec is valid at every isolation level (the digest itself is a placeholder).
CONFORMANCE_IMAGE = "registry.local/rh/conformance@sha256:" + "0" * 64


@dataclass(frozen=True)
class CaseResult:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def base_spec(caps: BackendCapabilities, **overrides: Any) -> ExecutionSpec:
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
        image=CONFORMANCE_IMAGE,
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


def rejects_invalid_spec(factory: BackendFactory) -> CaseResult:
    backend = factory()
    return _rejected("rejects_invalid_spec", backend, base_spec(backend.capabilities(), argv="echo ok; rm -rf /"), "argv_shell_string")


def timeout_enforced(factory: BackendFactory) -> CaseResult:
    backend = factory()
    spec = base_spec(backend.capabilities())
    spec = replace(spec, argv=("sleep", "30"), limits=replace(spec.limits, timeout_seconds=1), simulation={"duration_ms": 30_000})
    result = backend.run(spec)
    return _check("timeout_enforced", result.status == "timeout", f"expected timeout, got {result.status}")


def output_truncated_with_digest(factory: BackendFactory) -> CaseResult:
    backend = factory()
    payload = "x" * 1000
    spec = base_spec(backend.capabilities())
    spec = replace(spec, argv=("printf", payload), limits=replace(spec.limits, max_output_bytes=64), simulation={"stdout": payload})
    result = backend.run(spec)
    ok = (
        result.status == "ok"
        and result.truncated
        and len(result.stdout.encode("utf-8")) <= 64
        and result.stdout_sha256 == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    )
    return _check("output_truncated_with_digest", ok, f"status={result.status} truncated={result.truncated} len={len(result.stdout)}")


def output_file_digests(factory: BackendFactory) -> CaseResult:
    backend = factory()
    content = "report-1\n"
    spec = base_spec(
        backend.capabilities(),
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


def network_none_default(factory: BackendFactory) -> CaseResult:
    backend = factory()
    spec = base_spec(backend.capabilities())
    ok = spec.network == "none" and "none" in backend.capabilities().supports_network and backend.run(spec).status == "ok"
    return _check("network_none_default", ok, "the default spec (network none) must be supported and run")


def unsupported_network_rejected(factory: BackendFactory) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    invalid = _rejected("unsupported_network_rejected", backend, base_spec(caps, network="host"), "network_invalid")
    if not invalid.passed or "egress_proxy" in caps.supports_network:
        return invalid
    return _rejected("unsupported_network_rejected", backend, base_spec(caps, network="egress_proxy"), "network_unsupported")


def limits_required(factory: BackendFactory) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    missing = _rejected("limits_required", backend, base_spec(caps, limits=None), "limits_required")
    if not missing.passed:
        return missing
    spec = base_spec(caps)
    return _rejected("limits_required", backend, replace(spec, limits=replace(spec.limits, memory_mb=0)), "limit_memory_mb_required")


def deterministic_same_spec(factory: BackendFactory) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    spec = base_spec(caps)
    first, second = backend.run(spec), factory().run(replace(spec, labels={"run_id": "other"}))
    same_digest = first.spec_digest == second.spec_digest == spec_digest(spec)
    if not caps.deterministic:
        return _check("deterministic_same_spec", same_digest, "spec_digest must not depend on labels")
    return _check("deterministic_same_spec", same_digest and first == second, "same spec_digest must give the same result")


def result_identifies_backend(factory: BackendFactory) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    spec = base_spec(caps)
    result = backend.run(spec)
    ok = (
        result.status in RESULT_STATUSES
        and result.backend_id == caps.backend_id
        and result.backend_version == caps.backend_version
        and result.isolation_level == caps.isolation_level.label
        and result.spec_digest == spec_digest(spec)
    )
    return _check("result_identifies_backend", ok, f"{result.backend_id}/{result.isolation_level}/{result.status}")


def env_secret_names_rejected(factory: BackendFactory) -> CaseResult:
    backend = factory()
    caps = backend.capabilities()
    for name in ("OPENAI_API_KEY", "DB_PASSWORD", "GITHUB_TOKEN", "CLIENT_SECRET"):
        result = _rejected("env_secret_names_rejected", backend, base_spec(caps, env={name: "x"}), "env_secret_name")
        if not result.passed:
            return result
    return CaseResult("env_secret_names_rejected", True)


CASES: tuple[Callable[[BackendFactory], CaseResult], ...] = (
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


def run_cases(factory: BackendFactory, names: tuple[str, ...] | None = None) -> list[CaseResult]:
    """Run the named cases (default: all). A case that raises is reported as failed, never propagated."""
    results = []
    for case in CASES:
        if names is not None and case.__name__ not in names:
            continue
        try:
            results.append(case(factory))
        except Exception as exc:  # noqa: BLE001 - a crashing backend fails the case
            results.append(CaseResult(case.__name__, False, f"raised {type(exc).__name__}"))
    return results


def run_smoke(factory: BackendFactory) -> dict[str, Any]:
    results = run_cases(factory, SMOKE_CASES)
    return {"passed": all(item.passed for item in results), "cases": [item.to_dict() for item in results]}
