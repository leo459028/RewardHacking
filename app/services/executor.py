from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.schemas.agent import AgentRequest


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


class BoundedToolExecutor:
    def execute(
        self,
        request: AgentRequest,
        *,
        allowed_tool_map: dict[str, dict] | None = None,
    ) -> ToolExecutionResult:
        from app.services.mcp_gateway import resolve_tool_mcp_server, validate_tool_output

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
                }
            )

        output = (
            f"Executed {request.task_type} with {len(transcript)} bounded tool transcript entries. "
            f"Goal: {request.goal}. No destructive or external-network actions were used."
        )
        return ToolExecutionResult(output=output, tool_transcript=transcript, artifact_refs=[])
