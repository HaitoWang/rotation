from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import quote

from app.integrations.openai.http_client import (
    DEFAULT_IMPERSONATE,
    create_http_session,
    us_chrome_headers,
)

BASE_URL = "https://chatgpt.com"
STANDARD = "standard"
ADVANCED = "advanced"


class TeamApiError(RuntimeError):
    pass


class TeamChildAuthInvalidError(TeamApiError):
    """The child credential must be reauthorized before another join attempt."""


def _normalized_seat_type(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in {"advanced", "advanced_seat", "premium", "premium_seat", "prolite", "weekly", "weekly_only"}:
        return ADVANCED
    if normalized in {"standard", "standard_seat", "normal", "regular", "default"}:
        return STANDARD
    return "unknown"


def _openai_seat_type(value: Any) -> str:
    return "prolite" if _normalized_seat_type(value) == ADVANCED else "default"


def _email_domain(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text.rsplit("@", 1)[1] if "@" in text else ""


def _percent(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100 else None


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
        try:
            part = self.access_token.split(".")[1]
            part += "=" * (-len(part) % 4)
            payload = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        except (IndexError, ValueError, TypeError, json.JSONDecodeError):
            return
        auth = payload.get("https://api.openai.com/auth", {})
        profile = payload.get("https://api.openai.com/profile", {})
        if isinstance(auth, dict):
            self.account_id = self.account_id or str(auth.get("chatgpt_account_id") or "")
            self.user_id = self.user_id or str(auth.get("chatgpt_user_id") or auth.get("user_id") or payload.get("sub") or "")
        if isinstance(profile, dict):
            self.email = self.email or str(profile.get("email") or "").lower()


def _cookie_header(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(
            f"{key}={item}" for key, item in value.items() if item is not None and str(item)
        )
    text = str(value or "").strip()
    return text.split(":", 1)[1].strip() if text.lower().startswith("cookie:") else text


def parse_mother_session(raw: str, workspace_id: str = "") -> dict[str, str]:
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
    owner_user_id = ""
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
        if data.get("sessionToken") and not cookie_header:
            cookie_header = f"__Secure-next-auth.session-token={str(data['sessionToken']).strip()}"
        account = data.get("account") if isinstance(data.get("account"), dict) else {}
        account_id = account_id or str(
            data.get("workspace_id") or data.get("account_id") or data.get("accountId") or account.get("id") or ""
        ).strip()
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        owner_user_id = str(user.get("id") or data.get("user_id") or "").strip()
        email = str(user.get("email") or data.get("email") or "").strip().lower()
    elif text.count(".") == 2 and ";" not in text and "=" not in text:
        access_token = text.removeprefix("Bearer ").strip()
    else:
        cookie_header = _cookie_header(text)
    credentials = Credentials(access_token, cookie_header, account_id, owner_user_id, email)
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


def _payload_items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "users", "members", "invites", "requests"):
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
            try:
                return int(item["total"])
            except (KeyError, TypeError, ValueError):
                pass
    return None


def _member_view(item: dict, owner_user_id: str, owner_email: str) -> dict:
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    profile = item.get("profile") if isinstance(item.get("profile"), dict) else {}
    address = item.get("email_address") if isinstance(item.get("email_address"), dict) else {}
    member_id = str(item.get("user_id") or item.get("id") or user.get("id") or item.get("account_user_id") or "").strip()
    email = ""
    for value in (
        item.get("email"), item.get("emailAddress"), address.get("address"), address.get("email"),
        user.get("email"), user.get("email_address"), profile.get("email"), profile.get("email_address"),
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


def _seat_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _subscription_details(payload: Any) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    if {"seat_capacity", "seats_entitled", "seats_in_use"}.intersection(payload):
        return payload
    for key in ("subscription", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict) and {"seat_capacity", "seats_entitled", "seats_in_use"}.intersection(nested):
            return nested
    return None


def _seat_pools(payload: Any) -> dict[str, dict[str, Any]]:
    details = _subscription_details(payload) or {}
    assigned = details.get("assigned") if isinstance(details.get("assigned"), dict) else {}
    pools = {
        STANDARD: {"upstream_type": "default", "paid": 0, "assigned": _seat_count(assigned.get("default")) or 0, "available": 0, "held": 0, "renewal_requested": 0},
        ADVANCED: {"upstream_type": "prolite", "paid": 0, "assigned": _seat_count(assigned.get("prolite")) or 0, "available": 0, "held": 0, "renewal_requested": 0},
    }
    capacity = details.get("seat_capacity")
    entries = capacity if isinstance(capacity, list) else [dict(type=k, **v) for k, v in (capacity or {}).items() if isinstance(v, dict)]
    for item in entries:
        key = str(item.get("type") or "").lower()
        local = ADVANCED if key in {"advanced", "prolite", "premium"} else STANDARD if key in {"standard", "default", "regular"} else ""
        if local:
            for field in ("paid", "available", "held", "renewal_requested"):
                pools[local][field] = _seat_count(item.get(field)) or 0
    if not entries:
        entitled = _seat_count(details.get("seats_entitled")) or 0
        used = _seat_count(details.get("seats_in_use")) or 0
        pools[STANDARD].update(paid=entitled, assigned=used, available=max(0, entitled - used))
    return pools


class TeamApiClient:
    """HTTP-only Team client. Persistence and rotation policy live elsewhere."""

    def __init__(self, proxy: str = ""):
        headers = us_chrome_headers()
        self.session = create_http_session(
            proxy=str(proxy or "").strip() or None,
            impersonate=DEFAULT_IMPERSONATE,
            user_agent=headers["User-Agent"],
            accept_language=headers["Accept-Language"],
            client_hints={key: value for key, value in headers.items() if key.startswith("sec-ch-ua")},
        )
        self.headers = headers
        self._mother_credentials: dict[str, Credentials] = {}

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
        include_cookies: bool = True,
        include_device_id: bool = True,
        include_session_id: bool = True,
        retries: int = 2,
    ) -> tuple[int, Any]:
        headers = {**self.headers, "Accept": "application/json", "Origin": BASE_URL, "Referer": f"{BASE_URL}{referer}", "oai-language": "zh-CN"}
        if credentials.access_token:
            headers["Authorization"] = f"Bearer {credentials.access_token}"
        if include_cookies and credentials.cookie_header:
            headers["Cookie"] = credentials.cookie_header
        if account_id:
            headers["chatgpt-account-id"] = account_id
        if include_device_id and credentials.device_id:
            headers["oai-device-id"] = credentials.device_id
        if include_session_id and credentials.session_id:
            headers["oai-session-id"] = credentials.session_id
        kwargs: dict[str, Any] = {"headers": headers, "timeout": 30}
        if json_body is not None:
            kwargs["json"] = json_body
            headers["Content-Type"] = "application/json"
        elif empty_body:
            kwargs["data"] = b""
        last_error = None
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

    def _credentials(self, mother: dict) -> Credentials:
        key = str(mother.get("id") or mother.get("workspace_id") or "")
        cached = self._mother_credentials.get(key)
        if cached is not None:
            return cached
        credentials = Credentials(
            access_token=str(mother.get("access_token") or ""),
            cookie_header=str(mother.get("cookie_header") or ""),
            account_id=str(mother.get("workspace_id") or ""),
            user_id=str(mother.get("owner_user_id") or ""),
            email=str(mother.get("email") or ""),
        )
        credentials.apply_token_identity()
        self._mother_credentials[key] = credentials
        return credentials

    def _registered_credentials(self, account: dict) -> Credentials:
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        cookie_header = str(account.get("cookie_header") or "")
        if not cookie_header and account.get("session_token"):
            cookie_header = f"__Secure-next-auth.session-token={account['session_token']}"
        credentials = Credentials(
            access_token=str(account.get("access_token") or ""),
            cookie_header=cookie_header,
            account_id=str(extra.get("account_id") or account.get("account_id") or ""),
            user_id=str(extra.get("user_id") or account.get("user_id") or ""),
            email=str(account.get("email") or "").strip().lower(),
        )
        credentials.apply_token_identity()
        return credentials

    def _resolve_access_token(self, credentials: Credentials, label: str) -> None:
        if credentials.access_token:
            credentials.apply_token_identity()
            return
        if not credentials.cookie_header:
            raise TeamApiError(f"{label}缺少 Access Token")
        status, payload = self.request("GET", "/api/auth/session", credentials, retries=2)
        if not 200 <= status < 300 or not isinstance(payload, dict):
            raise TeamApiError(f"{label} Session 换取 Access Token 失败: HTTP {status}")
        credentials.access_token = str(payload.get("accessToken") or payload.get("access_token") or "").strip()
        if not credentials.access_token:
            raise TeamApiError(f"{label} Session 响应中没有 Access Token")
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        credentials.user_id = credentials.user_id or str(user.get("id") or "")
        credentials.email = credentials.email or str(user.get("email") or "").lower()
        credentials.account_id = credentials.account_id or str(account.get("id") or "")
        credentials.apply_token_identity()

    def _mother_request(self, mother: dict, method: str, path: str, **kwargs) -> tuple[int, Any]:
        credentials = self._credentials(mother)
        if not credentials.access_token and credentials.cookie_header:
            self._resolve_access_token(credentials, "母号")
        status, payload = self.request(method, path, credentials, **kwargs)
        if status in {401, 403} and credentials.cookie_header:
            credentials.access_token = ""
            self._resolve_access_token(credentials, "母号")
            status, payload = self.request(method, path, credentials, **kwargs)
        return status, payload

    def get_team_seats(self, mother: dict) -> dict:
        workspace_id = str(mother.get("workspace_id") or "")
        if not workspace_id:
            raise TeamApiError("母号缺少 workspace_id")
        status, payload = self._mother_request(mother, "GET", f"/backend-api/subscriptions?account_id={quote(workspace_id, safe='')}", account_id=workspace_id, referer="/admin/billing")
        if not 200 <= status < 300:
            raise TeamApiError(f"查询母号席位失败: HTTP {status}")
        details = _subscription_details(payload)
        pools = _seat_pools(payload)
        if not details:
            raise TeamApiError("母号席位响应缺少可识别的数据")
        preferred = ADVANCED if str(mother.get("preferred_seat_type") or "").lower() == ADVANCED else STANDARD
        return {
            "entitled": _seat_count(details.get("seats_entitled")),
            "in_use": _seat_count(details.get("seats_in_use")),
            "remaining_configured": pools[preferred]["available"],
            "preferred_seat_type": preferred,
            "pools": pools,
        }

    def get_team_members(self, mother: dict) -> dict:
        workspace_id = str(mother.get("workspace_id") or "")
        if not workspace_id:
            raise TeamApiError("母号缺少 workspace_id")
        credentials = self._credentials(mother)
        members: list[dict] = []
        offset = 0
        total = 0
        for _ in range(100):
            path = f"/backend-api/accounts/{quote(workspace_id, safe='')}/users?offset={offset}&limit=100&query="
            status, payload = self._mother_request(mother, "GET", path, account_id=workspace_id, referer="/admin/members?tab=members")
            if not 200 <= status < 300:
                raise TeamApiError(f"查询 Team 成员失败: HTTP {status}")
            entries = _payload_items(payload)
            total = _payload_total(payload) or total
            members.extend(_member_view(item, credentials.user_id, credentials.email) for item in entries)
            members = [item for item in members if item["id"]]
            if not entries or len(entries) < 100 or (total and len(members) >= total):
                break
            offset += len(entries)
        return {"members": members, "total_members": max(total, len(members))}

    @staticmethod
    def _require(status: int, payload: Any, action: str, *, allow_404: bool = False) -> None:
        if allow_404 and status == 404:
            return
        if not 200 <= status < 300:
            preview = json.dumps(payload, ensure_ascii=False)[:500] if isinstance(payload, (dict, list)) else str(payload)[:500]
            raise TeamApiError(f"{action}失败: HTTP {status}, {preview}")
        if isinstance(payload, dict) and payload.get("success") is False:
            raise TeamApiError(f"{action}失败: success=false")

    @staticmethod
    def _require_child_join(status: int, payload: Any, action: str) -> None:
        preview = json.dumps(payload, ensure_ascii=False) if isinstance(payload, (dict, list)) else str(payload)
        if status == 401 or (status == 403 and any(marker in preview.lower() for marker in ("token_invalidated", "authentication token", "session has ended"))):
            raise TeamChildAuthInvalidError(f"{action}失败: HTTP {status}, {preview[:500]}")
        TeamApiClient._require(status, payload, action)

    def invite_and_accept(self, mother: dict, account: dict, *, confirm: bool = True) -> dict:
        child = self._registered_credentials(account)
        self._resolve_access_token(child, "子号")
        if not child.email:
            raise TeamApiError("子号缺少邮箱")
        if _normalized_seat_type(mother.get("preferred_seat_type")) == ADVANCED or str(mother.get("join_mode") or "") == "auto_accept_request":
            return self._auto_accept_request_join(mother, child, confirm=confirm)
        return self._invite_accept_join(mother, child, confirm=confirm)

    def _invite_accept_join(self, mother: dict, child: Credentials, *, confirm: bool = True) -> dict:
        workspace_id = str(mother.get("workspace_id") or "")
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
        status, payload = self.request(
            "POST",
            f"/backend-api/accounts/{quote(workspace_id, safe='')}/invites/accept",
            child,
            account_id=child.account_id,
            include_cookies=False,
            include_session_id=False,
            empty_body=True,
            retries=2,
        )
        self._require_child_join(status, payload, "子号接受邀请")
        if not confirm:
            return {"member_id": "", "email": child.email, "seat_type": STANDARD}
        return self._confirm_joined_member(mother, child, desired_seat_type="default")

    def _auto_accept_request_join(self, mother: dict, child: Credentials, *, confirm: bool = True) -> dict:
        workspace_id = str(mother.get("workspace_id") or "")
        desired = _openai_seat_type(mother.get("preferred_seat_type"))
        if not _email_domain(mother.get("email")) or _email_domain(mother.get("email")) != _email_domain(child.email):
            raise TeamApiError("无需审核加入要求母号与子号使用相同邮箱域")
        if not mother.get("auto_accept_configured"):
            status, payload = self._mother_request(
                mother,
                "POST",
                f"/backend-api/accounts/{quote(workspace_id, safe='')}/settings/auto_accept_requests",
                account_id=workspace_id,
                referer="/admin/identity",
                json_body={"value": True},
            )
            self._require(status, payload, "母号开启无需审核")
            mother["auto_accept_configured"] = True
        pending = self._find_pending_request(mother, child.email)
        if not pending:
            status, payload = self.request(
                "POST",
                f"/backend-api/accounts/{quote(workspace_id, safe='')}/invites/request",
                child,
                account_id=child.account_id,
                include_cookies=False,
                include_session_id=False,
                empty_body=True,
                retries=2,
            )
            self._require_child_join(status, payload, "子号申请加入")
        if not confirm:
            return {"member_id": "", "email": child.email, "seat_type": _normalized_seat_type(desired)}
        return self._confirm_joined_member(mother, child, desired_seat_type=desired, approve_pending=True, pending=pending)

    def _find_pending_request(self, mother: dict, email: str) -> Optional[dict]:
        workspace_id = str(mother.get("workspace_id") or "")
        status, payload = self._mother_request(
            mother,
            "GET",
            f"/backend-api/accounts/{quote(workspace_id, safe='')}/invites?include_pending=false&include_requests=true&offset=0&limit=25&query={quote(email, safe='')}",
            account_id=workspace_id,
            referer="/admin/members?tab=requests",
        )
        self._require(status, payload, "查询待批准加入申请")
        target = str(email or "").strip().lower()
        for item in _payload_items(payload):
            if _member_view(item, "", "").get("email") == target:
                return item
        return None

    def _approve_pending_request(self, mother: dict, pending: dict, desired_seat_type: str) -> None:
        request_id = str(pending.get("id") or pending.get("invite_id") or pending.get("inviteId") or pending.get("request_id") or "").strip()
        if not request_id:
            raise TeamApiError("待批准 Team 申请缺少 request id")
        workspace_id = str(mother.get("workspace_id") or "")
        status, payload = self._mother_request(
            mother,
            "PATCH",
            f"/backend-api/accounts/{quote(workspace_id, safe='')}/invites/{quote(request_id, safe='')}",
            account_id=workspace_id,
            referer="/admin/members?tab=requests",
            json_body={"role": str(pending.get("role") or "standard-user"), "seat_type": desired_seat_type, "accept_request": True},
        )
        self._require(status, payload, "母号批准加入申请")

    def _ensure_member_seat_type(self, mother: dict, member: dict, desired_seat_type: str) -> dict:
        desired = _normalized_seat_type(desired_seat_type)
        current_raw = str(member.get("seat_type") or "")
        if _normalized_seat_type(current_raw) == desired or (desired == STANDARD and not current_raw):
            return member
        member_id = str(member.get("id") or "").strip()
        workspace_id = str(mother.get("workspace_id") or "")
        if not member_id:
            raise TeamApiError("成员缺少 user_id，无法切换席位")
        status, payload = self._mother_request(
            mother,
            "POST",
            f"/backend-api/accounts/{quote(workspace_id, safe='')}/users/{quote(member_id, safe='')}/seat/update",
            account_id=workspace_id,
            referer="/admin/members?tab=members",
            json_body={"operation": "switch", "seat_type": desired_seat_type, "flow_id": str(uuid.uuid4()), "mutation_attempt_id": str(uuid.uuid4())},
        )
        self._require(status, payload, "切换 Team 席位")
        email = str(member.get("email") or "").strip().lower()
        verified = None
        for attempt in range(8):
            detail = self.get_team_members(mother)
            verified = next((item for item in detail["members"] if item.get("email") == email), None)
            if verified and _normalized_seat_type(verified.get("seat_type")) == desired:
                return verified
            if attempt < 7:
                time.sleep(0.5)
        raise TeamApiError(f"席位切换后校验失败: expected={desired_seat_type} actual={(verified or {}).get('seat_type') or 'unknown'}")

    def _confirm_joined_member(
        self,
        mother: dict,
        child: Credentials,
        *,
        desired_seat_type: str = "default",
        approve_pending: bool = False,
        pending: Optional[dict] = None,
    ) -> dict:
        delays = (1.0, 2.0, 4.0, 8.0, 8.0)
        approved = False
        for attempt, delay in enumerate(delays):
            if pending and approve_pending and not approved:
                self._approve_pending_request(mother, pending, desired_seat_type)
                approved = True
            detail = self.get_team_members(mother)
            match = next((item for item in detail["members"] if item.get("email") == child.email), None)
            if match:
                member_id = str(match.get("id") or "")
                verified = self._ensure_member_seat_type(mother, match, desired_seat_type)
                return {"member_id": str(verified.get("id") or member_id), "email": child.email, "seat_type": _normalized_seat_type(verified.get("seat_type") or desired_seat_type)}
            if approve_pending and not approved:
                pending = self._find_pending_request(mother, child.email)
            if attempt < len(delays) - 1:
                time.sleep(delay)
        raise TeamApiError(f"加入接口已受理，但 Team 成员列表中未找到 {child.email}，禁止推送 Hub")

    def check_quota(self, account: dict, workspace_id: str) -> dict:
        refresh_token = str(account.get("refresh_token") or "").strip()
        if not refresh_token:
            return {"status": "auth_required", "primary_used_percent": None, "secondary_used_percent": None, "error": "缺少 Codex Refresh Token"}
        try:
            from app.integrations.panels.exporter import refresh_codex_token

            fresh = refresh_codex_token(refresh_token)
            access_token = str(fresh.get("access_token") or "").strip()
            if not access_token:
                raise TeamApiError("Token 刷新响应缺少 access_token")
        except Exception as exc:
            return {"status": "auth_required", "primary_used_percent": None, "secondary_used_percent": None, "error": f"Codex Token 刷新失败: {exc}"}
        credentials = self._registered_credentials(account)
        credentials.access_token = access_token
        credentials.cookie_header = ""
        credentials.account_id = str(workspace_id or "")
        status, payload = self.request("GET", "/backend-api/wham/usage", credentials, account_id=workspace_id, retries=2)
        if status == 401:
            return {"status": "auth_required", "primary_used_percent": None, "secondary_used_percent": None, "error": "授权失效 (HTTP 401)"}
        if not 200 <= status < 300:
            return {"status": "unknown", "primary_used_percent": None, "secondary_used_percent": None, "error": f"额度接口返回 HTTP {status}"}
        rate_limit = payload.get("rate_limit") if isinstance(payload, dict) else None
        if not isinstance(rate_limit, dict):
            return {"status": "unknown", "primary_used_percent": None, "secondary_used_percent": None, "error": "额度响应缺少 rate_limit"}
        primary = rate_limit.get("primary_window") if isinstance(rate_limit.get("primary_window"), dict) else {}
        secondary = rate_limit.get("secondary_window") if isinstance(rate_limit.get("secondary_window"), dict) else {}
        return {
            "status": "alive",
            "primary_used_percent": _percent(primary.get("used_percent")),
            "secondary_used_percent": _percent(secondary.get("used_percent")),
            "refresh_token": str(fresh.get("refresh_token") or "").strip(),
            "id_token": str(fresh.get("id_token") or "").strip(),
            "error": "",
        }

    def remove_member(self, mother: dict, member_id: str) -> dict:
        workspace_id = str(mother.get("workspace_id") or "")
        if not workspace_id or not member_id:
            raise TeamApiError("母号或成员 ID 缺失")
        status, payload = self._mother_request(
            mother,
            "DELETE",
            f"/backend-api/accounts/{quote(workspace_id, safe='')}/users/{quote(member_id, safe='')}",
            account_id=workspace_id,
            referer="/admin/members?tab=members",
            empty_body=True,
            retries=3,
        )
        if status not in {200, 204, 404}:
            raise TeamApiError(f"移出 Team 成员失败: HTTP {status}")
        return {"removed": status != 404, "already_absent": status == 404, "member_id": member_id}
