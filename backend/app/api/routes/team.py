from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_redis
from app.api.schemas import TeamMotherInput, TeamMotherPatch
from app.infrastructure.redis import RedisManager
from app.repositories.settings import SettingsRepository
from app.repositories.team import TeamRepository
from app.services.team_state import TeamStateStore

router = APIRouter(prefix="/api/v1/team", tags=["team"])
repository = TeamRepository()
settings_repository = SettingsRepository()


def _rotation_interval(value: object, default: int = 300) -> int:
    try:
        return max(5, min(86400, int(float(value))))
    except (TypeError, ValueError):
        return default


@router.get("/mothers")
async def list_mothers(
    enabled_only: bool = False,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return {
        "ok": True,
        "items": await repository.list_mothers(session, enabled_only=enabled_only),
    }


@router.post("/mothers", status_code=status.HTTP_201_CREATED)
async def create_mother(
    request: TeamMotherInput,
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    if request.preferred_seat_type not in {"standard", "advanced"}:
        raise HTTPException(status_code=422, detail="preferred_seat_type 无效")
    from app.integrations.team.client import parse_mother_session

    try:
        material = parse_mother_session(request.session, request.workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with session.begin():
        try:
            item = await repository.create_mother(
                session,
                {
                    **material,
                    "name": request.name,
                    "enabled": request.enabled,
                    "join_mode": request.join_mode,
                    "preferred_seat_type": request.preferred_seat_type,
                },
            )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                raise HTTPException(status_code=409, detail="workspace_id 已存在") from exc
            raise
    return {"ok": True, "item": item}


@router.get("/mothers/{mother_id}")
async def get_mother(mother_id: str, session: AsyncSession = Depends(get_db_session)) -> dict:
    item = await repository.get_mother(session, mother_id)
    if item is None:
        raise HTTPException(status_code=404, detail="母号不存在")
    pools = item.get("seat_capacity") or {}
    if isinstance(pools, dict) and isinstance(pools.get("pools"), dict):
        pools = pools["pools"]
    standard = pools.get("standard") if isinstance(pools, dict) else {}
    advanced = pools.get("advanced") if isinstance(pools, dict) else {}
    return {
        "ok": True,
        "item": item,
        "seats": {
            "entitled": item.get("seats_entitled"),
            "in_use": item.get("seats_in_use"),
            "remaining_configured": item.get("seats_remaining"),
            "remaining_standard": standard.get("available") if isinstance(standard, dict) else None,
            "remaining_advanced": advanced.get("available") if isinstance(advanced, dict) else None,
            "pools": pools,
        },
        "members": await repository.list_members(session, mother_id=mother_id),
    }


@router.patch("/mothers/{mother_id}")
async def update_mother(
    mother_id: str,
    request: TeamMotherPatch,
    session: AsyncSession = Depends(get_db_session),
    redis: RedisManager = Depends(get_redis),
) -> dict:
    changes = request.model_dump(exclude_none=True)
    raw_session = changes.pop("session", None)
    if raw_session:
        from app.integrations.team.client import parse_mother_session

        existing = await repository.get_mother(session, mother_id, include_secret=True)
        if existing is None:
            raise HTTPException(status_code=404, detail="母号不存在")
        try:
            changes.update(
                parse_mother_session(raw_session, changes.get("workspace_id", existing["workspace_id"]))
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    async with session.begin():
        item = await repository.update_mother(session, mother_id, changes)
        if item is None:
            raise HTTPException(status_code=404, detail="母号不存在")
        if item.get("enabled"):
            await repository.schedule_mother(session, mother_id, when=time.time())
    await TeamStateStore(redis).invalidate(mother_id)
    return {"ok": True, "item": item}


@router.delete("/mothers/{mother_id}")
async def delete_mother(
    mother_id: str,
    session: AsyncSession = Depends(get_db_session),
    redis: RedisManager = Depends(get_redis),
) -> dict:
    async with session.begin():
        try:
            deleted = await repository.delete_mother(session, mother_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="母号不存在")
    await TeamStateStore(redis).invalidate(mother_id)
    return {"ok": True}


@router.get("/members")
async def list_members(
    mother_id: str = "",
    member_status: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=500, ge=1, le=5000),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return {
        "ok": True,
        "items": await repository.list_members(
            session, mother_id=mother_id, status=member_status or "", limit=limit
        ),
    }


@router.get("/status")
async def team_status(session: AsyncSession = Depends(get_db_session)) -> dict:
    runtime_state = await settings_repository.get_value(session, "team_rotation_state", "stopped")
    interval = await settings_repository.get_value(session, "team_rotation_interval_seconds", 300)
    proxy = await settings_repository.get_value(session, "team_rotation_proxy", "")
    config = {
        "interval_seconds": _rotation_interval(interval),
        "quota_threshold": float(await settings_repository.get_value(session, "team_rotation_quota_threshold", 100) or 100),
        "quota_concurrency": int(await settings_repository.get_value(session, "team_rotation_quota_concurrency", 8) or 8),
        "mother_concurrency": int(await settings_repository.get_value(session, "team_rotation_mother_concurrency", 10) or 10),
        "join_concurrency": int(await settings_repository.get_value(session, "team_rotation_join_concurrency", 4) or 4),
        "hub_concurrency": int(await settings_repository.get_value(session, "team_rotation_hub_concurrency", 8) or 8),
        "seat_cache_ttl": int(await settings_repository.get_value(session, "team_rotation_seat_cache_ttl", 300) or 300),
        "member_refresh_interval": int(await settings_repository.get_value(session, "team_rotation_member_refresh_interval", 900) or 900),
        "operation_lease_seconds": int(await settings_repository.get_value(session, "team_rotation_operation_lease_seconds", 240) or 240),
        "retry_max_seconds": int(await settings_repository.get_value(session, "team_rotation_retry_max_seconds", 1800) or 1800),
        "proxy": str(proxy or ""),
    }
    mothers = await repository.list_mothers(session)
    next_cycles = [item["next_rotation_at"] for item in mothers if item.get("next_rotation_at")]
    active_cycle = next((item for item in mothers if item.get("rotation_stage") == "running"), None)
    return {
        "ok": True,
        "state": str(runtime_state or "stopped"),
        "config": config,
        "counts": await repository.counts(session),
        "mothers": mothers,
        "members": await repository.list_members(session),
        "events": await repository.list_events(session),
        "next_cycle_at": min(next_cycles) if next_cycles else None,
        "current_mother": active_cycle.get("name") if active_cycle else "",
        "last_error": next((item.get("last_error") for item in mothers if item.get("last_error")), ""),
    }


@router.get("/events")
async def list_events(
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return {"ok": True, "items": await repository.list_events(session, limit=limit)}


@router.post("/check", status_code=status.HTTP_202_ACCEPTED)
async def trigger_team_check(
    redis: RedisManager = Depends(get_redis),
    mother_id: str = "",
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    mother_id = str(mother_id or "").strip()
    if mother_id:
        async with session.begin():
            if not await repository.schedule_mother(session, mother_id, when=time.time()):
                raise HTTPException(status_code=404, detail="母号不存在")
    return await _enqueue_rotation_check(redis, mother_id)


async def _enqueue_rotation_check(redis: RedisManager, mother_id: str = "") -> dict:
    job_id = "team-rotation-dispatch-manual-{}".format(int(time.time() * 1000))
    await redis.enqueue("run_team_rotation_dispatch", job_id=job_id, force=True, mother_id=mother_id)
    return {"ok": True, "job_id": job_id, "mother_id": mother_id or None}


@router.post("/rotation/start")
async def start_rotation(payload: dict, session: AsyncSession = Depends(get_db_session), redis: RedisManager = Depends(get_redis)) -> dict:
    allowed = {
        "interval_seconds", "quota_threshold", "quota_concurrency", "mother_concurrency", "join_concurrency", "hub_concurrency",
        "seat_cache_ttl", "member_refresh_interval", "operation_lease_seconds", "retry_max_seconds", "proxy",
    }
    now = time.time()
    async with session.begin():
        await settings_repository.set(session, "team_rotation_state", "running")
        for key in allowed:
            if key in payload:
                value = _rotation_interval(payload[key]) if key == "interval_seconds" else payload[key]
                await settings_repository.set(session, f"team_rotation_{key}", value)
        scheduled = await repository.schedule_mothers(session, when=now)
        if not scheduled:
            raise HTTPException(status_code=409, detail="请先添加并启用至少一个母号")
    return await _enqueue_rotation_check(redis)


async def _set_rotation_state(state: str, session: AsyncSession) -> dict:
    async with session.begin():
        await settings_repository.set(session, "team_rotation_state", state)
    return {"ok": True, "state": state}


@router.post("/rotation/pause")
async def pause_rotation(session: AsyncSession = Depends(get_db_session), redis: RedisManager = Depends(get_redis)) -> dict:
    result = await _set_rotation_state("paused", session)
    return result


@router.post("/rotation/resume")
async def resume_rotation(session: AsyncSession = Depends(get_db_session), redis: RedisManager = Depends(get_redis)) -> dict:
    async with session.begin():
        await settings_repository.set(session, "team_rotation_state", "running")
        await repository.schedule_mothers(session, when=time.time())
    result = {"ok": True, "state": "running"}
    await _enqueue_rotation_check(redis)
    return result


@router.post("/rotation/stop")
async def stop_rotation(session: AsyncSession = Depends(get_db_session), redis: RedisManager = Depends(get_redis)) -> dict:
    result = await _set_rotation_state("stopped", session)
    return result


@router.post("/rotation/check-now")
async def check_rotation_now(redis: RedisManager = Depends(get_redis)) -> dict:
    return await _enqueue_rotation_check(redis)


@router.delete("/members/{mother_id}/{member_id}")
async def remove_member(
    mother_id: str,
    member_id: str,
    session: AsyncSession = Depends(get_db_session),
    redis: RedisManager = Depends(get_redis),
) -> dict:
    mother = await repository.get_mother(session, mother_id, include_secret=True)
    if mother is None:
        raise HTTPException(status_code=404, detail="母号不存在")
    await session.commit()
    from app.integrations.team.client import TeamApiClient, TeamApiError

    client = TeamApiClient()
    try:
        result = await asyncio.to_thread(client.remove_member, mother, member_id)
    except TeamApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        client.close()
    async with session.begin():
        await repository.mark_member_removed(session, mother_id, member_id)
        await repository.schedule_mother(session, mother_id, when=time.time())
        await repository.record_event(
            session, level="INFO", action="remove", message="管理员手动移出成员", mother_id=mother_id
        )
    await TeamStateStore(redis).invalidate(mother_id)
    await _enqueue_rotation_check(redis)
    return {"ok": True, **result}
