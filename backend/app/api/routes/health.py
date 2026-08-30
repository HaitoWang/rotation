from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    database = request.app.state.database
    redis = request.app.state.redis
    checks: dict[str, dict] = {}
    for name, check in (("postgres", database.ping), ("redis", redis.ping)):
        try:
            checks[name] = {"ok": True, "latency_ms": await check()}
        except Exception as exc:  # noqa: BLE001
            checks[name] = {"ok": False, "error": str(exc)[:240]}
    ok = all(item["ok"] for item in checks.values())
    return JSONResponse(status_code=200 if ok else 503, content={"ok": ok, "checks": checks})
