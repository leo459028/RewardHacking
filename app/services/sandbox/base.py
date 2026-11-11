"""Sandbox backend contract (v1.6.0 D18, D20, D21).

``SandboxBackend.run`` is synchronous on purpose: the tool path (``orchestrator.execute`` ->
``executor.execute``) is synchronous, and a real backend (v1.6.1) is called through ``asyncio.to_thread``.

Isolation level and evidence provenance are two independent fields (D22): a backend never raises provenance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable


class IsolationLevel(IntEnum):
    """Ordered isolation levels. ``SIMULATED`` means nothing was executed."""

    SIMULATED = 0
    PROCESS = 1
    CONTAINER = 2
    GVISOR = 3
    MICROVM = 4

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def parse(cls, value: Any) -> "IsolationLevel":
        if isinstance(value, cls):
            return value
        try:
            return cls[str(value).strip().upper()]
        except KeyError as exc:
            raise ValueError(f"unknown isolation level: {value!r}") from exc


NETWORK_MODES = ("none", "egress_proxy")  # egress_proxy is reserved for v1.6.3
RESULT_STATUSES = ("ok", "nonzero_exit", "timeout", "oom", "rejected", "error")
LIMIT_FIELDS = ("timeout_seconds", "cpu_millis", "memory_mb", "pids", "max_output_bytes", "max_output_files_bytes")


@dataclass(frozen=True)
class BackendCapabilities:
    backend_id: str
    backend_version: str
    isolation_level: IsolationLevel
    supports_network: frozenset[str]
    max_limits: dict[str, int]
    deterministic: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "backend_version": self.backend_version,
            "isolation_level": self.isolation_level.label,
            "supports_network": sorted(self.supports_network),
            "max_limits": dict(self.max_limits),
            "deterministic": self.deterministic,
        }


@dataclass(frozen=True)
class OutputFile:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    backend_id: str
    backend_version: str
    isolation_level: str
    spec_digest: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    stdout_sha256: str = ""  # sha256 of the output before truncation
    stderr_sha256: str = ""
    output_files: tuple[OutputFile, ...] = ()
    usage: dict[str, int | None] = field(default_factory=lambda: {"wall_ms": None, "cpu_ms": None, "max_rss_mb": None})
    started_at: str | None = None  # None when nothing was executed (simulated backend)
    finished_at: str | None = None
    rejection_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_files"] = [asdict(item) for item in self.output_files]
        data["rejection_reasons"] = list(self.rejection_reasons)
        return data


@runtime_checkable
class SandboxBackend(Protocol):
    def capabilities(self) -> BackendCapabilities: ...

    def run(self, spec: Any) -> ExecutionResult: ...
