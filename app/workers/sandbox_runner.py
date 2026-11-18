"""``sandbox-runner``: the only process with Docker access (v1.6.2 D19, D21).

It accepts no outside requests. It reads work from ``sandbox_jobs``, proves the backend with the full conformance suite
at start-up and on request, and writes a heartbeat to Redis. Start it with ``docker compose --profile sandbox up -d``.
Agent-run tools never come here: they stay simulated in the worker.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infra import postgres
from app.infra.postgres import close_engine, init_engine
from app.infra.redis import get_redis
from app.models.sandbox import SandboxConformanceRun, SandboxJob
from app.services.run_state import RedisRunStateStore
from app.services.sandbox import conformance, docker_config, gate, jobs
from app.services.sandbox.docker_backend import DockerBackend
from app.services.sandbox.policy import isolation_rejection
from app.services.sandbox.registry import DOCKER, configured_backend

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
HEARTBEAT_TTL_SECONDS = 60
POLL_SECONDS = 2.0
SWEEP_SECONDS = 300
# The runner can clear these itself by proving the backend again; a failed run needs a person.
REPROVE_REASONS = frozenset({gate.CONFORMANCE_STALE, gate.FINGERPRINT_MISMATCH})

STATE_DISABLED = "disabled"
STATE_DOCKER_UNREACHABLE = "docker_unreachable"
STATE_READY = "ready"
STATE_BUSY = "busy"

Handler = Callable[[SandboxJob], Awaitable[dict[str, Any]]]


class SandboxRunner:
    def __init__(
        self,
        settings: Any,
        *,
        session_factory: Any,
        state_store: Any,
        backend_factory: Callable[[], Any],
        runner_id: str | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.state_store = state_store
        self.backend_factory = backend_factory
        self.runner_id = runner_id or f"runner-{socket.gethostname()}"
        self.poll_seconds = poll_seconds
        self.state = STATE_DISABLED
        self.docker_server_version = ""
        self.current_job_id = ""
        self.gate_ready = False
        self.prepared = False
        self.last_sweep = time.monotonic()
        self.handlers: dict[str, Handler] = {jobs.KIND_CONFORMANCE: self._handle_conformance}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.sandbox_execution_enabled) and configured_backend(self.settings) == DOCKER

    async def run_conformance(self, triggered_by: str) -> SandboxConformanceRun:
        """D14: every case, against the real backend; the result decides the gate."""
        image = self.settings.sandbox_conformance_image
        report = await asyncio.to_thread(conformance.run_full_suite, self.backend_factory, image=image)
        async with self.session_factory() as session:
            run = await jobs.record_conformance(
                session, self.settings, report=report, docker_server_version=self.docker_server_version, triggered_by=triggered_by
            )
            await jobs.prune_conformance(session)
        logger.info("sandbox conformance %s passed=%s", run.run_id, run.passed)
        return run

    async def _docker_alive(self) -> bool:
        """Read the live Docker version. Losing Docker means the backend has to be proven again when it returns."""
        try:
            self.docker_server_version = await asyncio.to_thread(self.backend_factory().server_version)
            return True
        except Exception as exc:  # noqa: BLE001 - Docker may start after the runner, or restart under it
            self.state, self.prepared, self.gate_ready = STATE_DOCKER_UNREACHABLE, False, False
            logger.warning("sandbox runner cannot reach Docker: %s", type(exc).__name__)
            return False

    async def _sweep_orphans(self) -> None:
        """Containers of a killed runner have no deadline any more, so this repeats for as long as the runner lives."""
        self.last_sweep = time.monotonic()
        try:
            await asyncio.to_thread(self.backend_factory().sweep_orphans)
        except Exception as exc:  # noqa: BLE001 - the next sweep retries
            logger.warning("sandbox orphan sweep failed: %s", type(exc).__name__)

    async def prepare(self) -> bool:
        """Connect to Docker, remove leftovers of a crashed runner and prove the backend. ``False`` = try again later."""
        if not self.enabled:
            self.state = STATE_DISABLED
            return False
        if not await self._docker_alive():
            return False
        await self._sweep_orphans()
        self.state = STATE_READY
        await self.run_conformance("runner_startup")
        self.prepared = True
        return True

    async def _handle_conformance(self, job: SandboxJob) -> dict[str, Any]:
        run = await self.run_conformance(str((job.payload or {}).get("requested_by") or "job"))
        return {"conformance_run_id": run.run_id, "passed": bool(run.passed)}

    async def _gate(self) -> gate.GateStatus:
        async with self.session_factory() as session:
            return await gate.evaluate(session, self.settings, docker_server_version=self.docker_server_version)

    async def run_once(self) -> bool:
        """Handle at most one job. Returns ``True`` when a job was claimed."""
        if not await self._docker_alive():
            return False
        if time.monotonic() - self.last_sweep >= SWEEP_SECONDS:
            await self._sweep_orphans()
        async with self.session_factory() as session:
            await jobs.reclaim_expired(session)
        status = await self._gate()
        if REPROVE_REASONS.intersection(status.reasons):
            await self.run_conformance("runner_refresh")
            status = await self._gate()
        self.gate_ready = status.ready
        if not status.ready:
            async with self.session_factory() as session:
                await jobs.fail_queued(session, kinds=jobs.GATED_KINDS, error_code=gate.NOT_CONFORMANT)
        kinds = [kind for kind in self.handlers if kind == jobs.KIND_CONFORMANCE or status.ready]
        async with self.session_factory() as session:
            job = await jobs.claim_next(session, runner_id=self.runner_id, kinds=kinds)
        if job is None:
            return False
        await self._execute(job)
        return True

    async def _execute(self, job: SandboxJob) -> None:
        self.current_job_id, self.state = job.job_id, STATE_BUSY
        status, result, error_code = jobs.COMPLETED, {}, ""
        try:
            refusal = None
            if job.kind != jobs.KIND_CONFORMANCE:
                actual = docker_config.capabilities(self.settings).isolation_level
                refusal = isolation_rejection(actual, (job.payload or {}).get("risk_score"), self.settings)
            if refusal:
                status, error_code = jobs.FAILED, refusal
            else:
                result = await self.handlers[job.kind](job)
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the runner
            logger.exception("sandbox job %s failed", job.job_id)
            status, error_code = jobs.FAILED, f"runner_error:{type(exc).__name__}"
        finally:
            self.current_job_id, self.state = "", STATE_READY
        async with self.session_factory() as session:
            kept = await jobs.finish(session, job_id=job.job_id, runner_id=self.runner_id, status=status, result=result, error_code=error_code)
        if not kept:
            logger.warning("sandbox job %s: lease lost, result discarded", job.job_id)

    async def heartbeat_once(self) -> None:
        # The lease lives in the database, so a Redis outage must not end it.
        if self.current_job_id:
            try:
                async with self.session_factory() as session:
                    await jobs.renew_lease(session, job_id=self.current_job_id, runner_id=self.runner_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("sandbox lease renewal failed: %s", type(exc).__name__)
        payload = {
            "runner_id": self.runner_id,
            "state": self.state,
            "runtime": docker_config.runtime_of(self.settings),
            "docker_server_version": self.docker_server_version,
            "current_job_id": self.current_job_id,
            "gate_ready": self.gate_ready,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        await self.state_store.set_sandbox_runner_heartbeat(self.runner_id, payload, ttl_seconds=HEARTBEAT_TTL_SECONDS)

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.heartbeat_once()
            except Exception as exc:  # noqa: BLE001 - a Redis outage must not stop running work
                logger.warning("sandbox heartbeat failed: %s", type(exc).__name__)
            await self._sleep(stop, HEARTBEAT_SECONDS)

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def serve(self, stop: asyncio.Event) -> None:
        heartbeat = asyncio.create_task(self._heartbeat_loop(stop))
        try:
            while not stop.is_set():
                worked = False
                try:
                    if self.prepared or await self.prepare():
                        worked = await self.run_once()
                except Exception:  # noqa: BLE001 - e.g. the database is restarting
                    logger.exception("sandbox runner loop failed")
                if not worked:
                    await self._sleep(stop, self.poll_seconds)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


async def run_runner() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    init_engine(settings)
    client: Any = None

    def backend_factory() -> DockerBackend:
        # One SDK client per process; each conformance case gets its own backend object.
        nonlocal client
        backend = DockerBackend(settings, client=client)
        client = backend._docker()
        return backend

    runner = SandboxRunner(
        settings, session_factory=postgres.SessionLocal, state_store=RedisRunStateStore(get_redis(settings)), backend_factory=backend_factory
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:  # Windows development runs
            pass
    try:
        await runner.serve(stop)
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(run_runner())
