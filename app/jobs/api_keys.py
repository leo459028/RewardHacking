"""Database API key CLI (v1.6.3 plan D9): ``python -m app.jobs.api_keys <command>``.

Outside ``APP_ENV=dev`` / ``test`` environment API keys (``API_KEYS``) are refused, so a deployment that only has the
API needs another way to get its first key; this is it, next to ``python -m app.jobs.users`` for the first admin.

Commands:
    create --tenant <id> --role <role> --name <name>
        Creates an active database API key. The plaintext key is printed **once**, in this command's output; only
        its hash is stored. The ``api_key.created`` governance event (actor ``cli``) goes into the audit chain in the
        same transaction as the key.
    list [--tenant <id>] [--created-by env|key|user|cli|unknown] [--include-revoked]
        Lists keys with their creator (``app.services.api_key_provenance``), active keys only unless
        ``--include-revoked``. ``list --created-by env`` gives the same keys as the ``env_created_api_keys``
        operations check.

Output is one JSON object per line. Exit codes: 0 success, 2 refused (the JSON carries the error code).

    docker compose exec -T api python -m app.jobs.api_keys create --tenant default --role admin --name first-admin-key
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.models.tenant import Tenant, TenantApiKey
from app.services import api_key_provenance
from app.services.auth import generate_api_key, hash_api_key
from app.services.governance_audit import add_governance_event

CLI_ACTOR = api_key_provenance.CLI_ACTOR
# The same roles ``POST /v1/tenants/{tenant_id}/api-keys`` accepts.
API_KEY_ROLES = (
    "viewer",
    "operator",
    "admin",
    "super_admin",
    "finops_admin",
    "tenant_admin",
    "reviewer",
    "auditor",
    "end_user",
)
MAX_NAME_LENGTH = 128


class _Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def key_view(key: TenantApiKey, creator: api_key_provenance.KeyCreator | None = None) -> dict[str, Any]:
    """A key without its hash (and, of course, without the plaintext)."""
    view: dict[str, Any] = {
        "key_id": key.key_id,
        "tenant_id": key.tenant_id,
        "name": key.name,
        "role": key.role,
        "status": key.status,
        "key_prefix": key.key_prefix,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
    }
    if creator is not None:
        view["created_by"] = creator.as_dict()
    return view


async def create_api_key(session: AsyncSession, *, tenant_id: str, role: str, name: str) -> tuple[TenantApiKey, str]:
    tenant_id = tenant_id.strip()
    name = name.strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        raise _Refused("invalid_name", f"--name must be 1-{MAX_NAME_LENGTH} characters.")
    if role not in API_KEY_ROLES:
        raise _Refused("invalid_role", "Role must be a supported RBAC role.")
    tenant = await session.scalar(select(Tenant).where(Tenant.tenant_id == tenant_id))
    if tenant is None:
        raise _Refused("tenant_not_found", f"Tenant {tenant_id!r} does not exist.")
    if tenant.status != "active":
        raise _Refused("tenant_inactive", f"Tenant {tenant_id!r} is not active; its keys would be refused (403).")
    plaintext_key = generate_api_key()
    key = TenantApiKey(
        tenant_id=tenant_id,
        key_id=f"key_{uuid.uuid4().hex[:12]}",
        name=name,
        key_prefix=plaintext_key[:8],
        key_hash=hash_api_key(plaintext_key),
        role=role,
        status="active",
        revoked=False,
    )
    session.add(key)
    add_governance_event(
        session,
        tenant_id=tenant_id,
        user_id=CLI_ACTOR,
        event_type=api_key_provenance.API_KEY_CREATED,
        decision="created",
        reason=f"API key {key.key_id} ({role}) created from the CLI.",
        details={"key_id": key.key_id, "role": role, "key_prefix": key.key_prefix, "name": name},
    )
    await session.commit()
    await session.refresh(key)
    return key, plaintext_key


async def run_command(args: argparse.Namespace, session_factory: async_sessionmaker[AsyncSession]) -> tuple[int, dict[str, Any]]:
    command = args.command
    async with session_factory() as session:
        try:
            if command == "create":
                key, plaintext_key = await create_api_key(session, tenant_id=args.tenant, role=args.role, name=args.name)
                creators = await api_key_provenance.creators_for_keys(session, [key])
                return 0, {
                    "action": "create",
                    "api_key": key_view(key, creators[key.key_id]),
                    "plaintext_key": plaintext_key,
                    "notice": "The plaintext key is shown only once and is not stored; keep it in a secret manager now.",
                }
            rows = await api_key_provenance.list_keys_with_creators(
                session, tenant_id=args.tenant, created_by=args.created_by, active_only=not args.include_revoked
            )
            return 0, {
                "action": "list",
                "filter": {"tenant": args.tenant, "created_by": args.created_by, "include_revoked": args.include_revoked},
                "count": len(rows),
                "api_keys": [key_view(key, creator) for key, creator in rows],
            }
        except _Refused as exc:
            await session.rollback()
            return 2, {"action": command, "error": {"code": exc.code, "message": exc.message}}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.jobs.api_keys", description=__doc__.splitlines()[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", allow_abbrev=False, help="create a database API key (plaintext printed once)")
    create.add_argument("--tenant", required=True)
    create.add_argument("--role", required=True, choices=API_KEY_ROLES)
    create.add_argument("--name", required=True)
    listing = sub.add_parser("list", allow_abbrev=False, help="list API keys with their creator")
    listing.add_argument("--tenant", default=None)
    listing.add_argument("--created-by", default=None, choices=api_key_provenance.CREATOR_KINDS)
    listing.add_argument("--include-revoked", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()

    async def _run() -> tuple[int, dict[str, Any]]:
        if session_factory is not None:
            return await run_command(args, session_factory)
        from app.infra import postgres

        postgres.init_engine(settings)
        try:
            return await run_command(args, postgres.SessionLocal)
        finally:
            await postgres.close_engine()

    code, payload = asyncio.run(_run())
    sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
