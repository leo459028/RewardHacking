"""Sandbox execution spec, backend contract and conformance suite (v1.6.0 "Runtime Foundations").

This release ships the specification and a ``simulated`` backend only: nothing is executed, and evidence
provenance is unchanged. The Docker backend (v1.6.1) plugs in behind ``SandboxBackend`` and must pass
``conformance.CASES``. This package must not import ``app.api``, ``app.ui`` or ``app.workers``.
"""

from app.services.sandbox.base import BackendCapabilities, ExecutionResult, IsolationLevel, OutputFile, SandboxBackend
from app.services.sandbox.policy import isolation_satisfied, required_isolation
from app.services.sandbox.registry import SandboxConfigError, get_backend
from app.services.sandbox.simulated import SimulatedBackend
from app.services.sandbox.spec import ExecutionSpec, Limits, Mount, spec_digest, validate_spec

__all__ = [
    "BackendCapabilities",
    "ExecutionResult",
    "ExecutionSpec",
    "IsolationLevel",
    "Limits",
    "Mount",
    "OutputFile",
    "SandboxBackend",
    "SandboxConfigError",
    "SimulatedBackend",
    "get_backend",
    "isolation_satisfied",
    "required_isolation",
    "spec_digest",
    "validate_spec",
]
