from __future__ import annotations

from typing import Any, Protocol

from app.core.config import Settings
from app.infra.kafka import producer_factory
from app.schemas.events import BaseEvent


class EventPublisher(Protocol):
    async def publish(self, topic: str, key: str, event: BaseEvent | dict[str, Any]) -> None:
        ...


class InMemoryEventPublisher:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, topic: str, key: str, event: BaseEvent | dict[str, Any]) -> None:
        payload = event.model_dump() if isinstance(event, BaseEvent) else event
        self.events.append({"topic": topic, "key": key, "event": payload})


class KafkaEventPublisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def publish(self, topic: str, key: str, event: BaseEvent | dict[str, Any]) -> None:
        producer = producer_factory(self.settings)
        payload = event.model_dump_json() if isinstance(event, BaseEvent) else BaseEvent.model_validate(event).model_dump_json()
        await producer.start()
        try:
            await producer.send_and_wait(
                topic,
                key=key,
                value=payload.encode("utf-8"),
            )
        finally:
            await producer.stop()
