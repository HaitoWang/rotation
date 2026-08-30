from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.models import RegisteredCredential


def credential_dict(row: RegisteredCredential, *, include_secret: bool = True) -> Dict[str, Any]:
    fields = (
        "email", "password", "access_token", "session_token", "refresh_token", "id_token",
        "device_id", "csrf_token", "cookie_header", "totp_secret", "totp_factor_id",
        "mail_provider", "extra", "created_at", "updated_at", "deleted_at",
    )
    output = {field: getattr(row, field) for field in fields}
    if not include_secret:
        for field in (
            "password", "access_token", "session_token", "refresh_token", "id_token",
            "csrf_token", "cookie_header", "totp_secret",
        ):
            output.pop(field, None)
    return output


class CredentialRepository:
    async def list_by_emails(
        self, session: AsyncSession, emails: List[str], *, include_deleted: bool = False
    ) -> List[RegisteredCredential]:
        normalized = list(dict.fromkeys(item.strip().lower() for item in emails if item.strip()))
        if not normalized:
            return []
        query = select(RegisteredCredential).where(RegisteredCredential.email.in_(normalized))
        if not include_deleted:
            query = query.where(RegisteredCredential.deleted_at.is_(None))
        return list((await session.scalars(query)).all())

    async def get(
        self, session: AsyncSession, email: str, *, include_deleted: bool = False
    ) -> Optional[RegisteredCredential]:
        row = await session.get(RegisteredCredential, email.strip().lower())
        if row and row.deleted_at and not include_deleted:
            return None
        return row

    async def list(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        include_deleted: bool = False,
    ) -> tuple[List[RegisteredCredential], int]:
        query = select(RegisteredCredential).order_by(RegisteredCredential.created_at.desc())
        count_query = select(func.count()).select_from(RegisteredCredential)
        if not include_deleted:
            query = query.where(RegisteredCredential.deleted_at.is_(None))
            count_query = count_query.where(RegisteredCredential.deleted_at.is_(None))
        rows = list((await session.scalars(query.limit(limit).offset(offset))).all())
        total = int((await session.scalar(count_query)) or 0)
        return rows, total

    async def soft_delete(self, session: AsyncSession, emails: List[str]) -> int:
        normalized = list(dict.fromkeys(item.strip().lower() for item in emails if item.strip()))
        if not normalized:
            return 0
        result = await session.execute(
            update(RegisteredCredential)
            .where(RegisteredCredential.email.in_(normalized))
            .values(deleted_at=datetime.now(timezone.utc))
        )
        return result.rowcount

    async def restore(self, session: AsyncSession, email: str) -> bool:
        result = await session.execute(
            update(RegisteredCredential)
            .where(RegisteredCredential.email == email.strip().lower())
            .values(deleted_at=None)
        )
        return result.rowcount > 0

    async def update_fields(self, session: AsyncSession, email: str, **values: Any) -> bool:
        allowed = {
            "password", "access_token", "session_token", "refresh_token", "id_token",
            "device_id", "csrf_token", "cookie_header", "totp_secret", "totp_factor_id",
            "mail_provider", "extra",
        }
        changes = {key: value for key, value in values.items() if key in allowed}
        if not changes:
            return False
        result = await session.execute(
            update(RegisteredCredential)
            .where(RegisteredCredential.email == email.strip().lower())
            .values(**changes)
        )
        return result.rowcount > 0
