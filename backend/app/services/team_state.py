from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Optional

from app.infrastructure.redis import RedisManager

logger = logging.getLogger(__name__)


class TeamStateStore:
    """Redis hot state for Team rotation.

    PostgreSQL remains the durable source of truth. Redis only contains data
    that is safe to rebuild: seat snapshots, short leases, and a temporary
    global concurrency counter.
    """

    _RELEASE_LOCK = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    end
    return 0
    """

    def __init__(self, redis: RedisManager, *, prefix: str = "regert:team"):
        self.client = redis.client
        self.prefix = prefix.rstrip(":")

    def snapshot_key(self, mother_id: str) -> str:
        return f"{self.prefix}:mother:{mother_id}:snapshot"

    def lock_key(self, mother_id: str) -> str:
        return f"{self.prefix}:mother:{mother_id}:lock"

    def dispatch_slots_key(self) -> str:
        return f"{self.prefix}:dispatch:slots"

    async def get_snapshot(self, mother_id: str) -> Optional[dict[str, Any]]:
        try:
            raw = await self.client.get(self.snapshot_key(mother_id))
            if not raw:
                return None
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except Exception:  # Redis is a cache; callers can fall back to PG.
            logger.debug("could not read Team Redis snapshot", exc_info=True)
            return None

    async def put_snapshot(
        self,
        mother_id: str,
        snapshot: dict[str, Any],
        *,
        ttl: int,
    ) -> bool:
        value = {**snapshot, "cached_at": float(snapshot.get("cached_at") or time.time())}
        try:
            await self.client.set(
                self.snapshot_key(mother_id),
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                ex=max(10, int(ttl)),
            )
            return True
        except Exception:
            logger.warning("could not write Team Redis snapshot mother=%s", mother_id, exc_info=True)
            return False

    async def invalidate(self, mother_id: str) -> None:
        try:
            await self.client.delete(self.snapshot_key(mother_id))
        except Exception:
            logger.debug("could not invalidate Team Redis snapshot", exc_info=True)

    async def acquire(self, mother_id: str, *, ttl: int = 180) -> Optional[str]:
        token = uuid.uuid4().hex
        try:
            ok = await self.client.set(self.lock_key(mother_id), token, nx=True, ex=max(10, int(ttl)))
            return token if ok else None
        except Exception:
            logger.error("Team Redis unavailable; rotation is paused for mother=%s", mother_id, exc_info=True)
            return None

    async def release(self, mother_id: str, token: Optional[str]) -> None:
        if not token:
            return
        try:
            await self.client.eval(self._RELEASE_LOCK, 1, self.lock_key(mother_id), token)
        except Exception:
            logger.debug("could not release Team Redis lock", exc_info=True)

    async def extend(self, mother_id: str, token: Optional[str], *, ttl: int = 300) -> bool:
        if not token:
            return False
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('expire', KEYS[1], ARGV[2])
        end
        return 0
        """
        try:
            return bool(await self.client.eval(script, 1, self.lock_key(mother_id), token, max(10, int(ttl))))
        except Exception:
            return False

    async def acquire_dispatch_slot(self, *, limit: int, ttl: int = 300) -> Optional[str]:
        """Atomically reserve one global mother-processing slot."""
        token = uuid.uuid4().hex
        script = """
        local now = tonumber(ARGV[1])
        local limit = tonumber(ARGV[2])
        local ttl = tonumber(ARGV[3])
        redis.call('zremrangebyscore', KEYS[1], '-inf', now)
        if redis.call('zcard', KEYS[1]) >= limit then
          return ''
        end
        redis.call('zadd', KEYS[1], now + ttl, ARGV[4])
        redis.call('expire', KEYS[1], math.max(60, math.floor(ttl * 2)))
        return ARGV[4]
        """
        try:
            result = await self.client.eval(
                script,
                1,
                self.dispatch_slots_key(),
                time.time(),
                max(1, int(limit)),
                max(30, int(ttl)),
                token,
            )
            return token if result else None
        except Exception:
            logger.error("Team Redis unavailable; mother dispatch is paused", exc_info=True)
            return None

    async def release_dispatch_slot(self, token: Optional[str]) -> None:
        if not token:
            return
        try:
            await self.client.zrem(self.dispatch_slots_key(), token)
        except Exception:
            logger.debug("could not release Team dispatch slot", exc_info=True)

    async def extend_dispatch_slot(self, token: Optional[str], *, ttl: int = 300) -> bool:
        if not token:
            return False
        script = """
        if redis.call('zscore', KEYS[1], ARGV[1]) then
          redis.call('zadd', KEYS[1], ARGV[2], ARGV[1])
          redis.call('expire', KEYS[1], math.max(60, math.floor(ARGV[3] * 2)))
          return 1
        end
        return 0
        """
        try:
            ttl = max(30, int(ttl))
            return bool(await self.client.eval(
                script,
                1,
                self.dispatch_slots_key(),
                token,
                time.time() + ttl,
                ttl,
            ))
        except Exception:
            return False

    async def adjust_remaining(self, mother_id: str, delta: int) -> Optional[dict[str, Any]]:
        """Adjust the cached configured pool after a confirmed join/removal."""
        snapshot = await self.get_snapshot(mother_id)
        if snapshot is None:
            return None
        try:
            current = max(0, int(snapshot.get("remaining_configured") or 0))
            snapshot["remaining_configured"] = max(0, current + int(delta))
            if isinstance(snapshot.get("pools"), dict):
                preferred = str(snapshot.get("preferred_seat_type") or "standard")
                pool = snapshot["pools"].get(preferred)
                if isinstance(pool, dict):
                    pool["available"] = max(0, int(pool.get("available") or 0) + int(delta))
            snapshot["in_use"] = max(0, int(snapshot.get("in_use") or 0) - int(delta))
            snapshot["cached_at"] = time.time()
        except (TypeError, ValueError):
            return None
        await self.put_snapshot(mother_id, snapshot, ttl=86400)
        return snapshot
