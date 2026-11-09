"""v1.5.11 background judge triage (plan D10–D12).

A slow local model must not hold up ``POST /v1/runs/{id}/outcome`` or the worker. Triage already runs after the
verdict and review are committed and falls back on any failure, so moving it off the request path cannot weaken
safety: the review is visible immediately with ``triage_source="pending:<profile>"``, and the assessment fills the
four triage columns when it completes.

- ``dispatch_triage()`` is the single entry point used by the outcome and worker paths: ``inline`` profiles assess
  now (v1.5.10 behaviour), ``background`` profiles mark the review pending and schedule an in-process task.
- Background tasks use their own session (never the request session) and a per-event-loop semaphore
  (``EVALUATION_JUDGE_BACKGROUND_CONCURRENCY``, default 1 so a local model is not hit twice at once).
- Tasks lost to a restart are re-scheduled by ``sweep_pending_triage()``, which also runs opportunistically (at most
  once a minute per process) whenever new background triage is dispatched.
- A background task still only calls ``triage_review()``: it writes ``judge_assessments`` and the review's triage
  columns, never verdicts, priorities, statuses, Kafka events or leases (D12).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import trace_context, tracing
from app.core.config import Settings
from app.models.evaluation_review import EvaluationReview
from app.models.judge import JudgeAssessment
from app.services.evaluation.judges import build_judge, get_judge
from app.services.evaluation.judges.profiles import get_profile, resolve_active_profile

logger = logging.getLogger(__name__)

PENDING_PREFIX = "pending:"
OPEN_STATUSES = ("open", "needs_info")
SWEEP_SCAN_LIMIT = 200
OPPORTUNISTIC_SWEEP_INTERVAL_SECONDS = 60.0

# Tests inject their session factory here; production uses ``app.infra.postgres.SessionLocal``.
_SESSION_FACTORY_OVERRIDE: Callable[[], Any] | None = None
_TASKS: set[asyncio.Task] = set()
_IN_FLIGHT: set[str] = set()  # review IDs scheduled in this process and not finished yet
_SEMAPHORES: dict[int, tuple[int, asyncio.Semaphore]] = {}
_LAST_OPPORTUNISTIC_SWEEP = 0.0


def _session_factory() -> Callable[[], Any] | None:
    if _SESSION_FACTORY_OVERRIDE is not None:
        return _SESSION_FACTORY_OVERRIDE
    from app.infra import postgres

    return postgres.SessionLocal


def _semaphore(limit: int) -> asyncio.Semaphore:
    """One semaphore per running event loop (asyncio primitives cannot be shared across loops)."""
    loop = asyncio.get_running_loop()
    key = id(loop)
    current = _SEMAPHORES.get(key)
    if current is None or current[0] != limit:
        current = (limit, asyncio.Semaphore(max(1, limit)))
        _SEMAPHORES[key] = current
    return current[1]


def is_pending(review: EvaluationReview) -> bool:
    return (review.triage_source or "").startswith(PENDING_PREFIX)


def pending_profile(review: EvaluationReview) -> str:
    return (review.triage_source or "")[len(PENDING_PREFIX):] if is_pending(review) else ""


async def _run_background(
    review_id: str,
    profile_name: str,
    settings: Settings,
    factory: Callable[[], Any],
    trigger: trace_context.TraceContext | None = None,
) -> None:
    # v1.6.0 D4: background triage is its own trace, linked (not parented) to the span that scheduled it.
    with tracing.start_span(
        "judge.triage.background",
        context=trace_context.new_root(),
        new_span=False,
        links=[trigger] if trigger is not None else None,
        attributes={"rh.review_id": review_id, "rh.judge.profile": profile_name},
    ):
        await _run_background_inner(review_id, profile_name, settings, factory)


async def _run_background_inner(review_id: str, profile_name: str, settings: Settings, factory: Callable[[], Any]) -> None:
    from app.services.judge_triage import triage_review

    async with _semaphore(int(settings.evaluation_judge_background_concurrency)):
        try:
            async with factory() as session:
                review = await session.scalar(select(EvaluationReview).where(EvaluationReview.review_id == review_id))
                if review is None:
                    return
                if review.status not in OPEN_STATUSES:
                    # Decided before the judge got to it: drop the pending marker, spend no tokens.
                    if is_pending(review):
                        review.triage_source = ""
                        await session.commit()
                    return
                profile = get_profile(settings, profile_name) or resolve_active_profile(settings)
                await triage_review(session, review, settings, judge=build_judge(profile, settings))
        except Exception:  # noqa: BLE001 - background triage never raises; the sweep retries pending reviews
            logger.exception("background judge triage failed for review %s", review_id)


def schedule_triage(review_id: str, profile_name: str, settings: Settings) -> asyncio.Task | None:
    """Schedule background triage on the running loop; ``None`` when no session factory is available."""
    factory = _session_factory()
    if factory is None:
        return None
    task = asyncio.get_running_loop().create_task(
        _run_background(review_id, profile_name, settings, factory, trace_context.current())
    )
    _TASKS.add(task)
    _IN_FLIGHT.add(review_id)

    def _done(finished: asyncio.Task) -> None:
        _TASKS.discard(finished)
        _IN_FLIGHT.discard(review_id)

    task.add_done_callback(_done)
    return task


async def drain_background(timeout: float = 10.0) -> None:
    """Wait for background triage tasks on the current loop (used by tests and graceful shutdown)."""
    loop = asyncio.get_running_loop()
    tasks = [task for task in list(_TASKS) if task.get_loop() is loop and not task.done()]
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)


async def mark_pending(session: AsyncSession, review: EvaluationReview, profile_name: str) -> None:
    """Only the triage marker changes (D12); priority, status and findings are untouched."""
    review.triage_source = f"{PENDING_PREFIX}{profile_name}"
    await session.commit()
    await session.refresh(review)


async def dispatch_triage(
    session: AsyncSession, review: EvaluationReview, settings: Settings
) -> tuple[JudgeAssessment | None, bool]:
    """Triage a review with the active profile: inline now, or in the background for ``execution: background``."""
    from app.services.judge_triage import triage_review

    if settings.evaluation_judge_mode == "off":
        return None, False
    from app.services.judge_activation import maybe_refresh

    await maybe_refresh(session, settings)  # v1.5.12 (D7): pick up activations made in another container
    profile = resolve_active_profile(settings)
    if profile.execution == "background" and _session_factory() is not None:
        await mark_pending(session, review, profile.name)
        if schedule_triage(review.review_id, profile.name, settings) is not None:
            await _maybe_opportunistic_sweep(session, settings, exclude={review.review_id})
            return None, False
        logger.warning("background judge triage unavailable; assessing review %s inline", review.review_id)
    return await triage_review(session, review, settings, judge=get_judge(settings, profile=profile))


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def stale_pending_reviews(
    session: AsyncSession, settings: Settings, *, older_than_seconds: float | None = None, now: datetime | None = None
) -> list[EvaluationReview]:
    """Open reviews still pending after the threshold. Compared in Python so SQLite and Postgres behave the same."""
    threshold = settings.evaluation_judge_pending_sweep_seconds if older_than_seconds is None else older_than_seconds
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=float(threshold))
    rows = list(
        (
            await session.scalars(
                select(EvaluationReview)
                .where(EvaluationReview.status.in_(OPEN_STATUSES), EvaluationReview.triage_source.like(f"{PENDING_PREFIX}%"))
                .order_by(EvaluationReview.id.asc())
                .limit(SWEEP_SCAN_LIMIT)
            )
        ).all()
    )
    stale = []
    for row in rows:
        stamp = _aware(row.updated_at) or _aware(row.opened_at)
        if stamp is None or stamp <= cutoff:
            stale.append(row)
    return stale


async def sweep_pending_triage(
    session: AsyncSession,
    settings: Settings,
    *,
    older_than_seconds: float | None = None,
    now: datetime | None = None,
    exclude: set[str] | None = None,
) -> list[str]:
    """Re-schedule (or, without a background factory, assess inline) reviews whose triage task was lost."""
    from app.services.judge_triage import triage_review

    swept: list[str] = []
    for review in await stale_pending_reviews(session, settings, older_than_seconds=older_than_seconds, now=now):
        if (exclude and review.review_id in exclude) or review.review_id in _IN_FLIGHT:
            continue  # still queued or running in this process
        profile = get_profile(settings, pending_profile(review)) or resolve_active_profile(settings)
        if schedule_triage(review.review_id, profile.name, settings) is None:
            await triage_review(session, review, settings, judge=build_judge(profile, settings))
        swept.append(review.review_id)
    return swept


async def _maybe_opportunistic_sweep(session: AsyncSession, settings: Settings, *, exclude: set[str]) -> None:
    global _LAST_OPPORTUNISTIC_SWEEP
    now = time.monotonic()
    if now - _LAST_OPPORTUNISTIC_SWEEP < OPPORTUNISTIC_SWEEP_INTERVAL_SECONDS:
        return
    _LAST_OPPORTUNISTIC_SWEEP = now
    try:
        swept = await sweep_pending_triage(session, settings, exclude=exclude)
        if swept:
            logger.info("re-scheduled %d stale pending judge triage task(s)", len(swept))
    except Exception:  # noqa: BLE001
        logger.exception("opportunistic pending-triage sweep failed")


async def pending_summary(session: AsyncSession, settings: Settings, *, now: datetime | None = None) -> dict[str, Any]:
    """Counts for the readiness check and status API."""
    current = now or datetime.now(timezone.utc)
    rows = list(
        (
            await session.scalars(
                select(EvaluationReview)
                .where(EvaluationReview.status.in_(OPEN_STATUSES), EvaluationReview.triage_source.like(f"{PENDING_PREFIX}%"))
                .limit(SWEEP_SCAN_LIMIT)
            )
        ).all()
    )
    ages = [(current - stamp).total_seconds() for row in rows if (stamp := _aware(row.updated_at) or _aware(row.opened_at)) is not None]
    oldest = max(ages) if ages else 0.0
    threshold = float(settings.evaluation_judge_pending_sweep_seconds)
    return {
        "pending": len(rows),
        "oldest_pending_seconds": round(oldest, 1),
        "stale": sum(1 for age in ages if age >= threshold),
        "sweep_threshold_seconds": threshold,
        "in_flight_tasks": sum(1 for task in _TASKS if not task.done()),
    }
