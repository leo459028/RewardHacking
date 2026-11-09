from __future__ import annotations

import asyncio
import inspect
import json
import time
from datetime import datetime, timedelta, timezone

from aiokafka import AIOKafkaConsumer

from app.core import trace_context, tracing
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
from app.services.control_plane import (
    get_execution_plan_for_run,
    list_tool_definitions,
    sync_execution_plan_on_worker_completion,
)
from app.services.events import EventPublisher, KafkaEventPublisher
from app.services.evaluator import Evaluator
from app.services.finops import record_actual_usage
from app.services.mcp_gateway import HARD_DENY_FLAG_PREFIXES, worker_context as mcp_worker_context
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


def _missing_execution_plan_result(run_id: str) -> OrchestrationResult:
    message = "Control plane execution plan is missing; execution requires approval."
    response = AgentResponse(
        run_id=run_id,
        status="awaiting_approval",
        decision_reason=message,
        risk_score=1.0,
        policy_flags=["missing_execution_plan"],
    )
    summary = {
        "run_state": {"status": "awaiting_approval"},
        "control_plane": {
            "status": "missing_execution_plan",
            "reason": message,
        },
        "approval": {
            "status": "pending",
            "reason": message,
        },
        "event_timeline": [
            {
                "event_type": "control_plane.plan_missing",
                "status": "awaiting_approval",
            }
        ],
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

    request = AgentRequest.model_validate(run.request_payload)
    plan = await get_execution_plan_for_run(session, run_id)
    if plan is None:
        result = _missing_execution_plan_result(run_id)
        updated = await update_orchestration_result(session, run, request, result)
        await persist_worker_attempt(
            session,
            run_id=run_id,
            attempt_number=await worker_attempt_count(session, run_id) + 1,
            status=updated.status,
            failure_reason="missing_execution_plan",
        )
        await session.commit()
        await state_store.set_run_state(run_id, updated.status)
        return updated

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

    failure_reason = ""
    try:
        disabled_tools = list((await state_store.get_open_breakers(request.allowed_tools)).keys())
        registry_tools = await list_tool_definitions(session)
        allowed_tool_map = {str(item["tool_name"]): item for item in registry_tools}
        mcp_context = await mcp_worker_context(
            session,
            state_store,
            settings=get_settings(),
            request=request,
            tools=registry_tools,
        )
        execute_kwargs: dict = {"run_id": run_id, "disabled_tools": disabled_tools}
        execute_params = inspect.signature(orchestrator.execute).parameters
        if "mcp_context" in execute_params:
            execute_kwargs["mcp_context"] = mcp_context
        if "allowed_tool_map" in execute_params:
            execute_kwargs["allowed_tool_map"] = allowed_tool_map
        result = orchestrator.execute(request, **execute_kwargs)
        if result.response.status == "denied":
            from app.services.finops import release_run_budget_lease

            await release_run_budget_lease(
                session,
                state_store,
                run_id=run_id,
                tenant_id=run.tenant_id,
                reason=result.response.decision_reason or "mcp_worker_hard_deny",
            )
    except Exception as exc:
        failure_reason = str(exc)
        max_attempts = max_attempts or get_settings().worker_max_attempts
        if attempt_number >= max_attempts:
            result = _failed_result(run_id, f"Retry exhausted: {failure_reason}")
        else:
            result = _retry_result(run_id, f"Worker retry scheduled: {failure_reason}")

    elapsed_ms = max(1, int((time.perf_counter() - started_at) * 1000))
    updated = await update_orchestration_result(session, run, request, result)
    await sync_execution_plan_on_worker_completion(
        session,
        run=updated,
        duration_ms=elapsed_ms,
        failure_reason=failure_reason,
    )
    mcp_hard_denied = updated.status == "denied" and any(
        str(flag).startswith(HARD_DENY_FLAG_PREFIXES) for flag in (result.response.policy_flags or [])
    )
    if not mcp_hard_denied:
        await record_actual_usage(
            session,
            run=updated,
            plan=plan,
            is_failed_retry=bool(failure_reason),
            state_store=state_store,
        )
    else:
        await session.commit()
    if failure_reason:
        settings = get_settings()
        for tool_name in dict.fromkeys(request.allowed_tools or []):
            rec = await state_store.record_tool_failure(
                tool_name,
                reason=failure_reason,
                threshold=settings.tool_breaker_failure_threshold,
                ttl_seconds=settings.tool_breaker_ttl_seconds,
            )
            if rec.get("tripped"):
                from app.models.governance_event import GovernanceEvent

                session.add(
                    GovernanceEvent(
                        tenant_id=updated.tenant_id,
                        user_id=updated.user_id,
                        run_id=updated.run_id,
                        event_type="tool_breaker.opened",
                        decision="tripped",
                        reason=f"Auto-tripped after {rec.get('failure_count')} failures: {failure_reason}",
                        details={
                            "tool_name": tool_name,
                            "source": "worker_auto",
                            "failure_count": rec.get("failure_count"),
                            "threshold": rec.get("threshold"),
                        },
                    )
                )
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


async def process_message(
    value: dict,
    headers,
    *,
    session_factory,
    state_store: RunStateStore,
    event_publisher: EventPublisher,
    topic: str,
):
    """Process one request-topic message inside a consumer span (v1.6.0 D3: trace continues from the producer)."""
    run_id = value.get("run_id") or (value.get("payload") or {}).get("run_id")
    if not run_id:
        return None
    context = trace_context.context_from_message(headers, value.get("trace_id"))
    with tracing.start_span(f"{topic} process", kind=tracing.SPAN_KIND_CONSUMER, context=context, new_span=False):
        async with session_factory() as session:
            return await process_run_by_id(
                session,
                run_id=run_id,
                state_store=state_store,
                event_publisher=event_publisher,
            )


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
            if postgres.SessionLocal is None:
                raise RuntimeError("SessionLocal is not initialized")
            await process_message(
                message.value,
                message.headers,
                session_factory=postgres.SessionLocal,
                state_store=state_store,
                event_publisher=event_publisher,
                topic=settings.kafka_request_topic,
            )
    finally:
        await consumer.stop()
        await close_engine()


if __name__ == "__main__":
    asyncio.run(run_worker())
