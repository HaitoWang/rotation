"""FastAPI 主程序：路由 + SSE 流式日志。

启动:
    python -m webui.app
或者:
    python start_webui.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, export_formats, registrar, team_sso_sync  # noqa: E402
from .auto_loop import CONTROLLER as AUTO_LOOP  # noqa: E402
from .team_rotation import (  # noqa: E402
    CONTROLLER as TEAM_ROTATION,
    TeamApiError,
    parse_mother_session,
)
from ..mail_providers import (
    ImportValidationError,
    MailProviderError,
    create_mail_provider,
    get_provider_class,
    list_pooled_providers,
    list_providers,
)

# 启动时回收上个进程遗留的任务。此时新 worker 尚未启动，所有 in_use 都属于旧进程。
try:
    _recovered_runs = db.recover_interrupted_runs()
    _released = db.release_stale_in_use(stale_seconds=0)
    if _recovered_runs > 0:
        logging.getLogger("webui").info(f"[startup] 回收 {_recovered_runs} 个中断 run")
    if _released > 0:
        logging.getLogger("webui").info(f"[startup] 释放 {_released} 个卡死的 in_use 号")
except Exception as _e:
    logging.getLogger("webui").warning(f"[startup] release_stale 失败: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webui")
team_sso_sync.start_dispatcher()

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="GPT Outlook Register WebUI", docs_url=None, redoc_url=None)


# ──────────────────────── Pydantic 模型 ────────────────────────


class ImportReq(BaseModel):
    text: str = Field(..., description="每行一个号，格式由 kind 决定")
    kind: str = Field(
        "",
        description="邮箱来源（outlook / ...）。留空则按段数猜，"
                    "存在同段数来源时无法自动判断，建议前端必填",
    )


class RegisterReq(BaseModel):
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""
    otp_timeout: int = 10
    allow_existing_login: bool = True
    # 注册成功后自动绑定 TOTP 2FA。前端两个页面都**默认开**（主人要求每个号都绑）。
    # 这里的 default 保持 False —— 它只在「调用方没传这个字段」时生效，是给旧前端
    # 缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不替调用方做一个不可逆的决定。
    # 真实默认值由前端 form store 的 want2fa / autoWant2fa 决定。
    want_2fa: bool = False


# ──────────────────────── API ────────────────────────


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/import")
def api_import(req: ImportReq):
    """批量导入号池。**有一行不合法就整批拒绝**，一个都不写库。

    非法时返回 422，body 里带每一行的行号和原因，前端直接展示即可：

        {"ok": false, "message": "...", "errors": [{"line": 3, "error": "..."}]}
    """
    try:
        result = db.import_accounts(req.text, kind=req.kind)
    except ImportValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "message": str(e), "errors": e.errors},
        )
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result, "stats": db.stats()}


@app.get("/api/accounts")
def api_accounts(status: str = "", limit: int = 50, offset: int = 0, kind: str = ""):
    items = db.list_accounts(status=status, limit=limit, offset=offset, kind=kind)
    total = db.count_accounts(status=status, kind=kind)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "by_kind": db.stats_by_kind(),
    }


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="按 email 列表删")


@app.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """按状态或 email 列表批量删除号池。两个参数二选一（status 优先）。"""
    if req.status:
        n = db.delete_accounts_by_status(req.status)
        return {"ok": True, "deleted": n, "by": "status", "stats": db.stats()}
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "需要 status 或 emails")


@app.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"邮箱 {email} 不存在")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """批量重置：done / failed → available。"""
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@app.get("/api/stats")
def api_stats():
    return {"ok": True, "stats": db.stats()}


# ──────────────────────── 代理连通性测试 ────────────────────────


class ProxyTestReq(BaseModel):
    proxies: list[str] = Field(..., description="要测试的代理列表")
    timeout: int = Field(8, description="每个代理超时秒数")
    test_url: str = Field("https://api.ipify.org?format=json",
                          description="测试目标 URL（默认返回出口 IP）")


@app.post("/api/proxy/test")
def api_proxy_test(req: ProxyTestReq):
    """并发测试代理连通性。复用真实注册流程的 create_http_session（含 socks5->socks5h
    标准化、trust_env=False），保证「测试正常」== 「跑号能用」。返回 ok / 延迟 / 出口 IP。

    协议说明：不写协议的 `ip:port` 被 curl 按 HTTP 代理处理；SOCKS5 需显式写 socks5://。
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from ..http_client import create_http_session
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"加载 http_client 失败: {e}")

    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    timeout = max(1, min(int(req.timeout or 8), 60))
    test_url = (req.test_url or "https://api.ipify.org?format=json").strip()

    proxies = [p.strip() for p in (req.proxies or []) if p and p.strip()]
    if not proxies:
        raise HTTPException(400, "proxies 不能为空")

    def _test_one(proxy: str):
        t0 = _t.perf_counter()
        try:
            sess = create_http_session(proxy=proxy)
            resp = sess.get(test_url, timeout=timeout)
            latency = int((_t.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
            ip = ""
            try:
                ip = resp.json().get("ip", "")
            except Exception:
                ip = (resp.text or "").strip()[:64]
            return {"ok": True, "latency_ms": latency, "ip": ip}
        except Exception as e:  # noqa: BLE001
            latency = int((_t.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency, "error": str(e)[:140]}

    results = {}
    with ThreadPoolExecutor(max_workers=min(20, len(proxies))) as ex:
        for proxy, res in zip(proxies, ex.map(_test_one, proxies)):
            results[proxy] = res
    return {"ok": True, "results": results}


@app.post("/api/register")
def api_register(req: RegisterReq):
    """启动注册任务，返回 run_id。前端拿 run_id 去 /api/runs/{run_id}/stream 订阅 SSE。"""
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 要不要 claim 号池，由 provider 自己声明的 pooled 决定 ——
    # 原来写死 `mail_source == "cf_temp"`，加一种非池化邮箱就得改这里。
    if not provider_cls.pooled:
        # 非池化：地址由 provider 现造，用占位 account 走完后面的流程
        import time as _t
        account = {
            "email": f"{mail_source}_placeholder_{int(_t.time())}@placeholder.local",
            "password": "",
            "client_id": "",
            "refresh_token": "",
            "relay_url": "",
            "kind": mail_source,
        }
    elif req.email:
        account = db.claim_account(req.email)
        if not account:
            raise HTTPException(400, f"邮箱 {req.email} 不可用 (不存在 / 已 in_use / 已完成)")
        if (account.get("kind") or "outlook") != mail_source:
            # 号池里混放多种邮箱，点名的号必须和当前来源一致，
            # 否则会拿 Outlook 的凭证去初始化 Gmail provider
            db.release_unused(account["email"])
            raise HTTPException(
                400,
                f"{req.email} 是 {account.get('kind')} 的号，"
                f"当前邮箱来源是 {mail_source}，请先切换来源",
            )
    else:
        account = db.claim_next(kind=mail_source)
        if not account:
            raise HTTPException(
                400,
                f"号池里没有 available 的 {provider_cls.display_name} 账号；请先批量导入",
            )

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "proxy": req.proxy,
        "otp_timeout": int(req.otp_timeout),
        "allow_existing_login": req.allow_existing_login,
        "want_2fa": req.want_2fa,
    }
    run_id = registrar.start_registration(account, options)
    logger.info(f"[run] {run_id} -> {account['email']} (mail_source={mail_source})")
    return {"ok": True, "run_id": run_id, "email": account["email"]}


@app.get("/api/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request):
    """SSE 实时推送日志 + 事件。"""
    q = registrar.get_run_queue(run_id)
    if q is None:
        raise HTTPException(404, "run_id not found or finished")

    async def event_gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 从队列取消息（用 run_in_executor 避免阻塞 event loop）
                msg = await loop.run_in_executor(None, _safe_get, q)
                if msg is None:
                    # sentinel: 任务结束
                    yield "event: end\ndata: {}\n\n"
                    break
                if msg.startswith("__EVENT__:"):
                    yield f"event: status\ndata: {msg[len('__EVENT__:'):]}\n\n"
                else:
                    yield f"event: log\ndata: {json.dumps({'line': msg}, ensure_ascii=False)}\n\n"
        finally:
            registrar.remove_run_queue(run_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 避免 nginx 缓冲
            "Connection": "keep-alive",
        },
    )


def _safe_get(q):
    try:
        return q.get(timeout=60)
    except Exception:
        return ""  # 心跳：返空串让 SSE 检查 disconnect


@app.get("/api/runs")
def api_runs(limit: int = 50):
    return {"ok": True, "items": db.list_runs(limit=limit)}


@app.get("/api/registered")
def api_registered(limit: int = 20, offset: int = 0, filter: str = "all"):
    items = db.list_registered(limit=limit, offset=offset, filter_rt=filter)
    total = db.count_registered(filter_rt=filter)
    return {"ok": True, "items": items, "total": total}


@app.get("/api/registered/{email}")
def api_registered_one(email: str):
    row = db.get_registered(email)
    if not row:
        raise HTTPException(404, "not found")
    return {"ok": True, "data": row}


@app.delete("/api/registered/{email}")
def api_delete_registered(email: str):
    ok = db.delete_registered(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteRegisteredReq(BaseModel):
    emails: Optional[list[str]] = Field(None, description="按 email 列表删；留空 + all=true 则删全部")
    all: bool = False


@app.post("/api/registered/bulk_delete")
def api_bulk_delete_registered(req: BulkDeleteRegisteredReq):
    if req.all:
        n = db.delete_all_registered()
        return {"ok": True, "deleted": n, "by": "all"}
    if req.emails:
        n = db.delete_registered_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails"}
    raise HTTPException(400, "需要 emails 或 all=true")


# ──────────────────────── 批量导出（文本） ────────────────────────
# ⚠️ 路由顺序：
#   - formats 是 4 段路径，不会被 3 段的 GET /api/registered/{email} 吃掉；
#   - export 是 POST，而 {email} 那两条是 GET / DELETE，也不冲突。
# 要加新格式只改 webui/export_formats.py，这里和前端都不用动。


@app.get("/api/registered/export/formats")
def api_export_formats():
    """导出格式清单，前端下拉菜单据此渲染。"""
    return {"ok": True, "formats": export_formats.list_formats()}


class ExportRegisteredReq(BaseModel):
    format: str = Field(..., description="格式 id，见 GET /api/registered/export/formats")
    emails: Optional[list[str]] = Field(None, description="要导出的 email 列表")
    all: bool = Field(False, description="true = 导出全部当前筛选结果，忽略 emails")
    filter: str = Field("all", description="当前注册结果筛选条件")
    limit: int = Field(0, ge=0, le=100000, description="导出数量，0 表示当前筛选结果全部")
    soft_delete: bool = Field(False, description="导出成功后软删除本次导出的记录")


@app.post("/api/registered/export")
def api_export_registered(req: ExportRegisteredReq):
    fmt = export_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"未知导出格式: {req.format}")

    if req.all:
        rows = db.list_registered_full(limit=req.limit or 100000, filter_rt=req.filter)
    elif req.emails:
        rows = db.list_registered_by_emails(req.emails)
    else:
        raise HTTPException(400, "需要 emails 或 all=true")

    if req.limit:
        rows = rows[:req.limit]
    if not rows:
        raise HTTPException(400, "当前筛选条件下没有可导出的数据")

    # 不跳行：勾了几个号就几行 / 几个文件，字段为空也照样出。
    # 手动导出**不做 refresh_token 刷新、不因为缺 rt 拦截**，这是和自动推送的区别。
    base = {
        "ok": True,
        "count": len(rows),
        "filename": fmt.filename,
        "label": fmt.label,
        "mode": fmt.mode,
        "mime": fmt.mime,
    }

    if fmt.mode == "download":
        # 二进制（zip / json 文件）走 base64，前端解出来直接存盘，不弹预览
        blob = export_formats.render_bytes(rows, fmt)
        if req.soft_delete:
            db.soft_delete_registered_by_emails([row.get("email", "") for row in rows])
        return {**base, "b64": base64.b64encode(blob).decode("ascii"), "size": len(blob), "soft_deleted": bool(req.soft_delete)}

    text = export_formats.render_text(rows, fmt)
    if req.soft_delete:
        db.soft_delete_registered_by_emails([row.get("email", "") for row in rows])
    return {**base, "text": text, "soft_deleted": bool(req.soft_delete)}


@app.post("/api/registered/export/download")
def api_export_registered_download(req: ExportRegisteredReq):
    """Stream a text export directly from SQLite to the caller.

    The legacy endpoint above intentionally keeps its JSON response contract.
    The download endpoint avoids materializing rows, rendered text, or base64
    in memory, which is the path used by the Registered page for large exports.
    """
    fmt = export_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"未知导出格式: {req.format}")
    if fmt.mode != "text" or not fmt.render:
        raise HTTPException(400, "该格式暂不支持流式下载")
    if not req.all and not req.emails:
        raise HTTPException(400, "需要 emails 或 all=true")

    if req.all:
        count = db.count_registered(filter_rt=req.filter)
    else:
        count = db.count_registered_by_emails(req.emails or [])
    # The stream and the legacy endpoint both cap exports at 100k rows.
    # Keep the advertised count consistent when limit=0 means "all".
    count = min(count, req.limit or 100000)
    if count <= 0:
        raise HTTPException(400, "当前筛选条件下没有可导出的数据")

    def body():
        first = True
        exported_emails = []
        chunks = []
        chunk_size = 0
        for row in db.iter_registered_export_rows(
            fmt.id,
            limit=req.limit or 100000,
            filter_rt=req.filter,
            emails=None if req.all else req.emails,
        ):
            try:
                line = fmt.render(row)
            except Exception:
                line = ""
            exported_emails.append(row.get("email", ""))
            if first:
                first = False
                piece = line
            else:
                piece = "\n" + line
            chunks.append(piece)
            chunk_size += len(piece)
            if chunk_size >= 64 * 1024:
                yield "".join(chunks)
                chunks.clear()
                chunk_size = 0
        if chunks:
            yield "".join(chunks)
        if req.soft_delete and exported_emails:
            db.soft_delete_registered_by_emails(exported_emails)

    filename = quote(fmt.filename)
    return StreamingResponse(
        body(),
        media_type=fmt.mime,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
            "X-Export-Count": str(count),
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


# ──────────────────────── 邮箱来源配置 ────────────────────────


@app.get("/api/mail/providers")
def api_mail_providers(pooled_only: bool = False):
    """列出所有已注册的邮箱 provider 及其能力 / 配置项声明。

    前端据此渲染「邮箱来源」单选和对应的动态表单 ——
    以后加邮箱，前端一行都不用改。

        pooled_only=true  只返回能导入号池的（导入页用）
    """
    return {
        "ok": True,
        "providers": list_pooled_providers() if pooled_only else list_providers(),
        "current": db.get_setting("mail_source", "outlook"),
    }


@app.get("/api/settings/mail")
def api_get_mail_config():
    return {"ok": True, "config": db.get_mail_config()}


class SaveMailConfigReq(BaseModel):
    """字段不再写死。

    mail_source 之外的配置项由各 provider 的 config_fields 声明，
    前端原样回传，db.save_mail_config 按声明逐项存 ——
    加 provider 时这个模型不用动。
    """

    model_config = {"extra": "allow"}

    mail_source: Optional[str] = None


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    try:
        db.save_mail_config(req.model_dump(exclude_none=True))
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
def api_test_mail():
    """测试当前邮箱来源的连通性，具体怎么测由 provider 的 self_test() 决定。

    原来这里写死了 CF 的 api_url/domain/token 三个字段，
    换成让 provider 自检 —— 加邮箱不用回来改这个路由。
    """
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # 池化 provider 的连通性绑定在具体某个号上，没号可测 ——
    # 它的"测试"就是导入时的格式校验 + 跑一次注册。
    if provider_cls.pooled:
        raise HTTPException(
            400,
            f"{provider_cls.display_name} 是号池类型，不需要单独测试；"
            f"导入时会校验格式",
        )

    try:
        provider = create_mail_provider(mail_source, db.get_mail_settings())
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"构造 {provider_cls.display_name} 失败: {e}")

    try:
        result = provider.self_test()
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")
    if not result.get("ok"):
        raise HTTPException(500, result.get("message") or "连接失败")
    return {"ok": True, "message": result.get("message", "连接成功")}


# ──────────────────────── SMS 接码配置 ────────────────────────


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_provider: Optional[str] = None             # smsbower / herosms
    sms_api_key: Optional[str] = None              # 传 '***' 表示不修改
    sms_mode: Optional[str] = None                 # single / race / split（串行 1:1 分流）
    sms_supplier_strategy: Optional[str] = None    # balanced / success_first
    sms_smsbower_enabled: Optional[str] = None
    sms_smsbower_api_key: Optional[str] = None
    sms_smsbower_api_url: Optional[str] = None
    sms_herosms_enabled: Optional[str] = None
    sms_herosms_api_key: Optional[str] = None
    sms_herosms_api_url: Optional[str] = None
    sms_country: Optional[str] = None              # ID 或国家代码（'52' / 'th'）
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None    # 逗号分隔的 ID 列表，自动选号时只从这里挑
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_per_phone_timeout: Optional[str] = None    # 单号等待秒数（默认 80）
    sms_max_phone_attempts: Optional[str] = None   # 单个 run 最多尝试的手机号数（默认 3）


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    data = req.model_dump(exclude_none=True)
    raw_attempts = str(data.get("sms_max_phone_attempts") or "").strip()
    if raw_attempts:
        try:
            attempts = int(raw_attempts)
        except ValueError as exc:
            raise HTTPException(400, "最多手机号尝试次数必须是正整数") from exc
        if attempts < 1:
            raise HTTPException(400, "最多手机号尝试次数必须大于等于 1")
        data["sms_max_phone_attempts"] = str(attempts)
    db.save_sms_config(data)
    return {"ok": True, "config": db.get_sms_config()}


@app.post("/api/settings/sms/test")
def api_test_sms():
    """测试所有启用的 SMS provider 连通性并返回独立余额。"""
    cfg = db.get_sms_internal_config()

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from ..sms_provider import get_enabled_sms_providers
    providers = get_enabled_sms_providers(cfg)
    if not providers:
        raise HTTPException(400, "未启用或未配置接码平台 API Key")
    results = []
    for provider in providers:
        try:
            results.append({
                "platform": provider.platform_key,
                "ok": True,
                "balance": provider.get_balance(),
            })
        except Exception as e:
            results.append({"platform": provider.platform_key, "ok": False, "error": str(e)})
    ok_count = sum(1 for item in results if item["ok"])
    if not ok_count:
        raise HTTPException(502, detail={"message": "所有接码平台连接失败", "platforms": results})
    return {
        "ok": True,
        "platforms": results,
        "message": f"{ok_count}/{len(results)} 个接码平台连接正常",
    }


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """查询当前接码平台的国家排名（价格 + 库存）。"""
    cfg = db.get_sms_internal_config()

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from ..sms_provider import get_enabled_sms_providers, OPENAI_SMS_COUNTRIES, SMS_COUNTRY_NAMES_CN
    try:
        providers = get_enabled_sms_providers(cfg)
        if not providers:
            raise RuntimeError("未启用或未配置接码平台 API Key")
        provider = providers[0]
        rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        for r in rows:
            cid = str(r.get("country"))
            r["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            r["name_cn"] = SMS_COUNTRY_NAMES_CN.get(cid, "未知")
        return {"ok": True, "countries": rows[:30], "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)}
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


@app.get("/api/settings/sms/all_countries")
def api_sms_all_countries(provider: str = ""):
    """返回当前平台实际有库存的国家（动态查询）；查询失败则 fallback 到静态字典。"""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from ..sms_provider import (
        SMS_COUNTRY_NAMES_CN,
        OPENAI_SMS_COUNTRIES,
        create_sms_provider,
        get_enabled_sms_providers,
    )

    cfg = db.get_sms_internal_config()
    provider = str(provider or "").strip().lower()

    # 尝试从平台 API 动态获取有库存的国家
    providers = []
    try:
        if provider in {"smsbower", "herosms"}:
            providers = [create_sms_provider(provider, cfg)]
        else:
            providers = get_enabled_sms_providers(cfg)
    except Exception:
        providers = []
    if providers:
        try:
            merged = {}
            for p in providers:
                for row in p.get_top_countries(service=cfg.get("sms_service") or "dr"):
                    cid = str(row.get("country") or "")
                    item = merged.setdefault(cid, {
                        "id": cid,
                        "name_cn": SMS_COUNTRY_NAMES_CN.get(cid, f"国家{cid}"),
                        "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                        "price": None,
                        "count": 0,
                        "score": None,
                        "platforms": [],
                    })
                    price = row.get("price")
                    score = row.get("score")
                    if price is not None and (item["price"] is None or price < item["price"]):
                        item["price"] = price
                    if score is not None and (item["score"] is None or score < item["score"]):
                        item["score"] = score
                    item["count"] += int(row.get("count") or 0)
                    item["platforms"].append(p.platform_key)
            countries = sorted(
                merged.values(),
                key=lambda item: (item["score"] if item["score"] is not None else 999999, -item["count"]),
            )
            if countries:
                return {"ok": True, "countries": countries,
                        "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "live"}
        except Exception:
            pass

    # fallback: 静态字典
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {"id": cid, "name_cn": name, "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES}
        for cid, name in items
    ]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "static"}


# ──────────────────────── 自动导出 (CPA / SUB2API) ────────────────────────


class SaveExportConfigReq(BaseModel):
    # CPA
    cpa_enabled: Optional[str] = None       # "0" / "1"
    cpa_url: Optional[str] = None
    cpa_mgmt_key: Optional[str] = None      # 传 '***' 表示不修改
    cpa_timeout: Optional[str] = None
    # SUB2API
    sub2api_enabled: Optional[str] = None
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None   # '***' 不修改
    sub2api_group_ids: Optional[str] = None  # 逗号分隔，例 "2" 或 "1,2,3"
    sub2api_default_model: Optional[str] = None
    sub2api_models: Optional[list[str] | str] = None
    sub2api_concurrency: Optional[str] = None
    sub2api_timeout: Optional[str] = None
    # team-sso free 账号池
    team_sso_enabled: Optional[str] = None
    team_sso_url: Optional[str] = None
    team_sso_sync_key: Optional[str] = None  # '***' 不修改
    team_sso_timeout: Optional[str] = None


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class GetSub2ApiGroupsReq(BaseModel):
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None  # 空值或 '***' 使用已保存密钥
    sub2api_timeout: Optional[str] = None


@app.post("/api/settings/export/sub2api/groups")
def api_get_sub2api_groups(req: GetSub2ApiGroupsReq):
    """用当前表单或已保存配置查询可用 OpenAI 分组。"""
    from . import exporter

    cfg = dict(db.get_export_internal_config()["sub2api"])
    saved_url = str(cfg.get("sub2api_url") or "").rstrip("/").strip()
    if req.sub2api_url is not None:
        cfg["sub2api_url"] = req.sub2api_url.strip()
    api_key = str(req.sub2api_api_key or "").strip()
    if api_key and api_key != "***":
        cfg["sub2api_api_key"] = api_key
    elif str(cfg.get("sub2api_url") or "").rstrip("/").strip() != saved_url:
        cfg["sub2api_api_key"] = ""
    if req.sub2api_timeout is not None:
        cfg["sub2api_timeout"] = req.sub2api_timeout
    try:
        return exporter.get_sub2api_groups(cfg)
    except Exception as e:
        raise HTTPException(500, f"获取 SUB2API 分组失败: {e}")


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa / sub2api / team_sso")


@app.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """测试 CPA / SUB2API 连通性。"""
    from . import exporter
    cfg = db.get_export_internal_config()
    target = (req.target or "").strip().lower()
    try:
        if target == "cpa":
            return exporter.test_cpa(cfg["cpa"])
        if target == "sub2api":
            return exporter.test_sub2api(cfg["sub2api"])
        if target == "team_sso":
            return team_sso_sync.test_connection(cfg["team_sso"])
        raise HTTPException(400, f"未知 target: {target}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"测试失败: {e}")


class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    targets: list[str] = Field(default_factory=lambda: ["cpa", "sub2api"],
                                description="选择导出目标：cpa / sub2api")


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    cfg = db.get_export_internal_config()
    out = {"email": req.email, "cpa": None, "sub2api": None}
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    if "cpa" in targets:
        cpa_cfg = dict(cfg["cpa"])
        cpa_cfg["enabled"] = True  # 手动触发：强制启用
        try:
            out["cpa"] = exporter.export_to_cpa(cred, cpa_cfg)
        except Exception as e:
            out["cpa"] = {"ok": False, "error": str(e)}
    if "sub2api" in targets:
        sub2api_cfg = dict(cfg["sub2api"])
        sub2api_cfg["enabled"] = True
        try:
            out["sub2api"] = exporter.export_to_sub2api(cred, sub2api_cfg)
        except Exception as e:
            out["sub2api"] = {"ok": False, "error": str(e)}

    return {"ok": True, **out}


class UpdateCredReq(BaseModel):
    email: str = Field(..., description="要修改的已注册账号邮箱")
    # None = 该字段不动；空串 = 主动清空。前端不填的字段就别传。
    password: Optional[str] = Field(None, description="新密码，None=不修改")
    totp_secret: Optional[str] = Field(None, description="新 TOTP secret，None=不修改")


@app.post("/api/registered/update_credentials")
def api_update_credentials(req: UpdateCredReq):
    """手动修正已注册账号的密码 / TOTP secret。

    ⚠️ 只改本地库，不会同步到 OpenAI。用途是把外部已知凭证补进来或修正记录。

    改完的值会被登录流程直接用上（registrar 的 account_callback 走
    db.get_registered，不区分数据来源），所以 totp_secret 必须过 base32
    校验 —— 脏值存进去要等真登录时才炸，那时根本看不出是手填填错的。
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email 不能为空")
    if req.password is None and req.totp_secret is None:
        raise HTTPException(400, "没有要修改的字段")
    try:
        ok = db.update_registered_manual(
            email, password=req.password, totp_secret=req.totp_secret
        )
    except ValueError as e:
        # 校验失败：把具体原因带给前端，别让用户猜哪里填错了
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"未找到已注册账号: {email}")

    changed = [n for n, v in (("密码", req.password), ("TOTP secret", req.totp_secret))
               if v is not None]
    logger.info(f"[registered] 手动修改凭证 email={email} 字段={'+'.join(changed)}")
    return {"ok": True, "email": email, "changed": changed}


# ──────────────────────── Plus 试用检查 ────────────────────────


class CheckPlusReq(BaseModel):
    emails: list[str] = Field(..., description="要检查的邮箱列表")
    proxy: str = Field("", description="查询代理，留空直连")


@app.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """用 access_token 查询账号的 Plus 试用状态。"""
    from ..http_client import create_http_session, us_chrome_headers, DEFAULT_IMPERSONATE

    log = logging.getLogger("webui")
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    # 代理出口是美国：UA / 语言 / Client Hints 走统一的美国 Chrome146 头，
    # 不再写死 Chrome/145 也不再让 curl_cffi 补 macOS 提示。
    us_headers = us_chrome_headers()

    # 走和注册流程同一个 create_http_session，不再自己拼 proxies dict。
    # 它负责两件这里以前漏掉的事：
    #   1) socks5:// -> socks5h://，DNS 交给代理端解析。用本地 DNS 打
    #      chatgpt.com 经常握手失败，这是「填了 SOCKS5 就检测不出来」的真正原因。
    #   2) trust_env=False + 显式空代理，代理留空时是真直连，
    #      不会被系统 HTTP_PROXY/HTTPS_PROXY 悄悄接管。
    proxy = req.proxy.strip()

    def _new_sess(p):
        return create_http_session(
            proxy=p,
            impersonate=DEFAULT_IMPERSONATE,
            user_agent=us_headers["User-Agent"],
            accept_language=us_headers["Accept-Language"],
            client_hints={k: v for k, v in us_headers.items()
                          if k.startswith("sec-ch-ua")},
        )

    try:
        sess = _new_sess(proxy or None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"创建 HTTP 会话失败: {e}")

    note = ""
    fallback_used = False

    def _check(access_token: str):
        """打一次检测请求。代理整条路不通就降级直连，且只降一次 ——
        后面的号直接用直连，不再逐个去撞已经确认不通的代理。"""
        nonlocal sess, note, fallback_used
        headers = {
            **us_headers,
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        try:
            return sess.get(url, headers=headers, timeout=15)
        except Exception as e:  # noqa: BLE001
            if fallback_used or not proxy:
                raise
            fallback_used = True
            note = f"代理连不通（{type(e).__name__}），已自动改直连"
            log.warning(f"[check_plus] {note}: {str(e)[:140]}")
            sess = _new_sess(None)
            return sess.get(url, headers=headers, timeout=15)

    results = {}
    for email in req.emails:
        cred = db.get_registered(email)
        if not cred:
            results[email] = {"status": "not_found", "label": "未找到"}
            continue
        at = (cred.get("access_token") or "").strip()
        if not at:
            results[email] = {"status": "no_at", "label": "无AT"}
            continue
        try:
            resp = _check(at)
        except Exception as e:  # noqa: BLE001
            results[email] = {"status": "error", "label": "网络失败"}
            log.warning(f"[check_plus] {email} 请求失败: {str(e)[:140]}")
            continue
        if resp.status_code == 401:
            results[email] = {"status": "banned", "label": "封号"}
            continue
        if resp.status_code != 200:
            results[email] = {"status": "error", "label": f"HTTP {resp.status_code}"}
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            results[email] = {"status": "error", "label": "响应非 JSON"}
            continue
        accts = data.get("accounts", {})
        if not accts:
            results[email] = {"status": "error", "label": "无账户数据"}
            continue
        info = next(iter(accts.values()))
        acct = info.get("account", {})
        ent = info.get("entitlement", {})
        promo = info.get("eligible_promo_campaigns", {})
        if acct.get("is_deactivated", False):
            results[email] = {"status": "banned", "label": "封号"}
            continue
        plan = acct.get("plan_type", "free")
        has_sub = ent.get("has_active_subscription", False)
        has_plus_promo = "plus" in promo and promo["plus"].get("id") == "plus-1-month-free"
        if plan == "plus" or has_sub:
            results[email] = {"status": "plus_active", "label": "Plus生效中"}
        elif has_plus_promo:
            results[email] = {"status": "plus_eligible", "label": "可领Plus试用"}
        else:
            results[email] = {"status": "free", "label": "Free"}

    try:
        sess.close()
    except Exception:  # noqa: BLE001
        pass

    checked_at = time.time()
    for email, info in results.items():
        # error 和 not_found / no_at 一样不写库：它们不是「检测结论」而是没检测成，
        # 写进去号就从 unchecked 过滤器里消失了，看着像已经检测过。
        # 前端仍会收到 error 并显示红点，网络修好重新点一次即可覆盖。
        if info["status"] not in ("not_found", "no_at", "error"):
            db.update_plus_check(email, {**info, "checked_at": checked_at})

    return {"ok": True, "results": results, "note": note}


# ──────────────────────── auto-loop ────────────────────────


class AutoLoopStartReq(BaseModel):
    """跟 RegisterReq 复用同样的字段，auto-loop 内部传给每个 run。"""
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""              # 单代理（concurrency=1 + 无代理池时用）
    proxy_pool: str = Field(default="", max_length=16 * 1024 * 1024)  # 多代理池；优先于 proxy
    concurrency: int = 1         # 并发 worker 数（正整数，不设固定上限）
    otp_timeout: int = 10
    allow_existing_login: bool = True
    cool_down_seconds: float = 3.0  # 每个 worker 跑完后冷却（防风控）
    target_count: int = 0        # 目标成功数（0=不限量，达标自动停止）
    # 批量页已放开关且**默认开**（主人要求每个号都绑）。
    # 这里的 default 仍保持 False —— 它只在「前端没传这个字段」时生效，
    # 是给旧前端缓存 / 直接打 API 的保守兜底：漏传时宁可不绑，也不要
    # 替调用方做一个不可逆的决定。真实默认值由 AutoLoop.vue 的 autoWant2fa 决定。
    want_2fa: bool = False


@app.post("/api/auto/start")
def api_auto_start(req: AutoLoopStartReq):
    res = AUTO_LOOP.start(req.model_dump())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "启动失败"))
    return res


@app.post("/api/auto/pause")
def api_auto_pause():
    res = AUTO_LOOP.pause()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "暂停失败"))
    return res


@app.post("/api/auto/resume")
def api_auto_resume():
    res = AUTO_LOOP.resume()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "恢复失败"))
    return res


@app.post("/api/auto/stop")
def api_auto_stop():
    res = AUTO_LOOP.stop()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "停止失败"))
    return res


@app.get("/api/auto/status")
def api_auto_status():
    return {"ok": True, **AUTO_LOOP.status()}


@app.get("/api/auto/stream")
async def api_auto_stream(request: Request):
    """SSE 推送 auto-loop 状态变化 + run_started / run_finished 事件。"""
    q = AUTO_LOOP.subscribe()

    async def gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 阻塞拿消息，但每 30s 心跳
                try:
                    msg = await loop.run_in_executor(None, lambda: q.get(timeout=30))
                except Exception:
                    yield ": heartbeat\n\n"
                    continue
                if msg is None:
                    break
                kind = msg.get("kind", "state")
                data = msg.get("data", {})
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            AUTO_LOOP.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ──────────────────────── Team 轮转 ────────────────────────


class TeamMotherCreateReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    session: str = Field(..., min_length=1, description="Session JSON、Access Token 或 Cookie")
    workspace_id: str = Field("", max_length=200)
    enabled: bool = True


class TeamMotherUpdateReq(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    session: Optional[str] = Field(None, description="留空表示不修改凭证")
    workspace_id: Optional[str] = Field(None, max_length=200)
    enabled: Optional[bool] = None


class TeamRotationStartReq(BaseModel):
    interval_seconds: int = Field(300, ge=10, le=86400)
    quota_threshold: float = Field(100, ge=1, le=100)
    proxy: str = Field("", max_length=4096)


@app.get("/api/team/mothers")
def api_team_mothers():
    return {"ok": True, "items": db.list_team_mothers()}


@app.post("/api/team/mothers")
def api_create_team_mother(req: TeamMotherCreateReq):
    try:
        material = parse_mother_session(req.session, req.workspace_id)
        item = db.create_team_mother({
            **material,
            "name": req.name.strip(),
            "enabled": req.enabled,
        })
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "该 workspace_id 已存在") from exc
    return {"ok": True, "item": item}


@app.put("/api/team/mothers/{mother_id}")
def api_update_team_mother(mother_id: str, req: TeamMotherUpdateReq):
    existing = db.get_team_mother(mother_id)
    if not existing:
        raise HTTPException(404, "母号不存在")
    changes = req.model_dump(exclude_none=True)
    session = changes.pop("session", None)
    workspace_id = changes.get("workspace_id", existing["workspace_id"])
    if session is not None and session.strip():
        try:
            changes.update(parse_mother_session(session, workspace_id))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if "name" in changes:
        changes["name"] = str(changes["name"]).strip()
    try:
        item = db.update_team_mother(mother_id, changes)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "该 workspace_id 已存在") from exc
    return {"ok": True, "item": item}


@app.delete("/api/team/mothers/{mother_id}")
def api_delete_team_mother(mother_id: str):
    try:
        deleted = db.delete_team_mother(mother_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "母号不存在")
    return {"ok": True}


@app.get("/api/team/mothers/{mother_id}/detail")
def api_team_mother_detail(mother_id: str):
    try:
        return {"ok": True, **TEAM_ROTATION.inspect_mother(mother_id)}
    except TeamApiError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.delete("/api/team/mothers/{mother_id}/members/{member_id}")
def api_remove_team_member(mother_id: str, member_id: str):
    try:
        return {"ok": True, **TEAM_ROTATION.remove_member(mother_id, member_id)}
    except TeamApiError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/team/rotation/status")
def api_team_rotation_status():
    return {"ok": True, **TEAM_ROTATION.snapshot()}


@app.post("/api/team/rotation/start")
def api_team_rotation_start(req: TeamRotationStartReq):
    result = TEAM_ROTATION.start(req.model_dump())
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Team 轮转启动失败"))
    return result


@app.post("/api/team/rotation/pause")
def api_team_rotation_pause():
    result = TEAM_ROTATION.pause()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Team 轮转暂停失败"))
    return result


@app.post("/api/team/rotation/resume")
def api_team_rotation_resume():
    result = TEAM_ROTATION.resume()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Team 轮转恢复失败"))
    return result


@app.post("/api/team/rotation/stop")
def api_team_rotation_stop():
    result = TEAM_ROTATION.stop()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Team 轮转停止失败"))
    return result


@app.post("/api/team/rotation/check-now")
def api_team_rotation_check_now():
    result = TEAM_ROTATION.trigger()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "触发检查失败"))
    return result


# ──────────────────────── 静态资源 ────────────────────────


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.webui.app:app", host="127.0.0.1", port=8765, reload=False)
