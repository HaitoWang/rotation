from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db_session, get_redis
from app.api.schemas import RegisterRequest, RegisterResponse, RunResponse
from app.infrastructure.redis import RedisManager
from app.repositories.runs import RunRepository
from app.services.registration import RegistrationService

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])
runs = RunRepository()


@router.get("")
async def list_runs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    rows, total = await runs.list(session, status=status, limit=limit, offset=offset)
    return {"ok": True, "items": [RunResponse.from_model(row) for row in rows], "total": total}


@router.post("", response_model=RegisterResponse, status_code=202)
async def create_run(
    request: RegisterRequest,
    http_request: Request,
    redis: RedisManager = Depends(get_redis),
) -> dict:
    service = RegistrationService(http_request.app.state.database.session_factory, redis)
    try:
        return await service.enqueue(email=request.email, kind=request.kind, options=request.options)
    except LookupError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"任务入队失败: {exc}") from exc


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: str, session: AsyncSession = Depends(get_db_session)) -> RunResponse:
    run = await runs.get(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return RunResponse.from_model(run)


@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    redis: RedisManager = Depends(get_redis),
    session: AsyncSession = Depends(get_db_session),
):
    if await runs.get(session, run_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    last_id = request.headers.get("Last-Event-ID", "0-0")

    async def event_generator():
        async for message in redis.events(run_id, last_id=last_id):
            if await request.is_disconnected():
                break
            event = message.get("event", "message")
            data = json.dumps(message.get("data", {}), ensure_ascii=False)
            if event == "heartbeat":
                yield ": heartbeat\n\n"
            else:
                event_id = message.get("id")
                id_line = f"id: {event_id}\n" if event_id else ""
                yield f"{id_line}event: {event}\ndata: {data}\n\n"
                if event in {"done", "error"}:
                    break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
