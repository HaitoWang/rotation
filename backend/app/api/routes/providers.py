from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.repositories.settings import SettingsRepository

router = APIRouter(prefix="/api/v1", tags=["integrations"])


class ProxyTestRequest(BaseModel):
    proxies: list[str] = Field(min_length=1, max_length=1000)
    timeout: int = Field(default=8, ge=1, le=60)


@router.post("/proxy/test")
async def test_proxies(request: ProxyTestRequest) -> dict[str, Any]:
    from app.integrations.openai.http_client import create_http_session

    values = [item.strip() for item in request.proxies if item.strip()]

    def check(proxy: str) -> tuple[str, dict[str, Any]]:
        import time

        started = time.perf_counter()
        try:
            session = create_http_session(proxy=proxy)
            response = session.get("https://api.ipify.org?format=json", timeout=request.timeout)
            latency = int((time.perf_counter() - started) * 1000)
            payload = response.json() if response.status_code == 200 else {}
            return proxy, {"ok": response.status_code == 200, "latency_ms": latency, "ip": payload.get("ip", "")}
        except Exception as exc:  # noqa: BLE001
            return proxy, {"ok": False, "latency_ms": int((time.perf_counter() - started) * 1000), "error": str(exc)[:240]}

    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=min(20, len(values))) as executor:
        pairs = await loop.run_in_executor(executor, lambda: list(executor.map(check, values)))
    return {"ok": True, "results": dict(pairs)}


@router.get("/settings/sms/all-countries")
async def sms_countries(provider: str = "") -> dict:
    from app.integrations.sms.provider import SMS_COUNTRY_NAMES_CN

    return {
        "ok": True,
        "countries": [
            {"id": key, "name_cn": value, "openai_sms_safe": key == "52"}
            for key, value in SMS_COUNTRY_NAMES_CN.items()
        ],
    }


@router.get("/settings/sms/countries")
async def sms_top_countries() -> dict:
    return await sms_countries()


@router.post("/settings/{scope}/test")
async def test_setting(scope: str, session: AsyncSession = Depends(get_db_session)) -> dict:
    if scope not in {"mail", "sms", "export"}:
        raise HTTPException(status_code=404, detail="未知设置范围")
    settings = await SettingsRepository().all(session)
    return {"ok": True, "scope": scope, "configured": bool(settings)}
