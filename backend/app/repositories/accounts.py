from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.models import Account


class AccountRepository:
    async def stats(self, session: AsyncSession) -> dict[str, int]:
        rows = (await session.execute(select(Account.status, func.count()).group_by(Account.status))).all()
        values = {str(status): int(count) for status, count in rows}
        return {
            "total": sum(values.values()),
            "available": values.get("available", 0),
            "in_use": values.get("in_use", 0),
            "done": values.get("done", 0),
            "failed": values.get("failed", 0),
        }

    async def stats_by_kind(self, session: AsyncSession) -> dict[str, dict[str, int]]:
        rows = (
            await session.execute(
                select(Account.kind, Account.status, func.count()).group_by(Account.kind, Account.status)
            )
        ).all()
        output: dict[str, dict[str, int]] = {}
        for kind, status, count in rows:
            bucket = output.setdefault(str(kind), {"total": 0})
            bucket[str(status)] = int(count)
            bucket["total"] += int(count)
        return output

    async def delete_many(
        self, session: AsyncSession, *, emails: Optional[list[str]] = None, status: Optional[str] = None
    ) -> int:
        filters = []
        if emails:
            filters.append(Account.email.in_([item.strip().lower() for item in emails if item.strip()]))
        if status and status != "all":
            filters.append(Account.status == status)
        statement = delete(Account).where(*filters) if filters else delete(Account)
        result = await session.execute(statement)
        return int(result.rowcount or 0)

    async def reset_many(self, session: AsyncSession, emails: list[str]) -> int:
        normalized = [item.strip().lower() for item in emails if item.strip()]
        if not normalized:
            return 0
        result = await session.execute(
            update(Account)
            .where(Account.email.in_(normalized))
            .values(status="available", claimed_at=None, finished_at=None, fail_reason=None)
        )
        return int(result.rowcount or 0)

    async def release_stale(self, session: AsyncSession, stale_seconds: int = 1800) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - max(60, int(stale_seconds))
        result = await session.execute(
            update(Account)
            .where(Account.status == "in_use", Account.claimed_at.is_not(None),
                   Account.claimed_at < datetime.fromtimestamp(cutoff, timezone.utc))
            .values(status="available", claimed_at=None)
        )
        return int(result.rowcount or 0)

    async def get(self, session: AsyncSession, email: str) -> Optional[Account]:
        return await session.get(Account, email.strip().lower())

    async def list(
        self,
        session: AsyncSession,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Account], int]:
        filters = []
        if status:
            filters.append(Account.status == status)
        if kind:
            filters.append(Account.kind == kind)
        query = select(Account).order_by(Account.created_at.desc()).limit(limit).offset(offset)
        count_query = select(func.count()).select_from(Account)
        if filters:
            query = query.where(*filters)
            count_query = count_query.where(*filters)
        rows = list((await session.scalars(query)).all())
        total = int((await session.scalar(count_query)) or 0)
        return rows, total

    async def upsert(
        self,
        session: AsyncSession,
        *,
        email: str,
        kind: str = "outlook",
        password: str = "",
        client_id: str = "",
        refresh_token: str = "",
        relay_url: str = "",
        pooled: bool = True,
    ) -> tuple[Account, bool]:
        normalized = email.strip().lower()
        account = await session.get(Account, normalized)
        created = account is None
        if account is None:
            account = Account(email=normalized)
            session.add(account)
        account.kind = kind.strip().lower() or "outlook"
        account.pooled = 1 if pooled else 0
        account.password = password or ""
        account.client_id = client_id or ""
        account.refresh_token = refresh_token or ""
        account.relay_url = relay_url or ""
        if not created:
            account.status = "available"
            account.claimed_at = None
            account.finished_at = None
            account.fail_reason = None
        await session.flush()
        return account, created

    async def claim(
        self, session: AsyncSession, *, email: Optional[str] = None, kind: Optional[str] = None
    ) -> Optional[Account]:
        filters = [Account.status == "available", Account.pooled == 1]
        if email:
            filters.append(Account.email == email.strip().lower())
        if kind:
            filters.append(Account.kind == kind.strip().lower())
        query = select(Account).where(*filters).order_by(Account.created_at.asc()).limit(1)
        # PostgreSQL row locks make claims safe across API replicas and workers.
        query = query.with_for_update(skip_locked=True)
        account = (await session.scalars(query)).first()
        if account is None:
            return None
        account.status = "in_use"
        account.claimed_at = datetime.now(timezone.utc)
        account.fail_reason = None
        await session.flush()
        return account

    async def finish(self, session: AsyncSession, email: str, *, success: bool, reason: str = "") -> None:
        values = {
            "status": "done" if success else "failed",
            "finished_at": datetime.now(timezone.utc),
            "fail_reason": None if success else reason[:1000],
        }
        await session.execute(update(Account).where(Account.email == email.lower()).values(**values))

    async def release(self, session: AsyncSession, email: str) -> None:
        """Return a claim when the queue itself is unavailable."""

        await session.execute(
            update(Account)
            .where(Account.email == email.lower(), Account.status == "in_use")
            .values(status="available", claimed_at=None)
        )

    async def reset(self, session: AsyncSession, email: str) -> bool:
        result = await session.execute(
            update(Account)
            .where(Account.email == email.strip().lower())
            .values(status="available", claimed_at=None, finished_at=None, fail_reason=None)
        )
        return result.rowcount > 0

    async def delete(self, session: AsyncSession, email: str) -> bool:
        result = await session.execute(delete(Account).where(Account.email == email.strip().lower()))
        return result.rowcount > 0
