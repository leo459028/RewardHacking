from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from redis.asyncio import Redis

from app.infra.redis import get_json, set_json
from app.observability.metrics import TOOL_BREAKER_EVENTS_TOTAL


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

    async def close_breaker(self, tool_name: str) -> bool:
        ...

    async def get_open_breakers(self, tool_names: list[str]) -> dict[str, dict[str, Any]]:
        ...

    async def list_open_breakers(self) -> dict[str, dict[str, Any]]:
        ...

    async def open_mcp_breaker(
        self,
        mcp_server_id: str,
        *,
        reason: str,
        ttl_seconds: int = 1800,
    ) -> None:
        ...

    async def close_mcp_breaker(self, mcp_server_id: str) -> bool:
        ...

    async def get_open_mcp_breakers(self, mcp_server_ids: list[str]) -> dict[str, dict[str, Any]]:
        ...

    async def list_open_mcp_breakers(self) -> dict[str, dict[str, Any]]:
        ...

    async def record_tool_failure(
        self,
        tool_name: str,
        *,
        reason: str,
        threshold: int = 3,
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]:
        ...

    async def acquire_token_lease(
        self,
        lease: dict[str, Any],
        ttl_seconds: int = 7200,
    ) -> dict[str, Any]:
        ...

    async def release_token_lease(
        self,
        tenant_id: str,
        run_id: str,
        status: str = "released",
        reason: str = "",
    ) -> dict[str, Any] | None:
        ...

    async def list_active_token_leases(
        self,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
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

    # v1.5.12 (D14): per judge profile circuit breaker and consecutive-failure counter
    async def get_judge_breaker(self, profile: str) -> dict[str, Any] | None: ...

    async def open_judge_breaker(self, profile: str, *, reason: str, ttl_seconds: int, failures: int) -> dict[str, Any]: ...

    async def close_judge_breaker(self, profile: str) -> bool: ...

    async def record_judge_failure(self, profile: str) -> int: ...

    async def reset_judge_failures(self, profile: str) -> None: ...


@dataclass(frozen=True)
class QuotaResult:
    tenant_id: str
    bucket: str
    limit: int
    used: int
    remaining: int
    allowed: bool


@dataclass(frozen=True)
class TokenBudgetLease:
    lease_id: str
    run_id: str
    tenant_id: str
    user_id: str
    project_id: str
    model_name: str
    estimated_tokens: int
    estimated_base_cost: str
    currency: str
    status: str
    created_at: str
    expires_at: str
    ttl_seconds: int


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _five_minute_bucket(now: datetime | None = None) -> str:
    dt = now or _now_utc()
    return f"{dt.strftime('%Y%m%d%H')}{(dt.minute // 5) * 5:02d}"


MCP_BREAKER_PREFIX = "circuit:mcp:"


def mcp_breaker_key(mcp_server_id: str) -> str:
    return f"{MCP_BREAKER_PREFIX}{mcp_server_id}"


JUDGE_BREAKER_PREFIX = "circuit:judge:"
JUDGE_FAILURES_PREFIX = "judge_failures:"
JUDGE_FAILURES_TTL_SECONDS = 86400


def judge_breaker_key(profile: str) -> str:
    return f"{JUDGE_BREAKER_PREFIX}{profile}"


def judge_failures_key(profile: str) -> str:
    return f"{JUDGE_FAILURES_PREFIX}{profile}"


def _judge_breaker_record(profile: str, *, reason: str, ttl_seconds: int, failures: int) -> dict[str, Any]:
    now = _now_utc()
    return {
        "profile": profile,
        "status": "open",
        "reason": reason,
        "failures": failures,
        "opened_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        "ttl_seconds": ttl_seconds,
    }


def _is_breaker_active(breaker: dict[str, Any] | None) -> bool:
    if not breaker or breaker.get("status") != "open":
        return False
    expires_at_str = breaker.get("expires_at")
    if expires_at_str:
        try:
            exp = datetime.fromisoformat(str(expires_at_str))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= _now_utc():
                return False
        except ValueError:
            pass
    return True


class InMemoryRunStateStore:
    def __init__(self) -> None:
        self.run_states: dict[str, dict[str, Any]] = {}
        self.idempotency_keys: set[str] = set()
        self.breakers: dict[str, dict[str, Any]] = {}
        self.breaker_failures: dict[str, int] = {}
        self.token_leases: dict[str, dict[str, Any]] = {}
        self.quota_counters: dict[str, int] = {}
        self.mcp_breakers: dict[str, dict[str, Any]] = {}
        self.judge_breakers: dict[str, dict[str, Any]] = {}
        self.judge_failures: dict[str, int] = {}

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
        now = _now_utc()
        self.breakers[tool_name] = {
            "tool_name": tool_name,
            "status": "open",
            "reason": reason,
            "opened_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "ttl_seconds": ttl_seconds,
        }
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=tool_name, action="opened").inc()

    async def close_breaker(self, tool_name: str) -> bool:
        existed = tool_name in self.breakers and self.breakers[tool_name].get("status") == "open"
        self.breakers.pop(tool_name, None)
        prefix = f"breaker_failures:{tool_name}:"
        for k in [k for k in self.breaker_failures if k.startswith(prefix)]:
            self.breaker_failures.pop(k, None)
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=tool_name, action="closed").inc()
        return existed

    async def get_open_breakers(self, tool_names: list[str]) -> dict[str, dict[str, Any]]:
        target = set(tool_names)
        result: dict[str, dict[str, Any]] = {}
        for tool_name, breaker in list(self.breakers.items()):
            if not _is_breaker_active(breaker):
                self.breakers.pop(tool_name, None)
                continue
            if tool_name in target:
                result[tool_name] = breaker
        return result

    async def list_open_breakers(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for tool_name, breaker in list(self.breakers.items()):
            if not _is_breaker_active(breaker):
                self.breakers.pop(tool_name, None)
                continue
            result[tool_name] = breaker
        return result

    async def open_mcp_breaker(
        self,
        mcp_server_id: str,
        *,
        reason: str,
        ttl_seconds: int = 1800,
    ) -> None:
        now = _now_utc()
        record = {
            "mcp_server_id": mcp_server_id,
            "status": "open",
            "reason": reason,
            "opened_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "ttl_seconds": ttl_seconds,
        }
        self.mcp_breakers[mcp_breaker_key(mcp_server_id)] = record
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=f"mcp:{mcp_server_id}", action="opened").inc()

    async def close_mcp_breaker(self, mcp_server_id: str) -> bool:
        record = self.mcp_breakers.pop(mcp_breaker_key(mcp_server_id), None)
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=f"mcp:{mcp_server_id}", action="closed").inc()
        return bool(record and record.get("status") == "open")

    async def get_open_mcp_breakers(self, mcp_server_ids: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for server_id in mcp_server_ids:
            key = mcp_breaker_key(server_id)
            record = self.mcp_breakers.get(key)
            if record is None:
                continue
            if not _is_breaker_active(record):
                self.mcp_breakers.pop(key, None)
                continue
            result[server_id] = record
        return result

    async def list_open_mcp_breakers(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for key, record in list(self.mcp_breakers.items()):
            if not _is_breaker_active(record):
                self.mcp_breakers.pop(key, None)
                continue
            result[str(record.get("mcp_server_id") or key.split(MCP_BREAKER_PREFIX, 1)[-1])] = record
        return result

    async def record_tool_failure(
        self,
        tool_name: str,
        *,
        reason: str,
        threshold: int = 3,
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]:
        bucket_key = f"breaker_failures:{tool_name}:{_five_minute_bucket()}"
        count = self.breaker_failures.get(bucket_key, 0) + 1
        self.breaker_failures[bucket_key] = count
        tripped = False
        if count >= max(1, threshold):
            await self.open_breaker(
                tool_name,
                reason=f"Auto-tripped after {count} failures: {reason}",
                ttl_seconds=ttl_seconds,
            )
            self.breakers[tool_name]["failure_count"] = count
            tripped = True
        return {
            "tool_name": tool_name,
            "failure_count": count,
            "threshold": threshold,
            "tripped": tripped,
            "status": "open" if tripped else "closed",
            "reason": reason,
        }

    async def acquire_token_lease(
        self,
        lease: dict[str, Any],
        ttl_seconds: int = 7200,
    ) -> dict[str, Any]:
        now = _now_utc()
        tenant_id = str(lease.get("tenant_id") or "default")
        run_id = str(lease.get("run_id") or "")
        record = {
            **lease,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "status": str(lease.get("status") or "active"),
            "created_at": str(lease.get("created_at") or now.isoformat()),
            "expires_at": str(lease.get("expires_at") or (now + timedelta(seconds=ttl_seconds)).isoformat()),
            "ttl_seconds": int(ttl_seconds),
        }
        key = f"tenant:{tenant_id}:lease:{run_id}"
        self.token_leases[key] = record
        return record

    async def release_token_lease(
        self,
        tenant_id: str,
        run_id: str,
        status: str = "released",
        reason: str = "",
    ) -> dict[str, Any] | None:
        key = f"tenant:{tenant_id}:lease:{run_id}"
        existing = self.token_leases.pop(key, None)
        if existing is None:
            for k, val in list(self.token_leases.items()):
                if val.get("run_id") == run_id:
                    existing = self.token_leases.pop(k, None)
                    break
        if existing is None:
            return None
        return {
            **existing,
            "status": status,
            "released_reason": reason,
            "updated_at": _now_iso(),
        }

    async def list_active_token_leases(
        self,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        now = _now_utc()
        active: list[dict[str, Any]] = []
        for key, record in list(self.token_leases.items()):
            if record.get("status") != "active":
                self.token_leases.pop(key, None)
                continue
            exp_str = record.get("expires_at")
            if exp_str:
                try:
                    exp = datetime.fromisoformat(str(exp_str))
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if exp <= now:
                        self.token_leases.pop(key, None)
                        continue
                except ValueError:
                    pass
            if tenant_id and record.get("tenant_id") != tenant_id:
                continue
            active.append(record)
        return active

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


    async def get_judge_breaker(self, profile: str) -> dict[str, Any] | None:
        record = self.judge_breakers.get(judge_breaker_key(profile))
        if record is None:
            return None
        if not _is_breaker_active(record):
            self.judge_breakers.pop(judge_breaker_key(profile), None)
            return None
        return record

    async def open_judge_breaker(self, profile: str, *, reason: str, ttl_seconds: int, failures: int) -> dict[str, Any]:
        record = _judge_breaker_record(profile, reason=reason, ttl_seconds=ttl_seconds, failures=failures)
        self.judge_breakers[judge_breaker_key(profile)] = record
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=f"judge:{profile}", action="opened").inc()
        return record

    async def close_judge_breaker(self, profile: str) -> bool:
        record = self.judge_breakers.pop(judge_breaker_key(profile), None)
        if record is not None:
            TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=f"judge:{profile}", action="closed").inc()
        return record is not None

    async def record_judge_failure(self, profile: str) -> int:
        key = judge_failures_key(profile)
        self.judge_failures[key] = self.judge_failures.get(key, 0) + 1
        return self.judge_failures[key]

    async def reset_judge_failures(self, profile: str) -> None:
        self.judge_failures.pop(judge_failures_key(profile), None)


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
        now = _now_utc()
        await set_json(
            self.redis,
            f"breaker:{tool_name}",
            {
                "tool_name": tool_name,
                "status": "open",
                "reason": reason,
                "opened_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
                "ttl_seconds": ttl_seconds,
            },
            ttl_seconds=ttl_seconds,
        )
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=tool_name, action="opened").inc()

    async def close_breaker(self, tool_name: str) -> bool:
        deleted = int(await self.redis.delete(f"breaker:{tool_name}"))
        async for key in self.redis.scan_iter(match=f"breaker_failures:{tool_name}:*"):
            await self.redis.delete(key)
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=tool_name, action="closed").inc()
        return deleted > 0

    async def get_open_breakers(self, tool_names: list[str]) -> dict[str, dict[str, Any]]:
        open_breakers: dict[str, dict[str, Any]] = {}
        for tool_name in tool_names:
            breaker = await get_json(self.redis, f"breaker:{tool_name}")
            if _is_breaker_active(breaker):
                open_breakers[tool_name] = breaker
        return open_breakers

    async def list_open_breakers(self) -> dict[str, dict[str, Any]]:
        open_breakers: dict[str, dict[str, Any]] = {}
        async for raw_key in self.redis.scan_iter(match="breaker:*"):
            key_str = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
            tool_name = key_str.split("breaker:", 1)[-1]
            breaker = await get_json(self.redis, key_str)
            if _is_breaker_active(breaker):
                open_breakers[tool_name] = {"tool_name": tool_name, **breaker}
        return open_breakers

    async def open_mcp_breaker(
        self,
        mcp_server_id: str,
        *,
        reason: str,
        ttl_seconds: int = 1800,
    ) -> None:
        now = _now_utc()
        record = {
            "mcp_server_id": mcp_server_id,
            "status": "open",
            "reason": reason,
            "opened_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
            "ttl_seconds": ttl_seconds,
        }
        await set_json(self.redis, mcp_breaker_key(mcp_server_id), record, ttl_seconds=ttl_seconds)
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=f"mcp:{mcp_server_id}", action="opened").inc()

    async def close_mcp_breaker(self, mcp_server_id: str) -> bool:
        deleted = int(await self.redis.delete(mcp_breaker_key(mcp_server_id)))
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=f"mcp:{mcp_server_id}", action="closed").inc()
        return deleted > 0

    async def get_open_mcp_breakers(self, mcp_server_ids: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for server_id in mcp_server_ids:
            record = await get_json(self.redis, mcp_breaker_key(server_id))
            if _is_breaker_active(record):
                result[server_id] = record
        return result

    async def list_open_mcp_breakers(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        async for raw_key in self.redis.scan_iter(match=f"{MCP_BREAKER_PREFIX}*"):
            key_str = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
            server_id = key_str.split(MCP_BREAKER_PREFIX, 1)[-1]
            record = await get_json(self.redis, key_str)
            if _is_breaker_active(record):
                result[server_id] = {"mcp_server_id": server_id, **record}
        return result

    async def record_tool_failure(
        self,
        tool_name: str,
        *,
        reason: str,
        threshold: int = 3,
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]:
        bucket_key = f"breaker_failures:{tool_name}:{_five_minute_bucket()}"
        count = int(await self.redis.incr(bucket_key))
        if count == 1:
            await self.redis.expire(bucket_key, 300)
        tripped = False
        if count >= max(1, threshold):
            await self.open_breaker(
                tool_name,
                reason=f"Auto-tripped after {count} failures: {reason}",
                ttl_seconds=ttl_seconds,
            )
            tripped = True
        return {
            "tool_name": tool_name,
            "failure_count": count,
            "threshold": threshold,
            "tripped": tripped,
            "status": "open" if tripped else "closed",
            "reason": reason,
        }

    async def acquire_token_lease(
        self,
        lease: dict[str, Any],
        ttl_seconds: int = 7200,
    ) -> dict[str, Any]:
        now = _now_utc()
        tenant_id = str(lease.get("tenant_id") or "default")
        run_id = str(lease.get("run_id") or "")
        record = {
            **lease,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "status": str(lease.get("status") or "active"),
            "created_at": str(lease.get("created_at") or now.isoformat()),
            "expires_at": str(lease.get("expires_at") or (now + timedelta(seconds=ttl_seconds)).isoformat()),
            "ttl_seconds": int(ttl_seconds),
        }
        await set_json(
            self.redis,
            f"tenant:{tenant_id}:lease:{run_id}",
            record,
            ttl_seconds=ttl_seconds,
        )
        return record

    async def release_token_lease(
        self,
        tenant_id: str,
        run_id: str,
        status: str = "released",
        reason: str = "",
    ) -> dict[str, Any] | None:
        key = f"tenant:{tenant_id}:lease:{run_id}"
        existing = await get_json(self.redis, key)
        await self.redis.delete(key)
        if existing is None:
            return None
        return {
            **existing,
            "status": status,
            "released_reason": reason,
            "updated_at": _now_iso(),
        }

    async def list_active_token_leases(
        self,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        pattern = f"tenant:{tenant_id}:lease:*" if tenant_id else "tenant:*:lease:*"
        active: list[dict[str, Any]] = []
        async for raw_key in self.redis.scan_iter(match=pattern):
            key_str = raw_key.decode("utf-8") if isinstance(raw_key, bytes) else str(raw_key)
            record = await get_json(self.redis, key_str)
            if record and record.get("status") == "active":
                active.append(record)
        return active

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


    async def get_judge_breaker(self, profile: str) -> dict[str, Any] | None:
        record = await get_json(self.redis, judge_breaker_key(profile))
        return record if _is_breaker_active(record) else None

    async def open_judge_breaker(self, profile: str, *, reason: str, ttl_seconds: int, failures: int) -> dict[str, Any]:
        record = _judge_breaker_record(profile, reason=reason, ttl_seconds=ttl_seconds, failures=failures)
        await set_json(self.redis, judge_breaker_key(profile), record, ttl_seconds=ttl_seconds)
        TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=f"judge:{profile}", action="opened").inc()
        return record

    async def close_judge_breaker(self, profile: str) -> bool:
        deleted = int(await self.redis.delete(judge_breaker_key(profile)))
        if deleted:
            TOOL_BREAKER_EVENTS_TOTAL.labels(tool_name=f"judge:{profile}", action="closed").inc()
        return deleted > 0

    async def record_judge_failure(self, profile: str) -> int:
        key = judge_failures_key(profile)
        count = int(await self.redis.incr(key))
        await self.redis.expire(key, JUDGE_FAILURES_TTL_SECONDS)
        return count

    async def reset_judge_failures(self, profile: str) -> None:
        await self.redis.delete(judge_failures_key(profile))
