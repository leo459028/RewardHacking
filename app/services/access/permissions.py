"""The single Dashboard permission table (v1.6.1 plan D19-D22, feature six).

P1 defined the table; P3 attaches ``require_permission("<name>", view=...)`` (``app/ui/security.py``) to every
Dashboard route and P4 applies ``STEP_UP_PERMISSIONS``. Templates may use ``can()`` to hide buttons, but the server
always re-checks.

P3 aligned the table with the API's ``require_role()`` (D20: the API wins where it is narrower; where the API is
broader the Dashboard keeps the plan's narrower set). Differences are listed in the plan's progress log (P3 row) and
in ``docs/security.md``.

Roles: ``admin`` (platform), ``tenant_admin`` (bound to one tenant), ``reviewer``, ``operator`` and the read-only
``auditor``. ``tenant_id`` is required for ``tenant_admin``, optional for ``reviewer`` / ``operator`` / ``auditor``
and not allowed for ``admin``.
"""

from __future__ import annotations

ADMIN = "admin"
TENANT_ADMIN = "tenant_admin"
REVIEWER = "reviewer"
OPERATOR = "operator"
AUDITOR = "auditor"

ROLES: tuple[str, ...] = (ADMIN, TENANT_ADMIN, REVIEWER, OPERATOR, AUDITOR)
TENANT_REQUIRED_ROLES = frozenset({TENANT_ADMIN})
PLATFORM_ONLY_ROLES = frozenset({ADMIN})

_ALL = frozenset(ROLES)
_ADMIN = frozenset({ADMIN})
_ADMIN_TENANT_ADMIN = frozenset({ADMIN, TENANT_ADMIN})
_ADMIN_OPERATOR = frozenset({ADMIN, OPERATOR})
_REVIEWERS = frozenset({ADMIN, TENANT_ADMIN, REVIEWER})

# permission name -> roles that hold it (feature six). Unknown permissions are denied.
PERMISSIONS: dict[str, frozenset[str]] = {
    # every role
    "dashboard.view": _ALL,  # read views (tenant-bound users only see allow-listed views, D22)
    "account.self": _ALL,  # sign out, change one's own password
    # P3 view permissions where the API reads are narrower than "every role" (auditor added by D21)
    "benchmark.view": frozenset({ADMIN, TENANT_ADMIN, REVIEWER, AUDITOR}),  # API READ_ROLES: no operator
    "honeypots.package": _ADMIN_TENANT_ADMIN,  # API: WRITE_ROLES (the package must not reach agents)
    "judge.view": frozenset({ADMIN, TENANT_ADMIN, REVIEWER, AUDITOR}),  # API: reviewer view; platform page (D22)
    "retention.view": frozenset({ADMIN, OPERATOR, AUDITOR}),  # API: purges are operator / admin
    "finops.export": frozenset({ADMIN, TENANT_ADMIN, AUDITOR}),  # API export: admin, finops_admin, tenant_admin
    "api_keys.view": frozenset({ADMIN, AUDITOR}),  # API: listing keys is admin only
    # runs and pre-execution approvals (API: operator, admin)
    "runs.submit": _ADMIN_OPERATOR,  # Playground
    "approvals.decide": _ADMIN_OPERATOR,
    "approvals.expire": _ADMIN_OPERATOR,
    "outcomes.submit": _ADMIN_OPERATOR,
    # post-execution review
    "reviews.decide": _REVIEWERS,
    "reviews.retriage": _ADMIN_TENANT_ADMIN,  # API: tenant_admin, admin
    "evaluations.preview": _REVIEWERS,  # API is broader (viewer, operator); the Dashboard keeps the plan's set
    # runtime operations
    "worker.manage": _ADMIN_OPERATOR,  # DLQ requeue / resolve
    "runs.replay": _ADMIN_OPERATOR,
    "runs.resume": _ADMIN_OPERATOR,
    "breakers.manage": _ADMIN,  # API: tool and MCP breakers are admin only
    # platform configuration
    "control_plane.manage": _ADMIN,  # workflows, tools, MCP servers
    "tenants.manage": _ADMIN,
    "tenants.disable": _ADMIN,
    "api_keys.manage": _ADMIN,
    # evaluation policy and judges
    "evaluation_policy.write": _ADMIN_TENANT_ADMIN,  # add protected paths
    "evaluation_policy.relax": _ADMIN_TENANT_ADMIN,  # remove protected paths (step-up; dual control from P6)
    "judge.external_consent": _ADMIN_TENANT_ADMIN,
    "judge.profile.write": _ADMIN,  # create, edit, delete, activate, deactivate
    "judge.profile.operate": _ADMIN,  # probe, sweep
    "benchmark.manage": _ADMIN_TENANT_ADMIN,  # runs, review export, honeypot launch, attestation keys
    "benchmark.import": _ADMIN,  # API: Inspect log import is admin only
    # FinOps (Q10)
    "pricing.manage": _ADMIN,
    "pricing.import_apply": _ADMIN,
    "finops.manage": _ADMIN_TENANT_ADMIN,  # budgets, adjustments, violations, lease release
    # operations evidence and release gate
    "operations.manage": _ADMIN,
    # retention and legal holds
    "retention.plan": _ADMIN,
    "retention.apply": _ADMIN,
    "legal_hold.create": _ADMIN_TENANT_ADMIN,
    "legal_hold.release": _ADMIN,
    # access administration, dual control and the audit chain
    "users.manage": _ADMIN,
    "change_requests.decide": _ADMIN_TENANT_ADMIN,
    "audit_chain.read": frozenset({ADMIN, AUDITOR}),
    "audit_chain.verify": frozenset({ADMIN, AUDITOR}),
    "audit_chain.checkpoint": _ADMIN,
}

