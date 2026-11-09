"""Safe MCP Gateway (v1.5.7).

Deterministic, dependency-free guardrails for Model Context Protocol (MCP) tool calls:

* MCP Server Registry with trust tiers, transport policy and tenant allow-lists
  (custom servers are stored in ``tool_definitions`` under the reserved
  ``__mcp_server__:{id}`` namespace, so no Alembic migration is required).
* Lightweight JSON Schema subset validation for tool arguments / outputs.
* Per-field injection scan profiles (``x-scan-profile``) to avoid false positives
  on SQL / patch tools.
* Recursive sensitive argument redaction before anything is persisted.
* A single ``preflight()`` entry point shared by every submit path, and a
  ``worker_context()`` that re-checks hard-deny conditions at execution time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.control_plane import ToolDefinition
from app.observability.metrics import (
    MCP_ARGUMENT_REDACTIONS_TOTAL,
    MCP_GATEWAY_REQUESTS_TOTAL,
    MCP_SCHEMA_VIOLATIONS_TOTAL,
    QUOTA_DECISIONS_TOTAL,
)
from app.schemas.agent import AgentRequest
from app.schemas.control_plane import MCP_SERVER_TOOL_PREFIX
from app.services.run_state import QuotaResult, RunStateStore
from app.services.network_policy import is_external_endpoint as is_external_endpoint
from app.services.sensitive_data import (
    SENSITIVE_KEY_NAMES as SENSITIVE_KEY_NAMES,
    SENSITIVE_KEY_SUFFIXES as SENSITIVE_KEY_SUFFIXES,
    SENSITIVE_VALUE_PATTERNS as SENSITIVE_VALUE_PATTERNS,
)

MCP_GATEWAY_VERSION = "v1.5.7"
DEFAULT_MCP_SERVER_ID = "mcp-core-local"
CORE_MCP_SERVER_IDS = ("mcp-core-local", "mcp-data-verified", "mcp-deploy-verified")

HARD_DENY_FLAG_PREFIXES: tuple[str, ...] = (
    "mcp_server_blocked:",
    "mcp_tenant_not_allowed:",
    "mcp_argument_injection_detected:",
)
ESCALATE_FLAG_PREFIXES: tuple[str, ...] = (
    "mcp_untrusted_server:",
    "mcp_external_network_violation:",
    "mcp_server_circuit_breaker_open:",
)

UNTRUSTED_RISK_DELTA = 0.20
EXTERNAL_NETWORK_RISK_DELTA = 0.15

BUILTIN_MCP_SERVERS: list[dict[str, Any]] = [
    {
        "mcp_server_id": "mcp-core-local",
        "display_name": "Core Local MCP Server",
        "version": "1.0.0",
        "transport": "stdio",
        "endpoint": "local://stdio",
        "trust_tier": "trusted",
        "is_external_network": False,
        "status": "active",
        "requires_approval": False,
        "risk_level": "low",
        "quota_per_minute": None,
        "allowed_tenants": [],
        "metadata": {"source": "builtin"},
    },
    {
        "mcp_server_id": "mcp-data-verified",
        "display_name": "Verified Internal Data MCP Server",
        "version": "1.0.0",
        "transport": "sse",
        "endpoint": "https://mcp.internal.local/data",
        "trust_tier": "verified",
        "is_external_network": False,
        "status": "active",
        "requires_approval": False,
        "risk_level": "medium",
        "quota_per_minute": None,
        "allowed_tenants": [],
        "metadata": {"source": "builtin"},
    },
    {
        "mcp_server_id": "mcp-deploy-verified",
        "display_name": "Verified Internal Deploy MCP Server",
        "version": "1.0.0",
        "transport": "streamable_http",
        "endpoint": "https://mcp.internal.local/deploy",
        "trust_tier": "verified",
        "is_external_network": False,
        "status": "active",
        "requires_approval": True,
        "risk_level": "high",
        "quota_per_minute": None,
        "allowed_tenants": [],
        "metadata": {"source": "builtin"},
    },
    {
        "mcp_server_id": "mcp-external-untrusted",
        "display_name": "External Untrusted MCP Server",
        "version": "1.0.0",
        "transport": "streamable_http",
        "endpoint": "https://thirdparty-mcp.example.com/mcp",
        "trust_tier": "untrusted",
        "is_external_network": True,
        "status": "active",
        "requires_approval": True,
        "risk_level": "high",
        "quota_per_minute": 15,
        "allowed_tenants": [],
        "metadata": {"source": "builtin"},
    },
]

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

SCAN_PROFILE_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "path": (
        ("path_traversal", re.compile(r"\.\./|\.\.\\")),
        ("sensitive_path", re.compile(r"/etc/(passwd|shadow)|/proc/self", re.IGNORECASE)),
    ),
    "shell": (
        ("shell_rm", re.compile(r";\s*rm\s+-rf", re.IGNORECASE)),
        ("shell_subst", re.compile(r"\$\(")),
        ("shell_backtick", re.compile(r"`")),
        ("shell_download", re.compile(r"&&\s*(curl|wget)\b", re.IGNORECASE)),
        ("shell_pipe", re.compile(r"\|\s*(sh|bash)\b", re.IGNORECASE)),
    ),
    "sql_destructive": (
        ("sql_destructive", re.compile(r";\s*(drop|delete|truncate|alter)\b", re.IGNORECASE)),
    ),
    "sql": (
        ("sql_destructive", re.compile(r";\s*(drop|delete|truncate|alter)\b", re.IGNORECASE)),
        ("sql_union", re.compile(r"\bunion\s+select\b", re.IGNORECASE)),
        ("sql_comment", re.compile(r"--\s*$|/\*", re.MULTILINE)),
    ),
    "none": (),
}
DEFAULT_SCAN_PROFILE: tuple[str, ...] = ("path", "shell")

DEFAULT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["status", "summary"],
    "properties": {"status": {"type": "string"}, "summary": {"type": "string", "maxLength": 4096}},
}

_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (dict,),
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SchemaValidationResult:
    tool_name: str
    status: str = "passed"
    errors: list[str] = field(default_factory=list)
    injection_flags: list[str] = field(default_factory=list)
    violation_types: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors and not self.injection_flags


@dataclass
class McpGatewayContext:
    flags: list[str] = field(default_factory=list)
    hard_deny_flags: list[str] = field(default_factory=list)
    risk_delta: float = 0.0
    approved: bool = False
    tool_mcp_map: dict[str, str] = field(default_factory=dict)
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    schema_validation: dict[str, str] = field(default_factory=dict)
    schema_errors: dict[str, list[str]] = field(default_factory=dict)
    redacted_fields: dict[str, list[str]] = field(default_factory=dict)
    injection_tools: dict[str, list[str]] = field(default_factory=dict)
    schema_violation_tools: dict[str, list[str]] = field(default_factory=dict)
    schema_enforcement: dict[str, str] = field(default_factory=dict)
    approval_servers: list[str] = field(default_factory=list)

    @property
    def mcp_servers(self) -> list[str]:
        return list(dict.fromkeys(self.tool_mcp_map.values()))

    @property
    def primary_mcp_server_id(self) -> str:
        servers = self.mcp_servers
        return servers[0] if servers else DEFAULT_MCP_SERVER_ID

    def is_hard_deny(self, flag: str) -> bool:
        return flag in self.hard_deny_flags or flag.startswith(HARD_DENY_FLAG_PREFIXES)

    def add_flag(self, flag: str, *, hard: bool = False) -> None:
        if flag not in self.flags:
            self.flags.append(flag)
        if hard and flag not in self.hard_deny_flags:
            self.hard_deny_flags.append(flag)

    def summary(self) -> dict[str, Any]:
        return {
            "version": MCP_GATEWAY_VERSION,
            "primary_mcp_server_id": self.primary_mcp_server_id,
            "mcp_servers": self.mcp_servers,
            "tool_mcp_map": dict(self.tool_mcp_map),
            "servers": {key: dict(value) for key, value in self.servers.items()},
            "schema_validation": dict(self.schema_validation),
            "redacted_fields": {key: list(value) for key, value in self.redacted_fields.items()},
            "flags": list(self.flags),
            "approval_servers": list(self.approval_servers),
        }


@dataclass
class McpPreflightResult:
    sanitized_request: AgentRequest
    context: McpGatewayContext
    quota_error: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def normalize_mcp_server(raw: dict[str, Any], settings: Settings, *, source: str = "custom") -> dict[str, Any]:
    server = dict(raw)
    server_id = str(server.get("mcp_server_id") or "").strip()
    transport = str(server.get("transport") or "stdio")
    endpoint = str(server.get("endpoint") or "local://stdio")
    trust_tier = str(server.get("trust_tier") or "verified")
    is_external = server.get("is_external_network")
    if is_external is None:
        is_external = is_external_endpoint(transport, endpoint, settings.mcp_internal_host_suffix_list)
    requires_approval = bool(server.get("requires_approval", False)) or trust_tier == "untrusted"
    quota = server.get("quota_per_minute")
    try:
        quota_value = int(quota) if quota is not None else int(settings.mcp_server_call_limit_per_minute)
    except (TypeError, ValueError):
        quota_value = int(settings.mcp_server_call_limit_per_minute)
    if quota_value < 1:
        quota_value = int(settings.mcp_server_call_limit_per_minute)
    return {
        "mcp_server_id": server_id,
        "display_name": str(server.get("display_name") or server_id),
        "version": str(server.get("version") or "1.0.0"),
        "transport": transport,
        "endpoint": endpoint,
        "trust_tier": trust_tier,
        "is_external_network": bool(is_external),
        "status": str(server.get("status") or "active"),
        "requires_approval": requires_approval,
        "risk_level": str(server.get("risk_level") or "low"),
        "quota_per_minute": quota_value,
        "allowed_tenants": [str(item) for item in (server.get("allowed_tenants") or []) if str(item).strip()],
        "metadata": dict(server.get("metadata") or {}),
        "source": source,
    }


async def list_mcp_servers(session: AsyncSession, settings: Settings) -> list[dict[str, Any]]:
    rows = list(
        (
            await session.scalars(
                select(ToolDefinition).where(ToolDefinition.tool_name.like(f"{MCP_SERVER_TOOL_PREFIX}%"))
            )
        ).all()
    )
    custom: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = dict(row.metadata_payload or {})
        if payload.get("entity_type") != "mcp_server":
            continue
        server_raw = dict(payload.get("server") or {})
        server_raw.setdefault("mcp_server_id", row.tool_name[len(MCP_SERVER_TOOL_PREFIX):])
        server = normalize_mcp_server(server_raw, settings, source="custom")
        custom[server["mcp_server_id"]] = server
    servers = [custom.get(item["mcp_server_id"]) or normalize_mcp_server(item, settings, source="builtin") for item in BUILTIN_MCP_SERVERS]
    builtin_ids = {item["mcp_server_id"] for item in BUILTIN_MCP_SERVERS}
    servers.extend(server for server_id, server in sorted(custom.items()) if server_id not in builtin_ids)
    return servers


async def get_mcp_server_map(session: AsyncSession, settings: Settings) -> dict[str, dict[str, Any]]:
    return {item["mcp_server_id"]: item for item in await list_mcp_servers(session, settings)}


async def upsert_mcp_server(session: AsyncSession, payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    server = normalize_mcp_server(payload, settings, source="custom")
    server_id = server["mcp_server_id"]
    tool_name = f"{MCP_SERVER_TOOL_PREFIX}{server_id}"
    stored = {key: value for key, value in server.items() if key != "source"}
    metadata = {"entity_type": "mcp_server", "server": stored}
    existing = await session.scalar(select(ToolDefinition).where(ToolDefinition.tool_name == tool_name))
    if existing is not None:
        existing.display_name = server["display_name"][:128]
        existing.status = server["status"]
        existing.task_types = []
        existing.requires_approval = server["requires_approval"]
        existing.risk_level = server["risk_level"]
        existing.metadata_payload = metadata
    else:
        session.add(
            ToolDefinition(
                tool_name=tool_name,
                display_name=server["display_name"][:128],
                status=server["status"],
                task_types=[],
                requires_approval=server["requires_approval"],
                risk_level=server["risk_level"],
                metadata_payload=metadata,
            )
        )
    await session.commit()
    return server


def resolve_tool_mcp_server(tool: dict[str, Any] | None) -> str:
    if not tool:
        return DEFAULT_MCP_SERVER_ID
    meta = dict(tool.get("metadata") or {})
    server_id = str(meta.get("mcp_server_id") or "").strip()
    return server_id or DEFAULT_MCP_SERVER_ID


def build_tool_mcp_map(tool_names: list[str], tools: list[dict[str, Any]]) -> dict[str, str]:
    tool_index = {str(item.get("tool_name")): item for item in tools}
    return {name: resolve_tool_mcp_server(tool_index.get(name)) for name in dict.fromkeys(tool_names) if name}


# ---------------------------------------------------------------------------
# JSON Schema subset validation & injection scanning
# ---------------------------------------------------------------------------


def _json_depth(value: Any, limit: int) -> int:
    """Iterative depth computation (never recursive-unbounded)."""
    max_depth = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, (dict, list, tuple)):
            max_depth = max(max_depth, depth)
            if depth > limit:
                return depth
            children = current.values() if isinstance(current, dict) else current
            for child in children:
                stack.append((child, depth + 1))
    return max_depth


def _type_matches(value: Any, expected: str) -> bool:
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    if expected == "integer" and isinstance(value, float):
        return value.is_integer()
    types = _JSON_TYPES.get(expected)
    if types is None:
        return True
    return isinstance(value, types)


def _validate_against_schema(value: Any, schema: dict[str, Any], path: str, errors: list[str], depth: int, max_depth: int) -> None:
    if depth > max_depth:
        errors.append(f"{path}: exceeds maximum nesting depth {max_depth}")
        return
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_type_matches(value, item) for item in expected):
            errors.append(f"{path}: expected type {expected}")
            return
    elif isinstance(expected, str) and not _type_matches(value, expected):
        errors.append(f"{path}: expected type {expected}")
        return
    if "enum" in schema and value not in list(schema.get("enum") or []):
        errors.append(f"{path}: value not in enum {schema.get('enum')}")
    if isinstance(value, str):
        max_len = schema.get("maxLength")
        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(value) < min_len:
            errors.append(f"{path}: shorter than minLength {min_len}")
        if isinstance(max_len, int) and len(value) > max_len:
            errors.append(f"{path}: longer than maxLength {max_len}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and pattern:
            bounded = value[: max_len if isinstance(max_len, int) else 4096]
            try:
                if re.search(pattern, bounded) is None:
                    errors.append(f"{path}: does not match pattern")
            except re.error:
                errors.append(f"{path}: invalid schema pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: below minimum {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: above maximum {maximum}")
    if isinstance(value, dict):
        properties = dict(schema.get("properties") or {})
        for required in list(schema.get("required") or []):
            if required not in value:
                errors.append(f"{path}.{required}: required field missing")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key}: additional property not allowed")
        for key, child in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                _validate_against_schema(child, child_schema, f"{path}.{key}", errors, depth + 1, max_depth)
    if isinstance(value, (list, tuple)):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                _validate_against_schema(child, item_schema, f"{path}[{index}]", errors, depth + 1, max_depth)


def _scan_profile_for(field_schema: dict[str, Any] | None) -> tuple[str, ...]:
    if not isinstance(field_schema, dict) or "x-scan-profile" not in field_schema:
        return DEFAULT_SCAN_PROFILE
    raw = field_schema.get("x-scan-profile")
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw)
    return DEFAULT_SCAN_PROFILE


def _iter_strings(value: Any, limit: int) -> list[str]:
    found: list[str] = []
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > limit:
            continue
        if isinstance(current, str):
            found.append(current)
        elif isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
    return found


def scan_tool_arguments(args: dict[str, Any], schema: dict[str, Any] | None, *, max_depth: int = 8) -> list[str]:
    properties = dict((schema or {}).get("properties") or {})
    signals: list[str] = []
    for key, value in args.items():
        profiles = _scan_profile_for(properties.get(key))
        for text in _iter_strings(value, max_depth):
            for profile in profiles:
                for signal, pattern in SCAN_PROFILE_PATTERNS.get(profile, ()):
                    if pattern.search(text) and f"{key}:{signal}" not in signals:
                        signals.append(f"{key}:{signal}")
    return signals


def validate_tool_arguments(
    tool_name: str,
    args: Any,
    tool_def: dict[str, Any] | None,
    *,
    max_depth: int = 8,
    max_bytes: int = 65536,
) -> SchemaValidationResult:
    result = SchemaValidationResult(tool_name=tool_name)
    if not isinstance(args, dict):
        result.errors.append(f"{tool_name}: arguments must be a JSON object")
        result.violation_types.append("type_error")
        result.status = "failed"
        return result
    try:
        encoded = json.dumps(args, default=str, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        encoded = b""
        result.errors.append(f"{tool_name}: arguments are not JSON serializable")
    if len(encoded) > max_bytes:
        result.errors.append(f"{tool_name}: arguments exceed {max_bytes} bytes")
        result.violation_types.append("payload_too_large")
        result.status = "failed"
        return result
    if _json_depth(args, max_depth) > max_depth:
        result.errors.append(f"{tool_name}: arguments exceed maximum nesting depth {max_depth}")
        result.violation_types.append("payload_too_large")
        result.status = "failed"
        return result
    meta = dict((tool_def or {}).get("metadata") or {})
    schema = meta.get("input_schema") if isinstance(meta.get("input_schema"), dict) else None
    if schema is not None:
        _validate_against_schema(args, schema, tool_name, result.errors, 1, max_depth)
        if result.errors:
            result.violation_types.append("schema_violation")
    signals = scan_tool_arguments(args, schema, max_depth=max_depth)
    if signals:
        result.injection_flags.extend(signals)
        result.violation_types.append("injection")
    if result.injection_flags:
        result.status = "injection_detected"
    elif result.errors:
        result.status = "failed"
    else:
        result.status = "passed" if schema is not None else "no_schema"
    return result


def validate_tool_output(output: Any, tool_def: dict[str, Any] | None) -> tuple[str, list[str]]:
    meta = dict((tool_def or {}).get("metadata") or {})
    schema = meta.get("output_schema") if isinstance(meta.get("output_schema"), dict) else DEFAULT_OUTPUT_SCHEMA
    errors: list[str] = []
    _validate_against_schema(output, schema, "output", errors, 1, 8)
    return ("passed" if not errors else "failed"), errors


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: str, extra: set[str]) -> bool:
    lowered = key.strip().lower()
    if lowered in SENSITIVE_KEY_NAMES or lowered in extra:
        return True
    return lowered.endswith(SENSITIVE_KEY_SUFFIXES)


def sanitize_tool_arguments(
    args: dict[str, Any],
    *,
    sensitive_fields: list[str] | None = None,
    allow_pii: bool = False,
    max_depth: int = 8,
    tool_name: str = "",
) -> tuple[dict[str, Any], list[str]]:
    extra = {str(item).strip().lower() for item in (sensitive_fields or []) if str(item).strip()}
    redacted: list[str] = []

    def _walk(value: Any, path: str, depth: int) -> Any:
        if depth > max_depth:
            return "[TRUNCATED]"
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if _is_sensitive_key(str(key), extra):
                    cleaned[key] = "[REDACTED]"
                    redacted.append(child_path)
                    MCP_ARGUMENT_REDACTIONS_TOTAL.labels(tool_name=tool_name or "unknown", redaction_type="field_name").inc()
                else:
                    cleaned[key] = _walk(child, child_path, depth + 1)
            return cleaned
        if isinstance(value, (list, tuple)):
            return [_walk(child, f"{path}[{index}]", depth + 1) for index, child in enumerate(value)]
        if isinstance(value, str):
            text = value
            hit = False
            for pattern in SENSITIVE_VALUE_PATTERNS:
                if pattern.search(text):
                    text = pattern.sub("[REDACTED]", text)
                    hit = True
            if hit:
                redacted.append(path)
                MCP_ARGUMENT_REDACTIONS_TOTAL.labels(tool_name=tool_name or "unknown", redaction_type="value_pattern").inc()
            if not allow_pii and EMAIL_PATTERN.search(text):
                text = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", text)
                if path not in redacted:
                    redacted.append(path)
                MCP_ARGUMENT_REDACTIONS_TOTAL.labels(tool_name=tool_name or "unknown", redaction_type="pii_email").inc()
            return text
        return value

    cleaned_args = _walk(dict(args or {}), tool_name or "", 1)
    return cleaned_args, redacted


# ---------------------------------------------------------------------------
# Preflight (submit) & worker re-check
# ---------------------------------------------------------------------------


def _tool_index(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("tool_name")): item for item in tools}


def _evaluate_servers(
    context: McpGatewayContext,
    *,
    request: AgentRequest,
    server_map: dict[str, dict[str, Any]],
    open_mcp_breakers: dict[str, dict[str, Any]],
    include_escalations: bool,
) -> None:
    tenant_id = request.tenant_id or "default"
    untrusted = False
    external = False
    for server_id in context.mcp_servers:
        server = server_map.get(server_id)
        if server is None:
            context.add_flag(f"mcp_server_blocked:{server_id}", hard=True)
            context.servers[server_id] = {"transport": "unknown", "trust_tier": "unknown", "is_external_network": True, "status": "missing"}
            continue
        context.servers[server_id] = {
            "transport": server["transport"],
            "trust_tier": server["trust_tier"],
            "is_external_network": server["is_external_network"],
            "status": server["status"],
            "risk_level": server["risk_level"],
        }
        if server["trust_tier"] == "blocked" or server["status"] != "active":
            context.add_flag(f"mcp_server_blocked:{server_id}", hard=True)
            continue
        allowed_tenants = list(server.get("allowed_tenants") or [])
        if allowed_tenants and tenant_id not in allowed_tenants:
            context.add_flag(f"mcp_tenant_not_allowed:{server_id}", hard=True)
            continue
        if not include_escalations:
            continue
        if server["trust_tier"] == "untrusted":
            context.add_flag(f"mcp_untrusted_server:{server_id}")
            untrusted = True
        if request.constraints.no_external_network and server["is_external_network"]:
            context.add_flag(f"mcp_external_network_violation:{server_id}")
            external = True
        if server_id in open_mcp_breakers:
            context.add_flag(f"mcp_server_circuit_breaker_open:{server_id}")
        if server["requires_approval"] and server["trust_tier"] != "untrusted":
            context.approval_servers.append(server_id)
    if untrusted:
        context.risk_delta += UNTRUSTED_RISK_DELTA
    if external:
        context.risk_delta += EXTERNAL_NETWORK_RISK_DELTA


def _merged_tool_arguments(request: AgentRequest) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    meta_args = (request.metadata or {}).get("tool_arguments")
    if isinstance(meta_args, dict):
        merged.update(meta_args)
    merged.update(dict(request.tool_arguments or {}))
    return merged


async def check_mcp_server_quotas(
    state_store: RunStateStore,
    *,
    settings: Settings,
    tenant_id: str,
    user_id: str,
    tool_mcp_map: dict[str, str],
    mcp_server_limits: dict[str, int],
) -> tuple[bool, str, QuotaResult | None]:
    from datetime import datetime, timezone

    minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    for tool_name, server_id in tool_mcp_map.items():
        limit = int(mcp_server_limits.get(server_id) or settings.mcp_server_call_limit_per_minute)
        res = await state_store.increment_quota(
            tenant_id,
            f"mcp:{server_id}:calls:{minute}",
            limit=limit,
            ttl_seconds=120,
        )
        QUOTA_DECISIONS_TOTAL.labels(
            tenant_id=tenant_id,
            scope="mcp_server",
            decision="allowed" if res.allowed else "blocked",
        ).inc()
        if not res.allowed:
            return False, server_id, res
    return True, "", None


async def preflight(
    session: AsyncSession,
    state_store: RunStateStore,
    *,
    settings: Settings,
    agent_request: AgentRequest,
    tools: list[dict[str, Any]] | None = None,
) -> McpPreflightResult:
    """Shared Safe MCP Gateway preflight for every submit entry point (D16)."""
    from app.services.control_plane import list_tool_definitions

    tools = tools if tools is not None else await list_tool_definitions(session)
    index = _tool_index(tools)
    server_map = await get_mcp_server_map(session, settings)
    known_tools = [name for name in dict.fromkeys(agent_request.allowed_tools or []) if name in index]
    context = McpGatewayContext(tool_mcp_map=build_tool_mcp_map(known_tools, tools))
    tenant_id = agent_request.tenant_id or "default"

    if settings.quota_enabled and context.tool_mcp_map:
        limits = {server_id: int(server["quota_per_minute"]) for server_id, server in server_map.items()}
        allowed, blocked_server, quota = await check_mcp_server_quotas(
            state_store,
            settings=settings,
            tenant_id=tenant_id,
            user_id=agent_request.user_id,
            tool_mcp_map=context.tool_mcp_map,
            mcp_server_limits=limits,
        )
        if not allowed and quota is not None:
            return McpPreflightResult(
                sanitized_request=agent_request,
                context=context,
                quota_error={
                    "code": "mcp_server_quota_exceeded",
                    "message": f"MCP server call quota exceeded for {blocked_server}.",
                    "mcp_server_id": blocked_server,
                    "quota": quota,
                },
            )

    open_mcp_breakers = await state_store.get_open_mcp_breakers(context.mcp_servers)
    _evaluate_servers(
        context,
        request=agent_request,
        server_map=server_map,
        open_mcp_breakers=open_mcp_breakers,
        include_escalations=True,
    )

    raw_args = _merged_tool_arguments(agent_request)
    allowed_set = set(agent_request.allowed_tools or [])
    sanitized_args: dict[str, dict[str, Any]] = {}
    for tool_name, args in raw_args.items():
        tool_name = str(tool_name)
        tool_def = index.get(tool_name)
        meta = dict((tool_def or {}).get("metadata") or {})
        enforcement = str(meta.get("schema_enforcement") or settings.mcp_schema_enforcement_mode)
        if enforcement not in {"deny", "escalate"}:
            enforcement = "deny"
        context.schema_enforcement[tool_name] = enforcement
        server_id = context.tool_mcp_map.get(tool_name, resolve_tool_mcp_server(tool_def))
        if tool_name not in allowed_set:
            context.schema_validation[tool_name] = "undeclared_tool"
            context.schema_violation_tools[tool_name] = [f"{tool_name}: arguments supplied for a tool not in allowed_tools"]
            context.add_flag(f"mcp_tool_schema_violation:{tool_name}", hard=enforcement == "deny")
            MCP_SCHEMA_VIOLATIONS_TOTAL.labels(tool_name=tool_name, mcp_server_id=server_id, violation_type="undeclared_tool").inc()
        else:
            result = validate_tool_arguments(
                tool_name,
                args,
                tool_def,
                max_depth=settings.mcp_tool_arguments_max_depth,
                max_bytes=settings.mcp_tool_arguments_max_bytes,
            )
            context.schema_validation[tool_name] = result.status
            if result.errors:
                context.schema_errors[tool_name] = list(result.errors)
            if result.injection_flags:
                context.injection_tools[tool_name] = list(result.injection_flags)
                context.add_flag(f"mcp_argument_injection_detected:{tool_name}", hard=True)
                MCP_SCHEMA_VIOLATIONS_TOTAL.labels(tool_name=tool_name, mcp_server_id=server_id, violation_type="injection").inc()
            if result.errors:
                context.schema_violation_tools[tool_name] = list(result.errors)
                context.add_flag(f"mcp_tool_schema_violation:{tool_name}", hard=enforcement == "deny")
                for violation_type in result.violation_types:
                    if violation_type != "injection":
                        MCP_SCHEMA_VIOLATIONS_TOTAL.labels(
                            tool_name=tool_name, mcp_server_id=server_id, violation_type=violation_type
                        ).inc()
        if isinstance(args, dict):
            cleaned, redacted = sanitize_tool_arguments(
                args,
                sensitive_fields=list(meta.get("sensitive_fields") or []),
                allow_pii=agent_request.constraints.allow_pii_access,
                max_depth=settings.mcp_tool_arguments_max_depth,
                tool_name=tool_name,
            )
        else:
            cleaned, redacted = {"_invalid": "[REDACTED]"}, ["_invalid"]
        sanitized_args[tool_name] = cleaned
        if redacted:
            context.redacted_fields[tool_name] = redacted

    for tool_name in known_tools:
        if tool_name in context.schema_validation:
            continue
        meta = dict(index[tool_name].get("metadata") or {})
        if meta.get("strict_arguments_required"):
            enforcement = str(meta.get("schema_enforcement") or settings.mcp_schema_enforcement_mode)
            context.schema_validation[tool_name] = "missing_required_arguments"
            context.schema_violation_tools[tool_name] = [f"{tool_name}: tool_arguments are required"]
            context.add_flag(f"mcp_tool_schema_violation:{tool_name}", hard=enforcement != "escalate")
        else:
            context.schema_validation[tool_name] = "skipped_no_args"

    for server_id in context.mcp_servers:
        server = context.servers.get(server_id, {})
        hard = any(flag.endswith(f":{server_id}") for flag in context.hard_deny_flags)
        escalated = any(flag.endswith(f":{server_id}") for flag in context.flags)
        decision = "denied" if hard else ("escalated" if escalated else "allowed")
        MCP_GATEWAY_REQUESTS_TOTAL.labels(
            mcp_server_id=server_id,
            transport=str(server.get("transport", "unknown")),
            trust_tier=str(server.get("trust_tier", "unknown")),
            decision=decision,
        ).inc()

    metadata = {key: value for key, value in dict(agent_request.metadata or {}).items() if key not in {"tool_arguments", "mcp_gateway"}}
    metadata["mcp_gateway"] = context.summary()
    sanitized_request = agent_request.model_copy(update={"tool_arguments": sanitized_args, "metadata": metadata})
    return McpPreflightResult(sanitized_request=sanitized_request, context=context)


async def worker_context(
    session: AsyncSession,
    state_store: RunStateStore,
    *,
    settings: Settings,
    request: AgentRequest,
    tools: list[dict[str, Any]],
) -> McpGatewayContext:
    """Worker-side MCP re-check (D9).

    Hard-deny conditions (blocked / disabled / tenant not allowed) are always
    re-evaluated to close the queue-time TOCTOU window. Escalation conditions are
    skipped once the run has been approved (``requires_human_approval=True``).
    """
    known_tools = [name for name in dict.fromkeys(request.allowed_tools or []) if name in _tool_index(tools)]
    approved = bool(request.constraints.requires_human_approval)
    context = McpGatewayContext(tool_mcp_map=build_tool_mcp_map(known_tools, tools), approved=approved)
    server_map = await get_mcp_server_map(session, settings)
    open_mcp_breakers = {} if approved else await state_store.get_open_mcp_breakers(context.mcp_servers)
    _evaluate_servers(
        context,
        request=request,
        server_map=server_map,
        open_mcp_breakers=open_mcp_breakers,
        include_escalations=not approved,
    )
    return context


async def persist_preflight_audit(
    session: AsyncSession,
    *,
    run_id: str,
    request: AgentRequest,
    context: McpGatewayContext,
) -> None:
    from app.models.governance_event import GovernanceEvent
    from app.services.audit_log import _add_incident

    tenant_id = request.tenant_id or "default"
    added = False
    for tool_name, errors in context.schema_violation_tools.items():
        enforcement = context.schema_enforcement.get(tool_name, "deny")
        session.add(
            GovernanceEvent(
                tenant_id=tenant_id,
                user_id=request.user_id,
                run_id=run_id,
                event_type="mcp_schema_violation",
                decision="blocked" if enforcement == "deny" else "approval_required",
                reason=f"Tool argument schema violation for {tool_name}.",
                details={
                    "tool_name": tool_name,
                    "mcp_server_id": context.tool_mcp_map.get(tool_name, ""),
                    "errors": errors[:20],
                    "schema_enforcement": enforcement,
                },
            )
        )
        added = True
    for tool_name, signals in context.injection_tools.items():
        _add_incident(
            session,
            run_id=run_id,
            severity="high",
            code="mcp_argument_injection",
            detail=f"Injection signals in {tool_name} arguments: {', '.join(signals[:10])}",
        )
        session.add(
            GovernanceEvent(
                tenant_id=tenant_id,
                user_id=request.user_id,
                run_id=run_id,
                event_type="mcp_argument_injection",
                decision="blocked",
                reason=f"Injection signals detected in {tool_name} arguments.",
                details={"tool_name": tool_name, "signals": signals[:20]},
            )
        )
        added = True
    if added:
        await session.commit()
