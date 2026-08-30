from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.models import Setting


class SettingsRepository:
    async def all(self, session: AsyncSession) -> Dict[str, Any]:
        rows = (await session.scalars(select(Setting))).all()
        return {row.key: row.value.get("value", row.value) for row in rows}

    async def list_prefix(self, session: AsyncSession, prefix: str) -> Dict[str, Any]:
        rows = (await session.scalars(select(Setting).where(Setting.key.startswith(prefix)))).all()
        return {row.key: row.value.get("value", row.value) for row in rows}

    async def get(self, session: AsyncSession, key: str) -> Optional[Dict[str, Any]]:
        row = await session.get(Setting, key)
        return dict(row.value or {}) if row else None

    async def get_value(self, session: AsyncSession, key: str, default: Any = None) -> Any:
        value = await self.get(session, key)
        if value is None:
            return default
        return value.get("value", value)

    async def set(self, session: AsyncSession, key: str, value: Any) -> Dict[str, Any]:
        row = await session.get(Setting, key)
        payload = value if isinstance(value, dict) else {"value": value}
        if row is None:
            row = Setting(key=key, value=payload)
            session.add(row)
        else:
            row.value = payload
        await session.flush()
        return dict(row.value or {})
