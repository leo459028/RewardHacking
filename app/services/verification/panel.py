"""What the Run Detail and Review Detail pages show about platform re-runs (v1.6.2 D39)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.agent_run import AgentRun
from app.services.access.permissions import has_permission, view_allowed
from app.services.sandbox import gate
from app.services.verification import assurance, sampling

HISTORY_LIMIT = 10
VIEW_PERMISSION, VIEW = "verification.view", "verification"


def visible_to(principal: Any) -> bool:
    """The pages are open to more roles than the re-run results are."""
    return principal is not None and has_permission(principal.role, VIEW_PERMISSION) and view_allowed(principal.tenant_id, VIEW)


async def for_run(session: AsyncSession, settings: Settings, run: AgentRun) -> dict[str, Any]:
    rows = await sampling.list_verifications(session, run_id=run.run_id, limit=HISTORY_LIMIT)
    items = [sampling.verification_to_dict(row) for row in rows]
    suite_id = sampling.bound_suite_id(run)
    can_rerun = bool(suite_id) and await sampling.has_outcome(session, run.run_id)
    return {
        "records": items,
        "latest": items[0] if items else None,
        "suite_id": suite_id,
        "can_rerun": can_rerun,
        "replay_ready": can_rerun and (await gate.evaluate(session, settings)).ready,
        "note": assurance.ASSURANCE_NOTE,
        "note_zh": assurance.ASSURANCE_NOTE_ZH,
    }
