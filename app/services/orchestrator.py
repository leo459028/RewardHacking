from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from app.schemas.agent import AgentRequest, AgentResponse
from app.schemas.events import ExecutionEvent, EvaluationEvent, IncidentEvent, PolicyDecisionEvent
from app.schemas.policy import PolicyDecision
from app.services.executor import BoundedToolExecutor, ToolExecutionResult
from app.services.evaluator import Evaluator
from app.services.policy_engine import PolicyEngine
from app.services.prompt_sanitizer import sanitize_prompt

logger = structlog.get_logger()


@dataclass
class OrchestrationResult:
    response: AgentResponse
    execution_output: str
    evaluation_summary: dict


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentOrchestrator:
    def __init__(self, policy_engine: PolicyEngine, evaluator: Evaluator):
        self.policy_engine = policy_engine
        self.evaluator = evaluator
        self.executor = BoundedToolExecutor()

    def _evaluate_policy(
        self,
        request: AgentRequest,
        disabled_tools: list[str] | None,
    ) -> tuple[AgentRequest, PolicyDecision]:
        prompt, prompt_flags = sanitize_prompt(request.prompt)
        cleaned_request = request.model_copy(update={"prompt": prompt})
        policy = self.policy_engine.evaluate(
            request=cleaned_request,
            prompt_flags=prompt_flags,
            disabled_tools=disabled_tools,
        )
        return cleaned_request, policy

    def _policy_event(self, policy: PolicyDecision, run_id: str, trace_id: str) -> dict:
        return PolicyDecisionEvent(
            event_id=str(uuid4()),
            trace_id=trace_id,
            run_id=run_id,
            ts=_now_iso(),
            payload=policy.model_dump(),
        ).model_dump()

    def _submission_summary(
        self,
        *,
        run_id: str,
        trace_id: str,
        status: str,
        policy: PolicyDecision,
    ) -> dict:
        policy_event = self._policy_event(policy, run_id, trace_id)
        summary = {
            "policy": policy.model_dump(),
            "policy_event": policy_event,
            "event_timeline": [
                {
                    "event_type": "policy.decision",
                    "status": policy.decision.value,
                    "ts": policy_event["ts"],
                },
                {
                    "event_type": "run.state",
                    "status": status,
                    "ts": _now_iso(),
                },
            ],
            "run_state": {"status": status, "updated_at": _now_iso()},
        }
        if status == "awaiting_approval":
            summary["approval"] = {
                "status": "pending",
                "reason": policy.reason,
                "requested_at": _now_iso(),
            }
        return summary

    def submit(
        self,
        request: AgentRequest,
        *,
        disabled_tools: list[str] | None = None,
    ) -> OrchestrationResult:
        run_id = str(uuid4())
        trace_id = str(uuid4())
        _cleaned_request, policy = self._evaluate_policy(request, disabled_tools)

        if policy.decision.value == "deny":
            status = "denied"
        elif policy.requires_human_review:
            status = "awaiting_approval"
        else:
            status = "queued"

        response = AgentResponse(
            run_id=run_id,
            status=status,
            decision_reason=policy.reason,
            risk_score=policy.risk_score,
            policy_flags=policy.flags,
        )

        if status in {"denied", "awaiting_approval"}:
            logger.warning(
                "policy_block",
                run_id=run_id,
                trace_id=trace_id,
                reason=policy.reason,
                risk_score=policy.risk_score,
                flags=policy.flags,
            )

        return OrchestrationResult(
            response=response,
            execution_output="",
            evaluation_summary=self._submission_summary(
                run_id=run_id,
                trace_id=trace_id,
                status=status,
                policy=policy,
            ),
        )

    def execute(
        self,
        request: AgentRequest,
        *,
        run_id: str | None = None,
        disabled_tools: list[str] | None = None,
    ) -> OrchestrationResult:
        run_id = run_id or str(uuid4())
        trace_id = str(uuid4())
        cleaned_request, policy = self._evaluate_policy(request, disabled_tools)

        if policy.decision.value == "deny" or policy.requires_human_review:
            status = "denied" if policy.decision.value == "deny" else "awaiting_approval"
            response = AgentResponse(
                run_id=run_id,
                status=status,
                decision_reason=policy.reason,
                risk_score=policy.risk_score,
                policy_flags=policy.flags,
            )
            return OrchestrationResult(
                response=response,
                execution_output="",
                evaluation_summary=self._submission_summary(
                    run_id=run_id,
                    trace_id=trace_id,
                    status=status,
                    policy=policy,
                ),
            )

        execution: ToolExecutionResult = self.executor.execute(cleaned_request)
        evaluation = self.evaluator.evaluate(cleaned_request, execution.output, policy.flags)
        evaluation.run_id = run_id

        status = "completed" if evaluation.verdict == "pass" else "awaiting_approval"
        response = AgentResponse(
            run_id=run_id,
            status=status,
            decision_reason=policy.reason,
            risk_score=policy.risk_score,
            policy_flags=policy.flags,
        )

        summary = {
            "policy": policy.model_dump(),
            "policy_event": self._policy_event(policy, run_id, trace_id),
            "execution_event": ExecutionEvent(
                event_id=str(uuid4()),
                trace_id=trace_id,
                run_id=run_id,
                ts=_now_iso(),
                payload={
                    "output_preview": execution.output[:512],
                    "tool_transcript": execution.tool_transcript,
                    "artifact_refs": execution.artifact_refs,
                },
            ).model_dump(),
            "evaluation_event": EvaluationEvent(
                event_id=str(uuid4()),
                trace_id=trace_id,
                run_id=run_id,
                ts=_now_iso(),
                payload=evaluation.model_dump(),
            ).model_dump(),
            "run_state": {"status": status, "updated_at": _now_iso()},
        }
        summary["event_timeline"] = [
            {
                "event_type": "policy.decision",
                "status": policy.decision.value,
                "ts": summary["policy_event"]["ts"],
            },
            {
                "event_type": "execution.result",
                "status": "completed",
                "ts": summary["execution_event"]["ts"],
            },
            {
                "event_type": "evaluation.result",
                "status": evaluation.verdict,
                "ts": summary["evaluation_event"]["ts"],
            },
            {
                "event_type": "run.state",
                "status": status,
                "ts": summary["run_state"]["updated_at"],
            },
        ]

        if evaluation.verdict != "pass":
            summary["incident_event"] = IncidentEvent(
                event_id=str(uuid4()),
                trace_id=trace_id,
                run_id=run_id,
                ts=_now_iso(),
                payload={
                    "severity": "medium",
                    "code": "eval_review_required",
                    "detail": evaluation.explanation,
                },
            ).model_dump()

        return OrchestrationResult(
            response=response,
            execution_output=execution.output,
            evaluation_summary=summary,
        )

    def run(self, request: AgentRequest) -> OrchestrationResult:
        return self.execute(request)
