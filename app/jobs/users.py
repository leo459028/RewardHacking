"""Dashboard user CLI (v1.6.1 plan D14): ``python -m app.jobs.users <command>``.

Commands: ``create``, ``list``, ``disable``, ``enable``, ``unlock``, ``reset-password``, ``set-role``.
Passwords are read from standard input (prompted twice on a terminal, first line otherwise) or from the environment
variable named by ``--password-env``; never from a command-line argument, so they stay out of shell history.
Output is one JSON object per line without passwords or hashes. New and reset accounts must change their password
at first sign-in. Exit codes: 0 success, 2 refused (the JSON carries the error code).

Create the first admin:
    python -m app.jobs.users create --username alice --role admin --password-env RH_NEW_PASSWORD
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import sys
from typing import Any, Sequence, TextIO

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.services.access import permissions, users
from app.services.access.errors import AccessError

CLI_ACTOR = "cli"
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


class _PasswordInputError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def read_password(args: argparse.Namespace, stdin: TextIO) -> str:
    if args.password_env:
        value = os.environ.get(args.password_env)
        if not value:
            # Name the variable only when it looks like one, so a password typed here by mistake is not echoed.
            name = args.password_env if _ENV_NAME.fullmatch(args.password_env) else "given in --password-env"
            raise _PasswordInputError("password_env_missing", f"Environment variable {name} is not set or empty.")
        return value
    if stdin.isatty():
        first = getpass.getpass("New password: ")
        if first != getpass.getpass("Repeat password: "):
            raise _PasswordInputError("password_mismatch", "The two passwords differ.")
        return first
    line = stdin.readline()
    value = line[:-1] if line.endswith("\n") else line
    value = value[:-1] if value.endswith("\r") else value
    if not value:
        raise _PasswordInputError("password_missing", "No password on standard input (or use --password-env).")
    return value


async def run_command(
    args: argparse.Namespace, settings: Settings, session_factory: async_sessionmaker[AsyncSession], stdin: TextIO
) -> tuple[int, dict[str, Any]]:
    command = args.command
    async with session_factory() as session:
        try:
            if command == "list":
                return 0, {"action": "list", "users": [users.user_view(user) for user in await users.list_users(session)]}
            if command == "create":
                user = await users.create_user(
                    session,
                    settings,
                    username=args.username,
                    password=read_password(args, stdin),
                    role=args.role,
                    tenant_id=args.tenant_id,
                    display_name=args.display_name or "",
                    actor=CLI_ACTOR,
                )
            else:
                target = await users.get_user_by_username(session, args.username)
                if target is None:
                    raise AccessError("user_not_found", "User not found.", status_code=404)
                if command in {"disable", "enable"}:
                    user = await users.set_status(
                        session, user_id=target.user_id, status="disabled" if command == "disable" else "active", actor=CLI_ACTOR
                    )
                elif command == "unlock":
                    user = await users.unlock(session, user_id=target.user_id, actor=CLI_ACTOR)
                elif command == "reset-password":
                    user = await users.set_password(
                        session, settings, user_id=target.user_id, new_password=read_password(args, stdin),
                        actor=CLI_ACTOR, must_change_password=True,
                    )
                else:  # set-role
                    user = await users.change_role(
                        session, user_id=target.user_id, role=args.role, tenant_id=args.tenant_id, actor=CLI_ACTOR
                    )
            await session.commit()
            return 0, {"action": command, "user": users.user_view(user)}
        except (AccessError, _PasswordInputError) as exc:
            await session.rollback()
            return 2, {"action": command, "error": {"code": exc.code, "message": exc.message}}


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False: "--password <value>" must not be accepted as an abbreviation of --password-env.
    parser = argparse.ArgumentParser(prog="python -m app.jobs.users", description=__doc__.splitlines()[0], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", allow_abbrev=False, help="create a Dashboard user (must change the password at first sign-in)")
    create.add_argument("--username", required=True)
    create.add_argument("--role", required=True, choices=permissions.ROLES)
    create.add_argument("--tenant-id", default=None)
    create.add_argument("--display-name", default="")
    create.add_argument("--password-env", default=None, help="read the password from this environment variable")
    sub.add_parser("list", allow_abbrev=False, help="list users (no password hashes)")
    for name, help_text in (("disable", "disable a user and revoke their sessions"), ("enable", "re-enable a user"),
                            ("unlock", "clear a sign-in lockout")):
        item = sub.add_parser(name, allow_abbrev=False, help=help_text)
        item.add_argument("--username", required=True)
    reset = sub.add_parser("reset-password", allow_abbrev=False, help="set a new password (must change it at next sign-in)")
    reset.add_argument("--username", required=True)
    reset.add_argument("--password-env", default=None)
    role = sub.add_parser("set-role", allow_abbrev=False, help="change a role (revokes the user's sessions)")
    role.add_argument("--username", required=True)
    role.add_argument("--role", required=True, choices=permissions.ROLES)
    role.add_argument("--tenant-id", default=None)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    stdin: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    stdin = stdin or sys.stdin

    async def _run() -> tuple[int, dict[str, Any]]:
        if session_factory is not None:
            return await run_command(args, settings, session_factory, stdin)
        from app.infra import postgres

        postgres.init_engine(settings)
        try:
            return await run_command(args, settings, postgres.SessionLocal, stdin)
        finally:
            await postgres.close_engine()

    code, payload = asyncio.run(_run())
    sys.stdout.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
