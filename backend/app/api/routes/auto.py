from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_redis
from app.infrastructure.redis import RedisManager
from app.services.auto import AutoRunService

router = APIRouter(prefix="/api/v1/auto", tags=["auto"])


def _service(request: Request) -> AutoRunService:
    return AutoRunService(
        request.app.state.database.session_factory,
        request.app.state.redis,
        request.app.state.settings,
    )


@router.get("/status")
async def status(request: Request, session: AsyncSession = Depends(get_db_session)) -> dict:
    return await _service(request).status(session)


@router.post("/start")
async def start(payload: dict, request: Request, redis: RedisManager = Depends(get_redis)) -> dict:
    result = await _service(request).set_state("running", payload)
    await redis.enqueue("run_auto_tick", job_id=f"auto-tick-{int(__import__('time').time() * 1000)}")
    return {"ok": True, **result}


@router.post("/pause")
async def pause(request: Request) -> dict:
    return {"ok": True, **await _service(request).set_state("paused")}


@router.post("/resume")
async def resume(request: Request, redis: RedisManager = Depends(get_redis)) -> dict:
    result = await _service(request).set_state("running")
    await redis.enqueue("run_auto_tick", job_id=f"auto-tick-{int(__import__('time').time() * 1000)}")
    return {"ok": True, **result}


@router.post("/stop")
async def stop(request: Request) -> dict:
    return {"ok": True, **await _service(request).set_state("stopped")}


@router.get("/events")
async def events(request: Request, redis: RedisManager = Depends(get_redis)):
    async def generator():
        async for message in redis.events("auto"):
            if await request.is_disconnected():
                break
            if message.get("event") == "heartbeat":
                yield ": heartbeat\n\n"
                continue
            event = message.get("event", "message")
            payload = json.dumps(message.get("data", {}), ensure_ascii=False)
            yield f"event: {event}\ndata: {payload}\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")
