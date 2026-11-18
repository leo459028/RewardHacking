"""Docker backend configuration that needs no Docker SDK (v1.6.2 D16, D18, D20).

The API, the worker and the gate read the backend's identity and limits from here. Only ``docker_backend.py`` talks
to Docker.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.sandbox.base import BackendCapabilities, IsolationLevel
from app.services.sandbox.registry import SandboxConfigError
from app.services.sandbox.spec import image_reference_valid

BACKEND_ID = "docker"
BACKEND_VERSION = "v1.6.2"
RUNTIME_ISOLATION = {"runc": IsolationLevel.CONTAINER, "runsc": IsolationLevel.GVISOR}
PULL_POLICIES = ("never", "missing")
_USER = re.compile(r"^(\d+):(\d+)$")

MAX_LIMITS = {
    "cpu_millis": 4000,
    "memory_mb": 4096,
    "pids": 512,
    "max_output_files_bytes": 16_777_216,
}


def host_pids_limit(runtime: str, pids: int) -> int:
    """The cgroup limit Docker applies on the host for a spec that allows ``pids`` processes.

    gVisor's own threads count against this cgroup: measured about 36 host PIDs at rest plus 2 per sandboxed process,
    and a limit of 32 or less stops the sandbox from starting. So ``runsc`` gets headroom here, and the limit the
    program sees is the ``nproc`` hard limit from ``process_ulimits``.
    """
    return pids * 3 + 64 if runtime == "runsc" else pids


def process_ulimits(runtime: str, pids: int) -> list[dict[str, Any]]:
    """Set from outside the sandbox, without capabilities the program cannot raise it. ``runc`` uses the cgroup alone:
    there ``nproc`` would count every process of that UID on the host."""
    return [{"name": "nproc", "soft": pids, "hard": pids}] if runtime == "runsc" else []


def runtime_of(settings: Any) -> str:
    return str(getattr(settings, "sandbox_docker_runtime", "runc") or "").strip().lower()


def pull_policy(settings: Any) -> str:
    return str(getattr(settings, "sandbox_docker_pull", "never") or "").strip().lower()


def user_of(settings: Any) -> str:
    return str(getattr(settings, "sandbox_docker_user", "65534:65534") or "").strip()


def user_ids(settings: Any) -> tuple[int, int] | None:
    """``(uid, gid)``, or ``None`` when the value is malformed or uses root."""
    match = _USER.match(user_of(settings))
    if match is None:
        return None
    uid, gid = int(match.group(1)), int(match.group(2))
    return None if uid == 0 or gid == 0 else (uid, gid)


def local_image_ids_allowed(settings: Any) -> bool:
    """D18: never in production, whatever the setting says."""
    prod = str(getattr(settings, "app_env", "") or "").strip().lower() == "prod"
    return bool(getattr(settings, "sandbox_docker_allow_local_image_ids", False)) and not prod


def max_limits(settings: Any) -> dict[str, int]:
    return {
        **MAX_LIMITS,
        "timeout_seconds": int(getattr(settings, "sandbox_max_timeout_seconds", 300)),
        "max_output_bytes": int(getattr(settings, "sandbox_max_output_bytes", 1_048_576)),
    }


def sandbox_images(settings: Any) -> dict[str, str]:
    images = getattr(settings, "sandbox_images", None)
    return dict(images) if isinstance(images, dict) else {}


def config_problems(settings: Any) -> list[str]:
    """Stable reason codes for settings that must stop the Docker backend (empty = valid)."""
    allow_ids = local_image_ids_allowed(settings)
    problems: list[str] = []
    if runtime_of(settings) not in RUNTIME_ISOLATION:
        problems.append("runtime_invalid")
    if user_ids(settings) is None:
        problems.append("docker_user_invalid")
    if pull_policy(settings) not in PULL_POLICIES:
        problems.append("pull_policy_invalid")
    if any(value <= 0 for value in max_limits(settings).values()):
        problems.append("limits_invalid")
    if not image_reference_valid(getattr(settings, "sandbox_conformance_image", ""), allow_image_ids=allow_ids):
        problems.append("conformance_image_invalid")
    if any(not image_reference_valid(ref, allow_image_ids=allow_ids) for ref in sandbox_images(settings).values()):
        problems.append("sandbox_image_invalid")
    return problems


def capabilities(settings: Any) -> BackendCapabilities:
    runtime = runtime_of(settings)
    if runtime not in RUNTIME_ISOLATION:
        raise SandboxConfigError(f"Unsupported SANDBOX_DOCKER_RUNTIME {runtime!r}; supported: runc, runsc.")
    return BackendCapabilities(
        backend_id=BACKEND_ID,
        backend_version=BACKEND_VERSION,
        isolation_level=RUNTIME_ISOLATION[runtime],
        supports_network=frozenset({"none"}),
        max_limits=max_limits(settings),
        deterministic=False,
    )
