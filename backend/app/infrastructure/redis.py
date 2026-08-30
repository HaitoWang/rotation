from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from redis.asyncio import Redis

from app.core.config import Settings


class RedisManager:
    """Redis connection plus durable per-run event streams.

    Redis Streams are used for SSE events rather than an in-process queue. A
    browser can reconnect, and multiple API replicas can serve the same run.
    ARQ uses the same Redis URL for background jobs.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            health_check_interval=30,
        )
        self._arq_pool = None

    @property
    def stream_prefix(self) -> str:
        return self.settings.redis_event_channel_prefix

    def stream_key(self, run_id: str) -> str:
        return f"{self.stream_prefix}:{run_id}"

    async def start(self) -> None:
        # Import lazily so tooling that only imports the domain models does not
        # need to initialize an ARQ connection.
        from arq import create_pool
        from arq.connections import RedisSettings

        self._arq_pool = await create_pool(RedisSettings.from_dsn(self.settings.redis_url))

    async def ping(self) -> float:
        import time

        started = time.perf_counter()
        await self.client.ping()
        return round((time.perf_counter() - started) * 1000, 2)

    async def enqueue(self, function: str, *, job_id: str, **kwargs: Any) -> str:
        if self._arq_pool is None:
            raise RuntimeError("RedisManager.start() must run before enqueue")
        job = await self._arq_pool.enqueue_job(
            function,
            _job_id=job_id,
            _queue_name=self.settings.redis_queue_name,
            **kwargs,
        )
        if job is None:
            # ARQ returns None when the deterministic job id already exists.
            return job_id
        return job.job_id

    async def publish_event(self, run_id: str, event: str, data: dict[str, Any]) -> str:
        payload = {
            "event": event,
            "data": data,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return await self.client.xadd(
            self.stream_key(run_id),
            {"payload": json.dumps(payload, ensure_ascii=False)},
            maxlen=self.settings.redis_stream_maxlen,
            approximate=True,
        )

    async def events(
        self,
        run_id: str,
        *,
        last_id: str = "0-0",
        block_ms: int = 15000,
    ) -> AsyncIterator[dict[str, Any]]:
        """Read a run stream and keep the connection alive with heartbeats."""

        cursor = last_id or "0-0"
        key = self.stream_key(run_id)
        while True:
            response = await self.client.xread({key: cursor}, block=block_ms, count=100)
            if not response:
                yield {"event": "heartbeat", "data": {}}
                continue
            for _stream, messages in response:
                for message_id, fields in messages:
                    cursor = message_id
                    try:
                        payload = json.loads(fields["payload"])
                        if isinstance(payload, dict):
                            payload["id"] = message_id
                            yield payload
                        else:
                            yield {"event": "message", "data": payload, "id": message_id}
                    except (KeyError, TypeError, json.JSONDecodeError):
                        yield {"event": "message", "data": fields, "id": message_id}

    async def close(self) -> None:
        if self._arq_pool is not None:
            result = self._arq_pool.close()
            if inspect.isawaitable(result):
                await result
            self._arq_pool = None
        await self.client.aclose()
