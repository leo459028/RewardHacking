from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from aiokafka import AIOKafkaConsumer

from app.core.config import get_settings
from app.infra import postgres
from app.infra.postgres import close_engine, init_engine
from app.infra.redis import get_redis
from app.observability.metrics import (
    EVALUATOR_VERDICTS_TOTAL,
    WORKER_DLQ_TOTAL,
    WORKER_DURATION_SECONDS,
    WORKER_JOBS_TOTAL,
    WORKER_RETRIES_TOTAL,
)
from app.schemas.agent import AgentRequest, AgentResponse
from app.services.audit_log import (
    get_run,
    persist_worker_attempt,
    update_orchestration_result,
    worker_attempt_count,
)
from app.services.events import EventPublisher, KafkaEventPublisher
from app.services.evaluator import Evaluator
from app.services.orchestrator import AgentOrchestrator, OrchestrationResult
from app.services.policy_engine import PolicyEngine
from app.services.run_state import RedisRunStateStore, RunStateStore


def _events_for_topics(summary: dict) -> list[tuple[str, dict]]:
    settings = get_settings()
    topic_by_key = {
        "execution_event": settings.kafka_execution_topic,
        "evaluation_event": settings.kafka_evaluation_topic,
        "incident_event": settings.kafka_incident_topic,
    }
    return [
        (topic, summary[key])
        for key, topic in topic_by_key.items()
        if isinstance(summary.get(key), dict)
    ]


def _failed_result(run_id: str, message: str) -> OrchestrationResult:
    response = AgentResponse(
        run_id=run_id,
        status="failed",
        decision_reason=message,
        risk_score=1.0,
        policy_flags=["worker_failed"],
    )
    summary = {
        "run_state": {"status": "failed"},
        "worker_error": {"message": message},
        "event_timeline": [{"event_type": "worker.failed", "status": "failed"}],
    }
    return OrchestrationResult(response=response, execution_output="", evaluation_summary=summary)


def _retry_result(run_id: str, message: str) -> OrchestrationResult:
    response = AgentResponse(
        run_id=run_id,
        status="queued",
        decision_reason=message,
        risk_score=1.0,
        policy_flags=["worker_retry_scheduled"],
    )
    summary = {
        "run_state": {"status": "queued"},
        "worker_error": {"message": message},
        "event_timeline": [{"event_type": "worker.retry_scheduled", "status": "queued"}],
    }
    return OrchestrationResult(response=response, execution_output="", evaluation_summary=summary)


async def process_run_by_id(
    session,
    *,
    run_id: str,
    state_store: RunStateStore,
    event_publisher: EventPublisher,
    orchestrator: AgentOrchestrator | None = None,
    max_attempts: int | None = None,
):
    run = await get_run(session, run_id)
    if run is None:
        raise ValueError(f"Run not found: {run_id}")
    if run.status != "queued":
        return run

    attempt_number = await worker_attempt_count(session, run_id) + 1
    acquired = await state_store.mark_idempotency_key(f"run:{run_id}:worker:{attempt_number}")
    if not acquired:
        return run

    orchestrator = orchestrator or AgentOrchestrator(
        policy_engine=PolicyEngine(risk_threshold=get_settings().default_risk_threshold),
        evaluator=Evaluator(),
    )

    await state_store.set_run_state(run_id, "running")
    started_at = time.perf_counter()
    run.status = "running"
    await session.commit()
    await session.refresh(run)

    request = AgentRequest.model_validate(run.request_payload)
    failure_reason = ""
    try:
        disabled_tools = list((await state_store.get_open_breakers(request.allowed_tools)).keys())
        result = orchestrator.execute(request, run_id=run_id, disabled_tools=disabled_tools)
    except Exception as exc:
        failure_reason = str(exc)
        max_attempts = max_attempts or get_settings().worker_max_attempts
        if attempt_number >= max_attempts:
            result = _failed_result(run_id, f"Retry exhausted: {failure_reason}")
        else:
            result = _retry_result(run_id, f"Worker retry scheduled: {failure_reason}")

    updated = await update_orchestration_result(session, run, request, result)
    if failure_reason:
        attempt_status = "dlq" if updated.status == "failed" else "retry_scheduled"
        if attempt_status == "retry_scheduled":
            WORKER_RETRIES_TOTAL.inc()
        if attempt_status == "dlq":
            WORKER_DLQ_TOTAL.inc()
        next_retry_at = None
        if attempt_status == "retry_scheduled":
            next_retry_at = datetime.now(timezone.utc) + timedelta(
                seconds=get_settings().worker_retry_backoff_seconds * attempt_number
            )
        await persist_worker_attempt(
            session,
            run_id=run_id,
            attempt_number=attempt_number,
            status=attempt_status,
            failure_reason=failure_reason,
            next_retry_at=next_retry_at,
        )
        await session.commit()
    else:
        await persist_worker_attempt(
            session,
            run_id=run_id,
            attempt_number=attempt_number,
            status=updated.status,
        )
        await session.commit()
    payload = updated.evaluation_summary.get("evaluation_event", {}).get("payload", {})
    if payload.get("verdict"):
        EVALUATOR_VERDICTS_TOTAL.labels(verdict=payload["verdict"]).inc()
    WORKER_JOBS_TOTAL.labels(status=updated.status).inc()
    WORKER_DURATION_SECONDS.observe(time.perf_counter() - started_at)
    await state_store.set_run_state(run_id, updated.status)

    for topic, event_payload in _events_for_topics(updated.evaluation_summary):
        await event_publisher.publish(topic, run_id, event_payload)
    return updated


async def run_worker() -> None:
    settings = get_settings()
    init_engine(settings)
    state_store = RedisRunStateStore(get_redis(settings))
    event_publisher = KafkaEventPublisher(settings)
    consumer = AIOKafkaConsumer(
        settings.kafka_request_topic,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id="reward-guardrail-workers",
        value_deserializer=lambda value: json.loads(value.decode("utf-8")),
    )
    await consumer.start()
    try:
        async for message in consumer:
            run_id = message.value.get("run_id") or message.value.get("payload", {}).get("run_id")
            if not run_id:
                continue
            if postgres.SessionLocal is None:
                raise RuntimeError("SessionLocal is not initialized")
            async with postgres.SessionLocal() as session:
                await process_run_by_id(
                    session,
                    run_id=run_id,
                    state_store=state_store,
                    event_publisher=event_publisher,
                )
    finally:
        await consumer.stop()
        await close_engine()


if __name__ == "__main__":
    asyncio.run(run_worker())
