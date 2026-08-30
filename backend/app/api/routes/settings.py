from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session
from app.repositories.settings import SettingsRepository

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])
repository = SettingsRepository()


def _public(key: str, value: Any) -> Any:
    if any(token in key.lower() for token in ("key", "token", "password", "secret")) and value:
        return "***"
    return value


async def _get_prefix(session: AsyncSession, prefix: str) -> dict[str, Any]:
    values = await repository.all(session)
    if prefix == "mail_":
        selected = {
            key: value for key, value in values.items()
            if not key.startswith(("sms_", "cpa_", "sub2api_", "team_sso_"))
        }
    elif prefix == "export_":
        selected = {
            key: value for key, value in values.items()
            if key.startswith(("cpa_", "sub2api_", "team_sso_"))
        }
    else:
        selected = {key: value for key, value in values.items() if key.startswith(prefix)}
    return {key: _public(key, value) for key, value in selected.items()}


async def _set_prefix(session: AsyncSession, prefix: str, payload: Dict[str, Any]) -> dict[str, Any]:
    for key, value in payload.items():
        if value == "***":
            continue
        # Settings are stored under the names consumed by integrations. Mail
        # provider fields are already scoped (cf_api_url, gmail_*), while SMS
        # fields carry the sms_ prefix themselves.
        storage_key = key
        await repository.set(session, storage_key, value)
    return await _get_prefix(session, prefix)


@router.get("/mail/providers")
async def mail_providers(pooled_only: bool = False) -> dict:
    from app.integrations.mail.providers import list_pooled_providers, list_providers

    providers = list_pooled_providers() if pooled_only else list_providers()
    return {"ok": True, "providers": providers, "current": "outlook"}


@router.get("/mail")
async def get_mail_config(session: AsyncSession = Depends(get_db_session)) -> dict:
    return {"ok": True, "config": await _get_prefix(session, "mail_")}


@router.put("/mail")
async def set_mail_config(payload: Dict[str, Any], session: AsyncSession = Depends(get_db_session)) -> dict:
    async with session.begin():
        config = await _set_prefix(session, "mail_", payload)
    return {"ok": True, "config": config}


@router.get("/sms")
async def get_sms_config(session: AsyncSession = Depends(get_db_session)) -> dict:
    return {"ok": True, "config": await _get_prefix(session, "sms_")}


@router.put("/sms")
async def set_sms_config(payload: Dict[str, Any], session: AsyncSession = Depends(get_db_session)) -> dict:
    async with session.begin():
        config = await _set_prefix(session, "sms_", payload)
    return {"ok": True, "config": config}


@router.get("/export")
async def get_export_config(session: AsyncSession = Depends(get_db_session)) -> dict:
    return {"ok": True, "config": await _get_prefix(session, "export_")}


@router.put("/export")
async def set_export_config(payload: Dict[str, Any], session: AsyncSession = Depends(get_db_session)) -> dict:
    async with session.begin():
        config = await _set_prefix(session, "export_", payload)
    return {"ok": True, "config": config}


@router.get("/{key}")
async def get_setting(key: str, session: AsyncSession = Depends(get_db_session)) -> dict:
    return {"ok": True, "key": key, "value": _public(key, await repository.get_value(session, key))}


@router.put("/{key}")
async def set_setting(
    key: str,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    async with session.begin():
        value = await repository.set(session, key, payload)
    return {"ok": True, "key": key, "value": value}


@router.post("/export/sub2api/groups")
async def get_sub2api_groups(
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    from app.integrations.panels import exporter

    saved = await repository.all(session)
    config = {key: value for key, value in saved.items() if key.startswith("sub2api_")}
    for key, value in payload.items():
        if value and value != "***":
            config[key] = value
    try:
        return await asyncio.to_thread(exporter.get_sub2api_groups, config)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"获取 SUB2API 分组失败: {exc}") from exc


@router.post("/{scope}/test")
async def test_scope(
    scope: str,
    payload: Dict[str, Any] | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if scope not in {"mail", "sms", "export"}:
        raise HTTPException(status_code=404, detail="未知设置范围")
    if scope == "export" and (payload or {}).get("target") in {"cpa", "sub2api"}:
        from app.integrations.panels import exporter

        values = await repository.all(session)
        target = str(payload["target"])
        prefix = "cpa_" if target == "cpa" else "sub2api_"
        config = {key: value for key, value in values.items() if key.startswith(prefix)}
        try:
            result = await asyncio.to_thread(
                exporter.test_cpa if target == "cpa" else exporter.test_sub2api,
                config,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"{target} 测试失败: {exc}") from exc
    return {"ok": True, "scope": scope}
