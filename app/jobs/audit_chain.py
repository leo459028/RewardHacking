"""Audit chain job (v1.6.1 D34): ``python -m app.jobs.audit_chain --checkpoint | --verify | --export-checkpoint ID``.

- ``--checkpoint``: sign the current head (run daily by an external scheduler, like the retention job, so several
  API replicas never write checkpoints concurrently). Nothing new since the last checkpoint -> nothing is written.
- ``--verify [--from-seq N] [--to-seq M] [--public-keys FILE]``: full verification (no entry limit). With
  ``--public-keys`` only the keys in that file are trusted (the verifier's own copy, kept outside the database).
- ``--export-checkpoint acp_...``: print the checkpoint statement to store outside the database.

Output is one JSON object (hashes and identifiers only). Exit codes: 0 ok / written / nothing to do, 1 the chain
does not verify (or the checkpoint does not exist), 2 the job could not run (configuration or database error).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models.audit_chain import AuditChainCheckpoint
from app.services import audit_chain

JOB_ACTOR = "job:audit_chain"


async def run_job(args: argparse.Namespace, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, dict[str, Any]]:
    async with session_factory() as session:
        try:
            if args.checkpoint:
                result = await audit_chain.create_checkpoint(session, settings, actor=JOB_ACTOR)
                body: dict[str, Any] = {"action": "checkpoint", "created": result.created, "reason": result.reason}
                if result.checkpoint is not None:
                    body["checkpoint"] = audit_chain.checkpoint_to_dict(result.checkpoint)
                return 0, body
            if args.export_checkpoint:
                checkpoint = await session.scalar(select(AuditChainCheckpoint).where(AuditChainCheckpoint.checkpoint_id == args.export_checkpoint))
                if checkpoint is None:
                    return 1, {"action": "export_checkpoint", "error": {"code": "audit_chain_checkpoint_not_found"}}
                return 0, {"action": "export_checkpoint", **audit_chain.export_checkpoint(checkpoint)}
            result = await audit_chain.verify(
                session, settings, from_seq=args.from_seq, to_seq=args.to_seq, public_keys_file=args.public_keys
            )
            return (0 if result["status"] == audit_chain.OK else 1), {"action": "verify", **result}
        except audit_chain.AuditChainError as exc:
            return 2, {"action": _action(args), "error": {"code": exc.code, "message": exc.message}}


def _action(args: argparse.Namespace) -> str:
    return "checkpoint" if args.checkpoint else ("export_checkpoint" if args.export_checkpoint else "verify")


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.audit_chain", description=__doc__.splitlines()[0], allow_abbrev=False)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--checkpoint", action="store_true", help="sign the current chain head")
    group.add_argument("--verify", action="store_true", help="verify the whole chain (or a range)")
    group.add_argument("--export-checkpoint", metavar="ACP_ID", help="print a checkpoint statement")
    parser.add_argument("--from-seq", type=int, default=None)
    parser.add_argument("--to-seq", type=int, default=None)
    parser.add_argument("--public-keys", default=None, help="PEM file with the only public keys to trust")
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

    try:
        code, payload = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - a database or configuration error is exit 2, never a traceback with data
        code, payload = 2, {"action": _action(args), "error": {"code": "audit_chain_job_failed", "message": type(exc).__name__}}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
