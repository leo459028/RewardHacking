"""The single Dashboard permission table (v1.6.1 plan D19-D22, feature six).

P1 defines the table only; P3 attaches ``require_permission("<name>")`` to every Dashboard route and P4 applies
``STEP_UP_PERMISSIONS``. Templates may use ``can()`` to hide buttons, but the server always re-checks.

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
_SUBMITTERS = frozenset({ADMIN, TENANT_ADMIN, OPERATOR})
_REVIEWERS = frozenset({ADMIN, TENANT_ADMIN, REVIEWER})

# permission name -> roles that hold it (feature six). Unknown permissions are denied.
PERMISSIONS: dict[str, frozenset[str]] = {
    # every role
    "dashboard.view": _ALL,  # read views and exports (tenant-bound users only see allow-listed views, D22)
    "account.self": _ALL,  # sign out, change one's own password
    # runs and pre-execution approvals
    "runs.submit": _SUBMITTERS,  # Playground
    "approvals.decide": _SUBMITTERS,
    "approvals.expire": _SUBMITTERS,
    "outcomes.submit": _ADMIN_OPERATOR,
    # post-execution review
    "reviews.decide": _REVIEWERS,
    "reviews.retriage": _REVIEWERS,
    "evaluations.preview": _REVIEWERS,
    # runtime operations
    "worker.manage": _ADMIN_OPERATOR,  # DLQ requeue / resolve
    "runs.replay": _ADMIN_OPERATOR,
    "runs.resume": _ADMIN_OPERATOR,
    "breakers.manage": _ADMIN_OPERATOR,
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
    "benchmark.manage": _ADMIN_TENANT_ADMIN,  # runs, review export, honeypots, imports, attestation keys
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

# D22: Dashboard views a tenant-bound user may open (allow list; everything else is denied). Keys are view names
# used by P3's route declarations; platform views (control plane, pricing, operations, observability, worker, judge
# profiles, retention, users, audit chain) are deliberately absent.
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
        "playground",
        "benchmark",
        "change_requests",
        "account",
    }
)


def has_permission(role: str | None, permission: str) -> bool:
    return bool(role) and role in PERMISSIONS.get(permission, frozenset())


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
