"""``SimulatedBackend`` (v1.6.0 D23): deterministic, executes nothing, never sleeps.

It validates the spec like a real backend and answers from ``spec.simulation``:
``stdout`` / ``stderr`` (str), ``exit_code`` (int), ``duration_ms`` (int), ``oom`` (bool) and
``output_files`` (``[{"path": ..., "content": ...}]``). The same ``spec_digest`` always gives the same result.
Its isolation level is ``simulated`` and it never produces ``platform_verified`` evidence (D22).
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.services.sandbox.base import BackendCapabilities, ExecutionResult, IsolationLevel, OutputFile
from app.services.sandbox.spec import ExecutionSpec, spec_digest, validate_spec

BACKEND_ID = "simulated"
BACKEND_VERSION = "v1.6.0"
DEFAULT_MAX_LIMITS = {
    "timeout_seconds": 300,
    "cpu_millis": 4000,
    "memory_mb": 4096,
    "pids": 512,
    "max_output_bytes": 1_048_576,
    "max_output_files_bytes": 16_777_216,
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _truncate(text: str, limit: int) -> tuple[str, bool, str]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False, _sha(raw)
    return raw[:limit].decode("utf-8", errors="ignore"), True, _sha(raw)


class SimulatedBackend:
    def __init__(self, max_limits: dict[str, int] | None = None) -> None:
        self._caps = BackendCapabilities(
            backend_id=BACKEND_ID,
            backend_version=BACKEND_VERSION,
            isolation_level=IsolationLevel.SIMULATED,
            supports_network=frozenset({"none"}),
            max_limits={**DEFAULT_MAX_LIMITS, **(max_limits or {})},
            deterministic=True,
        )

    def capabilities(self) -> BackendCapabilities:
        return self._caps

    def _result(self, status: str, digest: str, **fields: Any) -> ExecutionResult:
        return ExecutionResult(
            status=status,
            backend_id=BACKEND_ID,
            backend_version=BACKEND_VERSION,
            isolation_level=IsolationLevel.SIMULATED.label,
            spec_digest=digest,
            **fields,
        )

    def run(self, spec: ExecutionSpec) -> ExecutionResult:
        digest = spec_digest(spec)
        reasons = validate_spec(spec, self._caps)
        sim = spec.simulation if spec.simulation is not None else {}
        if not isinstance(sim, dict):
            reasons.append("simulation_invalid")
        if reasons:
            return self._result("rejected", digest, rejection_reasons=tuple(reasons))

        limits = spec.limits
        duration_ms = int(sim.get("duration_ms", 0) or 0)
        usage = {"wall_ms": min(duration_ms, limits.timeout_seconds * 1000), "cpu_ms": None, "max_rss_mb": None}
        if duration_ms > limits.timeout_seconds * 1000:
            return self._result("timeout", digest, usage=usage)
        if sim.get("oom"):
            return self._result("oom", digest, exit_code=137, usage=usage)

        stdout, cut_out, stdout_sha = _truncate(str(sim.get("stdout", f"simulated {spec.tool_name}")), limits.max_output_bytes)
        stderr, cut_err, stderr_sha = _truncate(str(sim.get("stderr", "")), limits.max_output_bytes)
        files: list[OutputFile] = []
        budget = limits.max_output_files_bytes
        dropped = False
        for item in sorted(sim.get("output_files") or [], key=lambda entry: str(entry.get("path", ""))):
            content = str(item.get("content", "")).encode("utf-8")
            if len(content) > budget:
                dropped = True
                continue
            budget -= len(content)
            files.append(OutputFile(path=str(item.get("path", "")), sha256=_sha(content), bytes=len(content)))
        exit_code = int(sim.get("exit_code", 0) or 0)
        return self._result(
            "ok" if exit_code == 0 else "nonzero_exit",
            digest,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=cut_out or cut_err or dropped,
            stdout_sha256=stdout_sha,
            stderr_sha256=stderr_sha,
            output_files=tuple(files),
            usage=usage,
        )
