from __future__ import annotations

import asyncio
import time
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.queues import QueueRepository


class AsyncSmsPersistence:
    """Bridge blocking SMS provider callbacks to the async PostgreSQL queue."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], loop: asyncio.AbstractEventLoop):
        self.sessions = sessions
        self.loop = loop
        self.repository = QueueRepository()

    def _run(self, operation):
        future = asyncio.run_coroutine_threadsafe(operation(), self.loop)
        return future.result(timeout=15)

    def track(self, platform: str, activation_id: str, phone_number: str, acquired_at: float) -> None:
        async def operation():
            async with self.sessions() as session:
                async with session.begin():
                    await self.repository.track_sms_activation(
                        session,
                        platform=platform,
                        activation_id=activation_id,
                        phone_number=phone_number,
                        acquired_at=acquired_at,
                    )

        self._run(operation)

    def queue(
        self,
        platform: str,
        activation_id: str,
        *,
        phone_number: str = "",
        acquired_at: Optional[float] = None,
        error: str = "",
    ) -> None:
        async def operation():
            async with self.sessions() as session:
                async with session.begin():
                    acquired = float(acquired_at or 0)
                    not_before = time.time()
                    if platform.strip().lower() == "herosms":
                        not_before = max(not_before, acquired + 125)
                    await self.repository.enqueue_sms_cleanup(
                        session,
                        platform=platform.strip().lower(),
                        activation_id=activation_id.strip(),
                        phone_number=phone_number,
                        acquired_at=acquired,
                        cancel_after=acquired + 20 * 60,
                        not_before=not_before,
                        error=error,
                    )

        self._run(operation)

    def complete(self, platform: str, activation_id: str) -> None:
        async def operation():
            async with self.sessions() as session:
                async with session.begin():
                    await self.repository.complete_sms_cleanup(session, platform, activation_id)

        self._run(operation)
