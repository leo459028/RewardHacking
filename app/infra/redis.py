import json
from typing import Any
from redis.asyncio import Redis, from_url

from app.core.config import Settings


def get_redis(settings: Settings) -> Redis:
    return from_url(settings.redis_url, decode_responses=True)


async def set_json(redis: Redis, key: str, value: dict[str, Any], ttl_seconds: int = 3600) -> None:
    await redis.set(key, json.dumps(value, ensure_ascii=False), ex=ttl_seconds)


async def get_json(redis: Redis, key: str) -> dict[str, Any] | None:
    raw = await redis.get(key)
    if raw is None:
        return None
    return json.loads(raw)
