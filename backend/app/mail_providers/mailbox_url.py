"""Generic ``email----mailbox URL`` provider.

This adapter mirrors the URL-backed mailbox flow used by the sibling
``workspace`` project: snapshot the mailbox before registration, ignore codes
already present in that snapshot, and require the same new code to appear in
two consecutive polls before returning it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlsplit

import requests

from .base import (
    ConfigField,
    MailProvider,
    MailProviderError,
    register,
    validate_email,
)

logger = logging.getLogger(__name__)

_DEFAULT_ALLOWED_HOSTS = (
    os.getenv("REFUND_MAIL_HOST", "assurivo.com").strip().lower().rstrip(".")
    or "assurivo.com"
)
_OTP_KEYS = {
    "otp",
    "code",
    "verification_code",
    "verify_code",
    "verificationcode",
    "email_code",
    "emailcode",
    "auth_code",
    "authcode",
    "content",
    "body",
    "message",
}
_TIME_KEYS = {
    "timestamp",
    "created_at",
    "createdat",
    "received_at",
    "receivedat",
    "sent_at",
    "sentat",
    "mail_time",
    "mailtime",
    "datetime",
    "date",
    "time",
    "created",
    "received",
    "sent",
}
_OTP_RE = re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")
_SPAN_OTP_RE = re.compile(r"<span[^>]*>\s*(\d{6})\s*</span>", re.I)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_TIMESTAMP_PARAM_RE = re.compile(r"(?:m=\+\d+\.\d+|\bt=\d+\b)")


@dataclass(frozen=True)
class _MailboxSnapshot:
    codes: frozenset[str]
    fingerprint: str
    captured_at: float


def _normalise_key(value: Any) -> str:
    return str(value).lower().replace("-", "_")


def _codes_from_text(value: Any) -> list[str]:
    text = str(value or "")
    if not text:
        return []
    body_start = text.find("\r\n\r\n")
    body = text[body_start:] if body_start != -1 else text
    body = _EMAIL_RE.sub("", body)
    body = _TIMESTAMP_PARAM_RE.sub("", body)
    result: list[str] = []
    for code in [*_SPAN_OTP_RE.findall(body), *_OTP_RE.findall(body)]:
        if code.startswith(("19", "20")):
            continue
        if code not in result:
            result.append(code)
    return result


def _otp_codes(value: Any) -> list[str]:
    """Extract OTP candidates while preferring explicit provider fields."""

    def collect(item: Any) -> list[str]:
        if isinstance(item, dict):
            preferred: list[str] = []
            for key, child in item.items():
                if _normalise_key(key) in _OTP_KEYS:
                    preferred.extend(collect(child))
            if preferred:
                return preferred
            result: list[str] = []
            for child in item.values():
                result.extend(collect(child))
            return result
        if isinstance(item, (list, tuple)):
            result: list[str] = []
            for child in item:
                result.extend(collect(child))
            return result
        return _codes_from_text(item)

    result: list[str] = []
    for code in collect(value):
        if code not in result:
            result.append(code)
    return result


def _timestamp_value(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10**12:
            number /= 1000
        return number if 10**9 <= number <= 4 * 10**9 else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        number = float(text)
        if number > 10**12:
            number /= 1000
        if 10**9 <= number <= 4 * 10**9:
            return number
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _mailbox_timestamp(value: Any) -> Optional[float]:
    found: list[float] = []

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if _normalise_key(key) in _TIME_KEYS:
                    parsed = _timestamp_value(child)
                    if parsed is not None:
                        found.append(parsed)
                walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)

    walk(value)
    return max(found) if found else None


def _fingerprint(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        encoded = repr(value)
    return hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()


def _allowed_hosts(value: str) -> tuple[str, ...]:
    parts = re.split(r"[,;\s]+", str(value or "").strip().lower())
    hosts = tuple(part.rstrip(".") for part in parts if part.strip("."))
    return hosts or (_DEFAULT_ALLOWED_HOSTS,)


def _validate_access_url(value: str, allowed_hosts: tuple[str, ...]) -> str:
    url = str(value or "").strip()
    if not url or any(char.isspace() for char in url):
        raise ValueError("接码地址不能为空且不能包含空白字符")
    parsed = urlsplit(url)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("接码地址端口无效") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        raise ValueError("接码地址必须是有效的 HTTP(S) URL")
    if not any(host == item or host.endswith(f".{item}") for item in allowed_hosts):
        raise ValueError(f"接码地址域名必须是: {', '.join(allowed_hosts)}")
    return url


@register
class MailboxURLProvider(MailProvider):
    """Use a per-account HTTP URL to read mailbox OTPs."""

    kind = "mailbox_url"
    display_name = "通用接码地址"
    pooled = True
    ephemeral = False
    accepts_existing_account = True
    line_segments = 2
    import_hint = "邮箱----接码地址"
    import_placeholder = (
        "registered@example.com----"
        "https://assurivo.com/console/open.php?mail=registered%40example.com&pwd=SECRET"
    )
    config_fields = [
        ConfigField(
            "mailbox_url_allowed_hosts",
            "接码地址允许域名",
            required=False,
            placeholder=_DEFAULT_ALLOWED_HOSTS,
            help="默认 assurivo.com；多个域名用逗号分隔，只允许该域名及其子域名",
        ),
        ConfigField(
            "mailbox_url_timeout",
            "邮箱验证码超时（秒）",
            type="number",
            required=False,
            placeholder="150",
        ),
        ConfigField(
            "mailbox_url_poll_interval",
            "邮箱轮询间隔（秒）",
            type="number",
            required=False,
            placeholder="4",
        ),
        ConfigField(
            "mailbox_url_request_timeout",
            "单次接码请求超时（秒）",
            type="number",
            required=False,
            placeholder="30",
        ),
    ]

    def __init__(
        self,
        email: str,
        access_url: str,
        *,
        allowed_hosts: str = _DEFAULT_ALLOWED_HOSTS,
        timeout: int = 150,
        poll_interval: float = 4.0,
        request_timeout: float = 30.0,
        session=None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.email = str(email or "").strip().lower()
        validate_email(self.email)
        self.allowed_hosts = _allowed_hosts(allowed_hosts)
        self.access_url = _validate_access_url(access_url, self.allowed_hosts)
        self.timeout = max(10, min(int(timeout or 150), 900))
        self.poll_interval = max(0.1, min(float(poll_interval or 4), 30.0))
        self.request_timeout = max(1.0, min(float(request_timeout or 30), 120.0))
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._cancel_event = threading.Event()
        self._dead = False
        self._snapshot: Optional[_MailboxSnapshot] = None
        self._reference_fingerprint = ""

    @staticmethod
    def _number(value: Any, default: float, cast: Callable[[Any], Any]) -> Any:
        try:
            return cast(str(value or "").strip() or default)
        except (TypeError, ValueError):
            return cast(default)

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        if not account:
            raise MailProviderError(
                "通用接码地址需要从邮箱池领取账号，格式：邮箱----接码地址",
                fatal=True,
                kind=cls.kind,
            )
        try:
            return cls(
                account.get("email", ""),
                account.get("relay_url", ""),
                allowed_hosts=str(
                    settings.get("mailbox_url_allowed_hosts") or _DEFAULT_ALLOWED_HOSTS
                ),
                timeout=cls._number(settings.get("mailbox_url_timeout"), 150, int),
                poll_interval=cls._number(
                    settings.get("mailbox_url_poll_interval"), 4.0, float
                ),
                request_timeout=cls._number(
                    settings.get("mailbox_url_request_timeout"), 30.0, float
                ),
            )
        except ValueError as exc:
            raise MailProviderError(str(exc), fatal=True, kind=cls.kind) from exc

    @classmethod
    def parse_line(cls, line: str) -> dict:
        parts = [part.strip() for part in str(line or "").split("----")]
        if len(parts) != 2:
            raise ValueError(
                f"需要 2 段（邮箱----接码地址），实际 {len(parts)} 段"
            )
        email, access_url = parts
        validate_email(email)
        if not access_url.lower().startswith(("http://", "https://")):
            raise ValueError("第 2 段必须是 HTTP(S) 接码地址")
        return {
            "email": email.lower(),
            "relay_url": access_url,
            "kind": cls.kind,
        }

    def _request(self) -> Any:
        current = self.access_url
        for _ in range(6):
            response = self._session.get(
                current,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=self.request_timeout,
                allow_redirects=False,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if status not in {301, 302, 303, 307, 308}:
                return response
            location = str((getattr(response, "headers", {}) or {}).get("Location") or "").strip()
            if not location:
                return response
            current = _validate_access_url(urljoin(current, location), self.allowed_hosts)
        raise MailProviderError("接码地址重定向次数过多", kind="network")

    @staticmethod
    def _payload(response: Any) -> Any:
        try:
            return response.json()
        except (AttributeError, TypeError, ValueError):
            return str(getattr(response, "text", "") or "")

    def _fetch(self) -> tuple[Any, str, float]:
        try:
            response = self._request()
        except MailProviderError:
            raise
        except ValueError as exc:
            raise MailProviderError(
                str(exc), fatal=True, kind="config"
            ) from exc
        except Exception as exc:
            raise MailProviderError(
                f"接码地址请求失败: {type(exc).__name__}", kind="network"
            ) from exc
        status = int(getattr(response, "status_code", 0) or 0)
        if not 200 <= status < 300:
            raise MailProviderError(f"接码地址返回 HTTP {status}", kind="network")
        payload = self._payload(response)
        return payload, _fingerprint(payload), self._wall_clock()

    def create_mailbox(self) -> str:
        payload, fingerprint, captured_at = self._fetch()
        self._snapshot = _MailboxSnapshot(
            frozenset(_otp_codes(payload)), fingerprint, captured_at
        )
        self._reference_fingerprint = fingerprint
        logger.info("[mailbox-url] 邮箱快照完成: %s", self.email)
        return self.email

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        if self._snapshot is None:
            raise MailProviderError("通用接码地址尚未建立邮箱快照", fatal=True, kind="state")
        effective_timeout = max(1, min(max(int(timeout or 0), self.timeout), 900))
        deadline = self._clock() + effective_timeout
        cutoff = self._snapshot.captured_at
        if issued_after is not None:
            try:
                cutoff = max(cutoff, float(issued_after))
            except (TypeError, ValueError):
                pass
        pending = ""
        pending_fingerprint = ""
        last_status = "等待新验证码"
        while self._clock() < deadline:
            if self._cancel_event.is_set():
                raise RuntimeError("通用接码地址轮询已取消")
            try:
                payload, fingerprint, _ = self._fetch()
                codes = [
                    code
                    for code in _otp_codes(payload)
                    if code not in self._snapshot.codes
                ]
                timestamp = _mailbox_timestamp(payload)
                if timestamp is not None and timestamp < cutoff - 5:
                    codes = []
                if fingerprint == self._reference_fingerprint:
                    codes = []
                candidate = codes[-1] if codes else ""
                if (
                    candidate
                    and candidate == pending
                    and fingerprint == pending_fingerprint
                ):
                    self._reference_fingerprint = fingerprint
                    logger.info("[mailbox-url] 收到 OTP: %s**", candidate[:2])
                    return candidate
                pending = candidate
                pending_fingerprint = fingerprint if candidate else ""
                last_status = "发现新验证码，等待确认" if candidate else "等待新验证码"
            except MailProviderError as exc:
                last_status = str(exc)
            except Exception as exc:
                last_status = f"{type(exc).__name__}"
            self._sleeper(
                min(self.poll_interval, max(0.0, deadline - self._clock()))
            )
        raise TimeoutError(
            f"通用接码地址 OTP 超时 ({effective_timeout}s): {last_status}"
        )

    def mark_dead(self, reason: str = "") -> None:
        self._dead = True

    def finalize(self, success: bool = False) -> None:
        self._cancel_event.set()
        try:
            self._session.close()
        except Exception:
            pass


__all__ = ["MailboxURLProvider"]
