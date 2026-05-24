from __future__ import annotations

import asyncio
import json

from aiokafka import AIOKafkaConsumer

from app.core.config import get_settings
from app.infra import postgres
from app.infra.postgres import close_engine, init_engine
from app.infra.redis import get_redis
from app.schemas.agent import AgentRequest, AgentResponse
from app.services.audit_log import get_run, update_orchestration_result
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


async def process_run_by_id(
    session,
    *,
    run_id: str,
    state_store: RunStateStore,
    event_publisher: EventPublisher,
    orchestrator: AgentOrchestrator | None = None,
):
    run = await get_run(session, run_id)
    if run is None:
        raise ValueError(f"Run not found: {run_id}")
    if run.status != "queued":
        return run

    acquired = await state_store.mark_idempotency_key(f"run:{run_id}:worker")
    if not acquired:
        return run

    orchestrator = orchestrator or AgentOrchestrator(
        policy_engine=PolicyEngine(risk_threshold=get_settings().default_risk_threshold),
        evaluator=Evaluator(),
    )

    await state_store.set_run_state(run_id, "running")
    run.status = "running"
    await session.commit()
    await session.refresh(run)

    request = AgentRequest.model_validate(run.request_payload)
    try:
        disabled_tools = list((await state_store.get_open_breakers(request.allowed_tools)).keys())
        result = orchestrator.execute(request, run_id=run_id, disabled_tools=disabled_tools)
    except Exception as exc:
        result = _failed_result(run_id, f"Worker failed: {exc}")

    updated = await update_orchestration_result(session, run, request, result)
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
