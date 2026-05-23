from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

import structlog

from app.schemas.agent import AgentRequest, AgentResponse
from app.schemas.events import ExecutionEvent, EvaluationEvent, IncidentEvent, PolicyDecisionEvent
from app.services.evaluator import Evaluator
from app.services.policy_engine import PolicyEngine
from app.services.prompt_sanitizer import sanitize_prompt

logger = structlog.get_logger()


@dataclass
class OrchestrationResult:
    response: AgentResponse
    execution_output: str
    evaluation_summary: dict


class DummyToolExecutor:
    # Safe demo executor. Replace with real tool execution after ACL, sandbox, and approval gates.
    def execute(self, request: AgentRequest) -> str:
        tool_budget = min(request.constraints.max_tool_calls, 3)
        return (
            f"Executed {request.task_type} with {tool_budget} bounded tool steps. "
            f"Goal: {request.goal}. "
            "No destructive or external-network actions were used."
        )


class AgentOrchestrator:
    def __init__(self, policy_engine: PolicyEngine, evaluator: Evaluator):
        self.policy_engine = policy_engine
        self.evaluator = evaluator
        self.executor = DummyToolExecutor()

    def run(self, request: AgentRequest) -> OrchestrationResult:
        run_id = str(uuid4())
        trace_id = str(uuid4())

        prompt, prompt_flags = sanitize_prompt(request.prompt)
        policy = self.policy_engine.evaluate(
            request=request.model_copy(update={"prompt": prompt}),
            prompt_flags=prompt_flags,
        )

        if policy.decision.value in {"deny", "escalate"}:
            status = "escalated" if policy.decision.value == "escalate" else "denied"
            response = AgentResponse(
                run_id=run_id,
                status=status,
                decision_reason=policy.reason,
                risk_score=policy.risk_score,
                policy_flags=policy.flags,
            )
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
                evaluation_summary={
                    "event": PolicyDecisionEvent(
                        event_id=str(uuid4()),
                        trace_id=trace_id,
                        run_id=run_id,
                        ts=datetime.now(timezone.utc).isoformat(),
                        payload=policy.model_dump(),
                    ).model_dump(),
                },
            )

        execution_output = self.executor.execute(request)
        evaluation = self.evaluator.evaluate(request, execution_output, policy.flags)
        evaluation.run_id = run_id

        status = "completed" if evaluation.verdict == "pass" else "queued"
        response = AgentResponse(
            run_id=run_id,
            status=status,
            decision_reason=policy.reason,
            risk_score=policy.risk_score,
            policy_flags=policy.flags,
        )

        summary = {
            "policy": policy.model_dump(),
            "execution_event": ExecutionEvent(
                event_id=str(uuid4()),
                trace_id=trace_id,
                run_id=run_id,
                ts=datetime.now(timezone.utc).isoformat(),
                payload={"output_preview": execution_output[:512]},
            ).model_dump(),
            "evaluation_event": EvaluationEvent(
                event_id=str(uuid4()),
                trace_id=trace_id,
                run_id=run_id,
                ts=datetime.now(timezone.utc).isoformat(),
                payload=evaluation.model_dump(),
            ).model_dump(),
        }

        if evaluation.verdict != "pass":
            summary["incident_event"] = IncidentEvent(
                event_id=str(uuid4()),
                trace_id=trace_id,
                run_id=run_id,
                ts=datetime.now(timezone.utc).isoformat(),
                payload={
                    "severity": "medium",
                    "code": "eval_review_required",
                    "detail": evaluation.explanation,
                },
            ).model_dump()

        return OrchestrationResult(
            response=response,
            execution_output=execution_output,
            evaluation_summary=summary,
        )
