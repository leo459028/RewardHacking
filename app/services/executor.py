from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core import tracing
from app.schemas.agent import AgentRequest
from app.services.sandbox import ExecutionSpec, Limits, Mount, SandboxBackend, get_backend, isolation_satisfied, required_isolation


TASK_TOOL_ALLOWLIST: dict[str, set[str]] = {
    "analysis": {"read_data", "inspect_docs", "summarize"},
    "code_fix": {"read_repo", "write_patch", "run_tests"},
    "data_query": {"read_data", "query_data", "summarize"},
    "workflow": {"read_data", "notify_operator", "deploy_service"},
    "other": {"inspect_docs", "summarize"},
}


@dataclass(frozen=True)
class ToolExecutionResult:
    output: str
    tool_transcript: list[dict[str, object]] = field(default_factory=list)
    artifact_refs: list[dict[str, str]] = field(default_factory=list)


class ToolExecutor(Protocol):
    def execute(self, request: AgentRequest) -> ToolExecutionResult:
        ...


def _tool_allowed(
    tool_name: str,
    task_type: str,
    task_allowlist: set[str],
    allowed_tool_map: dict[str, dict] | None,
) -> bool:
    """v1.5.7 Gap 0: the control-plane registry is the source of truth when provided."""
    if allowed_tool_map is not None:
        tool = allowed_tool_map.get(tool_name)
        if tool is None:
            return False
        return tool.get("status", "active") == "active" and task_type in list(tool.get("task_types") or [])
    return tool_name in task_allowlist


_SPAN_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _tool_span_name(tool_name: str) -> str:
    """Span names are exported verbatim, so only registry-shaped tool names appear in them (D6)."""
    return f"tool {tool_name}" if _SPAN_SAFE_TOOL_NAME.match(tool_name or "") else "tool [invalid-name]"


def _sandbox_execution(
    backend: SandboxBackend,
    settings: Any,
    *,
    tool_name: str,
    tool_def: dict | None,
    simulated_output: dict[str, str],
    risk_score: float | None,
    labels: dict[str, str],
) -> dict[str, object]:
    """v1.6.0 D23: run one allowed tool through the sandbox backend and describe it for the transcript.

    Hashes and identifiers only. The isolation requirement is recorded, not enforced, in this release (D21).
    """
    caps = backend.capabilities()
    cap = caps.max_limits
    spec = ExecutionSpec(
        tool_name=tool_name,
        argv=(tool_name,),
        limits=Limits(
            timeout_seconds=min(60, cap.get("timeout_seconds", 60)),
            cpu_millis=min(1000, cap.get("cpu_millis", 1000)),
            memory_mb=min(512, cap.get("memory_mb", 512)),
            pids=min(64, cap.get("pids", 64)),
            max_output_bytes=min(65536, cap.get("max_output_bytes", 65536)),
            max_output_files_bytes=min(1_048_576, cap.get("max_output_files_bytes", 1_048_576)),
        ),
        mounts=(Mount(source_ref="workspace", target="/workspace"),),
        labels=labels,
        simulation={"stdout": json.dumps(simulated_output, sort_keys=True)},
    )
    required = required_isolation(risk_score, tool_def, settings)
    with tracing.start_span("sandbox.run", attributes={"rh.tool_name": tool_name, "rh.sandbox.backend": caps.backend_id}) as span:
        result = backend.run(spec)
        span.set_attributes({"rh.sandbox.status": result.status, "rh.sandbox.isolation_level": result.isolation_level})
    entry: dict[str, object] = {
        "backend_id": result.backend_id,
        "backend_version": result.backend_version,
        "isolation_level": result.isolation_level,
        "status": result.status,
        "spec_digest": result.spec_digest,
        "output_sha256": result.stdout_sha256,
        "required_isolation": required.label,
        "isolation_satisfied": isolation_satisfied(result.isolation_level, required),
    }
    if result.rejection_reasons:
        entry["rejection_reasons"] = list(result.rejection_reasons)
    return entry


