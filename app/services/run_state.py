from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from redis.asyncio import Redis

from app.infra.redis import get_json, set_json


class RunStateStore(Protocol):
    async def set_run_state(
        self,
        run_id: str,
        status: str,
        *,
        details: dict[str, Any] | None = None,
        ttl_seconds: int = 86400,
    ) -> None:
        ...

    async def mark_idempotency_key(self, key: str, *, ttl_seconds: int = 86400) -> bool:
        ...

    async def open_breaker(
        self,
        tool_name: str,
        *,
        reason: str,
        ttl_seconds: int = 1800,
    ) -> None:
        ...

    async def get_open_breakers(self, tool_names: list[str]) -> dict[str, dict[str, Any]]:
        ...

    async def increment_quota(
        self,
        tenant_id: str,
        bucket: str,
        *,
        limit: int,
        ttl_seconds: int,
    ) -> "QuotaResult":
        ...


@dataclass(frozen=True)
class QuotaResult:
    tenant_id: str
    bucket: str
    limit: int
    used: int
    remaining: int
    allowed: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class InMemoryRunStateStore:
    def __init__(self) -> None:
        self.run_states: dict[str, dict[str, Any]] = {}
        self.idempotency_keys: set[str] = set()
        self.breakers: dict[str, dict[str, Any]] = {}
        self.quota_counters: dict[str, int] = {}

    async def set_run_state(
        self,
        run_id: str,
        status: str,
        *,
        details: dict[str, Any] | None = None,
        ttl_seconds: int = 86400,
    ) -> None:
        self.run_states[run_id] = {
            "status": status,
            "updated_at": _now_iso(),
            "ttl_seconds": ttl_seconds,
            **(details or {}),
        }

    async def mark_idempotency_key(self, key: str, *, ttl_seconds: int = 86400) -> bool:
        if key in self.idempotency_keys:
            return False
        self.idempotency_keys.add(key)
        return True

    async def open_breaker(
        self,
        tool_name: str,
        *,
        reason: str,
        ttl_seconds: int = 1800,
    ) -> None:
        self.breakers[tool_name] = {
            "status": "open",
            "reason": reason,
            "opened_at": _now_iso(),
            "ttl_seconds": ttl_seconds,
        }

    async def get_open_breakers(self, tool_names: list[str]) -> dict[str, dict[str, Any]]:
        return {
            tool_name: breaker
            for tool_name, breaker in self.breakers.items()
            if tool_name in set(tool_names) and breaker.get("status") == "open"
        }

    async def increment_quota(
        self,
        tenant_id: str,
        bucket: str,
        *,
        limit: int,
        ttl_seconds: int,
    ) -> QuotaResult:
        key = f"tenant:{tenant_id}:quota:{bucket}"
        used = self.quota_counters.get(key, 0) + 1
        self.quota_counters[key] = used
        remaining = max(limit - used, 0)
        return QuotaResult(
            tenant_id=tenant_id,
            bucket=bucket,
            limit=limit,
            used=used,
            remaining=remaining,
            allowed=used <= limit,
        )


class RedisRunStateStore:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def set_run_state(
        self,
        run_id: str,
        status: str,
        *,
        details: dict[str, Any] | None = None,
        ttl_seconds: int = 86400,
    ) -> None:
        await set_json(
            self.redis,
            f"run:{run_id}:state",
            {"status": status, "updated_at": _now_iso(), **(details or {})},
            ttl_seconds=ttl_seconds,
        )

    async def mark_idempotency_key(self, key: str, *, ttl_seconds: int = 86400) -> bool:
        created = await self.redis.set(f"idempotency:{key}", "1", ex=ttl_seconds, nx=True)
        return bool(created)

    async def open_breaker(
        self,
        tool_name: str,
        *,
        reason: str,
        ttl_seconds: int = 1800,
    ) -> None:
        await set_json(
            self.redis,
            f"breaker:{tool_name}",
            {"status": "open", "reason": reason, "opened_at": _now_iso()},
            ttl_seconds=ttl_seconds,
        )

    async def get_open_breakers(self, tool_names: list[str]) -> dict[str, dict[str, Any]]:
        open_breakers: dict[str, dict[str, Any]] = {}
        for tool_name in tool_names:
            breaker = await get_json(self.redis, f"breaker:{tool_name}")
            if breaker and breaker.get("status") == "open":
                open_breakers[tool_name] = breaker
        return open_breakers

    async def increment_quota(
        self,
        tenant_id: str,
        bucket: str,
        *,
        limit: int,
        ttl_seconds: int,
    ) -> QuotaResult:
        key = f"tenant:{tenant_id}:quota:{bucket}"
        used = int(await self.redis.incr(key))
        if used == 1:
            await self.redis.expire(key, ttl_seconds)
        remaining = max(limit - used, 0)
        return QuotaResult(
            tenant_id=tenant_id,
            bucket=bucket,
            limit=limit,
            used=used,
            remaining=remaining,
            allowed=used <= limit,
        )
