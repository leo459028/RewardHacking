"""v1.6.3 secure defaults: API startup refusals and startup notices about API keys.

Only the API process (``app.main`` lifespan) calls this module. The worker, sandbox-runner and the ``app.jobs.*``
command-line tools never do, so an operator can still run migrations, create accounts or inspect data while the API
refuses to start (D3, D4).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.tenant import TenantApiKey
from app.services.auth import configured_env_api_key_count

logger = logging.getLogger(__name__)

# The startup notice about missing API keys reads the database. Tests switch it off because their app lifespan has no
# real database (the same pattern as ``judge_activation.RUN_ON_STARTUP``).
CHECK_DB_KEYS_ON_STARTUP = True
DB_KEY_CHECK_TIMEOUT_SECONDS = 5.0


class InsecureStartupConfiguration(RuntimeError):
    """The API refuses to start: a production-like APP_ENV with authentication switched off (v1.6.3 D4, D17)."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("Refusing to start the API: " + " ".join(self.reasons))


def startup_refusal_reasons(settings: Settings) -> list[str]:
    """Why the API must not start with these settings; empty when it may start (D4, D17).

    Only production-like environments (APP_ENV other than dev / test) are refused. The reasons name the setting to
    change and the explicit local-trial alternative; they never contain a secret.
    """
    if not settings.is_production_like:
        return []
    app_env = (settings.app_env or "").strip() or "(empty)"
    trial = "For a local trial only, set APP_ENV=dev instead."
    reasons: list[str] = []
    if not settings.auth_enabled:
        reasons.append(
            f"AUTH_ENABLED=false leaves the /v1 API open to anyone without an API key, and APP_ENV={app_env} is "
            "treated as production (only dev and test are development environments). Set AUTH_ENABLED=true and "
            "create database API keys with python -m app.jobs.api_keys create --tenant <id> --role <role> --name <name> "
            f"or in the Dashboard (/dashboard/governance). {trial}"
        )
    if settings.dashboard_auth_mode == "off":
        reasons.append(
            f"The Dashboard sign-in mode resolves to off (no sign-in, every visitor is an admin), and APP_ENV={app_env} "
            "is treated as production. Set DASHBOARD_AUTH_MODE=session (and remove DASHBOARD_AUTH_ENABLED=false), then "
            "create the first admin with: python -m app.jobs.users create --role admin ... "
            f"{trial}"
        )
    return reasons


def enforce_startup_security(settings: Settings) -> None:
    """Raise :class:`InsecureStartupConfiguration` when the API must not start (called before anything else)."""
    reasons = startup_refusal_reasons(settings)
    if reasons:
        for reason in reasons:
            logger.error("startup refused: %s", reason)
        raise InsecureStartupConfiguration(reasons)


def refused_env_api_key_count(settings: Settings) -> int:
    """Configured environment API keys that this APP_ENV refuses (D2); 0 in dev / test."""
    if settings.env_api_keys_allowed:
        return 0
    return configured_env_api_key_count(settings.api_keys)


def usable_env_api_key_count(settings: Settings) -> int:
    if not settings.env_api_keys_allowed:
        return 0
    return configured_env_api_key_count(settings.api_keys)


async def _active_db_key_count(session_factory: Callable[[], AsyncSession]) -> int:
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(TenantApiKey)
            .where(TenantApiKey.revoked.is_(False), TenantApiKey.status == "active")
        )
    return int(count or 0)


async def report_api_key_status(
    settings: Settings,
    session_factory: Callable[[], AsyncSession] | None,
    *,
    check_database: bool | None = None,
) -> list[str]:
    """Log the startup notices about API keys and return them (for tests). Never blocks startup (D2, D16).

    - API_KEYS set but refused in this APP_ENV: one warning with the number of well-formed keys only (also when that
      number is 0, so the log agrees with the ``bootstrap_keys_in_prod`` check, which fails on any non-empty value).
    - Authentication on, no usable environment key and no active database key: one notice on how to create one.
    A database that cannot be reached in time skips the second notice instead of failing startup.
    """
    notices: list[str] = []
    refused = refused_env_api_key_count(settings)
    if refused:
        message = (
            f"{refused} environment API key(s) in API_KEYS are refused because APP_ENV is not dev or test; "
            "remove API_KEYS and use database API keys."
        )
        logger.warning(message)
        notices.append(message)
    elif not settings.env_api_keys_allowed and (settings.api_keys or "").strip():
        message = (
            "API_KEYS is set but holds no well-formed key (key:tenant[:role]), and environment API keys are refused "
            "because APP_ENV is not dev or test anyway; remove API_KEYS and use database API keys."
        )
        logger.warning(message)
        notices.append(message)
    if not settings.auth_enabled or usable_env_api_key_count(settings):
        return notices
    if check_database is None:
        check_database = CHECK_DB_KEYS_ON_STARTUP
    if not check_database or session_factory is None:
        return notices
    try:
        active = await asyncio.wait_for(_active_db_key_count(session_factory), timeout=DB_KEY_CHECK_TIMEOUT_SECONDS)
    except Exception as exc:  # the database may still be starting; the notice is advisory
        logger.info("startup API key check skipped: %s", type(exc).__name__)
        return notices
    if active == 0:
        message = (
            "No API key is available: AUTH_ENABLED=true and there are no active database API keys, so every /v1 request "
            "is answered 401. Create one with python -m app.jobs.api_keys create --tenant <id> --role <role> --name "
            "<name>, or in the Dashboard (/dashboard/governance) after signing in as an admin."
        )
        logger.warning(message)
        notices.append(message)
    return notices
