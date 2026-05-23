from aiokafka import AIOKafkaProducer
from app.core.config import Settings


def producer_factory(settings: Settings) -> AIOKafkaProducer:
    return AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        value_serializer=lambda v: v,
        key_serializer=lambda k: k.encode("utf-8") if isinstance(k, str) else k,
    )
