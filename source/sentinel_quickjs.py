"""QuickJS-driven Sentinel token generator.

Adapted from
https://github.com/zc-zhangchen/any-auto-register
platforms/chatgpt/sentinel_browser.py:`_get_sentinel_token_via_quickjs`
+ scripts/js/openai_sentinel_quickjs.js (MIT License).

Why this exists:
  Pure-Python `sentinel.py` computes a synthetic PoW that *passes* OpenAI's
  surface validation (200 OK on `/sentinel/req`, `/authorize/continue`, etc.)
  but the OTP-dispatch service runs the actual sentinel SDK JS server-side
  to verify the token. Our synthetic token fails the deeper check → email
  silent-drop. To pass, we must run OpenAI's real `sdk.js` (downloaded from
  `sentinel.openai.com/sentinel/<ver>/sdk.js`) inside a JS VM and emit the
  same token the real browser would.

Implementation:
  - Spawn `node -e <wrapper>` per token request
  - Wrapper loads OpenAI's sdk.js + `openai_sentinel_quickjs.js` (a thin
    adapter that exposes `requirements`/`solve` actions over stdin/stdout)
  - Two passes: action=requirements → `request_p`, then `/sentinel/req` →
    challenge, then action=solve → `final_p` + `t`
  - Returns the same JSON-string shape `{p, t, c, id, flow}` as our
    pure-Python `build_sentinel_token`, so callers don't need to change

Public API:
  - `get_sentinel_token_via_quickjs(session, device_id, flow, ...) -> str | None`
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from copy import copy, deepcopy
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


SENTINEL_VERSION = "20260219f9f6"
SENTINEL_SDK_URL = f"https://sentinel.openai.com/sentinel/{SENTINEL_VERSION}/sdk.js"
SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"

# 代理出口是美国：任何未显式传入语言的兜底都必须是美国值，
# 绝不能出现 zh-CN（与 IP / navigator.language 跨层矛盾 → 静默死号）
_DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"


def _resolve_node_binary() -> str:
    return (os.getenv("OPENAI_SENTINEL_NODE_PATH", "") or "").strip() or "node"


def _quickjs_script_path() -> Path:
    return Path(__file__).resolve().parent / "openai_sentinel_quickjs.js"


_sdk_file_cache: Optional[Path] = None
_sdk_cache_lock = threading.Lock()


def _sentinel_parallelism() -> int:
    # 0 means unlimited. The production host has enough CPU/RAM and the old
    # fixed 12-slot gate caused more failures than it prevented.
    raw = (os.getenv("OPENAI_SENTINEL_MAX_CONCURRENCY", "0") or "0").strip()
    try:
        return max(0, min(512, int(raw)))
    except ValueError:
        return 0


_quickjs_parallelism = _sentinel_parallelism()
_quickjs_exec_slots = (
    threading.BoundedSemaphore(_quickjs_parallelism)
    if _quickjs_parallelism > 0
    else None
)


@contextmanager
def _quickjs_execution_slot():
    """Optional operator cap; production uses 0 so no artificial queue exists."""
    if _quickjs_exec_slots is None:
        yield
        return
    # An explicit cap is a backpressure choice, not a reason to fail a run.
    _quickjs_exec_slots.acquire()
    try:
        yield
    finally:
        _quickjs_exec_slots.release()


@contextmanager
def _isolated_session_cookies(session: Any):
    """Prevent Sentinel responses from mutating the auth session cookie jar."""
    cookies = getattr(session, "cookies", None)
    try:
        if hasattr(cookies, "jar"):
            saved = ("jar", [copy(cookie) for cookie in cookies.jar])
        else:
            saved = ("mapping", deepcopy(cookies))
    except Exception:
        saved = None
    try:
        yield
    finally:
        if saved is not None:
            current = getattr(session, "cookies", None)
            try:
                current.clear()
                if saved[0] == "jar":
                    for cookie in saved[1]:
                        current.jar.set_cookie(copy(cookie))
                else:
                    current.update(saved[1])
            except Exception:
                try:
                    session.cookies = deepcopy(saved[1])
                except Exception:
                    logger.warning("Sentinel 请求后恢复认证 cookie 失败", exc_info=True)


def _request_with_retry(session: Any, method: str, url: str, *, attempts: int = 3, **kwargs):
    """Sentinel 网络边界重试；覆盖 curl 77、代理抖动和临时 TLS 失败。"""
    last_error: Optional[Exception] = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return getattr(session, method)(url, **kwargs)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            logger.warning(
                "Sentinel %s 请求失败，第 %d/%d 次重试: %s",
                method.upper(), attempt, attempts, exc,
            )
            time.sleep(attempt)
    raise RuntimeError(f"Sentinel {method.upper()} 连续 {attempts} 次失败: {last_error}") from last_error


def _ensure_sdk_file(session: Any, timeout_ms: int, accept_language: str = "") -> Path:
    """Download OpenAI's actual sdk.js to /tmp cache (one-shot per version)."""
    global _sdk_file_cache
    if _sdk_file_cache and _sdk_file_cache.exists():
        return _sdk_file_cache

    cache_dir = Path(tempfile.gettempdir()) / "openai-sentinel-demo" / SENTINEL_VERSION
    cache_dir.mkdir(parents=True, exist_ok=True)
    sdk_file = cache_dir / "sdk.js"
    with _sdk_cache_lock:
        # Double-check after taking the lock; another worker may have filled
        # the cache while this worker was waiting.
        if _sdk_file_cache and _sdk_file_cache.exists():
            return _sdk_file_cache
        if sdk_file.exists() and sdk_file.stat().st_size > 0:
            _sdk_file_cache = sdk_file
            return sdk_file

        resp = _request_with_retry(
            session,
            "get",
            SENTINEL_SDK_URL,
            headers={
                "accept": "*/*",
                # 必须跟随本会话指纹的语言：这条请求与后续 /sentinel/req 同源，
                # 写死 zh-CN 会与美国出口 IP + navigator.language 跨层矛盾
                "accept-language": accept_language or _DEFAULT_ACCEPT_LANGUAGE,
                "referer": "https://auth.openai.com/",
                "sec-fetch-dest": "script",
                "sec-fetch-mode": "no-cors",
                "sec-fetch-site": "same-site",
            },
            timeout=max(10, int(timeout_ms / 1000)),
        )
        if getattr(resp, "status_code", 0) != 200:
            raise RuntimeError(f"下载 sdk.js 失败: HTTP {resp.status_code}")
        content = getattr(resp, "content", b"") or (resp.text or "").encode()
        if not content:
            raise RuntimeError("下载 sdk.js 失败: 响应为空")
        tmp_file = sdk_file.with_name(f".{sdk_file.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_file.write_bytes(content)
            os.replace(tmp_file, sdk_file)
        finally:
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass
        _sdk_file_cache = sdk_file
        return sdk_file


def _run_quickjs_action(
    *,
    action: str,
    sdk_file: Path,
    quickjs_script: Path,
    payload: dict,
    timeout_ms: int,
) -> dict:
    body = dict(payload)
    body["action"] = action
    process_timeout = max(10, int(timeout_ms / 1000) + 5)

    # sdk.js 采集 `""+new Date`（完整 Date.prototype.toString，含 GMT 偏移和时区名）。
    # 容器默认 Etc/UTC，会渲染成 "GMT+0000 (Coordinated Universal Time)"，
    # 与美国出口 IP 直接矛盾。TZ 能穿透 vm.createContext（已实测），
    # 所以在这里按指纹时区注入，让 token 里的时间戳就是美国时间。
    child_env = {
        **os.environ,
        "OPENAI_SENTINEL_SDK_FILE": str(sdk_file),
    }
    tz = str(body.get("timezone") or "").strip()
    if tz:
        child_env["TZ"] = tz

    with _quickjs_execution_slot():
        proc = subprocess.run(
            [_resolve_node_binary(), str(quickjs_script)],
            input=json.dumps(body, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=process_timeout,
            env=child_env,
        )
    if proc.returncode != 0:
        raise RuntimeError(f"QuickJS 执行失败: {(proc.stderr or proc.stdout or 'unknown').strip()[:300]}")
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("QuickJS 返回空输出")
    data = json.loads(out)
    if not isinstance(data, dict):
        raise RuntimeError("QuickJS 输出不是 JSON 对象")
    return data


def _fetch_sentinel_challenge(
    session: Any,
    *,
    device_id: str,
    flow: str,
    request_p: str,
    timeout_ms: int,
    accept_language: str = "",
) -> dict:
    body = {"p": request_p, "id": device_id, "flow": flow}
    resp = _request_with_retry(
        session,
        "post",
        SENTINEL_REQ_URL,
        data=json.dumps(body, separators=(",", ":")),
        headers={
            "origin": "https://sentinel.openai.com",
            "referer": f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_VERSION}",
            "content-type": "text/plain;charset=UTF-8",
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            # 跟随会话指纹；这是拿风控 token 的关键请求，写死 zh-CN 等于自曝
            "accept-language": accept_language or _DEFAULT_ACCEPT_LANGUAGE,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        },
        timeout=max(10, int(timeout_ms / 1000)),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"/sentinel/req HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Sentinel challenge 响应不是 JSON 对象")
    return payload


def get_sentinel_token_via_quickjs(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    timeout_ms: int = 45000,
    log: Optional[Callable[[str], None]] = None,
    user_agent: str = "",
    screen: str = "",
    lang: str = "",
    lang_full: str = "",
    browser_type: str = "",
    platform: str = "",
    vendor: Optional[str] = None,
    hardware_concurrency: int = 0,
    device_memory: Optional[int] = None,
    max_touch_points: int = 0,
    device_pixel_ratio: float = 0.0,
    timezone: str = "",  # IANA 时区名（如 Asia/Tokyo）
    # Client Hints 全套（QuickJS 路径不直接用，但为了签名统一接收）
    sec_ch_ua_full_version_list: str = "",
    sec_ch_ua_arch: str = "",
    sec_ch_ua_bitness: str = "",
    sec_ch_ua_model: str = "",
    sec_ch_ua_platform_version: str = "",
) -> Optional[tuple[str, str]]:
    """Try the QuickJS path. Return JSON string on success, None on any failure.

    Caller is expected to fall back to pure-Python sentinel on None.

    指纹一致性：``platform`` / ``vendor`` / ``hardware_concurrency`` 等按调用方
    传入的浏览器家族画像喂给 sdk.js 的 navigator，避免 UA 说 Windows Chrome 但
    navigator 报 MacIntel/Apple 的硬伤。未传时按 UA 推断合理默认值。
    """
    log = log or (lambda m: logger.info(m))
    quickjs_script = _quickjs_script_path()
    if not quickjs_script.exists():
        log(f"Sentinel QuickJS 脚本不存在: {quickjs_script}")
        return None

    did = str(device_id or uuid.uuid4())

    screen_w, screen_h = "1920", "1080"
    if screen and "x" in screen:
        parts = screen.split("x", 1)
        screen_w, screen_h = parts[0], parts[1]

    lang_primary = lang or "en-US"
    languages = [lang_primary]
    if lang_full:
        for part in lang_full.split(","):
            tag = part.split(";")[0].strip()
            if tag and tag not in languages:
                languages.append(tag)

    # ── 指纹一致性：platform / vendor 未显式传入时按 UA 推断，绝不写死 MacIntel ──
    ua_l = (user_agent or "").lower()
    if not platform:
        if "iphone" in ua_l:
            platform = "iPhone"
        elif "windows" in ua_l:
            platform = "Win32"
        elif "mac" in ua_l:
            platform = "MacIntel"
        else:
            platform = "Win32"
    if vendor is None:
        if "firefox" in ua_l:
            vendor = ""                       # Firefox navigator.vendor 为空串
        elif "chrome" in ua_l:
            vendor = "Google Inc."
        else:
            vendor = "Apple Computer, Inc."   # Safari / iOS
    hw_conc = int(hardware_concurrency) if hardware_concurrency else 8

    env_payload = {
        "device_id": did,
        "user_agent": user_agent or "Mozilla/5.0",
        "screen_width": screen_w,
        "screen_height": screen_h,
        "language": lang_primary,
        "languages": languages,
        "platform": platform,
        "vendor": vendor,
        "hardware_concurrency": hw_conc,
        "browser_type": browser_type or "",
        "device_pixel_ratio": float(device_pixel_ratio) if device_pixel_ratio else 1.0,
        "max_touch_points": int(max_touch_points),
        # 代理出口美国：兜底给美国时区，不能是 UTC
        "timezone": timezone or "America/New_York",  # IANA 时区名
    }
    # deviceMemory 仅 Chromium 暴露；None 时不下发该键，JS 侧保持 undefined
    if device_memory is not None:
        env_payload["device_memory"] = int(device_memory)

    try:
        # sentinel.openai.com can set load-balancer cookies on the shared client.
        # Restoring the jar keeps the auth.openai.com state created by OAuth init
        # intact; otherwise authorize/continue may return 409 invalid_state.
        # sentinel 的两条 HTTP 请求必须与本会话指纹同语言
        wire_lang = lang_full or (f"{lang_primary},en;q=0.9" if lang_primary else "")

        with _isolated_session_cookies(session):
            sdk_file = _ensure_sdk_file(session, timeout_ms, accept_language=wire_lang)

            requirements = _run_quickjs_action(
                action="requirements",
                sdk_file=sdk_file,
                quickjs_script=quickjs_script,
                payload=env_payload,
                timeout_ms=timeout_ms,
            )
            request_p = str(requirements.get("request_p") or "").strip()
            if not request_p:
                log("Sentinel QuickJS 失败: requirements 未返回 request_p")
                return None

            challenge = _fetch_sentinel_challenge(
                session, device_id=did, flow=flow, request_p=request_p, timeout_ms=timeout_ms,
                accept_language=wire_lang,
            )
        c_value = str(challenge.get("token") or "").strip()
        if not c_value:
            log("Sentinel QuickJS 失败: challenge token 为空")
            return None

        solve_payload = dict(env_payload)
        solve_payload.update({
            "request_p": request_p,
            "challenge": challenge,
            "flow": flow,
            "behavior_duration_ms": 4200,
        })
        solved = _run_quickjs_action(
            action="solve",
            sdk_file=sdk_file,
            quickjs_script=quickjs_script,
            payload=solve_payload,
            timeout_ms=timeout_ms,
        )

        so_token_raw = str(solved.get("so_token") or "").strip()

        # SO token 要不要，是**服务端在 challenge 里说了算**的，不是每个 flow 都有。
        # sdk.js 里 SO 采集器的启动条件（去混淆）：
        #     challenge.so.required === true && typeof challenge.so.collector_dx === 'string'
        # 实测 2026-08-06 三个 flow 的 /sentinel/req 响应：
        #     authorize_continue    → 有 so 块, required=true
        #     oauth_create_account  → 有 so 块, required=true
        #     username_password_create → **顶层根本没有 so 键**
        # 也就是说真实浏览器跑 username_password_create 同样不会有 SO token。
        # 以前这里无条件要求 so_token 非空，把「服务端没要」误判成「我们没算出来」，
        # 打出「中止以避免封号」——是误报。更糟的是调用方降级时会沿用上一个 flow 的
        # SO token 继续发，等于给一个明说不需要 SO 的请求塞了个别的 flow 的凭证，
        # 比不发更像异常特征。现在按服务端的要求判定。
        so_required = bool((challenge.get("so") or {}).get("required") is True)

        sdk_token = str(solved.get("token") or "").strip()
        if not sdk_token:
            log("Sentinel QuickJS 失败: SDK token 为空，中止以避免封号")
            return None
        if so_required and not so_token_raw:
            # 服务端确实要了 SO token 但我们没算出来 —— 这才是真异常，保持中止
            log("Sentinel QuickJS 失败: 服务端要求 SO token 但求解为空，中止以避免封号")
            return None
        log(f"Sentinel QuickJS OK (len={len(sdk_token)}, "
            f"so={'Y' if so_token_raw else 'N/A(服务端未要求)'})")
        return (sdk_token, so_token_raw)
    except Exception as e:
        log(f"Sentinel QuickJS 异常: {e}")
        return None
