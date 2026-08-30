from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.repositories.queues import QueueRepository
from app.repositories.settings import SettingsRepository

logger = logging.getLogger(__name__)


class SMSCleanupService:
    """Cancel expired SMS rentals from a restart-safe Postgres lease queue."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self.sessions = sessions
        self.queue = QueueRepository()
        self.settings = SettingsRepository()

    async def dispatch_once(self, *, limit: int = 16) -> int:
        async with self.sessions() as session:
            async with session.begin():
                config = await self.settings.list_prefix(session, "sms_")
                rows = await self.queue.claim_sms_cleanup(session, limit=limit, lease_seconds=180)
        if not rows:
            return 0
        providers: Dict[str, Any] = {}
        for row in rows:
            platform = str(row.platform or "").strip().lower()
            try:
                if platform not in providers:
                    providers[platform] = await asyncio.to_thread(
                        self._create_provider, platform, config
                    )
                provider = providers[platform]
                ok = await asyncio.to_thread(self._cancel, provider, row)
                if not ok:
                    raise RuntimeError(getattr(provider, "last_cancel_error", "取消未确认成功"))
                async with self.sessions() as session:
                    async with session.begin():
                        await self.queue.complete_sms_cleanup(session, platform, row.activation_id)
            except Exception as exc:  # noqa: BLE001
                async with self.sessions() as session:
                    async with session.begin():
                        await self.queue.fail_sms_cleanup(
                            session, platform, row.activation_id, str(exc)
                        )
                logger.warning("sms cleanup failed platform=%s activation=%s: %s", platform, row.activation_id, exc)
        return len(rows)

    @staticmethod
    def _create_provider(platform: str, config: Dict[str, Any]):
        from app.integrations.sms.provider import create_sms_provider

        return create_sms_provider(platform, config)

    @staticmethod
    def _cancel(provider, row) -> bool:
        from app.integrations.sms.provider import SmsActivation

        provider.current_activation = SmsActivation(
            row.activation_id,
            row.phone_number or "",
            "",
            {"acquired_at": float(row.acquired_at or 0)},
        )
        return bool(provider.cancel(row.activation_id, record_failure=False))
