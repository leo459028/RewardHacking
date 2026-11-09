from __future__ import annotations

from typing import Any, Protocol

from app.core import trace_context, tracing
from app.core.config import Settings
from app.infra.kafka import producer_factory
from app.schemas.events import BaseEvent


class EventPublisher(Protocol):
    async def publish(self, topic: str, key: str, event: BaseEvent | dict[str, Any]) -> None:
        ...


def _event_trace_id(event: BaseEvent | dict[str, Any]) -> str:
    if isinstance(event, BaseEvent):
        return event.trace_id
    return str((event or {}).get("trace_id") or "")


def _publish_parent(event: BaseEvent | dict[str, Any]) -> trace_context.TraceContext:
    """D3: the producer span's parent: the current span, else the event's ``trace_<32 hex>`` id, else a new trace."""
    current = trace_context.current()
    if current is not None:
        return current
    internal = trace_context.trace_id_from_internal(_event_trace_id(event))
    if internal is not None:
        return trace_context.TraceContext(trace_id=internal, span_id=trace_context.new_span_id())
    return trace_context.new_root()


def _messaging(topic: str, operation: str) -> dict[str, str]:
    return {"messaging.system": "kafka", "messaging.destination.name": topic, "messaging.operation.type": operation}


class InMemoryEventPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, topic: str, key: str, event: BaseEvent | dict[str, Any]) -> None:
        payload = event.model_dump() if isinstance(event, BaseEvent) else event
        with tracing.start_span(f"{topic} publish", kind=tracing.SPAN_KIND_PRODUCER, context=_publish_parent(event), attributes=_messaging(topic, "send")) as span:
            headers = span.context.headers()
        self.events.append({"topic": topic, "key": key, "event": payload, "headers": headers})


class KafkaEventPublisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def publish(self, topic: str, key: str, event: BaseEvent | dict[str, Any]) -> None:
        producer = producer_factory(self.settings)
        payload = event.model_dump_json() if isinstance(event, BaseEvent) else BaseEvent.model_validate(event).model_dump_json()
        await producer.start()
        try:
            with tracing.start_span(f"{topic} publish", kind=tracing.SPAN_KIND_PRODUCER, context=_publish_parent(event), attributes=_messaging(topic, "send")) as span:
                await producer.send_and_wait(
                    topic,
                    key=key,
                    value=payload.encode("utf-8"),
                    headers=trace_context.kafka_headers(span.context),
                )
        finally:
            await producer.stop()
