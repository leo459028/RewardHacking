"""``ExecutionSpec`` and its pure validator (v1.6.0 D19).

``validate_spec`` returns a list of stable reason codes (empty = valid). It never raises on bad input, so a
backend can turn the reasons into a ``rejected`` result. ``spec_digest`` (sha256 of canonical JSON without
``labels``) is the subject of the sandbox execution attestation planned for v1.6.1.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.sandbox.base import LIMIT_FIELDS, NETWORK_MODES, BackendCapabilities, IsolationLevel

_IMAGE_DIGEST = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::\d+(?=/))?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*@sha256:[0-9a-f]{64}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ARTIFACT_REF = re.compile(r"^artifact:sha256:[0-9a-f]{64}$")
_FORBIDDEN_TARGETS = ("/proc", "/sys", "/dev", "/var/run", "/run")
WORKSPACE_REF = "workspace"


@dataclass(frozen=True)
class Mount:
    source_ref: str  # "workspace" or "artifact:sha256:<64 hex>"; never a host path
    target: str
    read_only: bool = True


@dataclass(frozen=True)
class Limits:
    timeout_seconds: int
    cpu_millis: int
    memory_mb: int
    pids: int
    max_output_bytes: int
    max_output_files_bytes: int


@dataclass(frozen=True)
class ExecutionSpec:
    tool_name: str
    argv: tuple[str, ...]
    limits: Limits | None
    image: str = ""
    workdir: str = "/workspace"
    mounts: tuple[Mount, ...] = ()
    network: str = "none"
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)  # run_id / tenant_id / trace_id; not part of the digest
    simulation: dict[str, Any] | None = None  # canned response, read only by the simulated backend


def is_secret_env_name(name: str) -> bool:
    return name.endswith("_API_KEY") or "SECRET" in name or "TOKEN" in name or "PASSWORD" in name


def _bad_path(path: Any) -> bool:
    return not isinstance(path, str) or not path.startswith("/") or ".." in path.split("/") or "\\" in path or "\x00" in path


def _under(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def validate_spec(spec: ExecutionSpec, caps: BackendCapabilities) -> list[str]:
    reasons: list[str] = []
    if not isinstance(spec.tool_name, str) or not spec.tool_name.strip():
        reasons.append("tool_name_required")

    if isinstance(spec.argv, str):
        reasons.append("argv_shell_string")
    elif not isinstance(spec.argv, (tuple, list)) or not spec.argv:
        reasons.append("argv_required")
    elif any(not isinstance(item, str) or "\x00" in item for item in spec.argv) or not spec.argv[0]:
        reasons.append("argv_invalid")

    if caps.isolation_level >= IsolationLevel.CONTAINER:
        if not isinstance(spec.image, str) or not _IMAGE_DIGEST.match(spec.image):
            reasons.append("image_not_digest_pinned")
    elif spec.image and not isinstance(spec.image, str):
        reasons.append("image_invalid")

    if _bad_path(spec.workdir):
        reasons.append("workdir_invalid")

    for mount in spec.mounts or ():
        source = getattr(mount, "source_ref", None)
        target = getattr(mount, "target", None)
        if source != WORKSPACE_REF and not (isinstance(source, str) and _ARTIFACT_REF.match(source)):
            reasons.append("mount_source_not_allowed")
        if _bad_path(target):
            reasons.append("mount_target_invalid")
        elif _under(target, _FORBIDDEN_TARGETS) or target == "/" or "docker.sock" in target:
            reasons.append("mount_target_forbidden")

    if spec.network not in NETWORK_MODES:
        reasons.append("network_invalid")
    elif spec.network not in caps.supports_network:
        reasons.append("network_unsupported")

    if not isinstance(spec.env, dict):
        reasons.append("env_invalid")
    else:
        for name, value in spec.env.items():
            if not isinstance(name, str) or not _ENV_NAME.match(name):
                reasons.append("env_name_invalid")
            elif is_secret_env_name(name):
                reasons.append("env_secret_name")
            if not isinstance(value, str):
                reasons.append("env_value_invalid")

    if spec.limits is None:
        reasons.append("limits_required")
    else:
        for name in LIMIT_FIELDS:
            value = getattr(spec.limits, name, None)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                reasons.append(f"limit_{name}_required")
            elif name in caps.max_limits and value > caps.max_limits[name]:
                reasons.append(f"limit_{name}_exceeds_max")

    return list(dict.fromkeys(reasons))


def _canonical(spec: ExecutionSpec) -> dict[str, Any]:
    limits = spec.limits
    return {
        "tool_name": spec.tool_name,
        "argv": list(spec.argv) if isinstance(spec.argv, (tuple, list)) else spec.argv,
        "image": spec.image,
        "workdir": spec.workdir,
        "mounts": [
            {"source_ref": getattr(m, "source_ref", None), "target": getattr(m, "target", None), "read_only": bool(getattr(m, "read_only", True))}
            for m in (spec.mounts or ())
        ],
        "network": spec.network,
        "env": dict(spec.env) if isinstance(spec.env, dict) else spec.env,
        "limits": {name: getattr(limits, name, None) for name in LIMIT_FIELDS} if limits is not None else None,
        "simulation": spec.simulation,
    }


def spec_digest(spec: ExecutionSpec) -> str:
    """sha256 of the canonical JSON of the spec, without ``labels``."""
    raw = json.dumps(_canonical(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
