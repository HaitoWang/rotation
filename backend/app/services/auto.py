from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.infrastructure.redis import RedisManager
from app.repositories.settings import SettingsRepository
from app.services.registration import RegistrationService


class AutoRunService:
    """Durable auto-registration control plane backed by Postgres settings."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        redis: RedisManager,
        settings: Settings,
    ):
        self.sessions = sessions
        self.redis = redis
        self.settings = settings
        self.repository = SettingsRepository()

    async def status(self, session: AsyncSession | None = None) -> dict[str, Any]:
        owns = session is None
        if owns:
            session = self.sessions()
        try:
            state = await self.repository.get_value(session, "auto_state", "stopped")
            config = await self.repository.get_value(session, "auto_config", {})
            ok = await self.repository.get_value(session, "auto_registered_ok", 0)
            failed = await self.repository.get_value(session, "auto_registered_fail", 0)
            return {
                "state": str(state or "stopped"),
                "registered_ok": int(ok or 0),
                "registered_fail": int(failed or 0),
                "config": config if isinstance(config, dict) else {},
                "updated_at": await self.repository.get_value(session, "auto_updated_at", 0),
            }
        finally:
            if owns:
                await session.close()

    async def set_state(self, state: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self.sessions() as session:
            async with session.begin():
                await self.repository.set(session, "auto_state", state)
                await self.repository.set(session, "auto_updated_at", time.time())
                if config is not None:
                    await self.repository.set(session, "auto_config", config)
                value = await self.status(session)
        await self.redis.publish_event("auto", "state", value)
        return value

    async def tick(self) -> bool:
        async with self.sessions() as session:
            value = await self.status(session)
        if value["state"] != "running":
            return False
        options = dict(value.get("config") or {})
        options.pop("target_count", None)
        service = RegistrationService(self.sessions, self.redis)
        try:
            await service.enqueue(
                email=None,
                kind=str(options.pop("kind", "") or "") or None,
                options=options,
            )
            return True
        except LookupError:
            return False

    async def record_result(self, *, success: bool, data: dict[str, Any]) -> None:
        key = "auto_registered_ok" if success else "auto_registered_fail"
        async with self.sessions() as session:
            async with session.begin():
                current = int(await self.repository.get_value(session, key, 0) or 0)
                await self.repository.set(session, key, current + 1)
        await self.redis.publish_event("auto", "run_finished", data)
