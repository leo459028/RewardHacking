"""Verification suites: the platform's answer sheet (v1.6.2 D22, D24).

A suite never changes: a new upload under the same name is a new version, and a suite can only be disabled. Publishing,
disabling and choosing the tenant default are governance events, so they land in the audit chain. A run is bound to a
suite before it executes; the outcome cannot choose or change it.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evaluation_review import TenantEvaluationPolicy
from app.models.sandbox import SandboxArtifact, VerificationSuite
from app.schemas.agent import AgentRequest
from app.services.evaluation.evidence import matches_any, normalize_path
from app.services.governance_audit import add_governance_event
from app.services.sandbox import docker_config
from app.services.verification.errors import VerificationError

METADATA_KEY = "verification_suite_id"
ACTIVE, DISABLED = "active", "disabled"
FRAMEWORK = "pytest"
SUPPORTED_TASK_TYPE = "code_fix"
DEFAULT_TEST_PATHS = ("tests/**", "**/test_*.py", "**/*_test.py")
DEFAULT_TEST_COMMAND = ("-q",)
MAX_ARGS = 32
MAX_ARG_CHARS = 256
MAX_TEST_PATHS = 50
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REPORT_OPTIONS = ("--junitxml", "--junit-xml", "--junit-prefix", "-o", "--override-ini", "-c", "--rootdir", "-p")


def _invalid(code: str, message: str, status_code: int = 422) -> VerificationError:
    return VerificationError(code, message, status_code)


def clean_test_command(values: Any) -> list[str]:
    """pytest arguments only: the platform decides where the report goes."""
    args = list(DEFAULT_TEST_COMMAND) if values in (None, "", []) else values
    if not isinstance(args, list) or len(args) > MAX_ARGS or any(not isinstance(item, str) for item in args):
        raise _invalid("suite_test_command_invalid", f"test_command must be a list of at most {MAX_ARGS} strings.")
    for item in args:
        if not item or len(item) > MAX_ARG_CHARS or "\x00" in item or item.split("=", 1)[0] in _REPORT_OPTIONS:
            raise _invalid("suite_test_command_invalid", f"test_command may not contain {item[:64]!r}.")
    return list(args)


def clean_test_paths(values: Any) -> list[str]:
    paths = list(DEFAULT_TEST_PATHS) if values in (None, "", []) else values
    if not isinstance(paths, list) or len(paths) > MAX_TEST_PATHS or any(not isinstance(item, str) for item in paths):
        raise _invalid("suite_test_paths_invalid", f"test_paths must be a list of at most {MAX_TEST_PATHS} glob patterns.")
    cleaned = list(dict.fromkeys(normalize_path(item) for item in paths if normalize_path(item)))
    if not cleaned:
        raise _invalid("suite_test_paths_invalid", "test_paths must not be empty.")
    return cleaned


def validate_definition(settings: Any, *, name: str, base_ref: str, image_name: str) -> None:
    if not _NAME.match(name or ""):
        raise _invalid("suite_name_invalid", "Use 1 to 128 letters, digits, dots, underscores or hyphens.")
    if len(base_ref or "") > 255:
        raise _invalid("suite_base_ref_invalid", "base_ref may hold at most 255 characters.")
    if image_name not in docker_config.sandbox_images(settings):
        raise _invalid("suite_image_unknown", f"{image_name[:64]!r} is not listed in SANDBOX_IMAGES.")


def require_tests(paths: tuple[str, ...], test_paths: list[str]) -> None:
    if not any(matches_any(path, test_paths) for path in paths):
        raise _invalid("suite_no_tests", "No file in the snapshot matches test_paths.")


async def publish(
    session: AsyncSession, settings: Any, *, tenant_id: str, name: str, artifact_digest: str, base_ref: str = "",
    image_name: str = "python", test_command: Any = None, test_paths: Any = None, actor: str,
) -> VerificationSuite:
    validate_definition(settings, name=name, base_ref=base_ref, image_name=image_name)
    command, paths = clean_test_command(test_command), clean_test_paths(test_paths)
    if await session.get(SandboxArtifact, artifact_digest) is None:
        raise _invalid("artifact_missing", "The snapshot of this suite is not stored.", 409)
    latest = await session.scalar(
        select(func.max(VerificationSuite.version)).where(VerificationSuite.tenant_id == tenant_id, VerificationSuite.name == name)
    )
    suite = VerificationSuite(
        suite_id=f"vsu_{uuid4().hex}", tenant_id=tenant_id, name=name, version=int(latest or 0) + 1, artifact_digest=artifact_digest,
        base_ref=base_ref or "", framework=FRAMEWORK, image_name=image_name, test_command=command, test_paths=paths,
        status=ACTIVE, created_by=actor[:128],
    )
    session.add(suite)
    add_governance_event(
        session, tenant_id=tenant_id, user_id=actor[:128], event_type="verification_suite.published", decision=ACTIVE,
        reason=f"Verification suite {name} version {suite.version} was published.",
        details={"suite_id": suite.suite_id, "name": name, "version": suite.version, "artifact_digest": artifact_digest},
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _invalid("suite_version_conflict", "Another version of this suite was published at the same moment.", 409) from exc
    return suite


async def get_suite(session: AsyncSession, suite_id: str) -> VerificationSuite | None:
    return await session.scalar(select(VerificationSuite).where(VerificationSuite.suite_id == suite_id))


async def list_suites(session: AsyncSession, *, tenant_id: str | None = None, limit: int = 100) -> list[VerificationSuite]:
    stmt = select(VerificationSuite).order_by(VerificationSuite.created_at.desc(), VerificationSuite.id.desc()).limit(limit)
    if tenant_id:
        stmt = stmt.where(VerificationSuite.tenant_id == tenant_id)
    return list((await session.scalars(stmt)).all())


async def _policy(session: AsyncSession, tenant_id: str) -> TenantEvaluationPolicy | None:
    return await session.scalar(select(TenantEvaluationPolicy).where(TenantEvaluationPolicy.tenant_id == tenant_id))


async def default_suite_id(session: AsyncSession, tenant_id: str) -> str:
    policy = await _policy(session, tenant_id)
    return str(policy.default_verification_suite_id or "") if policy is not None else ""


async def disable(session: AsyncSession, suite: VerificationSuite, *, actor: str) -> VerificationSuite:
    if suite.status == DISABLED:
        raise _invalid("suite_already_disabled", "This suite is already disabled.", 409)
    suite.status = DISABLED
    policy = await _policy(session, suite.tenant_id)
    was_default = policy is not None and policy.default_verification_suite_id == suite.suite_id
    if was_default:
        policy.default_verification_suite_id = ""
    add_governance_event(
        session, tenant_id=suite.tenant_id, user_id=actor[:128], event_type="verification_suite.disabled", decision=DISABLED,
        reason=f"Verification suite {suite.name} version {suite.version} was disabled.",
        details={"suite_id": suite.suite_id, "artifact_digest": suite.artifact_digest, "default_cleared": was_default},
    )
    await session.commit()
    return suite


async def set_default(session: AsyncSession, suite: VerificationSuite, *, enabled: bool, actor: str) -> str:
    """D24: the suite new ``code_fix`` runs of this tenant are bound to when the request names none."""
    if enabled and suite.status != ACTIVE:
        raise _invalid("verification_suite_invalid", "Only an active suite can be the tenant default.", 409)
    policy = await _policy(session, suite.tenant_id)
    if policy is None:
        policy = TenantEvaluationPolicy(tenant_id=suite.tenant_id, protected_paths=[], external_judge_profiles=[])
        session.add(policy)
    previous = str(policy.default_verification_suite_id or "")
    value = suite.suite_id if enabled else ("" if previous == suite.suite_id else previous)
    policy.default_verification_suite_id = value
    add_governance_event(
        session, tenant_id=suite.tenant_id, user_id=actor[:128], event_type="verification_suite.default_changed",
        decision="set" if enabled else "cleared", reason="The tenant's default verification suite changed.",
        details={"suite_id": suite.suite_id, "previous": previous, "current": value},
    )
    await session.commit()
    return value


async def bound_request(session: AsyncSession, request: AgentRequest) -> AgentRequest:
    """D24: check the suite named by the request, or bind the tenant default to a ``code_fix`` run."""
    tenant_id = request.tenant_id or "default"
    named = (request.metadata or {}).get(METADATA_KEY)
    if named not in (None, ""):
        suite = await get_suite(session, named) if isinstance(named, str) else None
        usable = suite is not None and suite.tenant_id == tenant_id and suite.status == ACTIVE
        if not usable or request.task_type != SUPPORTED_TASK_TYPE:
            raise _invalid("verification_suite_invalid", "The suite must exist, be active, belong to this tenant and the task must be code_fix.")
        return request
    if request.task_type != SUPPORTED_TASK_TYPE:
        return request
    default_id = await default_suite_id(session, tenant_id)
    if not default_id:
        return request
    return request.model_copy(update={"metadata": {**dict(request.metadata or {}), METADATA_KEY: default_id}})


def suite_to_dict(suite: VerificationSuite) -> dict[str, Any]:
    created = suite.created_at.isoformat() if suite.created_at is not None else None
    return {
        "suite_id": suite.suite_id,
        "tenant_id": suite.tenant_id,
        "name": suite.name,
        "version": suite.version,
        "artifact_digest": suite.artifact_digest,
        "base_ref": suite.base_ref,
        "framework": suite.framework,
        "image_name": suite.image_name,
        "test_command": list(suite.test_command or []),
        "test_paths": list(suite.test_paths or []),
        "status": suite.status,
        "created_by": suite.created_by,
        "created_at": created,
    }
