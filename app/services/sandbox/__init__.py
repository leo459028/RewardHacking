"""Sandbox execution spec, backend contract and conformance suite (v1.6.0 "Runtime Foundations", v1.6.2 runtime).

Agent-run tools always use the ``simulated`` backend, which executes nothing. The Docker backend (v1.6.2) plugs in
behind ``SandboxBackend``, exists only inside ``sandbox-runner`` and must pass ``conformance.CASES`` and
``conformance.HARDENING_CASES``. This package must not import ``app.api``, ``app.ui`` or ``app.workers``.
"""

from app.services.sandbox.base import BackendCapabilities, ExecutionResult, IsolationLevel, OutputFile, SandboxBackend
from app.services.sandbox.policy import isolation_satisfied, required_isolation
from app.services.sandbox.registry import SandboxConfigError, get_backend, get_tool_backend
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
    "get_tool_backend",
    "isolation_satisfied",
    "required_isolation",
    "spec_digest",
    "validate_spec",
]
