"""Pluggable, advisory LLM-as-judge providers (v1.5.10).

A judge only orders review work (``triage_priority`` P1–P3, ``triage_score``). It never writes a
verdict, a review priority, a review status or a run status, and never takes part in hard-deny
decisions (AGENTS.md 2.3). Any primary-provider failure falls back to the deterministic provider.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.services.evaluation.judges.base import JudgeInput, JudgeOutput, JudgeProvider, triage_priority_for
from app.services.evaluation.judges.deterministic import DETERMINISTIC_VERSION, DeterministicJudge
from app.services.evaluation.judges.openai_compatible import OpenAICompatibleJudge

__all__ = [
    "DETERMINISTIC_VERSION",
    "DeterministicJudge",
    "JudgeInput",
    "JudgeOutput",
    "JudgeProvider",
    "JudgeResult",
    "OpenAICompatibleJudge",
    "assess_with_fallback",
    "get_judge",
    "triage_priority_for",
]

# Tests inject an ``httpx.MockTransport`` here; production always uses the real network stack.
_TEST_TRANSPORT: httpx.AsyncBaseTransport | None = None


def set_test_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _TEST_TRANSPORT
    _TEST_TRANSPORT = transport


def get_judge(settings: Settings, *, provider: str | None = None) -> JudgeProvider:
    name = provider or settings.effective_judge_provider
    if name == "openai_compatible":
        return OpenAICompatibleJudge(
            base_url=settings.evaluation_judge_base_url,
            model=settings.evaluation_judge_model,
            api_key=settings.evaluation_judge_api_key.get_secret_value(),
            timeout_seconds=settings.evaluation_judge_timeout_seconds,
            max_output_tokens=settings.evaluation_judge_max_output_tokens,
            allow_external=settings.evaluation_judge_allow_external,
            internal_suffixes=settings.mcp_internal_host_suffix_list,
            transport=_TEST_TRANSPORT,
        )
    return DeterministicJudge()


@dataclass
class JudgeResult:
    """Primary output plus the effective triage values (fallback when the primary did not succeed)."""

    primary: JudgeOutput
    fallback: JudgeOutput | None
    triage_score: float
    triage_priority: str
    triage_source: str
    injection_suspected: bool

    @property
    def status(self) -> str:
        return self.primary.status


async def assess_with_fallback(
    judge_input: JudgeInput,
    settings: Settings,
    *,
    judge: JudgeProvider | None = None,
    skip_primary_status: str = "",
) -> JudgeResult:
    """Run the configured judge; on any failure (or when skipped, e.g. ``budget_exceeded``) use the deterministic one."""
    deterministic = DeterministicJudge()
    primary_judge = judge or get_judge(settings)
    if skip_primary_status:
        primary = JudgeOutput(
            provider=primary_judge.name,
            model=getattr(primary_judge, "model", ""),
            judge_version=primary_judge.version(),
            status=skip_primary_status,
            error=skip_primary_status,
        )
    else:
        try:
            primary = await primary_judge.assess(judge_input)
        except Exception as exc:  # noqa: BLE001 - a judge failure must never break evaluation
            primary = JudgeOutput(
                provider=primary_judge.name,
                model=getattr(primary_judge, "model", ""),
                judge_version=primary_judge.version(),
                status="error",
                error=type(exc).__name__,
            )
    fallback: JudgeOutput | None = None
    if primary.ok:
        effective = primary
    else:
        fallback = deterministic.assess_sync(judge_input)
        effective = fallback
    score = float(effective.suspicion or 0.0)
    return JudgeResult(
        primary=primary,
        fallback=fallback,
        triage_score=round(score, 6),
        triage_priority=triage_priority_for(score, injection_suspected=judge_input.injection_suspected),
        triage_source=effective.judge_version,
        injection_suspected=judge_input.injection_suspected,
    )
