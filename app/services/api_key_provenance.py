"""Who created each database API key (v1.6.3 plan D6).

``TenantApiKey`` has no ``created_by`` column, and v1.6.3 adds no migration, so the creator is read back from the
``api_key.created`` governance events. Those events sit in the audit chain and are never rewritten; this module only
reads them. The parsing is deterministic:

* the event for a key is the earliest ``api_key.created`` event of the key's tenant whose ``details.key_id`` names
  the key, or (API events written before v1.6.3, which carried the id only in the text) whose ``reason`` is exactly
  ``API key <key_id> created.``;
* ``details.actor_key_id == "legacy"`` means an environment API key (``API_KEYS``) created it. Which environment key
  it was cannot be told afterwards: every environment key is recorded as ``legacy``, ``demo-key`` included;
* another ``actor_key_id`` is a database API key (``key:<id>``); ``user_id == "cli"`` is the
  ``python -m app.jobs.api_keys`` CLI; any other ``user_id`` is a Dashboard account (``user:<name>``, or the historical
  ``dashboard-admin`` string in ``off`` mode);
* no event (never written, or removed by a retention purge), or an event with no actor (written while
  ``AUTH_ENABLED=false``), is ``unknown``.

Operations (``env_created_api_keys``), the Dashboard Governance page and the CLI ``list`` all use these functions,
so they always agree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.governance_event import GovernanceEvent
from app.models.tenant import TenantApiKey

API_KEY_CREATED = "api_key.created"
ENV_ACTOR_KEY_ID = "legacy"
CLI_ACTOR = "cli"

CREATOR_ENV = "env"
CREATOR_KEY = "key"
CREATOR_USER = "user"
CREATOR_CLI = "cli"
CREATOR_UNKNOWN = "unknown"
CREATOR_KINDS = (CREATOR_ENV, CREATOR_KEY, CREATOR_USER, CREATOR_CLI, CREATOR_UNKNOWN)

ENV_LABEL = "environment key (legacy)"
RUNBOOK_SECTION = "docs/runbook.md § 17 (API keys created with an environment key)"
RUNBOOK_URL = (
    "https://github.com/leo459028/RewardHacking/blob/main/docs/runbook.md"
    "#17-api-keys-created-with-an-environment-key-v163"
)

# API events before v1.6.3 named the key only here; the whole reason must match, so a Dashboard key *name* that
# happens to contain this text ("API key <name> (<role>) issued for tenant ...") never does.
_LEGACY_REASON = re.compile(r"API key (key_[A-Za-z0-9_]+) created\.")


@dataclass(frozen=True)
class KeyCreator:
    """The creator of one API key. ``label`` is what the Dashboard and CLI show."""

    kind: str
    label: str
    actor: str = ""
    event_id: int | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "actor": self.actor, "event_id": self.event_id, "detail": self.detail}


UNKNOWN_NO_EVENT = KeyCreator(kind=CREATOR_UNKNOWN, label="unknown", detail="no api_key.created event found")


def event_key_id(event: GovernanceEvent) -> str | None:
    """The key an ``api_key.created`` event is about, or ``None`` when it names none."""
    details = event.details if isinstance(event.details, dict) else {}
    key_id = details.get("key_id")
    if isinstance(key_id, str) and key_id:
        return key_id
    match = _LEGACY_REASON.fullmatch((event.reason or "").strip())
    return match.group(1) if match else None


def creator_from_event(event: GovernanceEvent) -> KeyCreator:
    details = event.details if isinstance(event.details, dict) else {}
    actor_key_id = details.get("actor_key_id")
    actor_key_id = actor_key_id if isinstance(actor_key_id, str) else ""
    user_id = (event.user_id or "").strip()
    if actor_key_id == ENV_ACTOR_KEY_ID:
        return KeyCreator(kind=CREATOR_ENV, label=ENV_LABEL, actor="key:legacy", event_id=event.id)
    if actor_key_id:
        return KeyCreator(kind=CREATOR_KEY, label=f"key:{actor_key_id}", actor=f"key:{actor_key_id}", event_id=event.id)
    if user_id == CLI_ACTOR:
        return KeyCreator(kind=CREATOR_CLI, label="cli", actor=CLI_ACTOR, event_id=event.id)
    if user_id:
        return KeyCreator(kind=CREATOR_USER, label=user_id, actor=user_id, event_id=event.id)
    return KeyCreator(
        kind=CREATOR_UNKNOWN, label="unknown", event_id=event.id, detail="event records no actor (AUTH_ENABLED=false?)"
    )


def attribute_keys(keys: Iterable[TenantApiKey], events: Iterable[GovernanceEvent]) -> dict[str, KeyCreator]:
    """Pure attribution: ``{key_id: KeyCreator}`` for every key, from the given events (any order)."""
    first_event: dict[tuple[str, str], GovernanceEvent] = {}
    for event in sorted(events, key=lambda item: (item.id is None, item.id or 0)):
        if event.event_type != API_KEY_CREATED:
            continue
        key_id = event_key_id(event)
        if key_id is None:
            continue
        first_event.setdefault((event.tenant_id, key_id), event)
    result: dict[str, KeyCreator] = {}
    for key in keys:
        event = first_event.get((key.tenant_id, key.key_id))
        result[key.key_id] = creator_from_event(event) if event is not None else UNKNOWN_NO_EVENT
    return result


async def creators_for_keys(session: AsyncSession, keys: Sequence[TenantApiKey]) -> dict[str, KeyCreator]:
    if not keys:
        return {}
    tenants = sorted({key.tenant_id for key in keys})
    events = (
        await session.scalars(
            select(GovernanceEvent)
            .where(GovernanceEvent.event_type == API_KEY_CREATED, GovernanceEvent.tenant_id.in_(tenants))
            .order_by(GovernanceEvent.id)
        )
    ).all()
    return attribute_keys(keys, events)


def is_active(key: TenantApiKey) -> bool:
    return key.status == "active" and not key.revoked


async def list_keys_with_creators(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    created_by: str | None = None,
    active_only: bool = False,
) -> list[tuple[TenantApiKey, KeyCreator]]:
    """Keys (newest first, then ``key_id``) with their creators, optionally filtered by tenant, kind and status."""
    if created_by is not None and created_by not in CREATOR_KINDS:
        raise ValueError(f"created_by must be one of {', '.join(CREATOR_KINDS)}")
    stmt = select(TenantApiKey)
    if tenant_id is not None:
        stmt = stmt.where(TenantApiKey.tenant_id == tenant_id)
    keys = list((await session.scalars(stmt.order_by(TenantApiKey.id.desc()))).all())
    if active_only:
        keys = [key for key in keys if is_active(key)]
    creators = await creators_for_keys(session, keys)
    rows = [(key, creators[key.key_id]) for key in keys]
    if created_by is not None:
        rows = [row for row in rows if row[1].kind == created_by]
    return rows


async def active_env_created_keys(session: AsyncSession, *, tenant_id: str | None = None) -> list[TenantApiKey]:
    """D7: active database keys an environment key created, sorted by ``key_id`` (Operations and CLI use this)."""
    rows = await list_keys_with_creators(session, tenant_id=tenant_id, created_by=CREATOR_ENV, active_only=True)
    return sorted((key for key, _ in rows), key=lambda key: key.key_id)