# D26 / feature seven: permissions that need a recent re-authentication (applied in P4).
STEP_UP_PERMISSIONS = frozenset(
    {
        "retention.apply",
        "legal_hold.release",
        "judge.profile.write",
        "evaluation_policy.relax",
        "users.manage",
        "api_keys.manage",
        "tenants.disable",
        "pricing.import_apply",
        "change_requests.decide",
        "audit_chain.checkpoint",
    }
)

# Permissions that only read (D21: the auditor holds nothing else). ``audit_chain.verify`` re-computes hashes and
# writes nothing.
READ_PERMISSIONS = frozenset(
    {
        "dashboard.view",
        "account.self",
        "benchmark.view",
        "judge.view",
        "retention.view",
        "finops.export",
        "api_keys.view",
        "audit_chain.read",
        "audit_chain.verify",
    }
)

# D22: Dashboard views a tenant-bound user may open (allow list; everything else is denied). Keys are the ``view``
# names of the route declarations in ``app/ui``; platform views (control plane, pricing, operations, observability,
# worker, judge profiles, retention, users, audit chain) are deliberately absent. P3 removed ``benchmark``: its page
# shows cross-tenant data (benchmark runs, honeypot results, cheat rates), so tenant-bound users use the API for it.
# ``budgets`` (the FinOps budget page, tenant-filtered) was added. ``PLATFORM_VIEW`` marks platform-only actions.
PLATFORM_VIEW = "platform"
TENANT_VIEW_ALLOWLIST = frozenset(
    {
        "overview",
        "runs",
        "run_detail",
        "approvals",
        "governance",
        "reviews",
        "review_detail",
        "costs",
        "budgets",
        "playground",
        "change_requests",
        "account",
    }
)


def has_permission(role: str | None, permission: str) -> bool:
    return bool(role) and role in PERMISSIONS.get(permission, frozenset())


def view_allowed(tenant_id: str | None, view: str | None) -> bool:
    """D22: platform users open every view; tenant-bound users only the allow-listed ones."""
    return not tenant_id or view is None or view in TENANT_VIEW_ALLOWLIST


def permissions_for(role: str) -> frozenset[str]:
    return frozenset(name for name, roles in PERMISSIONS.items() if role in roles)


def requires_step_up(permission: str) -> bool:
    return permission in STEP_UP_PERMISSIONS


def tenant_rule_problem(role: str, tenant_id: str | None) -> str | None:
    """Error code when ``role`` and ``tenant_id`` do not fit together (D22), otherwise ``None``."""
    if role not in ROLES:
        return "invalid_role"
    if role in TENANT_REQUIRED_ROLES and not tenant_id:
        return "tenant_required_for_role"
    if role in PLATFORM_ONLY_ROLES and tenant_id:
        return "role_is_platform_only"
    return None
