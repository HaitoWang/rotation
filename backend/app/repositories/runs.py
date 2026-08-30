from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.models import RegistrationRun


class RunRepository:
    async def list(
        self,
        session: AsyncSession,
        *,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[RegistrationRun], int]:
        query = select(RegistrationRun).order_by(RegistrationRun.started_at.desc()).limit(limit).offset(offset)
        count_query = select(func.count()).select_from(RegistrationRun)
        if status:
            query = query.where(RegistrationRun.status == status)
            count_query = count_query.where(RegistrationRun.status == status)
        return (
            list((await session.scalars(query)).all()),
            int((await session.scalar(count_query)) or 0),
        )

    async def create(
        self, session: AsyncSession, *, run_id: str, email: str, options: dict
    ) -> RegistrationRun:
        run = RegistrationRun(
            run_id=run_id,
            email=email,
            status="queued",
            options=options,
            result={},
            log_path="",
        )
        session.add(run)
        await session.flush()
        return run

    async def get(self, session: AsyncSession, run_id: str) -> Optional[RegistrationRun]:
        return await session.get(RegistrationRun, run_id)

    async def mark_running(self, session: AsyncSession, run_id: str) -> None:
        run = await session.get(RegistrationRun, run_id)
        if run:
            run.status = "running"
            run.started_at = datetime.now(timezone.utc)
            await session.flush()

    async def finish(
        self,
        session: AsyncSession,
        run_id: str,
        *,
        success: bool,
        result: Optional[Dict] = None,
        error: str = "",
        category: str = "",
    ) -> None:
        run = await session.get(RegistrationRun, run_id)
        if run is None:
            return
        run.status = "done" if success else "failed"
        run.result = result or {}
        run.error = error[:4000] if error else None
        run.error_category = category or None
        run.finished_at = datetime.now(timezone.utc)
        await session.flush()
