"""Retention purge job (v1.6.0 D16): ``python -m app.jobs.retention --plan`` / ``--apply-latest``.

Scheduling is left to an external scheduler so that several API replicas never purge concurrently.
``--plan`` stores a plan created by ``job:retention``; ``--apply-latest`` applies only the newest plan of that
same job that is still inside ``AUDIT_RETENTION_PLAN_TTL_MINUTES`` (and only when
``AUDIT_RETENTION_PURGE_MODE=apply``). Output is one JSON object without any run content.
Exit codes: 0 success, 1 nothing to apply, 2 refused (the JSON carries the error code).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.services import retention


async def run_job(args: argparse.Namespace, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, dict[str, Any]]:
    async with session_factory() as session:
        try:
            if args.plan:
                purge = await retention.build_plan(session, settings, actor=retention.JOB_ACTOR)
                return 0, {"action": "plan", **_summary(purge)}
            purge = await retention.latest_applicable_plan(session, created_by=retention.JOB_ACTOR)
            if purge is None:
                return 1, {"action": "apply_latest", "error": {"code": "retention_no_applicable_plan"}}
            purge = await retention.apply_plan(
                session, settings, purge.purge_id, plan_digest=purge.plan_digest, confirm=True, actor=retention.JOB_ACTOR
            )
            return 0, {"action": "apply_latest", **_summary(purge)}
        except retention.RetentionError as exc:
            return 2, {"action": "plan" if args.plan else "apply_latest", "error": {"code": exc.code, "message": exc.message}}


def _summary(purge: Any) -> dict[str, Any]:
    data = retention.purge_to_dict(purge)
    return {
        "purge_id": data["purge_id"],
        "status": data["status"],
        "plan_digest": data["plan_digest"],
        "candidate_count": len(data["candidate_run_ids"]),
        "excluded": {reason: item["count"] for reason, item in data["excluded_summary"].items()},
        "applied_counts": data["applied_counts"],
        "skipped_at_apply": len(data["skipped_at_apply"]),
        "failures": len(data["failures"]),
        "receipt_digest": data["receipt_digest"],
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.retention", description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", action="store_true", help="create a purge plan (read-only)")
    group.add_argument("--apply-latest", action="store_true", help="apply the newest unexpired plan of this job")
    args = parser.parse_args(argv)
    settings = settings or get_settings()

    async def _run() -> tuple[int, dict[str, Any]]:
        if session_factory is not None:
            return await run_job(args, settings, session_factory)
        from app.infra import postgres

        postgres.init_engine(settings)
        try:
            return await run_job(args, settings, postgres.SessionLocal)
        finally:
            await postgres.close_engine()

    code, payload = asyncio.run(_run())
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
