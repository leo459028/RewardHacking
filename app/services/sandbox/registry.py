"""Backend selection (v1.6.0 D23). ``SANDBOX_BACKEND=simulated`` is the default and the only option here."""

from __future__ import annotations

from typing import Any

from app.services.sandbox.base import SandboxBackend
from app.services.sandbox.simulated import SimulatedBackend

SUPPORTED_BACKENDS = ("simulated",)


class SandboxConfigError(RuntimeError):
    """Unknown ``SANDBOX_BACKEND`` or a non-positive maximum: fail closed instead of running misconfigured."""


def get_backend(settings: Any) -> SandboxBackend:
    name = str(getattr(settings, "sandbox_backend", "simulated") or "").strip().lower()
    if name == "simulated":
        max_limits = {
            "timeout_seconds": int(getattr(settings, "sandbox_max_timeout_seconds", 300)),
            "max_output_bytes": int(getattr(settings, "sandbox_max_output_bytes", 1_048_576)),
        }
        bad = sorted(key for key, value in max_limits.items() if value <= 0)
        if bad:
            # Otherwise every tool would be silently "rejected" while the transcript still reads "simulated".
            raise SandboxConfigError(f"Sandbox maximums must be greater than 0: {', '.join(bad)}.")
        return SimulatedBackend(max_limits=max_limits)
    raise SandboxConfigError(f"Unsupported SANDBOX_BACKEND {name!r}; supported: {', '.join(SUPPORTED_BACKENDS)}.")
