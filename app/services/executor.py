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


class BoundedToolExecutor:
    def execute(self, request: AgentRequest) -> ToolExecutionResult:
        task_allowlist = TASK_TOOL_ALLOWLIST.get(request.task_type, TASK_TOOL_ALLOWLIST["other"])
        tool_budget = min(request.constraints.max_tool_calls, 3)
        requested_tools = request.allowed_tools or ["summarize"]
        transcript: list[dict[str, object]] = []
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
            if tool_name not in task_allowlist:
                transcript.append(
                    {
                        "tool_name": tool_name,
                        "status": "blocked",
                        "detail": f"Tool is not allowed for task type {request.task_type}.",
                        "sandbox_policy": sandbox_policy,
                        "artifact_refs": [],
                    }
                )
                continue
            transcript.append(
                {
                    "tool_name": tool_name,
                    "status": "simulated",
                    "detail": "Bounded v0.3 executor recorded the tool action without external side effects.",
                    "sandbox_policy": sandbox_policy,
                    "artifact_refs": [],
                }
            )

        output = (
            f"Executed {request.task_type} with {len(transcript)} bounded tool transcript entries. "
            f"Goal: {request.goal}. No destructive or external-network actions were used."
        )
        return ToolExecutionResult(output=output, tool_transcript=transcript, artifact_refs=[])
