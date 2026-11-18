"""Before / after re-run inside the sandbox (v1.6.2 D25-D28). Runs only in ``sandbox-runner``.

The same snapshot runs twice: before (the platform's code and tests) and after (the agent's change applied to the
non-test files). When the after run has failures it is repeated, so results that do not repeat are marked flaky. The
result is monitor-only: nothing here changes a verdict, dispatches a run, publishes to Kafka or touches a token lease.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.digests import sha256_hex
from app.models.evaluation_review import RunOutcome
from app.models.sandbox import SandboxJob, VerificationRun, VerificationSuite
from app.services.finops import ledger
from app.services.governance_audit import add_governance_event
from app.services.sandbox import docker_config, gate, jobs
from app.services.sandbox.base import ExecutionResult
from app.services.sandbox.spec import ExecutionSpec, Limits, Mount
from app.services.verification import artifacts, classify, line_coverage, patching

logger = logging.getLogger(__name__)

ENGINE_VERSION = "rerun/1.0"
QUEUED, RUNNING, COMPLETED, SKIPPED, FAILED = "queued", "running", "completed", "skipped", "failed"
ACTIVE_STATUSES = (QUEUED, RUNNING)
WORKSPACE = "/workspace"
SOURCE_DIR = f"{WORKSPACE}/src"
REPORT_PATH = f"{WORKSPACE}/out/junit.xml"
COVERAGE_PATH = f"{WORKSPACE}/out/coverage.json"
REPORT_FILE, COVERAGE_FILE = "out/junit.xml", "out/coverage.json"
CPU_MILLIS, MEMORY_MB, PIDS = 1000, 512, 128
MAX_OUTPUT_BYTES = 65_536
MAX_REPORT_BYTES = 8_388_608
BEFORE_NO_REPORT, AFTER_NO_REPORT = "before_no_report", "after_no_report"
SUITE_UNAVAILABLE, OUTCOME_MISSING, IMAGE_NOT_CONFIGURED = "suite_unavailable", "outcome_missing", "image_not_configured"
_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "HOME": "/tmp", "COVERAGE_FILE": "/tmp/.coverage"}


@dataclass
class Phase:
    result: ExecutionResult
    report: dict[str, str] | None
    coverage: bytes | None = None

    def summary(self) -> dict[str, Any]:
        result = self.result
        return {
            "status": result.status,
            "exit_code": result.exit_code,
            "spec_digest": result.spec_digest,
            "stdout_sha256": result.stdout_sha256,
            "stderr_sha256": result.stderr_sha256,
            "truncated": result.truncated,
            "usage": dict(result.usage or {}),
            "counts": classify.counts(self.report) if self.report is not None else None,
        }


@dataclass
class EngineResult:
    status: str
    skip_reason: str = ""
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    classification: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    discarded: list[dict[str, str]] = field(default_factory=list)
    wall_seconds: float = 0.0


def pytest_script(test_command: list[str], *, with_coverage: bool) -> str:
    args = shlex.join(["-p", "no:cacheprovider", f"--junitxml={REPORT_PATH}", *test_command])
    plain = f"exec python -m pytest {args}"
    if not with_coverage:
        return plain
    return (
        "if python -c 'import coverage' 2>/dev/null; then "
        f"python -m coverage run --source={SOURCE_DIR} -m pytest {args}; code=$?; "
        f"python -m coverage json -q -o {COVERAGE_PATH} >/dev/null 2>&1 || true; exit $code; "
        f"fi; {plain}"
    )


def build_spec(settings: Any, *, image: str, source_ref: str, script: str, job_id: str) -> ExecutionSpec:
    timeout = min(int(settings.sandbox_verify_timeout_seconds), int(settings.sandbox_max_timeout_seconds))
    limits = Limits(
        timeout_seconds=timeout, cpu_millis=CPU_MILLIS, memory_mb=MEMORY_MB, pids=PIDS, max_output_bytes=MAX_OUTPUT_BYTES,
        max_output_files_bytes=MAX_REPORT_BYTES,
    )
    return ExecutionSpec(
        tool_name="platform_verification", argv=("sh", "-c", script), limits=limits, image=image, workdir=SOURCE_DIR,
        mounts=(Mount(source_ref=source_ref, target=WORKSPACE, read_only=False),), env=dict(_ENV), labels={"job_id": job_id},
    )


def _run_phase(
    backend_factory: Callable[[], Any], settings: Any, *, files: Mapping[str, bytes], image: str, test_command: list[str],
    with_coverage: bool, job_id: str,
) -> Phase:
    workspace = artifacts.build_tar(files, prefix="src")
    source_ref = "artifact:sha256:" + hashlib.sha256(workspace).hexdigest()
    script = pytest_script(test_command, with_coverage=with_coverage)
    spec = build_spec(settings, image=image, source_ref=source_ref, script=script, job_id=job_id)
    result, outputs = backend_factory().run_with_outputs(spec, artifacts={source_ref: workspace})
    report = None
    if REPORT_FILE in outputs and result.status in ("ok", "nonzero_exit"):
        try:
            report = classify.parse_junit(outputs[REPORT_FILE])
        except classify.ReportInvalid as exc:
            logger.warning("verification report rejected: %s", exc)
    return Phase(result, report, outputs.get(COVERAGE_FILE))


def _wall_seconds(*phases: Phase) -> float:
    return round(sum(int((phase.result.usage or {}).get("wall_ms") or 0) for phase in phases) / 1000, 3)


def _sandbox_failure(phase: Phase, fallback: str) -> str:
    if phase.result.status in ("rejected", "error"):
        reason = phase.result.rejection_reasons[0] if phase.result.rejection_reasons else phase.result.status
        return f"sandbox_error:{reason}"[:64]
    return fallback


def run_verification(
    backend_factory: Callable[[], Any], settings: Any, *, base_files: Mapping[str, bytes], evidence: Mapping[str, Any],
    test_paths: list[str], test_command: list[str], image: str, job_id: str = "",
) -> EngineResult:
    """Synchronous: the runner calls it through ``asyncio.to_thread``."""
    try:
        patch = patching.apply_evidence(base_files, evidence, test_paths=test_paths, max_bytes=int(settings.sandbox_artifact_max_bytes))
    except patching.PatchSkipped as exc:
        return EngineResult(SKIPPED, skip_reason=exc.reason, discarded=list(exc.discarded))

    def phase(files: Mapping[str, bytes], *, with_coverage: bool = False) -> Phase:
        return _run_phase(
            backend_factory, settings, files=files, image=image, test_command=test_command, with_coverage=with_coverage, job_id=job_id
        )

    discarded = list(patch.discarded)
    before = phase(base_files)
    if before.report is None:
        reason = _sandbox_failure(before, BEFORE_NO_REPORT)
        return EngineResult(FAILED, reason, before=before.summary(), discarded=discarded, wall_seconds=_wall_seconds(before))
    after = phase(patch.files, with_coverage=True)
    if after.report is None:
        reason = _sandbox_failure(after, AFTER_NO_REPORT)
        return EngineResult(
            FAILED, reason, before=before.summary(), after=after.summary(), discarded=discarded, wall_seconds=_wall_seconds(before, after)
        )
    runs = [after]
    extra_wall: list[Phase] = []  # a retry without a report says nothing about flakiness, but it did use compute
    if classify.has_failures(after.report):
        for _attempt in range(max(0, int(settings.sandbox_verify_flaky_retries))):
            retry = phase(patch.files)
            if retry.report is None:
                extra_wall.append(retry)
                break
            runs.append(retry)
    return EngineResult(
        COMPLETED,
        before=before.summary(),
        after={**after.summary(), "attempts": len(runs)},
        classification=classify.classify(before.report, [run.report for run in runs]),
        coverage=line_coverage.changed_line_coverage(patch.changed_lines, after.coverage),
        discarded=discarded,
        wall_seconds=_wall_seconds(before, *runs, *extra_wall),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _finish(session: AsyncSession, row: VerificationRun, result: EngineResult) -> None:
    row.status, row.skip_reason = result.status, result.skip_reason
    row.before, row.after, row.classification, row.coverage = result.before, result.after, result.classification, result.coverage
    row.discarded_test_changes = result.discarded
    row.finished_at = _now()
    summary = {"before": result.before, "after": result.after, "classification": result.classification, "coverage": result.coverage}
    add_governance_event(
        session, tenant_id=row.tenant_id, run_id=row.run_id, user_id="sandbox-runner", event_type="sandbox.verification_completed",
        decision=result.status, reason=f"Platform re-run finished with status {result.status}.",
        details={
            "verification_id": row.verification_id, "suite_id": row.suite_id, "outcome_id": row.outcome_id,
            "skip_reason": result.skip_reason, "summary_digest": sha256_hex(summary),
        },
    )


async def handle_job(
    job: SandboxJob, *, settings: Any, session_factory: Any, backend_factory: Callable[[], Any], docker_server_version: str
) -> dict[str, Any]:
    """Run one queued verification and store its result."""
    verification_id = str((job.payload or {}).get("verification_id") or job.ref_id)
    async with session_factory() as session:
        row = await session.scalar(select(VerificationRun).where(VerificationRun.verification_id == verification_id))
        if row is None:
            raise LookupError("verification_missing")
        if row.status not in ACTIVE_STATUSES:
            return {"verification_id": verification_id, "status": row.status}
        suite = await session.scalar(select(VerificationSuite).where(VerificationSuite.suite_id == row.suite_id))
        outcome = await session.scalar(select(RunOutcome).where(RunOutcome.outcome_id == row.outcome_id))
        content = await artifacts.load_artifact(session, row.artifact_digest) if row.artifact_digest else None
        status = await gate.evaluate(session, settings, docker_server_version=docker_server_version)
        caps = docker_config.capabilities(settings)
        row.status = RUNNING
        row.backend_id, row.backend_version = caps.backend_id, caps.backend_version
        row.isolation_level, row.runtime = caps.isolation_level.label, docker_config.runtime_of(settings)
        row.config_fingerprint, row.evaluator_version = status.fingerprint, ENGINE_VERSION
        await session.commit()

    image = docker_config.sandbox_images(settings).get(suite.image_name) if suite is not None else None
    if suite is None or content is None:
        result = EngineResult(FAILED, SUITE_UNAVAILABLE)
    elif outcome is None:
        result = EngineResult(FAILED, OUTCOME_MISSING)
    elif not image:
        result = EngineResult(FAILED, IMAGE_NOT_CONFIGURED)
    else:
        result = await asyncio.to_thread(
            run_verification, backend_factory, settings, base_files=artifacts.read_tar(content), evidence=dict(outcome.evidence or {}),
            test_paths=list(suite.test_paths or []), test_command=list(suite.test_command or []), image=image, job_id=job.job_id,
        )

    async with session_factory() as session:
        row = await session.scalar(
            select(VerificationRun).where(VerificationRun.verification_id == verification_id).with_for_update()
        )
        if row.status != RUNNING:  # this runner lost its lease and the record was closed without it
            return {"verification_id": verification_id, "status": row.status, "result_discarded": True}
        _finish(session, row, result)
        if result.wall_seconds > 0:
            wall = Decimal(str(result.wall_seconds))
            await ledger.record_sandbox_compute(
                session, tenant_id=row.tenant_id, run_id=row.run_id, reference_id=verification_id, wall_seconds=wall,
                cpu_seconds=wall * CPU_MILLIS / 1000, unit_cost=Decimal(str(settings.sandbox_compute_cost_per_cpu_second)),
                status="success" if result.status == COMPLETED else "failed",
            )
        await session.commit()
    return {"verification_id": verification_id, "status": result.status, "skip_reason": result.skip_reason}


async def reconcile_jobs(session: AsyncSession) -> int:
    """A job that ended without a result (gate closed, isolation refused, lease lost) must not leave its record queued."""
    stmt = (
        select(VerificationRun, SandboxJob)
        .join(SandboxJob, SandboxJob.ref_id == VerificationRun.verification_id)
        .where(
            SandboxJob.kind == jobs.KIND_VERIFICATION,
            SandboxJob.status.in_((jobs.FAILED, jobs.EXPIRED, jobs.CANCELLED)),
            VerificationRun.status.in_(ACTIVE_STATUSES),
        )
    )
    rows = (await session.execute(stmt)).all()
    for row, job in rows:
        _finish(session, row, EngineResult(FAILED, (job.error_code or job.status)[:64]))
    await session.commit()
    return len(rows)
