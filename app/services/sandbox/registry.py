"""Backend selection (v1.6.0 D23). ``SANDBOX_BACKEND=simulated`` is the default and the only option here."""

from __future__ import annotations

from typing import Any

from app.services.sandbox.base import SandboxBackend
from app.services.sandbox.simulated import SimulatedBackend

SUPPORTED_BACKENDS = ("simulated",)


class SandboxConfigError(RuntimeError):
    """Unknown ``SANDBOX_BACKEND``: fail closed instead of silently running on another backend."""


def get_backend(settings: Any) -> SandboxBackend:
    name = str(getattr(settings, "sandbox_backend", "simulated") or "").strip().lower()
    if name == "simulated":
        return SimulatedBackend(
            max_limits={
                "timeout_seconds": int(getattr(settings, "sandbox_max_timeout_seconds", 300)),
                "max_output_bytes": int(getattr(settings, "sandbox_max_output_bytes", 1_048_576)),
            }
        )
    raise SandboxConfigError(f"Unsupported SANDBOX_BACKEND {name!r}; supported: {', '.join(SUPPORTED_BACKENDS)}.")