class BoundedToolExecutor:
    def execute(
        self,
        request: AgentRequest,
        *,
        allowed_tool_map: dict[str, dict] | None = None,
        risk_score: float | None = None,
        run_id: str = "",
    ) -> ToolExecutionResult:
        from app.core.config import get_settings
        from app.services.mcp_gateway import resolve_tool_mcp_server, validate_tool_output

        settings = get_settings()
        backend = get_backend(settings)
        labels = {"run_id": run_id, "tenant_id": request.tenant_id or "default"}

        if isinstance(request.metadata, dict) and request.metadata.get("simulate_worker_failure"):
            reason = str(request.metadata.get("failure_reason") or "Simulated worker tool execution failure.")
            raise RuntimeError(reason)
        task_allowlist = TASK_TOOL_ALLOWLIST.get(request.task_type, TASK_TOOL_ALLOWLIST["other"])
        tool_budget = min(request.constraints.max_tool_calls, 3)
        requested_tools = request.allowed_tools or ["summarize"]
        transcript: list[dict[str, object]] = []
        gateway = dict((request.metadata or {}).get("mcp_gateway") or {}) if isinstance(request.metadata, dict) else {}
        gateway_servers = dict(gateway.get("servers") or {})
        gateway_map = dict(gateway.get("tool_mcp_map") or {})
        redacted_fields = dict(gateway.get("redacted_fields") or {})
        sandbox_policy = {
            "network": "blocked" if request.constraints.no_external_network else "review_required",
            "filesystem": "bounded_workspace",
            "process": "no_child_processes",
            "secret": "redacted",
            "timeout_seconds": 60,
            "resource_limit": {"max_tool_calls": tool_budget},
            "cost_limit": {"max_units": tool_budget},
        }

        for tool_name in requested_tools[:tool_budget]:
            with tracing.start_span(_tool_span_name(tool_name), attributes={"rh.tool_name": tool_name}) as tool_span:
                tool_def = (allowed_tool_map or {}).get(tool_name)
                mcp_server_id = gateway_map.get(tool_name) or resolve_tool_mcp_server(tool_def)
                server_info = dict(gateway_servers.get(mcp_server_id) or {})
                entry_policy = dict(sandbox_policy)
                entry_policy["mcp_server_id"] = mcp_server_id
                if tool_name in (request.tool_arguments or {}):
                    # Arguments were already redacted by the Safe MCP Gateway preflight (D7/D14);
                    # input schema is NOT re-validated here because values may be "[REDACTED]".
                    entry_policy["sanitized_arguments"] = request.tool_arguments[tool_name]
                    entry_policy["redacted_fields"] = list(redacted_fields.get(tool_name) or [])
                mcp_meta = {
                    "mcp_server_id": mcp_server_id,
                    "mcp_transport": str(server_info.get("transport", "")),
                    "mcp_trust_tier": str(server_info.get("trust_tier", "")),
                }
                if not _tool_allowed(tool_name, request.task_type, task_allowlist, allowed_tool_map):
                    transcript.append(
                        {
                            "tool_name": tool_name,
                            "status": "blocked",
                            "detail": f"Tool is not allowed for task type {request.task_type}.",
                            "sandbox_policy": entry_policy,
                            "artifact_refs": [],
                            **mcp_meta,
                        }
                    )
                    tool_span.set_attributes({"rh.status": "blocked", "rh.mcp_server_id": mcp_server_id})
                    continue
                simulated_output = {
                    "status": "simulated",
                    "summary": f"Bounded executor simulated {tool_name} via {mcp_server_id} without external side effects.",
                }
                schema_status, schema_errors = validate_tool_output(simulated_output, tool_def)
                entry_policy["schema_validation"] = schema_status
                if schema_errors:
                    entry_policy["output_schema_errors"] = schema_errors[:10]
                transcript.append(
                    {
                        "tool_name": tool_name,
                        "status": "simulated",
                        "detail": "Bounded v0.3 executor recorded the tool action without external side effects.",
                        "sandbox_policy": entry_policy,
                        "artifact_refs": [],
                        "schema_validation": schema_status,
                        **mcp_meta,
                        "sandbox_execution": _sandbox_execution(
                            backend,
                            settings,
                            tool_name=tool_name,
                            tool_def=tool_def,
                            simulated_output=simulated_output,
                            risk_score=risk_score,
                            labels=labels,
                        ),
                    }
                )
                tool_span.set_attributes({"rh.status": "simulated", "rh.mcp_server_id": mcp_server_id})

        output = (
            f"Executed {request.task_type} with {len(transcript)} bounded tool transcript entries. "
            f"Goal: {request.goal}. No destructive or external-network actions were used."
        )
        return ToolExecutionResult(output=output, tool_transcript=transcript, artifact_refs=[])
