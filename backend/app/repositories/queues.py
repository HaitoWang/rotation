from __future__ import annotations

import time
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.models import SMSActivationCleanup, TeamSSOSyncQueue


class QueueRepository:
    """Lease-based persistence for work that must survive process restarts."""

    async def enqueue_sso(self, session: AsyncSession, *, email: str, content: str) -> None:
        row = await session.get(TeamSSOSyncQueue, email.lower())
        now = time.time()
        if row is None:
            session.add(
                TeamSSOSyncQueue(
                    email=email.lower(),
                    content=content,
                    attempts=0,
                    next_attempt_at=0,
                    lease_until=0,
                    updated_at=now,
                )
            )
        else:
            row.content = content
            row.next_attempt_at = 0
            row.lease_until = 0
            row.last_error = None
            row.updated_at = now

    async def enqueue_sms_cleanup(
        self,
        session: AsyncSession,
        *,
        platform: str,
        activation_id: str,
        phone_number: str = "",
        acquired_at: float = 0,
        cancel_after: float = 0,
        not_before: float = 0,
        error: str = "",
    ) -> None:
        key = (platform, activation_id)
        row = await session.get(SMSActivationCleanup, key)
        now = time.time()
        if row is None:
            session.add(
                SMSActivationCleanup(
                    platform=platform,
                    activation_id=activation_id,
                    phone_number=phone_number,
                    acquired_at=acquired_at or now,
                    cancel_after=cancel_after or now,
                    status="pending_cancel",
                    attempts=0,
                    next_attempt_at=max(now, not_before or now),
                    lease_until=0,
                    last_error=error[:1000] or None,
                    updated_at=now,
                )
            )
        else:
            row.status = "pending_cancel"
            row.cancel_after = cancel_after or row.cancel_after
            due = max(now, not_before or now)
            row.next_attempt_at = min(row.next_attempt_at, due) if row.next_attempt_at else due
            row.lease_until = 0
            if error:
                row.last_error = error[:1000]
            row.updated_at = now

    async def track_sms_activation(
        self,
        session: AsyncSession,
        *,
        platform: str,
        activation_id: str,
        phone_number: str = "",
        acquired_at: float = 0,
        lifetime_seconds: float = 20 * 60,
    ) -> None:
        """Persist an active SMS rental before the upstream request continues."""
        now = time.time()
        acquired = float(acquired_at or now)
        row = await session.get(SMSActivationCleanup, (platform, activation_id))
        if row is None:
            session.add(
                SMSActivationCleanup(
                    platform=platform,
                    activation_id=activation_id,
                    phone_number=phone_number,
                    acquired_at=acquired,
                    cancel_after=acquired + max(60.0, float(lifetime_seconds)) + 60.0,
                    status="active",
                    attempts=0,
                    next_attempt_at=0,
                    lease_until=0,
                    updated_at=now,
                )
            )
        else:
            if phone_number:
                row.phone_number = phone_number
            row.acquired_at = acquired
            row.status = "active"
            row.cancel_after = acquired + max(60.0, float(lifetime_seconds)) + 60.0
            row.next_attempt_at = 0
            row.lease_until = 0
            row.last_error = None
            row.updated_at = now

    async def claim_sso(
        self, session: AsyncSession, *, limit: int = 16, lease_seconds: float = 45
    ) -> List[TeamSSOSyncQueue]:
        now = time.time()
        query = (
            select(TeamSSOSyncQueue)
            .where(TeamSSOSyncQueue.next_attempt_at <= now, TeamSSOSyncQueue.lease_until <= now)
            .order_by(TeamSSOSyncQueue.next_attempt_at.asc())
            .limit(max(1, min(limit, 100)))
            .with_for_update(skip_locked=True)
        )
        rows = list((await session.scalars(query)).all())
        for row in rows:
            row.lease_until = now + lease_seconds
            row.attempts += 1
            row.updated_at = now
        await session.flush()
        return rows

    async def complete_sso(self, session: AsyncSession, email: str) -> bool:
        from sqlalchemy import delete

        result = await session.execute(delete(TeamSSOSyncQueue).where(TeamSSOSyncQueue.email == email.lower()))
        return result.rowcount > 0

    async def fail_sso(self, session: AsyncSession, email: str, error: str, retry_seconds: float = 60) -> bool:
        row = await session.get(TeamSSOSyncQueue, email.lower())
        if row is None:
            return False
        row.lease_until = 0
        row.next_attempt_at = time.time() + retry_seconds
        row.last_error = error[:1000]
        row.updated_at = time.time()
        return True

    async def claim_sms_cleanup(
        self, session: AsyncSession, *, limit: int = 16, lease_seconds: float = 45
    ) -> List[SMSActivationCleanup]:
        now = time.time()
        query = (
            select(SMSActivationCleanup)
            .where(
                (
                    (SMSActivationCleanup.status == "pending_cancel")
                    & (SMSActivationCleanup.next_attempt_at <= now)
                )
                | (
                    (SMSActivationCleanup.status == "active")
                    & (SMSActivationCleanup.cancel_after <= now)
                ),
                SMSActivationCleanup.lease_until <= now,
            )
            .order_by(SMSActivationCleanup.cancel_after.asc())
            .limit(max(1, min(limit, 100)))
            .with_for_update(skip_locked=True)
        )
        rows = list((await session.scalars(query)).all())
        for row in rows:
            row.status = "pending_cancel"
            row.lease_until = now + lease_seconds
            row.attempts += 1
            row.updated_at = now
        await session.flush()
        return rows

    async def complete_sms_cleanup(
        self, session: AsyncSession, platform: str, activation_id: str
    ) -> bool:
        from sqlalchemy import delete

        result = await session.execute(
            delete(SMSActivationCleanup).where(
                SMSActivationCleanup.platform == platform,
                SMSActivationCleanup.activation_id == activation_id,
            )
        )
        return result.rowcount > 0

    async def fail_sms_cleanup(
        self, session: AsyncSession, platform: str, activation_id: str, error: str, retry_seconds: float = 60
    ) -> bool:
        row = await session.get(SMSActivationCleanup, (platform, activation_id))
        if row is None:
            return False
        row.lease_until = 0
        row.next_attempt_at = time.time() + retry_seconds
        row.last_error = error[:1000]
        row.updated_at = time.time()
        return True
