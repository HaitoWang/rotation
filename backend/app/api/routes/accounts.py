from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.api.schemas import AccountImportRequest, AccountResponse, AccountTextImportRequest
from app.infrastructure.models import Account
from app.repositories.accounts import AccountRepository

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])
repository = AccountRepository()


@router.post("/import/text", status_code=status.HTTP_201_CREATED)
async def import_account_text(
    request: AccountTextImportRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Import provider-specific account formats into PostgreSQL."""

    from app.integrations.mail.providers import ImportValidationError, parse_import_text

    try:
        rows = parse_import_text(request.text or "", request.kind or "")
    except ImportValidationError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "errors": exc.errors}) from exc
    inserted = 0
    updated = 0
    async with session.begin():
        for row in rows:
            from app.integrations.mail.providers import get_provider_class

            provider_kind = row.get("kind") or request.kind or "outlook"
            try:
                pooled = bool(get_provider_class(provider_kind).pooled)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"邮箱 provider 不可用: {provider_kind}") from exc
            _account, created = await repository.upsert(
                session,
                email=row["email"],
                kind=provider_kind,
                password=row.get("password", ""),
                client_id=row.get("client_id", ""),
                refresh_token=row.get("refresh_token", ""),
                relay_url=row.get("relay_url", ""),
                pooled=pooled,
            )
            if created:
                inserted += 1
            else:
                updated += 1
    return {"ok": True, "parsed": len(rows), "inserted": inserted, "updated": updated}


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_accounts(
    request: AccountImportRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    inserted = 0
    updated = 0
    async with session.begin():
        for item in request.accounts:
            from app.integrations.mail.providers import get_provider_class

            try:
                pooled = bool(get_provider_class(item.kind).pooled)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"邮箱 provider 不可用: {item.kind}") from exc
            _account, created = await repository.upsert(
                session,
                email=item.email,
                kind=item.kind,
                password=item.password,
                client_id=item.client_id,
                refresh_token=item.refresh_token,
                relay_url=item.relay_url,
                pooled=pooled,
            )
            if created:
                inserted += 1
            else:
                updated += 1
    return {"ok": True, "inserted": inserted, "updated": updated}


@router.get("", include_in_schema=True)
async def list_accounts(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    kind: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    rows, total = await repository.list(
        session, status=status_filter, kind=kind, limit=limit, offset=offset
    )
    return {
        "ok": True,
        "items": [AccountResponse.from_model(row) for row in rows],
        "total": total,
        "by_kind": await repository.stats_by_kind(session),
    }


@router.get("/stats")
async def account_stats(session: AsyncSession = Depends(get_db_session)) -> dict:
    return {"ok": True, "stats": await repository.stats(session)}


@router.post("/bulk-delete")
async def bulk_delete_accounts(
    payload: dict,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    async with session.begin():
        deleted = await repository.delete_many(
            session,
            emails=payload.get("emails"),
            status=payload.get("status"),
        )
    return {"ok": True, "deleted": deleted}


@router.post("/bulk-reset")
async def bulk_reset_accounts(payload: dict, session: AsyncSession = Depends(get_db_session)) -> dict:
    async with session.begin():
        reset = await repository.reset_many(session, payload.get("emails") or [])
    return {"ok": True, "reset": reset}


@router.post("/reset-failed")
async def reset_failed_accounts(session: AsyncSession = Depends(get_db_session)) -> dict:
    rows = (await session.scalars(select(Account))).all()
    await session.commit()
    async with session.begin():
        reset = await repository.reset_many(
            session,
            [row.email for row in rows if row.status == "failed"],
        )
    return {"ok": True, "reset": reset}


@router.post("/release-stale")
async def release_stale_accounts(
    stale_seconds: int = Query(default=1800, ge=60, le=86400),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    async with session.begin():
        released = await repository.release_stale(session, stale_seconds)
    return {"ok": True, "released": released}


# Keep the static bulk/stat paths ahead of this dynamic path in Starlette's
# route table so `/stats` cannot be interpreted as an email address.
@router.get("/{email}", response_model=AccountResponse)
async def get_account(email: str, session: AsyncSession = Depends(get_db_session)) -> AccountResponse:
    account = await repository.get(session, email)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return AccountResponse.from_model(account)


@router.post("/{email}/reset")
async def reset_account(email: str, session: AsyncSession = Depends(get_db_session)) -> dict:
    async with session.begin():
        reset = await repository.reset(session, email)
    if not reset:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"ok": True, "email": email.strip().lower()}


@router.delete("/{email}")
async def delete_account(email: str, session: AsyncSession = Depends(get_db_session)) -> dict:
    async with session.begin():
        deleted = await repository.delete(session, email)
    if not deleted:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"ok": True}
