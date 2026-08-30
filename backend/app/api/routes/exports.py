from __future__ import annotations

import asyncio
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.repositories.credentials import CredentialRepository, credential_dict
from app.repositories.settings import SettingsRepository

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])
credentials = CredentialRepository()


@router.post("/panel")
async def export_panel(
    payload: dict[str, Any],
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    email = str(payload.get("email") or "").strip().lower()
    row = await credentials.get(session, email)
    if row is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    values = await SettingsRepository().all(session)
    await session.commit()
    targets = {str(item).strip().lower() for item in payload.get("targets") or []}
    from app.integrations.panels import exporter

    result = {"email": email, "cpa": None, "sub2api": None}
    if targets & {"cpa", "sub2api"}:
        cpa = {key: value for key, value in values.items() if key.startswith("cpa_")}
        sub2api = {key: value for key, value in values.items() if key.startswith("sub2api_")}
        if "cpa" in targets:
            cpa["enabled"] = True
        if "sub2api" in targets:
            sub2api["enabled"] = True
        output = await asyncio.to_thread(
            exporter.run_exports,
            credential_dict(row),
            cpa_cfg=cpa if "cpa" in targets else None,
            sub2api_cfg=sub2api if "sub2api" in targets else None,
        )
        result.update(output)
    return {"ok": True, **result}


class ExportRequest(BaseModel):
    format: str = Field(min_length=1, max_length=64)
    emails: Optional[List[str]] = None
    all: bool = False
    limit: int = Field(default=0, ge=0, le=100000)
    soft_delete: bool = False


@router.get("/formats")
async def list_formats() -> dict:
    from app.services import export_formats

    return {"ok": True, "formats": export_formats.list_formats()}


@router.post("")
async def export_credentials(
    request: ExportRequest,
    session: AsyncSession = Depends(get_db_session),
):
    from app.services import export_formats

    fmt = export_formats.get_format(request.format)
    if fmt is None or not fmt.render:
        raise HTTPException(status_code=400, detail="未知或不支持的导出格式")
    if request.all:
        rows, _total = await credentials.list(
            session, limit=request.limit or 100000, offset=0, include_deleted=False
        )
    elif request.emails:
        rows = await credentials.list_by_emails(session, request.emails)
    else:
        raise HTTPException(status_code=400, detail="需要 emails 或 all=true")
    rows = rows[: request.limit] if request.limit else rows
    if not rows:
        raise HTTPException(status_code=400, detail="当前筛选条件下没有可导出的数据")
    rendered = export_formats.render_text([credential_dict(row) for row in rows], fmt)
    if request.soft_delete:
        await credentials.soft_delete(session, [row.email for row in rows])
        await session.commit()
    return JSONResponse(
        {
            "ok": True,
            "mode": "text",
            "text": rendered,
            "count": len(rows),
            "filename": fmt.filename,
            "label": fmt.label,
            "mime": fmt.mime,
            "soft_deleted": bool(request.soft_delete),
        },
        headers={"Cache-Control": "no-store"},
    )
