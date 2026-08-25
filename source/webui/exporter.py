"""导出注册凭证到 CPA / SUB2API 面板（路线 2 实现）。

参考 zc-zhangchen/any-auto-register 的 platforms/chatgpt/cpa_upload.py 和 sub2api_upload.py。

核心改造：
  ★ 导出前先用 refresh_token 调 https://auth.openai.com/oauth/token 换新的 Codex
    风格 access_token（client_id=app_EMoamEEZ73f0CkXaXp7hrann）。
    主项目 run_register 末尾会用 get_auth_session() 把 Codex access_token 覆盖
    成 ChatGPT 网页 NextAuth 风格，但 NextAuth 风格的 token 在 CPA/SUB2API 不可用，
    所以这里单独刷新。

两种导出目标：
  1. CPA：multipart 文件上传 → POST /v0/management/auth-files
     Bearer 鉴权，文件名 {email}.json
  2. SUB2API / Hub：批量 POST /api/v1/admin/accounts/batch
     x-api-key 鉴权（无登录流程）

全部用 curl_cffi impersonate 模拟浏览器 TLS 指纹，绕过 CF Bot 拦截。
指纹配方统一取 http_client 的美国 Chrome146（DEFAULT_IMPERSONATE / us_chrome_headers），
不再各处写死 chrome110。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

# OpenAI / Codex 常量
OPENAI_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_SCOPE = "openid email profile offline_access"

# 默认值
DEFAULT_TIMEOUT = 30
DEFAULT_SUB2API_GROUP_IDS = [2]
DEFAULT_SUB2API_MODEL = "gpt-5.4"
DEFAULT_SUB2API_MODELS = (
    "gpt-image-2",
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-5.6",
    "gpt-5.6-sol",
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "codex-auto-review",
)
DEFAULT_SUB2API_CONCURRENCY = 3
DEFAULT_SUB2API_FINGERPRINT_MODE = "session"
SUB2API_FINGERPRINT_MODES = ("off", "device", "session", "full")
SUB2API_DEFAULT_EXPIRES_IN = 863999  # 跟 any-auto-register 一致
MAX_ATTEMPTS = 3
RETRY_DELAYS_S = [3.0, 7.0]


# ──────────────────────── 工具函数 ────────────────────────

# 代理出口是美国：统一取 http_client 的美国 Chrome146 配方。
# http_client 导入失败时退回内联常量，绝不退回 chrome110/macOS。
try:
    from http_client import DEFAULT_IMPERSONATE as _IMPERSONATE, us_chrome_headers as _us_headers
except Exception:  # pragma: no cover
    _IMPERSONATE = "chrome146"

    def _us_headers() -> dict:
        return {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/146.0.0.0 Safari/537.36"),
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }


def _decode_jwt_payload(token: str) -> dict:
    """解析 JWT payload 段。失败返回 {}。"""
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        p = parts[1]
        pad = (4 - len(p) % 4) % 4
        data = json.loads(base64.urlsafe_b64decode(p + "=" * pad))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _b64url_json(d: dict) -> str:
    raw = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _get_auth(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    auth_info = payload.get("https://api.openai.com/auth")
    return auth_info if isinstance(auth_info, dict) else {}


def _get_profile(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    p = payload.get("https://api.openai.com/profile")
    return p if isinstance(p, dict) else {}


def _first(*values) -> str:
    for v in values:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _parse_group_ids(raw: Any, fallback: list[int] | None = None) -> list[int]:
    """支持字符串 '1,2'、列表、单值等格式，返回 list[int]。"""
    if isinstance(raw, str):
        candidates = [s.strip() for s in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    elif raw is None:
        candidates = []
    else:
        candidates = [raw]

    out: list[int] = []
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            out.append(int(text))
        except ValueError:
            continue
    return out or list(fallback or DEFAULT_SUB2API_GROUP_IDS)


def _positive_int(value: Any, default: int, maximum: int = 1000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def parse_sub2api_models(raw: Any, fallback: Any = None) -> list[str]:
    """解析模型数组、JSON 字符串或逗号/换行分隔字符串并保持顺序去重。"""
    candidates: list[Any]
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            candidates = list(decoded) if isinstance(decoded, list) else []
        else:
            candidates = text.replace("\r", "\n").replace(",", "\n").split("\n")
    elif isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    elif raw is None:
        candidates = []
    else:
        candidates = [raw]

    models: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        model = str(item or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)

    if models:
        return models
    defaults = DEFAULT_SUB2API_MODELS if fallback is None else fallback
    return parse_sub2api_models(defaults, fallback=()) if defaults else []


def _utc_iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _import_cffi():
    """惰性 import curl_cffi。失败抛 RuntimeError。"""
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests
    except ImportError as e:
        raise RuntimeError(f"curl_cffi 未安装，无法导出（pip install curl-cffi）: {e}")


def _import_cffi_mime():
    """惰性 import CurlMime。"""
    try:
        from curl_cffi import CurlMime
        return CurlMime
    except ImportError as e:
        raise RuntimeError(f"curl_cffi CurlMime 不可用: {e}")


# ──────────────────────── 核心：刷新 Codex access_token ────────────────────────


def refresh_codex_token(refresh_token: str, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """用 Codex refresh_token 换一组新的 access_token / id_token / refresh_token(滚动)。

    参考 any-auto-register/platforms/chatgpt/token_refresh.py 风格。

    返回 OpenAI 原始响应 dict：
        {access_token, refresh_token, id_token, expires_in, token_type}
    失败抛 RuntimeError。
    """
    rt = str(refresh_token or "").strip()
    if not rt:
        raise RuntimeError("缺少 refresh_token，无法刷新 Codex access_token")

    cffi = _import_cffi()
    body = {
        "grant_type": "refresh_token",
        "client_id": CODEX_CLIENT_ID,
        "refresh_token": rt,
        "scope": CODEX_SCOPE,
    }
    # 这条是唯一真正打 OpenAI 的请求：环境头必须和注册时一致（美国 Windows Chrome146）。
    # 以前只有 Origin/Referer，没有 UA / Accept-Language / Client Hints，
    # curl_cffi 会替我们补内建的 macOS 提示 —— 和注册用的 Windows 指纹跨层矛盾。
    headers = {
        **_us_headers(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://auth.openai.com",
        "Referer": "https://auth.openai.com/",
    }

    resp = cffi.post(
        OPENAI_TOKEN_ENDPOINT,
        headers=headers,
        data=body,
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate=_IMPERSONATE,
    )

    if resp.status_code != 200:
        body_text = ""
        try:
            body_text = (resp.text or "")[:300]
        except Exception:
            pass
        raise RuntimeError(
            f"OpenAI token 刷新失败 HTTP {resp.status_code}: {body_text}"
        )

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError("OpenAI token 刷新返回非 JSON")

    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError(f"OpenAI token 刷新返回无 access_token: {str(data)[:200]}")

    return data


# ──────────────────────── CPA：生成 token JSON ────────────────────────


def _build_compat_id_token(*, access_token: str, email: str) -> str:
    """access_token 缺 id_token 时构造一个本地解析用的兼容 token。

    完全照 any-auto-register/cpa_upload.py:_build_compat_id_token 实现。
    注意：签名是固定字符串，仅供 CPA 等不校验签名的本地环境解析。
    """
    payload = _decode_jwt_payload(access_token)
    if not payload:
        return ""

    auth_info = _get_auth(payload)
    profile = _get_profile(payload)
    email_from_token = (profile.get("email") or payload.get("email") or email or "").strip()
    email_verified = bool(profile.get("email_verified", payload.get("email_verified", True)))
    account_id = str(auth_info.get("chatgpt_account_id") or auth_info.get("account_id") or "").strip()
    user_id = str(
        auth_info.get("chatgpt_user_id")
        or auth_info.get("user_id")
        or payload.get("sub")
        or ""
    ).strip()
    iat = int(payload.get("iat") or 0)
    exp = int(payload.get("exp") or 0)
    auth_time = int(payload.get("pwd_auth_time") or payload.get("auth_time") or iat or 0)
    session_id = str(
        payload.get("session_id")
        or f"compat_session_{(account_id or user_id or 'unknown').replace('-', '')[:24]}"
    ).strip()
    plan_type = str(auth_info.get("chatgpt_plan_type") or "free").strip() or "free"
    organization_id = str(
        auth_info.get("organization_id")
        or f"org-{hashlib.sha1((account_id or email_from_token or user_id).encode('utf-8')).hexdigest()[:24]}"
    )
    project_id = str(
        auth_info.get("project_id")
        or f"proj_{hashlib.sha1((organization_id + ':' + (account_id or user_id)).encode('utf-8')).hexdigest()[:24]}"
    )

    compat_auth = {
        "chatgpt_account_id": account_id,
        "chatgpt_plan_type": plan_type,
        "chatgpt_subscription_active_start": auth_info.get("chatgpt_subscription_active_start"),
        "chatgpt_subscription_active_until": auth_info.get("chatgpt_subscription_active_until"),
        "chatgpt_subscription_last_checked": auth_info.get("chatgpt_subscription_last_checked"),
        "chatgpt_user_id": user_id,
        "completed_platform_onboarding": bool(auth_info.get("completed_platform_onboarding", False)),
        "groups": auth_info.get("groups", []),
        "is_org_owner": bool(auth_info.get("is_org_owner", True)),
        "localhost": bool(auth_info.get("localhost", True)),
        "organization_id": organization_id,
        "organizations": auth_info.get("organizations") or [
            {"id": organization_id, "is_default": True, "role": "owner", "title": "Personal"}
        ],
        "project_id": project_id,
        "user_id": str(auth_info.get("user_id") or user_id or "").strip(),
    }

    compat_payload = {
        "amr": ["pwd", "otp", "mfa", "urn:openai:amr:otp_email"],
        "at_hash": hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:22],
        "aud": [CODEX_CLIENT_ID],
        "auth_provider": "password",
        "auth_time": auth_time,
        "email": email_from_token,
        "email_verified": email_verified,
        "exp": exp,
        "https://api.openai.com/auth": compat_auth,
        "iat": iat,
        "iss": payload.get("iss") or "https://auth.openai.com",
        "jti": f"compat-{hashlib.sha1(access_token.encode('utf-8')).hexdigest()[:32]}",
        "name": email_from_token or "OpenAI User",
        "rat": auth_time,
        "sid": session_id,
        "sub": payload.get("sub") or user_id,
    }

    header = {"alg": "RS256", "typ": "JWT", "kid": "compat"}
    signature = base64.urlsafe_b64encode(b"compat_signature_for_cpa_parsing_only").decode("ascii").rstrip("=")
    return f"{_b64url_json(header)}.{_b64url_json(compat_payload)}.{signature}"


def build_cpa_token_json(cred: dict) -> dict:
    """生成 CPA `/v0/management/auth-files` 的 multipart 文件内容。

    严格对齐 any-auto-register/cpa_upload.py:generate_token_json：
    8 个字段，UTC+8 时区。
    """
    access_token = str(cred.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("未读取到可导入的 access_token")
    refresh_token = str(cred.get("refresh_token") or "").strip()
    id_token = str(cred.get("id_token") or "").strip()
    email = str(cred.get("email") or "").strip()
    if not id_token:
        id_token = _build_compat_id_token(access_token=access_token, email=email)

    payload = _decode_jwt_payload(access_token)
    auth_info = _get_auth(payload)
    account_id = str(auth_info.get("chatgpt_account_id") or "").strip()

    tz_cn = timezone(timedelta(hours=8))
    expired_str = ""
    exp = payload.get("exp")
    if isinstance(exp, int) and exp > 0:
        expired_str = datetime.fromtimestamp(exp, tz=tz_cn).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    last_refresh = datetime.now(tz=tz_cn).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    return {
        "type": "codex",
        "email": email,
        "expired": expired_str,
        "id_token": id_token,
        "account_id": account_id,
        "access_token": access_token,
        "last_refresh": last_refresh,
        "refresh_token": refresh_token,
    }


# ──────────────────────── CPA：上传 ────────────────────────


def export_to_cpa(cred: dict, cfg: dict, *,
                    log_fn: Optional[Callable[[str, str], None]] = None) -> dict:
    """CPA multipart 上传。"""
    log = log_fn or (lambda m, lvl="info": logger.info(m))

    api_url = (cfg.get("cpa_url") or "").rstrip("/").strip()
    api_key = (cfg.get("cpa_mgmt_key") or "").strip()
    timeout = int(cfg.get("cpa_timeout") or DEFAULT_TIMEOUT)
    if not api_url:
        raise RuntimeError("CPA 未配置 URL")
    if not api_key:
        raise RuntimeError("CPA 未配置管理密钥")

    cffi = _import_cffi()
    CurlMime = _import_cffi_mime()

    token_data = build_cpa_token_json(cred)
    email = token_data.get("email") or "unknown"
    filename = f"{email}.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")
    upload_url = f"{api_url}/v0/management/auth-files"
    # CLIProxyAPI 官方文档：两种 header 都接受。同时发以应对不同版本/部署的解析差异。
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Management-Key": api_key,
    }

    log(
        f"[CPA] 上传目标: {upload_url}  "
        f"文件名={filename}  内容大小={len(file_content)}B  "
        f"含 access_token={'是' if token_data.get('access_token') else '否'}  "
        f"含 refresh_token={'是' if token_data.get('refresh_token') else '否'}  "
        f"含 id_token={'是' if token_data.get('id_token') else '否'}",
        "info",
    )

    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        mime = None
        try:
            log(f"[CPA] 第 {attempt}/{MAX_ATTEMPTS} 次 multipart 上传 {filename}...", "info")
            mime = CurlMime()
            mime.addpart(
                name="file",
                data=file_content,
                filename=filename,
                content_type="application/json",
            )
            resp = cffi.post(
                upload_url,
                multipart=mime,
                headers=headers,
                proxies=None,
                verify=False,
                timeout=timeout,
                impersonate=_IMPERSONATE,
            )
            # 详细日志：HTTP 状态 + 响应体
            try:
                body_preview = (resp.text or "")[:400]
            except Exception:
                body_preview = "(无法读取响应体)"
            log(
                f"[CPA] 服务器响应: HTTP {resp.status_code}  body={body_preview!r}",
                "info" if resp.status_code in (200, 201) else "warn",
            )
            if resp.status_code in (200, 201):
                log(f"[CPA] ✅ 上传成功 {filename}", "ok")
                return {"ok": True, "email": email, "file_name": filename,
                        "message": f"CPA 上传成功: {filename}"}
            msg = f"HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = str(detail.get("message") or detail.get("error") or detail.get("detail") or msg)
            except Exception:
                msg = f"{msg}: {body_preview}"
            last_err = msg
            # 失败时也打详细日志（即使 4xx 不重试，也要让主人看到原因）
            log(f"[CPA] ❌ 上传失败: {msg}", "error")
            if attempt < MAX_ATTEMPTS and resp.status_code >= 500:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(f"[CPA] 第 {attempt} 次失败 ({msg})，{delay:.0f}s 后重试", "warn")
                time.sleep(delay)
                continue
            return {"ok": False, "error": msg, "email": email, "file_name": filename}
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(f"[CPA] 第 {attempt} 次异常 ({e})，{delay:.0f}s 后重试", "warn")
                time.sleep(delay)
                continue
            return {"ok": False, "error": str(e), "email": email, "file_name": filename}
        finally:
            if mime is not None:
                try:
                    mime.close()
                except Exception:
                    pass
    return {"ok": False, "error": last_err or "重试耗尽", "email": email, "file_name": filename}


# ──────────────────────── SUB2API：构建 payload ────────────────────────


def build_sub2api_payload(
    cred: dict,
    group_ids: list[int],
    *,
    default_model: str = DEFAULT_SUB2API_MODEL,
    supported_models: Any = None,
    concurrency: int = DEFAULT_SUB2API_CONCURRENCY,
    fingerprint_mode: str = DEFAULT_SUB2API_FINGERPRINT_MODE,
) -> dict:
    """构建 Hub batch API 中的一条 OAuth account。

    严格对齐 any-auto-register/sub2api_upload.py:_build_sub2api_account_payload。
    """
    access_token = str(cred.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("未读取到可导入的 access_token")
    refresh_token = str(cred.get("refresh_token") or "").strip()
    id_token = str(cred.get("id_token") or "").strip()
    email = str(cred.get("email") or "").strip()
    # Hub 的展示名称可由调用方覆盖（例如重授权后的账号），凭证里的
    # email 始终保留真实邮箱，避免影响账号识别和后续授权。
    display_name = str(cred.get("name") or email).strip() or email

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _get_auth(access_payload)

    expires_at = access_payload.get("exp")
    if not isinstance(expires_at, int) or expires_at <= 0:
        expires_at = int(time.time()) + SUB2API_DEFAULT_EXPIRES_IN

    # organization_id 优先从 id_token 抽（更准），fallback 从 access_token
    id_auth = _get_auth(_decode_jwt_payload(id_token))
    organization_id = str(id_auth.get("organization_id") or "").strip()
    if not organization_id:
        orgs = id_auth.get("organizations") or []
        if isinstance(orgs, list):
            for o in orgs:
                if isinstance(o, dict):
                    organization_id = str(o.get("id") or "").strip()
                    if organization_id:
                        break
    if not organization_id:
        organization_id = str(access_auth.get("organization_id") or access_auth.get("poid") or "").strip()

    client_id = str(
        cred.get("client_id") or access_payload.get("client_id") or CODEX_CLIENT_ID
    ).strip() or CODEX_CLIENT_ID

    # Team 轮转必须把母号 workspace_id 写进 Hub。调用方传入的 account_id
    # 优先级高于 token 中仍可能指向个人空间的 chatgpt_account_id。
    chatgpt_account_id = str(
        cred.get("account_id") or access_auth.get("chatgpt_account_id") or ""
    ).strip()
    chatgpt_user_id = str(
        cred.get("user_id") or access_auth.get("chatgpt_user_id")
        or access_auth.get("user_id") or access_payload.get("sub") or ""
    ).strip()
    plan_type = str(
        cred.get("plan_type") or access_auth.get("chatgpt_plan_type") or "free"
    ).strip() or "free"
    model = str(default_model or DEFAULT_SUB2API_MODEL).strip() or DEFAULT_SUB2API_MODEL
    models = (
        parse_sub2api_models(None)
        if supported_models is None
        else parse_sub2api_models(supported_models, fallback=())
    )
    if model not in models:
        models.append(model)
    model_mapping = {item: item for item in models}
    model_mapping["codex-main"] = model

    account = {
        "name": display_name,
        "notes": str(cred.get("notes") or "批量导入"),
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": _utc_iso(expires_at),
            "id_token": id_token,
            "email": email,
            "chatgpt_account_id": chatgpt_account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "organization_id": organization_id,
            "plan_type": plan_type,
            "client_id": client_id,
            "model_mapping": model_mapping,
        },
        "proxy_id": None,
        "group_ids": list(group_ids) if group_ids else list(DEFAULT_SUB2API_GROUP_IDS),
        "concurrency": _positive_int(concurrency, DEFAULT_SUB2API_CONCURRENCY),
        "priority": 0,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }
    mode = str(fingerprint_mode or DEFAULT_SUB2API_FINGERPRINT_MODE).strip().lower()
    if mode not in SUB2API_FINGERPRINT_MODES:
        mode = DEFAULT_SUB2API_FINGERPRINT_MODE
    extra: dict[str, Any] = {}
    if mode != "off":
        extra["codex_fingerprint_mode"] = mode
    if plan_type.lower() == "team" and chatgpt_account_id:
        extra.update({
            "openai_team_rotation_managed": True,
            "openai_team_workspace_id": chatgpt_account_id,
            "openai_team_plan_type": "team",
        })
    if extra:
        account["extra"] = extra
    return account


# ──────────────────────── SUB2API：上传 ────────────────────────


def export_to_sub2api(cred: dict, cfg: dict, *,
                        log_fn: Optional[Callable[[str, str], None]] = None,
                        existing_account_id: Any = None) -> dict:
    """SUB2API x-api-key 直连上传（无登录流程）。"""
    log = log_fn or (lambda m, lvl="info": logger.info(m))

    api_url = (cfg.get("sub2api_url") or "").rstrip("/").strip()
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    if not api_url:
        raise RuntimeError("SUB2API 未配置 URL")
    if not api_key:
        raise RuntimeError("SUB2API 未配置 API Key")

    group_ids = _parse_group_ids(cfg.get("sub2api_group_ids"))
    default_model = str(
        cfg.get("sub2api_default_model") or DEFAULT_SUB2API_MODEL
    ).strip() or DEFAULT_SUB2API_MODEL
    supported_models = (
        parse_sub2api_models(cfg.get("sub2api_models"), fallback=())
        if "sub2api_models" in cfg
        else parse_sub2api_models(None)
    )
    concurrency = _positive_int(
        cfg.get("sub2api_concurrency"), DEFAULT_SUB2API_CONCURRENCY
    )
    fingerprint_mode = str(
        cfg.get("sub2api_fingerprint_mode") or DEFAULT_SUB2API_FINGERPRINT_MODE
    ).strip().lower()
    if fingerprint_mode not in SUB2API_FINGERPRINT_MODES:
        fingerprint_mode = DEFAULT_SUB2API_FINGERPRINT_MODE
    timeout = int(cfg.get("sub2api_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()

    account_payload = build_sub2api_payload(
        cred,
        group_ids,
        default_model=default_model,
        supported_models=supported_models,
        concurrency=concurrency,
        fingerprint_mode=fingerprint_mode,
    )
    payload = {"accounts": [account_payload]}
    email = account_payload.get("name") or "unknown"
    url = f"{api_url}/api/v1/admin/accounts/batch"
    access_token_fingerprint = hashlib.sha256(
        account_payload["credentials"]["access_token"].encode("utf-8")
    ).hexdigest()[:16]
    idempotency_seed = (
        f"{email}\0{account_payload['credentials'].get('chatgpt_account_id', '')}"
        f"\0{access_token_fingerprint}\0{fingerprint_mode}"
    )
    idempotency_key = "openai-oauth-import-" + hashlib.sha256(
        idempotency_seed.encode("utf-8")
    ).hexdigest()[:24]
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{api_url}/admin/accounts",
        "x-api-key": api_key,
        "Idempotency-Key": idempotency_key,
    }

    existing_id = str(existing_account_id or "").strip()
    if existing_id:
        update_url = f"{api_url}/api/v1/admin/accounts/{quote(existing_id, safe='')}"
        update_payload = {
            key: value for key, value in account_payload.items()
            if key != "platform"
        }
        update_payload["status"] = "active"
        try:
            log(f"[SUB2API] 更新现有账号 #{existing_id} {email}...", "info")
            response = cffi.put(
                update_url,
                headers=headers,
                json=update_payload,
                proxies=None,
                verify=False,
                timeout=timeout,
                impersonate=_IMPERSONATE,
            )
            if response.status_code in (200, 201):
                for path, body in (
                    (f"/api/v1/admin/accounts/{quote(existing_id, safe='')}/recover-state", {}),
                    (f"/api/v1/admin/accounts/{quote(existing_id, safe='')}/schedulable", {"schedulable": True}),
                ):
                    recovery = cffi.post(
                        f"{api_url}{path}",
                        headers=headers,
                        json=body,
                        proxies=None,
                        verify=False,
                        timeout=timeout,
                        impersonate=_IMPERSONATE,
                    )
                    if not 200 <= recovery.status_code < 300:
                        raise RuntimeError(
                            f"更新账号 #{existing_id} 后恢复状态失败 HTTP {recovery.status_code}"
                        )
                log(f"[SUB2API] ✅ 已更新现有账号 #{existing_id} {email}", "ok")
                return {
                    "ok": True,
                    "email": email,
                    "account_id": existing_id,
                    "updated": True,
                    "message": f"SUB2API 账号已更新 #{existing_id}",
                }
            if response.status_code != 404:
                body_preview = str(getattr(response, "text", "") or "")[:300]
                return {
                    "ok": False,
                    "email": email,
                    "error": f"更新 SUB2API 账号 #{existing_id} 失败 HTTP {response.status_code}: {body_preview}",
                }
            log(f"[SUB2API] 账号 #{existing_id} 已不存在，回退创建新账号", "warn")
        except Exception as exc:
            return {"ok": False, "email": email, "error": str(exc)}

    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log(
                f"[SUB2API] 第 {attempt}/{MAX_ATTEMPTS} 次上传 {email} "
                f"(group_ids={group_ids}, default_model={default_model}, "
                f"models={len(supported_models)}, concurrency={concurrency}, "
                f"fingerprint={fingerprint_mode})...",
                "info",
            )
            resp = cffi.post(
                url,
                headers=headers,
                json=payload,
                proxies=None,
                verify=False,
                timeout=timeout,
                impersonate=_IMPERSONATE,
            )
            if resp.status_code in (200, 201):
                new_id = ""
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        new_id = str(data.get("id") or data.get("ID") or "").strip()
                        candidates = data.get("accounts") or data.get("results")
                        nested = data.get("data")
                        if isinstance(nested, dict) and not candidates:
                            candidates = nested.get("accounts") or nested.get("results")
                        if not new_id and isinstance(candidates, list):
                            for item in candidates:
                                if not isinstance(item, dict) or item.get("success") is False:
                                    continue
                                new_id = str(item.get("id") or item.get("ID") or "").strip()
                                if new_id:
                                    break
                except Exception:
                    pass
                log(f"[SUB2API] ✅ 上传成功 {email} (id={new_id or 'unknown'})", "ok")
                return {"ok": True, "email": email, "account_id": new_id,
                        "idempotency_key": idempotency_key,
                        "message": f"SUB2API 批量上传成功 #{new_id or 'unknown'}"}
            msg = f"HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = str(
                        detail.get("message") or detail.get("msg")
                        or detail.get("error") or msg
                    )
            except Exception:
                msg = f"{msg} - {(resp.text or '')[:200]}"
            last_err = msg
            if attempt < MAX_ATTEMPTS and resp.status_code >= 500:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(f"[SUB2API] 第 {attempt} 次失败 ({msg})，{delay:.0f}s 后重试", "warn")
                time.sleep(delay)
                continue
            return {"ok": False, "error": msg, "email": email}
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(f"[SUB2API] 第 {attempt} 次异常 ({e})，{delay:.0f}s 后重试", "warn")
                time.sleep(delay)
                continue
            return {"ok": False, "error": str(e), "email": email}
    return {"ok": False, "error": last_err or "重试耗尽", "email": email}


# ──────────────────────── 连通性测试 ────────────────────────


def get_sub2api_groups(cfg: dict) -> dict:
    """读取 Hub 中可用于 OpenAI 账号的 active 分组。"""
    api_url = (cfg.get("sub2api_url") or "").rstrip("/").strip()
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    if not api_url:
        raise RuntimeError("SUB2API 未配置 URL")
    if not api_key:
        raise RuntimeError("SUB2API 未配置 API Key")

    timeout = int(cfg.get("sub2api_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()
    url = f"{api_url}/api/v1/admin/groups/all?platform=openai"
    resp = cffi.get(
        url,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{api_url}/admin/groups",
            "x-api-key": api_key,
        },
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate=_IMPERSONATE,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(f"SUB2API 鉴权失败 (HTTP {resp.status_code})，请检查 API Key")
    if resp.status_code != 200:
        raise RuntimeError(f"SUB2API 分组接口返回 HTTP {resp.status_code}: {(resp.text or '')[:200]}")

    try:
        body = resp.json()
    except Exception as exc:
        raise RuntimeError("SUB2API 分组接口返回非 JSON") from exc
    if not isinstance(body, dict):
        raise RuntimeError("SUB2API 分组接口响应格式错误")
    if body.get("code") not in (None, 0):
        raise RuntimeError(str(body.get("message") or "SUB2API 分组接口返回失败"))

    groups: list[dict] = []
    for item in body.get("data") or []:
        if not isinstance(item, dict):
            continue
        platform = str(item.get("platform") or "").strip().lower()
        status = str(item.get("status") or "").strip().lower()
        if platform != "openai" or status != "active":
            continue
        try:
            group_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        groups.append({
            "id": group_id,
            "name": str(item.get("name") or f"Group {group_id}").strip(),
            "platform": "openai",
            "status": "active",
        })
    return {
        "ok": True,
        "groups": groups,
        "message": f"获取到 {len(groups)} 个可用 OpenAI 分组",
    }


def _sub2api_admin_get(cfg: dict, path: str) -> tuple[int, Any]:
    """调用 Sub2API 管理 GET 接口，统一处理认证和 JSON 响应。"""
    api_url = (cfg.get("sub2api_url") or "").rstrip("/").strip()
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    if not api_url:
        raise RuntimeError("SUB2API 未配置 URL")
    if not api_key:
        raise RuntimeError("SUB2API 未配置 API Key")
    timeout = int(cfg.get("sub2api_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()
    resp = cffi.get(
        f"{api_url}{path}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{api_url}/admin/accounts",
            "x-api-key": api_key,
        },
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate=_IMPERSONATE,
    )
    try:
        payload = resp.json()
    except Exception:
        payload = str(getattr(resp, "text", "") or "")[:1000]
    return int(resp.status_code), payload


def _sub2api_payload_data(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _sub2api_future(value: Any, now: Optional[float] = None) -> bool:
    """识别 Unix 秒、ISO 时间和数字字符串形式的未来时间。"""
    if value is None or value == "":
        return False
    now = time.time() if now is None else now
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) > now
        text = str(value).strip()
        if not text:
            return False
        try:
            return float(text) > now
        except ValueError:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp() > now
    except (TypeError, ValueError, OverflowError):
        return False


def _sub2api_timestamp(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _sub2api_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sub2api_codex_windows(extra: Any, now: float) -> dict:
    """Read cached 5h/7d usage captured by Sub2API from real model traffic."""
    extra = extra if isinstance(extra, dict) else {}
    out = {
        "five_used": _sub2api_number(extra.get("codex_5h_used_percent")),
        "five_reset": _sub2api_timestamp(extra.get("codex_5h_reset_at")),
        "seven_used": _sub2api_number(extra.get("codex_7d_used_percent")),
        "seven_reset": _sub2api_timestamp(extra.get("codex_7d_reset_at")),
    }
    for prefix in ("primary", "secondary"):
        used = _sub2api_number(extra.get(f"codex_{prefix}_used_percent"))
        window = _sub2api_number(extra.get(f"codex_{prefix}_window_minutes"))
        reset = _sub2api_timestamp(extra.get(f"codex_{prefix}_reset_at"))
        if reset is None:
            after = _sub2api_number(extra.get(f"codex_{prefix}_reset_after_seconds"))
            reset = now + max(0.0, after) if after is not None else None
        if used is None or window is None:
            continue
        if window >= 1440 and out["seven_used"] is None:
            out["seven_used"], out["seven_reset"] = used, reset
        elif window < 1440 and out["five_used"] is None:
            out["five_used"], out["five_reset"] = used, reset
    return out


def _sub2api_model_limit_active(extra: Any, now: float) -> bool:
    """兼容 extra.model_rate_limits 的不同版本结构。"""
    if not isinstance(extra, (dict, list)):
        return False
    if isinstance(extra, list):
        return any(_sub2api_model_limit_active(item, now) for item in extra)
    for key, value in extra.items():
        key_text = str(key).lower()
        if key_text in {"reset_at", "resets_at", "reset_time", "until", "until_unix"}:
            if _sub2api_future(value, now):
                return True
        if isinstance(value, (dict, list)) and _sub2api_model_limit_active(value, now):
            return True
    return False


def get_sub2api_account_status(
    cfg: dict,
    account_id: Any,
    *,
    expected_workspace_id: str = "",
) -> dict:
    """读取一个 Hub 账号的调度状态，不调用 ChatGPT 余额接口。

    返回 classification：healthy、short_rate_limited、weekly_exhausted、
    auth_required、error、missing 或 hub_error。
    """
    account_id_text = str(account_id or "").strip()
    if not account_id_text:
        return {"ok": False, "classification": "missing", "error": "缺少 Sub2API account_id"}
    status, payload = _sub2api_admin_get(
        cfg, f"/api/v1/admin/accounts/{quote(account_id_text, safe='')}"
    )
    if status in (401, 403):
        return {
            "ok": False,
            "classification": "hub_error",
            "http_status": status,
            "error": f"Sub2API 管理 API 鉴权失败 (HTTP {status})",
        }
    if status == 404:
        return {
            "ok": False,
            "classification": "missing",
            "http_status": status,
            "error": "Sub2API 账号不存在",
        }
    if not 200 <= status < 300:
        return {
            "ok": False,
            "classification": "hub_error",
            "http_status": status,
            "error": f"Sub2API 账号状态接口返回 HTTP {status}",
        }
    data = _sub2api_payload_data(payload)
    if not data:
        return {"ok": False, "classification": "hub_error", "http_status": status, "error": "Sub2API 账号状态响应为空"}

    now = time.time()
    expected_workspace = str(expected_workspace_id or "").strip()
    credentials = data.get("credentials") if isinstance(data.get("credentials"), dict) else {}
    if expected_workspace:
        actual_workspace = str(credentials.get("chatgpt_account_id") or "").strip()
        actual_plan = str(credentials.get("plan_type") or "").strip().lower()
        if actual_workspace != expected_workspace or actual_plan != "team":
            return {
                "ok": True,
                "classification": "team_mismatch",
                "http_status": status,
                "account": data,
                "error": (
                    "Sub2API Team 路由被覆盖: "
                    f"plan_type={actual_plan or 'missing'}, "
                    f"workspace={actual_workspace or 'missing'}"
                ),
            }
    account_status = str(data.get("status") or "").strip().lower()
    error_message = str(data.get("error_message") or data.get("error") or "").strip()
    if account_status == "error":
        return {
            "ok": True,
            "classification": "auth_required",
            "http_status": status,
            "account": data,
            "error": error_message or "Sub2API 账号处于 error 状态",
        }

    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}
    windows = _sub2api_codex_windows(extra, now)
    visibly_limited = (
        account_status in {"rate_limited", "limited", "overloaded", "quota_exhausted"}
        or data.get("schedulable") is False
        or _sub2api_future(data.get("rate_limit_reset_at"), now)
    )
    seven_active = windows["seven_reset"] is None or windows["seven_reset"] > now
    five_active = windows["five_reset"] is None or windows["five_reset"] > now
    if (
        windows["seven_used"] is not None
        and windows["seven_used"] >= 100
        and seven_active
        and visibly_limited
    ):
        return {
            "ok": True,
            "classification": "weekly_exhausted",
            "http_status": status,
            "account": data,
            "reset_at": windows["seven_reset"],
            "error": f"Sub2API 账号 7d 额度达到 {windows['seven_used']:g}%",
        }
    if (
        windows["five_used"] is not None
        and windows["five_used"] >= 100
        and five_active
        and visibly_limited
    ):
        reset_at = windows["five_reset"] or _sub2api_timestamp(data.get("rate_limit_reset_at"))
        return {
            "ok": True,
            "classification": "short_rate_limited",
            "http_status": status,
            "account": data,
            "reset_at": reset_at,
            "error": f"Sub2API 账号 5h 额度达到 {windows['five_used']:g}%",
        }

    if account_status in {"rate_limited", "limited", "overloaded", "quota_exhausted"}:
        return {
            "ok": True,
            "classification": "short_rate_limited",
            "http_status": status,
            "account": data,
            "reset_at": _sub2api_timestamp(data.get("rate_limit_reset_at")),
            "error": error_message or f"Sub2API 账号临时限流状态为 {account_status}",
        }

    if _sub2api_future(data.get("rate_limit_reset_at"), now):
        return {
            "ok": True,
            "classification": "short_rate_limited",
            "http_status": status,
            "account": data,
            "reset_at": _sub2api_timestamp(data.get("rate_limit_reset_at")),
            "error": f"Sub2API 账号限流，重置时间 {data.get('rate_limit_reset_at')}",
        }

    if _sub2api_future(data.get("overload_until"), now):
        return {
            "ok": True,
            "classification": "short_rate_limited",
            "http_status": status,
            "account": data,
            "reset_at": _sub2api_timestamp(data.get("overload_until")),
            "error": f"Sub2API 账号处于过载冷却，结束时间 {data.get('overload_until')}",
        }

    if _sub2api_model_limit_active(extra.get("model_rate_limits"), now):
        return {
            "ok": True,
            "classification": "short_rate_limited",
            "http_status": status,
            "account": data,
            "error": "Sub2API 账号存在未重置的模型限流",
        }

    temp_until = data.get("temp_unschedulable_until")
    if _sub2api_future(temp_until, now):
        reason = str(data.get("temp_unschedulable_reason") or error_message).strip()
        lower_reason = reason.lower()
        auth_markers = ("401", "unauthorized", "token", "session has ended", "invalidated", "refresh")
        rate_markers = ("rate limit", "rate_limited", "rate-limit", "限流", "overload", "quota")
        return {
            "ok": True,
            "classification": (
                "short_rate_limited"
                if any(marker in lower_reason for marker in rate_markers)
                else "auth_required"
                if any(marker in lower_reason for marker in auth_markers)
                else "error"
            ),
            "http_status": status,
            "account": data,
            "reset_at": _sub2api_timestamp(temp_until),
            "error": reason or "Sub2API 账号临时停调",
        }

    if account_status != "active" or data.get("schedulable") is False:
        return {
            "ok": True,
            "classification": "inactive",
            "http_status": status,
            "account": data,
            "error": error_message or f"Sub2API 账号不可调度 (status={account_status or 'unknown'})",
        }

    return {"ok": True, "classification": "healthy", "http_status": status, "account": data, "error": ""}


def test_cpa(cfg: dict) -> dict:
    """CPA 连通性测试：GET /v0/management/auth-files 真校验 Bearer key。

    用 GET 而不是 OPTIONS，因为 OPTIONS 是 CORS 预检，多数 CPA 实现不校验 Authorization，
    会让 key 错误的配置误以为通了，到真上传时才返 401。
    """
    api_url = (cfg.get("cpa_url") or "").rstrip("/").strip()
    api_key = (cfg.get("cpa_mgmt_key") or "").strip()
    if not api_url:
        raise RuntimeError("CPA 未配置 URL")
    if not api_key:
        raise RuntimeError("CPA 未配置管理密钥")
    timeout = int(cfg.get("cpa_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()

    resp = cffi.get(
        f"{api_url}/v0/management/auth-files",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Management-Key": api_key,
        },
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate=_IMPERSONATE,
    )
    if resp.status_code in (200, 201, 204):
        return {"ok": True, "message": f"CPA 连通正常 + 密钥有效 (HTTP {resp.status_code})"}
    if resp.status_code in (401, 403):
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            pass
        raise RuntimeError(
            f"CPA 鉴权失败 (HTTP {resp.status_code})：管理密钥错误。响应：{body}"
        )
    # 405 Method Not Allowed 表示路径对但不允许 GET，至少 URL 通了
    if resp.status_code == 405:
        return {"ok": True, "message": f"CPA URL 可达（HTTP 405），但无法用 GET 验证密钥；请实际上传一次确认"}
    raise RuntimeError(f"CPA 返回 HTTP {resp.status_code}: {(resp.text or '')[:200]}")


def test_sub2api(cfg: dict) -> dict:
    """SUB2API 连通性测试：GET 一个无害端点（用 admin/accounts list 验证 key）。"""
    api_url = (cfg.get("sub2api_url") or "").rstrip("/").strip()
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    if not api_url:
        raise RuntimeError("SUB2API 未配置 URL")
    if not api_key:
        raise RuntimeError("SUB2API 未配置 API Key")
    timeout = int(cfg.get("sub2api_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()

    resp = cffi.get(
        f"{api_url}/api/v1/admin/accounts",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{api_url}/admin/accounts",
            "x-api-key": api_key,
        },
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate=_IMPERSONATE,
    )
    if resp.status_code in (200, 201):
        return {"ok": True, "message": f"SUB2API 连通正常 (HTTP {resp.status_code})"}
    if resp.status_code in (401, 403):
        raise RuntimeError(f"SUB2API 鉴权失败 (HTTP {resp.status_code})，请检查 API Key")
    raise RuntimeError(f"SUB2API 返回 HTTP {resp.status_code}: {(resp.text or '')[:200]}")


# ──────────────────────── 统一入口（注册完成后调用） ────────────────────────


def run_exports(cred: dict, *,
                  cpa_cfg: Optional[dict] = None,
                  sub2api_cfg: Optional[dict] = None,
                  log_fn: Optional[Callable[[str, str], None]] = None,
                  token_update_fn: Optional[Callable[[dict], None]] = None,
                  sub2api_account_id: Any = None) -> dict:
    """注册完成后的可选导出入口。

    步骤：
      1. 检查两个目标是否有任一启用，全部未启用直接返回
      2. 用 cred['refresh_token'] 刷新一次拿新的 Codex access_token / id_token
         （主项目最终保存的 access_token 是 NextAuth 风格的，CPA/SUB2API 不接受）
      3. 用刷新后的 cred 走 CPA / SUB2API 导出

    返回：
        {"cpa": {...} 或 None, "sub2api": {...} 或 None, "any_attempted": bool}
    """
    log = log_fn or (lambda m, lvl="info": logger.info(m))
    out: dict = {"cpa": None, "sub2api": None, "any_attempted": False}

    cpa_on = bool(cpa_cfg and cpa_cfg.get("enabled"))
    sub2_on = bool(sub2api_cfg and sub2api_cfg.get("enabled"))
    if not (cpa_on or sub2_on):
        return out

    # ─ 关键：先用 refresh_token 换 Codex 风格 access_token ─
    try:
        log("[exporter] 用 refresh_token 换新的 Codex access_token...", "info")
        fresh = refresh_codex_token(cred.get("refresh_token", ""))
        cred = {
            **cred,
            "access_token":  fresh["access_token"],
            "refresh_token": fresh.get("refresh_token") or cred.get("refresh_token"),
            "id_token":      fresh.get("id_token") or cred.get("id_token", ""),
        }
        if token_update_fn is not None:
            try:
                token_update_fn({
                    "email": cred.get("email") or "",
                    "refresh_token": cred.get("refresh_token") or "",
                    "id_token": cred.get("id_token") or "",
                })
            except Exception as update_exc:
                log(f"[exporter] Codex 滚动 Token 写回失败: {update_exc}", "warn")
        log(
            f"[exporter] ✅ Codex token 刷新成功 "
            f"(access_token len={len(fresh['access_token'])} "
            f"id_token len={len(fresh.get('id_token') or '')})",
            "ok",
        )
    except Exception as e:
        log(f"[exporter] ❌ Codex token 刷新失败，无法导出: {e}", "error")
        if cpa_on:
            out["any_attempted"] = True
            out["cpa"] = {"ok": False, "error": f"Codex token 刷新失败: {e}"}
        if sub2_on:
            out["any_attempted"] = True
            out["sub2api"] = {"ok": False, "error": f"Codex token 刷新失败: {e}"}
        return out

    if cpa_on:
        out["any_attempted"] = True
        try:
            out["cpa"] = export_to_cpa(cred, cpa_cfg, log_fn=log)
        except Exception as e:
            log(f"[CPA] 导出异常: {e}", "error")
            out["cpa"] = {"ok": False, "error": str(e)}

    if sub2_on:
        out["any_attempted"] = True
        try:
            out["sub2api"] = export_to_sub2api(
                cred,
                sub2api_cfg,
                log_fn=log,
                existing_account_id=sub2api_account_id,
            )
        except Exception as e:
            log(f"[SUB2API] 导出异常: {e}", "error")
            out["sub2api"] = {"ok": False, "error": str(e)}

    return out
