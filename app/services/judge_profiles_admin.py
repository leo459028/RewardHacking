"""v1.5.11 judge profile administration: listing, connectivity probe and local zero-cost pricing (plan D13, D18).

- ``profiles_view()``: safe view of every profile (no keys, host only) with the effective external flag and the
  structured-output mode the backend actually accepted.
- ``probe_profile()``: sends one fixed, harmless synthetic input (no tenant data, so no tenant consent is needed;
  the global external switch still applies). It never writes a ``judge_assessments`` row; tokens are ledgered with
  ``metadata.purpose="probe"``.
- ``ensure_zero_cost_pricing()``: creates zero-price input/output rows for a ``qa_local`` profile so local models
  stop raising ``missing_pricing_rule`` violations. Real providers are never zeroed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.finops import PricingCatalog
from app.services.evaluation.judges import JudgeResult, build_judge, triage_priority_for
from app.services.evaluation.judges.base import JudgeInput
from app.services.evaluation.judges.openai_compatible import cached_mode
from app.services.evaluation.judges.profiles import JudgeProfile, get_profile, load_profiles, resolve_active_profile

LOCAL_PRICING_PROVIDER = "qa_local"
PROBE_INPUT = JudgeInput(
    task_type="code_fix",
    goal="Fix a typo in a docstring",
    diff=(
        "diff --git a/app/util.py b/app/util.py\n--- a/app/util.py\n+++ b/app/util.py\n@@ -1,2 +1,2 @@\n"
        ' def value():\n-    """Retrun the value."""\n+    """Return the value."""\n'
    ),
    test_report='{"format": "summary", "summary": {"passed": 3, "failed": 0}}',
    final_message="Fixed the docstring typo. All 3 tests pass.",
    findings=[],
)


class JudgeProfileAdminError(Exception):
    def __init__(self, code: str, detail: str, status_code: int):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code


def _profile_or_error(settings: Settings, name: str) -> JudgeProfile:
    profile = get_profile(settings, name)
    if profile is None:
        raise JudgeProfileAdminError("judge_profile_not_found", f"Judge profile {name!r} is not defined.", 404)
    return profile


def effective_external(profile: JudgeProfile, settings: Settings) -> bool:
    if profile.provider == "deterministic":
        return False
    return bool(getattr(build_judge(profile, settings), "is_external", False))


def profiles_view(settings: Settings) -> list[dict[str, Any]]:
    active = resolve_active_profile(settings).name
    rows = []
    for profile in load_profiles(settings).values():
        judge = build_judge(profile, settings)
        rows.append(
            {
                **profile.public_dict(settings),
                "judge_version": judge.version(),
                "active": profile.name == active,
                "effective_external": effective_external(profile, settings),
                "accepted_structured_output": cached_mode(profile.base_url, profile.model) if profile.provider != "deterministic" else "",
            }
        )
    return sorted(rows, key=lambda row: (not row["active"], row["name"] != "deterministic", row["name"]))


async def probe_profile(session: AsyncSession, settings: Settings, name: str, *, tenant_id: str = "default") -> dict[str, Any]:
    from app.services.judge_triage import _record_judge_usage

    profile = _profile_or_error(settings, name)
    judge = build_judge(profile, settings)
    output = await judge.assess(PROBE_INPUT)
    result = JudgeResult(
        primary=output,
        fallback=None,
        triage_score=float(output.suspicion or 0.0),
        triage_priority=triage_priority_for(output.suspicion),
        triage_source=output.judge_version,
        injection_suspected=False,
    )
    usage_id = await _record_judge_usage(
        session, settings=settings, tenant_id=tenant_id, run_id="", result=result, extra={"purpose": "probe", "judge_profile": profile.name}
    )
    return {
        "profile": profile.name,
        "provider": output.provider,
        "model": output.model,
        "judge_version": output.judge_version,
        "status": output.status,
        "error": output.error,
        "suspicion": output.suspicion,
        "structured_output": output.structured_output,
        "attempts": output.attempts,
        "latency_ms": output.latency_ms,
        "input_tokens": output.input_tokens,
        "output_tokens": output.output_tokens,
        "effective_external": effective_external(profile, settings),
        "usage_id": usage_id,
        "assessment_written": False,
    }


async def _pricing_rows(session: AsyncSession, provider: str, model: str) -> list[PricingCatalog]:
    return list(
        (
            await session.scalars(
                select(PricingCatalog).where(
                    PricingCatalog.provider == provider,
                    PricingCatalog.model_name == model,
                    PricingCatalog.resource_type == "model",
                    PricingCatalog.is_active.is_(True),
                )
            )
        ).all()
    )


async def ensure_zero_cost_pricing(session: AsyncSession, settings: Settings, name: str) -> dict[str, Any]:
    from app.schemas.finops import PricingCatalogCreateRequest
    from app.services.finops import create_pricing_item

    profile = _profile_or_error(settings, name)
    if profile.provider == "deterministic":
        raise JudgeProfileAdminError("judge_profile_not_priced", "The deterministic judge uses no tokens.", 409)
    if profile.pricing_provider != LOCAL_PRICING_PROVIDER:
        raise JudgeProfileAdminError(
            "judge_profile_not_local",
            f"Zero-cost pricing is only created for pricing_provider '{LOCAL_PRICING_PROVIDER}' (got '{profile.pricing_provider}').",
            409,
        )
    model = profile.effective_pricing_model
    existing = {str(row.token_type) for row in await _pricing_rows(session, LOCAL_PRICING_PROVIDER, model)}
    created: list[str] = []
    for token_type in ("input", "output"):
        if token_type in existing:
            continue
        row = await create_pricing_item(
            session,
            PricingCatalogCreateRequest(
                provider=LOCAL_PRICING_PROVIDER,
                resource_type="model",
                resource_subtype="chat_completion",
                model_name=model,
                token_type=token_type,
                price_unit="per_1m_tokens",
                unit_price=Decimal("0"),
                currency="USD",
                source_url=f"manual://judge-profile/{profile.name}",
                source_type="manual",
                pricing_version="judge-profile-local",
            ),
        )
        created.append(row.pricing_id)
    return {"profile": profile.name, "provider": LOCAL_PRICING_PROVIDER, "model": model, "created": created, "already_present": sorted(existing)}


async def missing_pricing(session: AsyncSession, settings: Settings, profile: JudgeProfile) -> bool:
    if profile.provider == "deterministic":
        return False
    return not await _pricing_rows(session, profile.pricing_provider, profile.effective_pricing_model)
