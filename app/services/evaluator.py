"""Backward-compatible evaluator facade.

Since v1.5.8 evaluation is performed by ``app.services.evaluation.EvaluationEngine``. The legacy
``evaluate(request, result_text, policy_flags)`` signature is kept for existing call sites and
wraps the input as ``simulated`` evidence. Outcome evidence goes through ``evaluate_context``.
"""

from __future__ import annotations

from app.schemas.agent import AgentRequest
from app.schemas.evaluation import EvaluationResult
from app.services.evaluation.engine import EvaluationEngine
from app.services.evaluation.evidence import EvaluationContext, context_from_executor


class Evaluator:
    def __init__(self, engine: EvaluationEngine | None = None):
        self.engine = engine or EvaluationEngine()

    def evaluate(
        self,
        request: AgentRequest,
        result_text: str,
        policy_flags: list[str],
    ) -> EvaluationResult:
        ctx = context_from_executor(
            task_type=request.task_type,
            goal=request.goal,
            metadata=request.metadata if isinstance(request.metadata, dict) else {},
            result_text=result_text,
            policy_flags=policy_flags,
        )
        return self.engine.evaluate_context(ctx)

    def evaluate_context(self, ctx: EvaluationContext) -> EvaluationResult:
        return self.engine.evaluate_context(ctx)
