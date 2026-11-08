"""v1.5.12 in-process snapshot of judge profile activation (plan D3, D4, D7).

``resolve_active_profile()`` must stay synchronous (it is called from many sync and async code paths), so the
database state it needs is copied into this module-level snapshot:

- ``states``: one :class:`ProfileState` per ``judge_profile_states`` row (last probe and activation);
- ``db_configs``: Dashboard-managed profiles from ``judge_profile_configs`` (``name -> (version, config)``).

``app.services.judge_activation`` fills the snapshot at startup, after every probe / activation write and at most
every ``EVALUATION_JUDGE_ACTIVATION_REFRESH_SECONDS`` when triage is dispatched. This module has no database or
settings dependency, so the judge package can import it without cycles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ProfileState:
    profile_name: str
    probe_status: str = ""
    probe_error: str = ""
    probe_fingerprint: str = ""
    probed_at: datetime | None = None
    probe_result: dict[str, Any] = field(default_factory=dict)
    active: bool = False
    activation_fingerprint: str = ""
    activated_by: str = ""
    activated_at: datetime | None = None


@dataclass
class ActivationSnapshot:
    states: dict[str, ProfileState] = field(default_factory=dict)
    db_configs: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    loaded: bool = False
    loaded_at: float = 0.0  # time.monotonic() of the last successful refresh

    def db_signature(self) -> tuple:
        return tuple(sorted((name, version) for name, (version, _config) in self.db_configs.items()))


_SNAPSHOT = ActivationSnapshot()

# Test-suite switch only (set in tests/conftest.py, never from settings): pre-v1.5.12 tests select a judge through
# EVALUATION_JUDGE_PROFILE / EVALUATION_JUDGE_PROVIDER without probing it first. The v1.5.12 tests turn the probe
# gate back on (D2) with the ``activation_gate`` fixture. Production always requires a successful probe.
TRUST_ENV_PROFILE_WITHOUT_PROBE = False


def snapshot() -> ActivationSnapshot:
    return _SNAPSHOT


def install(states: dict[str, ProfileState], db_configs: dict[str, tuple[int, dict[str, Any]]]) -> ActivationSnapshot:
    global _SNAPSHOT
    _SNAPSHOT = ActivationSnapshot(states=dict(states), db_configs=dict(db_configs), loaded=True, loaded_at=time.monotonic())
    return _SNAPSHOT


def reset() -> None:
    """Forget everything (tests; also the state before the first refresh)."""
    global _SNAPSHOT
    _SNAPSHOT = ActivationSnapshot()


def age_seconds() -> float:
    return float("inf") if not _SNAPSHOT.loaded else time.monotonic() - _SNAPSHOT.loaded_at
