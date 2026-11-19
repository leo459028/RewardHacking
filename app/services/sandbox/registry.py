"""Backend selection (v1.6.0 D23, v1.6.2 D19).

``SANDBOX_BACKEND`` is ``simulated`` (default) or ``docker``. Only ``app.workers.sandbox_runner`` builds the Docker
backend, so the API and the worker can never reach Docker: ``get_backend`` refuses ``docker``. Agent-run tools use
``get_tool_backend``, which stays simulated in this release (plan 1.3).
"""

from __future__ import annotations

from typing import Any

from app.services.sandbox.base import SandboxBackend
from app.services.sandbox.simulated import SimulatedBackend

SIMULATED = "simulated"
DOCKER = "docker"
SUPPORTED_BACKENDS = (SIMULATED, DOCKER)


class SandboxConfigError(RuntimeError):
    """Unknown ``SANDBOX_BACKEND`` or a non-positive maximum: fail closed instead of running misconfigured."""


def configured_backend(settings: Any) -> str:
    return str(getattr(settings, "sandbox_backend", SIMULATED) or "").strip().lower()


def _simulated(settings: Any) -> SimulatedBackend:
    max_limits = {
        "timeout_seconds": int(getattr(settings, "sandbox_max_timeout_seconds", 300)),
        "max_output_bytes": int(getattr(settings, "sandbox_max_output_bytes", 1_048_576)),
    }
    bad = sorted(key for key, value in max_limits.items() if value <= 0)
    if bad:
        # Otherwise every tool would be silently "rejected" while the transcript still reads "simulated".
        raise SandboxConfigError(f"Sandbox maximums must be greater than 0: {', '.join(bad)}.")
    return SimulatedBackend(max_limits=max_limits)


def _unsupported(name: str) -> SandboxConfigError:
    return SandboxConfigError(f"Unsupported SANDBOX_BACKEND {name!r}; supported: {', '.join(SUPPORTED_BACKENDS)}.")


def get_backend(settings: Any) -> SandboxBackend:
    name = configured_backend(settings)
    if name == SIMULATED:
        return _simulated(settings)
    if name == DOCKER:
        raise SandboxConfigError("The docker backend is available only inside sandbox-runner.")
    raise _unsupported(name)


def get_tool_backend(settings: Any) -> SandboxBackend:
    """The backend for agent-run tools: always simulated here, whatever runs the platform's own re-runs."""
    name = configured_backend(settings)
    if name not in SUPPORTED_BACKENDS:
        raise _unsupported(name)
    return _simulated(settings)
