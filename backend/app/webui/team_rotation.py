"""ChatGPT Team member rotation built on top of the registered account pool."""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Optional
from urllib.parse import quote

from ..http_client import DEFAULT_IMPERSONATE, create_http_session, us_chrome_headers

from . import db


logger = logging.getLogger("team_rotation")
BASE_URL = os.getenv("CHATGPT_BASE_URL", "https://chatgpt.com").rstrip("/")


class TeamApiError(RuntimeError):
    pass


def _decode_jwt_payload(token: str) -> dict:
    try:
        part = str(token or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        return payload if isinstance(payload, dict) else {}
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _token_identity(token: str) -> dict:
    payload = _decode_jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    profile = payload.get("https://api.openai.com/profile")
    auth = auth if isinstance(auth, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    return {
        "account_id": str(auth.get("chatgpt_account_id") or "").strip(),
        "user_id": str(
            auth.get("chatgpt_user_id")
            or auth.get("user_id")
            or payload.get("sub")
            or ""
        ).strip(),
        "account_user_id": str(auth.get("chatgpt_account_user_id") or "").strip(),
        "email": str(profile.get("email") or "").strip().lower(),
    }


def _cookie_header(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(
            f"{key}={item}" for key, item in value.items() if item is not None and str(item)
        )
    text = str(value or "").strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    return text


def _cookie_value(header: str, name: str) -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(header or "")
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


@dataclass
class Credentials:
    access_token: str = ""
    cookie_header: str = ""
    account_id: str = ""
    user_id: str = ""
    email: str = ""
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def apply_token_identity(self) -> None:
        identity = _token_identity(self.access_token)
        self.account_id = self.account_id or identity["account_id"]
        self.user_id = self.user_id or identity["user_id"]
        self.email = self.email or identity["email"]


def parse_mother_session(raw: str, workspace_id: str = "") -> dict:
    """Parse session JSON, a JWT access token, or a Cookie header."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("母号 Session / Access Token 不能为空")

    data: dict[str, Any] = {}
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"母号 Session JSON 无法解析: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError("母号 Session 必须是 JSON 对象")
        data = value

    access_token = ""
    cookie_header = ""
    account_id = str(workspace_id or "").strip()
    user_id = ""
    email = ""
    if data:
        for key in ("accessToken", "access_token", "at", "token"):
            if data.get(key):
                access_token = str(data[key]).removeprefix("Bearer ").strip()
                break
        for key in ("cookie_header", "cookies", "cookie", "Cookie"):
            if data.get(key):
                cookie_header = _cookie_header(data[key])
                break
        if data.get("sessionToken"):
            session_token = str(data["sessionToken"]).strip()
            if session_token and not cookie_header:
                cookie_header = f"__Secure-next-auth.session-token={session_token}"
        for key in ("workspace_id", "account_id", "accountId", "chatgpt_account_id"):
            if data.get(key) and not account_id:
                account_id = str(data[key]).strip()
                break
        account = data.get("account") if isinstance(data.get("account"), dict) else {}
        account_id = account_id or str(account.get("id") or "").strip()
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        user_id = str(user.get("id") or data.get("user_id") or "").strip()
        email = str(user.get("email") or data.get("email") or "").strip().lower()
    elif text.count(".") == 2 and ";" not in text and "=" not in text:
        access_token = text.removeprefix("Bearer ").strip()
    else:
        cookie_header = _cookie_header(text)

    credentials = Credentials(access_token, cookie_header, account_id, user_id, email)
    credentials.apply_token_identity()
    if not credentials.access_token and not credentials.cookie_header:
        raise ValueError("母号 Session 中未识别到 Access Token 或 Cookie")
    if not credentials.account_id:
        raise ValueError("无法识别母号工作区 ID，请手动填写 workspace_id")
    return {
        "access_token": credentials.access_token,
        "cookie_header": credentials.cookie_header,
        "workspace_id": credentials.account_id,
        "owner_user_id": credentials.user_id,
        "email": credentials.email,
    }


def _seat_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _subscription_details(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    keys = {"seat_capacity", "seats_entitled", "seats_in_use"}
    if keys.intersection(payload):
        return payload
    for key in ("subscription", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict) and keys.intersection(nested):
            return nested
    return None


def _remaining_default_seats(payload: Any) -> Optional[int]:
    details = _subscription_details(payload)
    if not details:
        return None
    capacity = details.get("seat_capacity")
    entries = []
    if isinstance(capacity, list):
        entries = [item for item in capacity if isinstance(item, dict)]
    elif isinstance(capacity, dict):
        for seat_type, value in capacity.items():
            if isinstance(value, dict):
                entries.append({"type": seat_type, **value})
    for item in entries:
        if str(item.get("type") or "").lower() == "default":
            available = _seat_count(item.get("available"))
            if available is not None:
                return available
    entitled = _seat_count(details.get("seats_entitled"))
    in_use = _seat_count(details.get("seats_in_use"))
    if entitled is not None and in_use is not None:
        return max(0, entitled - in_use)
    return None


def _payload_items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "users", "members"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    nested = payload.get("data")
    return _payload_items(nested) if nested is not payload else []


def _payload_total(payload: Any) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    for item in (payload, payload.get("data")):
        if isinstance(item, dict):
            total = _seat_count(item.get("total"))
            if total is not None:
                return total
    return None


def _nested(item: dict, key: str) -> dict:
    value = item.get(key)
    return value if isinstance(value, dict) else {}


def _member_view(item: dict, owner_user_id: str = "", owner_email: str = "") -> dict:
    user = _nested(item, "user")
    profile = _nested(item, "profile")
    address = _nested(item, "email_address")
    member_id = str(
        item.get("user_id")
        or item.get("id")
        or user.get("id")
        or item.get("account_user_id")
        or ""
    ).strip()
    email = ""
    for value in (
        item.get("email"), item.get("email_address"), item.get("emailAddress"),
        address.get("address"), address.get("email"), user.get("email"),
        user.get("email_address"), profile.get("email"), profile.get("email_address"),
    ):
        if isinstance(value, str) and value.strip():
            email = value.strip().lower()
            break
    role = str(item.get("role") or item.get("workspace_role") or "").strip()
    return {
        "id": member_id,
        "email": email,
        "name": str(item.get("name") or user.get("name") or profile.get("name") or "").strip(),
        "role": role,
        "seat_type": str(item.get("seat_type") or item.get("seatType") or "").strip(),
        "is_owner": bool(owner_user_id and member_id == owner_user_id)
        or bool(owner_email and email == owner_email.lower())
        or role.lower() == "owner",
        "created_at": item.get("created_at") or item.get("created_time"),
    }


def _percent(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100 else None


class TeamApiClient:
    def __init__(self, proxy: str = ""):
        self.proxy = str(proxy or "").strip()
        chrome_headers = us_chrome_headers()
        self.session = create_http_session(
            proxy=self.proxy or None,
            impersonate=DEFAULT_IMPERSONATE,
            user_agent=chrome_headers["User-Agent"],
            accept_language=chrome_headers["Accept-Language"],
            client_hints={key: value for key, value in chrome_headers.items() if key.startswith("sec-ch-ua")},
        )
        self.chrome_headers = chrome_headers

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def request(
        self,
        method: str,
        path: str,
        credentials: Credentials,
        *,
        account_id: str = "",
        referer: str = "/",
        json_body: Any = None,
        empty_body: bool = False,
        retries: int = 2,
    ) -> tuple[int, Any]:
        headers = {
            **self.chrome_headers,
            "Accept": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}{referer}",
            "oai-language": "zh-CN",
        }
        if credentials.access_token:
            headers["Authorization"] = f"Bearer {credentials.access_token}"
        if credentials.cookie_header:
            headers["Cookie"] = credentials.cookie_header
        if account_id:
            headers["chatgpt-account-id"] = account_id
        if credentials.device_id:
            headers["oai-device-id"] = credentials.device_id
        if credentials.session_id:
            headers["oai-session-id"] = credentials.session_id
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        kwargs: dict[str, Any] = {"headers": headers, "timeout": 30}
        if json_body is not None:
            kwargs["json"] = json_body
        elif empty_body:
            kwargs["data"] = b""

        last_error: Optional[Exception] = None
        for attempt in range(1, max(1, retries) + 1):
            try:
                response = self.session.request(method, f"{BASE_URL}{path}", **kwargs)
                try:
                    payload = response.json()
                except (ValueError, TypeError):
                    payload = str(getattr(response, "text", "") or "")[:4000]
                if response.status_code not in {500, 502, 503, 504} or attempt >= retries:
                    return int(response.status_code), payload
            except Exception as exc:
                last_error = exc
                if attempt >= retries:
                    break
            time.sleep(min(0.8 * attempt, 2.0))
        raise TeamApiError(f"请求 {path} 失败: {type(last_error).__name__ if last_error else 'network'}")


class TeamService:
    def __init__(self, proxy: str = ""):
        self.client = TeamApiClient(proxy)
        self._mother_credentials: dict[str, Credentials] = {}

    def close(self) -> None:
        self.client.close()

    def _credentials_from_mother(self, mother: dict) -> Credentials:
        mother_id = str(mother.get("id") or mother.get("workspace_id") or "")
        cached = self._mother_credentials.get(mother_id)
        if cached:
            return cached
        credentials = Credentials(
            access_token=str(mother.get("access_token") or ""),
            cookie_header=str(mother.get("cookie_header") or ""),
            account_id=str(mother.get("workspace_id") or ""),
            user_id=str(mother.get("owner_user_id") or ""),
            email=str(mother.get("email") or ""),
        )
        credentials.apply_token_identity()
        self._resolve_access_token(credentials, "母号")
        self._mother_credentials[mother_id] = credentials
        return credentials

    def _credentials_from_registered(self, account: dict) -> Credentials:
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        cookie_header = str(account.get("cookie_header") or "")
        if not cookie_header and account.get("session_token"):
            cookie_header = f"__Secure-next-auth.session-token={account['session_token']}"
        credentials = Credentials(
            access_token=str(account.get("access_token") or ""),
            cookie_header=cookie_header,
            account_id=str(extra.get("account_id") or account.get("account_id") or ""),
            user_id=str(extra.get("user_id") or account.get("user_id") or ""),
            email=str(account.get("email") or "").lower(),
        )
        credentials.apply_token_identity()
        return credentials

    def _resolve_access_token(self, credentials: Credentials, label: str) -> None:
        if credentials.access_token:
            credentials.apply_token_identity()
            return
        if not credentials.cookie_header:
            raise TeamApiError(f"{label}缺少 Access Token")
        status, payload = self.client.request(
            "GET", "/api/auth/session", credentials, retries=2
        )
        if not 200 <= status < 300 or not isinstance(payload, dict):
            raise TeamApiError(f"{label} Session 换取 Access Token 失败: HTTP {status}")
        token = str(payload.get("accessToken") or payload.get("access_token") or "").strip()
        if not token:
            raise TeamApiError(f"{label} Session 响应中没有 Access Token")
        credentials.access_token = token
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        credentials.user_id = credentials.user_id or str(user.get("id") or "")
        credentials.email = credentials.email or str(user.get("email") or "").lower()
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        credentials.account_id = credentials.account_id or str(account.get("id") or "")
        credentials.apply_token_identity()

    @staticmethod
    def _require(status: int, payload: Any, action: str, allow_404: bool = False) -> None:
        if allow_404 and status == 404:
            return
        if not 200 <= status < 300:
            preview = json.dumps(payload, ensure_ascii=False)[:500] if isinstance(payload, (dict, list)) else str(payload)[:500]
            raise TeamApiError(f"{action}失败: HTTP {status}, {preview}")
        if isinstance(payload, dict) and payload.get("success") is False:
            raise TeamApiError(f"{action}失败: success=false")

    def _mother_request(self, mother: dict, method: str, path: str, **kwargs) -> tuple[int, Any]:
        credentials = self._credentials_from_mother(mother)
        status, payload = self.client.request(method, path, credentials, **kwargs)
        if status in {401, 403} and credentials.cookie_header:
            credentials.access_token = ""
            self._resolve_access_token(credentials, "母号")
            status, payload = self.client.request(method, path, credentials, **kwargs)
        return status, payload

    def get_team_detail(self, mother: dict) -> dict:
        workspace_id = str(mother.get("workspace_id") or "")
        if not workspace_id:
            raise TeamApiError("母号缺少 workspace_id")
        status, payload = self._mother_request(
            mother,
            "GET",
            f"/backend-api/subscriptions?account_id={quote(workspace_id, safe='')}",
            account_id=workspace_id,
            referer="/admin/billing",
        )
        self._require(status, payload, "查询母号席位")
        details = _subscription_details(payload)
        remaining = _remaining_default_seats(payload)
        if not details or remaining is None:
            raise TeamApiError("母号席位响应缺少可识别的数据")

        credentials = self._credentials_from_mother(mother)
        members: list[dict] = []
        offset = 0
        total = 0
        for _ in range(100):
            path = (
                f"/backend-api/accounts/{quote(workspace_id, safe='')}/users"
                f"?offset={offset}&limit=100&query="
            )
            member_status, member_payload = self._mother_request(
                mother,
                "GET",
                path,
                account_id=workspace_id,
                referer="/admin/members?tab=members",
            )
            self._require(member_status, member_payload, "查询 Team 成员")
            entries = _payload_items(member_payload)
            upstream_total = _payload_total(member_payload)
            if upstream_total is not None:
                total = upstream_total
            members.extend(
                view
                for view in (
                    _member_view(item, credentials.user_id, credentials.email)
                    for item in entries
                )
                if view["id"]
            )
            if not entries or len(entries) < 100 or (upstream_total is not None and len(members) >= upstream_total):
                break
            offset += len(entries)

        capacity = []
        raw_capacity = details.get("seat_capacity")
        if isinstance(raw_capacity, list):
            for item in raw_capacity:
                if isinstance(item, dict):
                    capacity.append({
                        "type": str(item.get("type") or ""),
                        "available": _seat_count(item.get("available")),
                        "paid": _seat_count(item.get("paid")),
                    })
        return {
            "seats": {
                "entitled": _seat_count(details.get("seats_entitled")),
                "in_use": _seat_count(details.get("seats_in_use")),
                "remaining_default": remaining,
                "capacity": capacity,
            },
            "members": members,
            "total_members": max(total, len(members)),
        }

    def invite_and_accept(self, mother: dict, account: dict) -> dict:
        workspace_id = str(mother.get("workspace_id") or "")
        child = self._credentials_from_registered(account)
        self._resolve_access_token(child, "子号")
        if not child.email:
            raise TeamApiError("子号缺少邮箱")
        status, payload = self._mother_request(
            mother,
            "POST",
            f"/backend-api/accounts/{quote(workspace_id, safe='')}/invites",
            account_id=workspace_id,
            referer="/admin/members?tab=members",
            json_body={
                "email_addresses": [child.email],
                "flow_id": str(uuid.uuid4()),
                "role": "standard-user",
                "seat_type": "default",
                "resend_emails": True,
                "submission_id": str(uuid.uuid4()),
            },
        )
        self._require(status, payload, "母号邀请子号")

        status, payload = self.client.request(
            "POST",
            f"/backend-api/accounts/{quote(workspace_id, safe='')}/invites/accept",
            child,
            account_id=child.account_id,
            empty_body=True,
            retries=2,
        )
        self._require(status, payload, "子号接受邀请")

        member_id = child.user_id
        for attempt in range(3):
            detail = self.get_team_detail(mother)
            match = next(
                (item for item in detail["members"] if item.get("email") == child.email),
                None,
            )
            if match:
                member_id = str(match.get("id") or member_id)
                break
            if attempt < 2:
                time.sleep(1)
        if not member_id:
            raise TeamApiError("子号已接受邀请，但无法确认 Team 成员 ID")
        return {"member_id": member_id, "email": child.email}

    def check_quota(self, account: dict, workspace_id: str) -> dict:
        credentials = self._credentials_from_registered(account)
        self._resolve_access_token(credentials, "子号")
        status, payload = self.client.request(
            "GET",
            "/backend-api/wham/usage",
            credentials,
            account_id=workspace_id,
            retries=2,
        )
        if status in {401, 403}:
            return {
                "status": "dead",
                "http_status": status,
                "primary_used_percent": None,
                "secondary_used_percent": None,
                "error": f"授权失效 (HTTP {status})",
            }
        if not 200 <= status < 300:
            return {
                "status": "unknown",
                "http_status": status,
                "primary_used_percent": None,
                "secondary_used_percent": None,
                "error": f"额度接口返回 HTTP {status}",
            }
        rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else None
        if not isinstance(rate_limit, dict):
            return {
                "status": "unknown",
                "http_status": status,
                "primary_used_percent": None,
                "secondary_used_percent": None,
                "error": "额度响应缺少 rate_limit",
            }
        primary = rate_limit.get("primary_window") if isinstance(rate_limit.get("primary_window"), dict) else {}
        secondary = rate_limit.get("secondary_window") if isinstance(rate_limit.get("secondary_window"), dict) else {}
        return {
            "status": "alive",
            "http_status": status,
            "primary_used_percent": _percent(primary.get("used_percent")),
            "secondary_used_percent": _percent(secondary.get("used_percent")),
            "error": "",
        }

    def remove_member(self, mother: dict, member_id: str) -> dict:
        credentials = self._credentials_from_mother(mother)
        if credentials.user_id and credentials.user_id == member_id:
            raise TeamApiError("不能移出母号本人")
        workspace_id = str(mother.get("workspace_id") or "")
        status, payload = self._mother_request(
            mother,
            "DELETE",
            f"/backend-api/accounts/{quote(workspace_id, safe='')}/users/{quote(member_id, safe='')}",
            account_id=workspace_id,
            referer="/admin/members?tab=members",
            empty_body=True,
            retries=3,
        )
        self._require(status, payload, "移出子号", allow_404=True)
        return {"removed": True, "already_absent": status == 404, "member_id": member_id}


class RotationState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


class TeamRotationController:
    def __init__(self, service_factory=TeamService):
        self._service_factory = service_factory
        self._lock = threading.RLock()
        self._state = RotationState.STOPPED
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._wake_event = threading.Event()
        self._mother_locks: dict[str, threading.Lock] = {}
        self._started_at = 0.0
        self._last_cycle_at = 0.0
        self._next_cycle_at = 0.0
        self._cycle_count = 0
        self._current_mother = ""
        self._last_error = ""
        self._options = self._load_options()

    @staticmethod
    def _load_options() -> dict:
        try:
            interval = max(10, int(float(db.get_setting("team_rotation_interval_seconds", "300"))))
        except (TypeError, ValueError):
            interval = 300
        try:
            threshold = min(100.0, max(1.0, float(db.get_setting("team_rotation_threshold", "100"))))
        except (TypeError, ValueError):
            threshold = 100.0
        return {
            "interval_seconds": interval,
            "quota_threshold": threshold,
            "proxy": db.get_setting("team_rotation_proxy", ""),
        }

    def _save_options(self, options: dict) -> dict:
        interval = max(10, int(options.get("interval_seconds") or 300))
        threshold = min(100.0, max(1.0, float(options.get("quota_threshold") or 100)))
        proxy = str(options.get("proxy") or "").strip()
        db.set_setting("team_rotation_interval_seconds", interval)
        db.set_setting("team_rotation_threshold", threshold)
        db.set_setting("team_rotation_proxy", proxy)
        self._options = {
            "interval_seconds": interval,
            "quota_threshold": threshold,
            "proxy": proxy,
        }
        return dict(self._options)

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in {RotationState.RUNNING, RotationState.PAUSED}:
                return {"ok": False, "error": f"Team 轮转已在运行 (state={self._state})"}
            if not db.list_team_mothers(enabled_only=True):
                return {"ok": False, "error": "请先添加并启用至少一个母号"}
            try:
                self._save_options(options or self._options)
            except (TypeError, ValueError):
                return {"ok": False, "error": "轮询间隔或额度阈值格式无效"}
            self._stop_event.clear()
            self._pause_event.clear()
            self._wake_event.clear()
            self._state = RotationState.RUNNING
            self._started_at = time.time()
            self._next_cycle_at = self._started_at
            self._last_error = ""
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="team-rotation"
            )
            self._thread.start()
        self._event("INFO", "control", "Team 轮转已启动")
        return {"ok": True, **self.snapshot()}

    def pause(self) -> dict:
        with self._lock:
            if self._state != RotationState.RUNNING:
                return {"ok": False, "error": f"当前 state={self._state}，不可暂停"}
            self._pause_event.set()
            self._state = RotationState.PAUSED
            self._wake_event.set()
        self._event("INFO", "control", "Team 轮转已暂停，当前请求完成后生效")
        return {"ok": True, **self.snapshot()}

    def resume(self) -> dict:
        with self._lock:
            if self._state != RotationState.PAUSED:
                return {"ok": False, "error": f"当前 state={self._state}，不可恢复"}
            self._pause_event.clear()
            self._state = RotationState.RUNNING
            self._next_cycle_at = time.time()
            self._wake_event.set()
        self._event("INFO", "control", "Team 轮转已恢复")
        return {"ok": True, **self.snapshot()}

    def stop(self) -> dict:
        with self._lock:
            if self._state == RotationState.STOPPED:
                return {"ok": False, "error": "Team 轮转未运行"}
            self._stop_event.set()
            self._pause_event.clear()
            self._wake_event.set()
        self._event("INFO", "control", "已请求停止 Team 轮转")
        return {"ok": True, **self.snapshot()}

    def trigger(self) -> dict:
        with self._lock:
            if self._state == RotationState.STOPPED:
                return {"ok": False, "error": "请先启动 Team 轮转"}
            if self._state == RotationState.PAUSED:
                return {"ok": False, "error": "轮转已暂停，请先恢复"}
            self._next_cycle_at = time.time()
            self._wake_event.set()
        return {"ok": True, **self.snapshot()}

    def snapshot(self) -> dict:
        with self._lock:
            options = dict(self._options)
            options["proxy_configured"] = bool(options.get("proxy"))
            return {
                "state": self._state,
                "started_at": self._started_at,
                "last_cycle_at": self._last_cycle_at,
                "next_cycle_at": self._next_cycle_at,
                "cycle_count": self._cycle_count,
                "current_mother": self._current_mother,
                "last_error": self._last_error,
                "config": options,
                "counts": db.team_rotation_counts(),
                "mothers": db.list_team_mothers(),
                "members": db.list_team_rotation_members(limit=500),
                "events": db.list_team_rotation_events(limit=100),
            }

    def inspect_mother(self, mother_id: str) -> dict:
        mother = db.get_team_mother(mother_id)
        if not mother:
            raise TeamApiError("母号不存在")
        with self._mother_lock(mother_id):
            service = self._service_factory(self._options.get("proxy", ""))
            try:
                detail = service.get_team_detail(mother)
            finally:
                service.close()
        seats = detail["seats"]
        db.record_team_mother_check(
            mother_id,
            entitled=seats.get("entitled"),
            in_use=seats.get("in_use"),
            remaining=seats.get("remaining_default"),
        )
        return detail

    def remove_member(self, mother_id: str, member_id: str) -> dict:
        mother = db.get_team_mother(mother_id)
        if not mother:
            raise TeamApiError("母号不存在")
        with self._mother_lock(mother_id):
            service = self._service_factory(self._options.get("proxy", ""))
            try:
                result = service.remove_member(mother, member_id)
            finally:
                service.close()
        for item in db.list_team_rotation_members(mother_id=mother_id, limit=5000):
            if str(item.get("member_id") or "") == member_id and item.get("status") in {"pending", "active"}:
                db.update_team_rotation_member(
                    item["id"], status="removed", removed_at=time.time(), error="管理员手动移出"
                )
                break
        self._event("INFO", "remove", f"管理员手动移出成员 {member_id}", mother_id)
        return result

    def _mother_lock(self, mother_id: str) -> threading.Lock:
        with self._lock:
            return self._mother_locks.setdefault(mother_id, threading.Lock())

    def _event(self, level: str, action: str, message: str, mother_id: str = "", email: str = "") -> None:
        db.add_team_rotation_event(level, action, message, mother_id, email)
        getattr(logger, level.lower(), logger.info)("[%s] %s", action, message)

    def _loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    self._wake_event.wait(0.5)
                    self._wake_event.clear()
                    continue
                self._run_cycle()
                if self._stop_event.is_set():
                    break
                with self._lock:
                    self._next_cycle_at = time.time() + self._options["interval_seconds"]
                while not self._stop_event.is_set() and not self._pause_event.is_set():
                    remaining = self._next_cycle_at - time.time()
                    if remaining <= 0:
                        break
                    if self._wake_event.wait(min(remaining, 1.0)):
                        self._wake_event.clear()
                        with self._lock:
                            self._next_cycle_at = time.time()
                        break
        finally:
            with self._lock:
                self._state = RotationState.STOPPED
                self._current_mother = ""
                self._next_cycle_at = 0.0
            self._event("INFO", "control", "Team 轮转已停止")

    def _run_cycle(self) -> None:
        with self._lock:
            self._last_cycle_at = time.time()
            self._cycle_count += 1
            self._last_error = ""
        mothers = db.list_team_mothers(include_secret=True, enabled_only=True)
        self._event("INFO", "cycle", f"开始第 {self._cycle_count} 轮，启用母号 {len(mothers)} 个")
        for mother in mothers:
            if self._stop_event.is_set() or self._pause_event.is_set():
                break
            mother_id = mother["id"]
            with self._lock:
                self._current_mother = mother.get("name") or mother_id
            try:
                with self._mother_lock(mother_id):
                    self._process_mother(mother)
            except Exception as exc:
                message = str(exc)[:1000]
                db.record_team_mother_check(mother_id, error=message)
                self._event("ERROR", "mother", message, mother_id)
                with self._lock:
                    self._last_error = message
        with self._lock:
            self._current_mother = ""

    def _process_mother(self, mother: dict) -> None:
        mother_id = mother["id"]
        service = self._service_factory(self._options.get("proxy", ""))
        try:
            detail = service.get_team_detail(mother)
            seats = detail["seats"]
            db.record_team_mother_check(
                mother_id,
                entitled=seats.get("entitled"),
                in_use=seats.get("in_use"),
                remaining=seats.get("remaining_default"),
            )
            upstream_by_email = {
                item["email"]: item for item in detail["members"] if item.get("email")
            }
            assignments = db.list_team_rotation_members(mother_id=mother_id, limit=5000)
            for assignment in assignments:
                upstream = upstream_by_email.get(str(assignment.get("email") or "").lower())
                if upstream and assignment.get("status") != "active":
                    db.update_team_rotation_member(
                        assignment["id"],
                        status="active",
                        member_id=upstream.get("id") or assignment.get("member_id"),
                        joined_at=assignment.get("joined_at") or time.time(),
                        error="",
                    )
                elif not upstream and assignment.get("status") == "active":
                    db.update_team_rotation_member(
                        assignment["id"], status="removed", removed_at=time.time(), error="成员已不在 Team"
                    )

            removed_any = False
            active_members = db.list_team_rotation_members(mother_id=mother_id, status="active", limit=5000)
            for assignment in active_members:
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                account = db.get_registered(assignment["email"])
                if not account:
                    self._remove_assignment(service, mother, assignment, "本地凭证已删除", "removed")
                    removed_any = True
                    continue
                quota = service.check_quota(account, mother["workspace_id"])
                checked_at = time.time()
                primary = quota.get("primary_used_percent")
                secondary = quota.get("secondary_used_percent")
                db.update_team_rotation_member(
                    assignment["id"],
                    primary_used_percent=primary,
                    secondary_used_percent=secondary,
                    last_checked_at=checked_at,
                    error=quota.get("error") or "",
                )
                values = [value for value in (primary, secondary) if value is not None]
                exhausted = bool(values and max(values) >= self._options["quota_threshold"])
                if quota.get("status") == "dead" or exhausted:
                    reason = quota.get("error") or f"额度达到 {max(values):g}%"
                    self._remove_assignment(service, mother, assignment, reason, "exhausted")
                    removed_any = True

            if removed_any:
                detail = service.get_team_detail(mother)
                seats = detail["seats"]

            remaining = int(seats.get("remaining_default") or 0)
            consecutive_join_failures = 0
            while remaining > 0 and not self._stop_event.is_set() and not self._pause_event.is_set():
                claim = db.claim_team_rotation_candidate(mother_id)
                if not claim:
                    self._event("WARNING", "pool", "注册成功账号池已无可用 Access Token", mother_id)
                    break
                account = db.get_registered(claim["email"])
                if not account:
                    db.update_team_rotation_member(claim["id"], status="failed", error="账号凭证不存在")
                    continue
                try:
                    joined = service.invite_and_accept(mother, account)
                except Exception as exc:
                    db.update_team_rotation_member(claim["id"], status="failed", error=str(exc)[:1000])
                    self._event("ERROR", "join", str(exc), mother_id, claim["email"])
                    consecutive_join_failures += 1
                    if consecutive_join_failures >= 3:
                        self._event(
                            "ERROR",
                            "join",
                            "连续 3 个子号加入失败，本轮停止补位",
                            mother_id,
                        )
                        break
                    continue
                consecutive_join_failures = 0
                db.update_team_rotation_member(
                    claim["id"],
                    status="active",
                    member_id=joined["member_id"],
                    joined_at=time.time(),
                    error="",
                )
                self._event("INFO", "join", "子号已加入 Team", mother_id, claim["email"])
                remaining -= 1

            # 加入 Team 成功后再推 Hub。历史 active 成员和上轮失败的成员也会在这里重试。
            for assignment in db.list_team_rotation_members(
                mother_id=mother_id, status="active", limit=5000
            ):
                if self._stop_event.is_set() or self._pause_event.is_set():
                    break
                if assignment.get("hub_status") == "success":
                    continue
                self._push_assignment_to_hub(mother, assignment)

            final_detail = service.get_team_detail(mother)
            final_seats = final_detail["seats"]
            db.record_team_mother_check(
                mother_id,
                entitled=final_seats.get("entitled"),
                in_use=final_seats.get("in_use"),
                remaining=final_seats.get("remaining_default"),
            )
        finally:
            service.close()

    def _remove_assignment(
        self,
        service: TeamService,
        mother: dict,
        assignment: dict,
        reason: str,
        status: str,
    ) -> None:
        member_id = str(assignment.get("member_id") or "")
        if not member_id:
            raise TeamApiError(f"无法移出 {assignment['email']}: 缺少成员 ID")
        service.remove_member(mother, member_id)
        db.update_team_rotation_member(
            assignment["id"],
            status=status,
            removed_at=time.time(),
            error=str(reason)[:1000],
        )
        self._event("INFO", "remove", reason, mother["id"], assignment["email"])

    def _push_assignment_to_hub(self, mother: dict, assignment: dict) -> None:
        try:
            export_cfg = db.get_export_internal_config().get("sub2api", {})
        except Exception as exc:
            db.update_team_rotation_member(
                assignment["id"], hub_status="failed", hub_error=f"读取 Hub 配置失败: {exc}"
            )
            return

        if not export_cfg.get("enabled"):
            db.update_team_rotation_member(
                assignment["id"], hub_status="disabled", hub_error=""
            )
            return

        account = db.get_registered(assignment["email"])
        if not account:
            db.update_team_rotation_member(
                assignment["id"], hub_status="failed", hub_error="本地凭证不存在"
            )
            return

        try:
            from . import exporter

            def export_log(message: str, level: str = "info") -> None:
                event_level = "ERROR" if level == "error" else "WARNING" if level == "warn" else "INFO"
                self._event(
                    event_level,
                    "hub",
                    message,
                    mother["id"],
                    assignment["email"],
                )

            result = exporter.run_exports(
                {
                    **account,
                    "account_id": mother["workspace_id"],
                    "plan_type": "team",
                    "notes": "Team 轮转",
                },
                cpa_cfg=None,
                sub2api_cfg=export_cfg,
                log_fn=export_log,
            ).get("sub2api") or {}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}

        if result.get("ok"):
            db.update_team_rotation_member(
                assignment["id"],
                hub_status="success",
                hub_pushed_at=time.time(),
                hub_error="",
            )
            self._event(
                "INFO", "hub", "子号已推送到 Hub", mother["id"], assignment["email"]
            )
        else:
            error = str(result.get("error") or "Hub 推送失败")[:1000]
            db.update_team_rotation_member(
                assignment["id"], hub_status="failed", hub_error=error
            )
            self._event(
                "ERROR", "hub", error, mother["id"], assignment["email"]
            )


CONTROLLER = TeamRotationController()


__all__ = [
    "CONTROLLER",
    "Credentials",
    "RotationState",
    "TeamApiError",
    "TeamRotationController",
    "TeamService",
    "_remaining_default_seats",
    "_token_identity",
    "parse_mother_session",
]
