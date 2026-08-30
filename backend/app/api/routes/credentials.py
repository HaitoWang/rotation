from __future__ import annotations

import asyncio
import time
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.repositories.credentials import CredentialRepository, credential_dict
from app.repositories.accounts import AccountRepository
from app.services.registration import RegistrationService

router = APIRouter(prefix="/api/v1/credentials", tags=["credentials"])
repository = CredentialRepository()
accounts = AccountRepository()


@router.post("/check-plus")
async def check_plus(payload: Dict[str, Any], session: AsyncSession = Depends(get_db_session)) -> dict:
    from app.integrations.openai.http_client import DEFAULT_IMPERSONATE, create_http_session, us_chrome_headers

    rows, _ = await repository.list(session, limit=100000) if payload.get("all") else (
        await repository.list_by_emails(session, payload.get("emails") or []), 0
    )
    proxy = str(payload.get("proxy") or "").strip() or None
    endpoint = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    chrome = us_chrome_headers()

    def check(row):
        token = str(row.access_token or "").strip()
        if not token:
            return row.email, {"status": "no_at", "label": "无AT"}
        try:
            client = create_http_session(proxy=proxy, impersonate=DEFAULT_IMPERSONATE)
            response = client.get(endpoint, headers={**chrome, "Authorization": f"Bearer {token}"}, timeout=15)
            if response.status_code == 401:
                return row.email, {"status": "banned", "label": "封号"}
            if response.status_code != 200:
                return row.email, {"status": "error", "label": f"HTTP {response.status_code}"}
            body = response.json()
            account = next(iter((body.get("accounts") or {}).values()), {})
            info = account.get("account") or {}
            entitlement = account.get("entitlement") or {}
            promo = account.get("eligible_promo_campaigns") or {}
            if info.get("is_deactivated"):
                return row.email, {"status": "banned", "label": "封号"}
            if info.get("plan_type") == "plus" or entitlement.get("has_active_subscription"):
                return row.email, {"status": "plus_active", "label": "Plus生效中"}
            if (promo.get("plus") or {}).get("id") == "plus-1-month-free":
                return row.email, {"status": "plus_eligible", "label": "可领Plus试用"}
            return row.email, {"status": "free", "label": "Free"}
        except Exception as exc:  # noqa: BLE001
            return row.email, {"status": "error", "label": str(exc)[:120]}

    results = dict(await asyncio.gather(*(asyncio.to_thread(check, row) for row in rows)))
    await session.commit()
    checked_at = time.time()
    async with session.begin():
        for row in rows:
            info = results.get(row.email, {})
            if info.get("status") in {"error", "no_at"}:
                continue
            extra = dict(row.extra or {})
            extra["plus_check"] = {**info, "checked_at": checked_at}
            row.extra = extra
    return {"ok": True, "results": results, "summary": {"total": len(results)}}


@router.post("/bulk-delete")
async def bulk_delete_credentials(payload: Dict[str, Any], session: AsyncSession = Depends(get_db_session)) -> dict:
    emails = payload.get("emails") or []
    if payload.get("all"):
        rows, _ = await repository.list(session, limit=100000, include_deleted=False)
        emails = [row.email for row in rows]
        await session.commit()
    if not emails:
        raise HTTPException(status_code=400, detail="需要 emails 或 all=true")
    async with session.begin():
        deleted = await repository.soft_delete(session, emails)
    return {"ok": True, "deleted": deleted}


@router.post("/{email}/reauthorize")
async def reauthorize_credential(
    email: str,
    payload: Dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    account = await accounts.get(session, email)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    kind = account.kind
    await session.rollback()
    async with session.begin():
        await accounts.reset(session, email)
    service = RegistrationService(request.app.state.database.session_factory, request.app.state.redis)
    try:
        result = await service.enqueue(
            email=email,
            kind=kind,
            options={"proxy": str(payload.get("proxy") or ""), "reauthorize": True},
        )
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "queued": True, **result}


@router.post("/bulk-reauthorize")
async def bulk_reauthorize_credentials(
    payload: Dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    emails = [str(item).strip().lower() for item in payload.get("emails") or [] if str(item).strip()]
    if not emails:
        raise HTTPException(status_code=400, detail="emails 不能为空")
    service = RegistrationService(request.app.state.database.session_factory, request.app.state.redis)
    results = []
    for email in emails:
        try:
            account = await accounts.get(session, email)
            kind = account.kind if account else ""
            await session.rollback()
            async with session.begin():
                if account is None:
                    raise LookupError("账号不存在")
                await accounts.reset(session, email)
            queued = await service.enqueue(
                email=email,
                kind=kind,
                options={"proxy": str(payload.get("proxy") or ""), "reauthorize": True},
            )
            results.append({"email": email, "ok": True, **queued})
        except Exception as exc:  # noqa: BLE001
            results.append({"email": email, "ok": False, "error": str(exc)[:500]})
    success = sum(1 for item in results if item["ok"])
    return {"ok": True, "results": results, "success": success, "failed": len(results) - success}


@router.post("/delete-banned")
async def delete_banned_credentials() -> dict:
    # Plus/banned checks are intentionally a separate integration. Until that
    # integration is enabled, do not guess based on token contents.
    return {"ok": True, "deleted": 0}


@router.get("")
async def list_credentials(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    include_deleted: bool = False,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    rows, total = await repository.list(
        session, limit=limit, offset=offset, include_deleted=include_deleted
    )
    return {"ok": True, "items": [credential_dict(row) for row in rows], "total": total}


@router.get("/{email}")
async def get_credential(email: str, session: AsyncSession = Depends(get_db_session)) -> dict:
    row = await repository.get(session, email)
    if row is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    value = credential_dict(row)
    return {"ok": True, "item": value, "data": value}


@router.patch("/{email}")
async def update_credential(
    email: str,
    values: Dict[str, Any],
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    async with session.begin():
        if not await repository.update_fields(session, email, **values):
            raise HTTPException(status_code=404, detail="凭证不存在或没有可更新字段")
        row = await repository.get(session, email, include_deleted=True)
    return {"ok": True, "item": credential_dict(row)}


@router.delete("/{email}")
async def delete_credential(email: str, session: AsyncSession = Depends(get_db_session)) -> dict:
    async with session.begin():
        deleted = await repository.soft_delete(session, [email])
    if not deleted:
        raise HTTPException(status_code=404, detail="凭证不存在")
    return {"ok": True}
