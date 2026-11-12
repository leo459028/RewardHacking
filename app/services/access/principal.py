"""Who is acting on the Dashboard (v1.6.1 plan D9, D16, D23).

Routes only ever see a ``Principal``. A sign-in provider returns an ``Identity`` (what the provider asserts), which
``providers.resolve_user()`` maps to a ``DashboardUser`` and ``sessions.create_session()`` turns into a session.
``LOCAL_ADMIN`` is the synthetic principal of ``DASHBOARD_AUTH_MODE=off`` (local development only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class Identity:
    provider: str
    subject: str
    username: str
    display_name: str = ""
    claims: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: str
    tenant_id: str | None
    session_id: str | None
    auth_method: str
    reauth_at: datetime | None
    display_name: str = ""
    must_change_password: bool = False
    synthetic: bool = False

    @property
    def actor(self) -> str:
        """D23: the operator string written to audit records in ``session`` mode."""
        return f"user:{self.username}"

    @property
    def is_platform(self) -> bool:
        return self.tenant_id is None


LOCAL_ADMIN = Principal(
    user_id="local-admin",
    username="local-admin",
    role="admin",
    tenant_id=None,
    session_id=None,
    auth_method="none",
    reauth_at=None,
    display_name="Local admin (Dashboard sign-in off)",
    synthetic=True,
)


def principal_for(user: Any, session_row: Any) -> Principal:
    """Build the request principal from a ``DashboardUser`` and its ``DashboardSession``."""
    return Principal(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        tenant_id=user.tenant_id,
        session_id=session_row.session_id,
        auth_method=session_row.auth_method,
        reauth_at=session_row.reauth_at,
        display_name=user.display_name or "",
        must_change_password=bool(user.must_change_password),
    )
