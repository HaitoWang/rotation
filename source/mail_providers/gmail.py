"""SmsBower 临时 Gmail 邮箱 provider。

SmsBower 的邮箱 API 与手机号接码 API 是两条独立链路：

    GET /getActivation -> 租取临时 Gmail
    GET /getCode       -> 轮询 OpenAI 邮件验证码
    GET /setStatus     -> 2 取消 / 3 完成 / 5 等待下一封

这个 provider 负责邮箱 OTP。使用 Gmail 邮箱来源时，registrar 会把
注册流程中的手机号验证码也固定到 SmsBower，避免双会话/跨平台串号。
"""
from __future__ import annotations

import json
import hashlib
import logging
import re
import threading
import time
from typing import Optional
from urllib.parse import urljoin

import requests

from .base import ConfigField, MailProvider, MailProviderError, extract_otp, register

logger = logging.getLogger(__name__)

_OTP_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_DEFAULT_BASE_URL = "https://smsbower.page/api/mail/"


class GmailSmsBowerError(MailProviderError):
    """SmsBower 邮箱接口错误。"""


@register
class GmailMailProvider(MailProvider):
    """从 SmsBower 临时租取 Gmail 并轮询邮件 OTP。"""

    kind = "gmail"
    display_name = "Gmail（SmsBower 临时邮箱）"
    pooled = False
    ephemeral = True
    line_segments = 0
    config_fields = [
        ConfigField(
            "gmail_smsbower_api_key",
            "SmsBower Gmail API Key",
            type="password",
            required=False,
            help="SmsBower 邮箱 API 的 Key；与手机号接码 Key 可相同，也可单独配置",
        ),
        ConfigField(
            "gmail_smsbower_api_url",
            "SmsBower 邮箱 API 地址",
            required=False,
            placeholder=_DEFAULT_BASE_URL,
            help="默认 https://smsbower.page/api/mail/，末尾斜杠可省略",
        ),
        ConfigField(
            "gmail_smsbower_service",
            "Gmail 服务代码",
            required=False,
            placeholder="dr",
            help="OpenAI/ChatGPT 通常使用 dr",
        ),
        ConfigField(
            "gmail_smsbower_domain",
            "邮箱域名",
            required=False,
            placeholder="gmail.com",
            help="默认 gmail.com；仅填写 SmsBower 邮箱 API 支持的域名",
        ),
        ConfigField(
            "gmail_smsbower_max_price",
            "邮箱最高单价",
            type="number",
            required=False,
            placeholder="0.05",
            help="留空表示不设置价格上限",
        ),
        ConfigField(
            "gmail_smsbower_timeout",
            "邮件验证码超时（秒）",
            type="number",
            required=False,
            placeholder="120",
        ),
        ConfigField(
            "gmail_smsbower_poll_interval",
            "邮件轮询间隔（秒）",
            type="number",
            required=False,
            placeholder="5",
        ),
    ]

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        service: str = "dr",
        domain: str = "gmail.com",
        max_price: Optional[float] = None,
        timeout: int = 120,
        poll_interval: float = 5.0,
        session=None,
    ):
        api_key = (api_key or "").strip()
        if not api_key:
            raise GmailSmsBowerError("Gmail SmsBower 未配置 API Key", fatal=True, kind="config")
        self.api_key = api_key
        self.base_url = (base_url or _DEFAULT_BASE_URL).strip().rstrip("/") + "/"
        self.service = (service or "dr").strip() or "dr"
        self.domain = (domain or "gmail.com").strip().lower() or "gmail.com"
        self.max_price = max_price if max_price is None or max_price > 0 else None
        self.timeout = max(10, min(int(timeout or 120), 900))
        self.poll_interval = max(1.0, min(float(poll_interval or 5), 30.0))
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._activation_id = ""
        self._email = ""
        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._dead = False
        self._finalized = False

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        def _float_or_none(value) -> Optional[float]:
            text = str(value or "").strip()
            if not text:
                return None
            try:
                parsed = float(text)
            except (TypeError, ValueError):
                raise GmailSmsBowerError("Gmail SmsBower 最高单价不是有效数字", fatal=True, kind="config")
            if parsed < 0:
                raise GmailSmsBowerError("Gmail SmsBower 最高单价不能为负数", fatal=True, kind="config")
            return parsed or None

        def _int_or_default(value, default: int) -> int:
            try:
                return int(str(value or "").strip() or default)
            except (TypeError, ValueError):
                return default

        def _float_or_default(value, default: float) -> float:
            try:
                return float(str(value or "").strip() or default)
            except (TypeError, ValueError):
                return default

        return cls(
            api_key=str(settings.get("gmail_smsbower_api_key") or "").strip(),
            base_url=str(settings.get("gmail_smsbower_api_url") or _DEFAULT_BASE_URL),
            service=str(settings.get("gmail_smsbower_service") or "dr"),
            domain=str(settings.get("gmail_smsbower_domain") or "gmail.com"),
            max_price=_float_or_none(settings.get("gmail_smsbower_max_price")),
            timeout=_int_or_default(settings.get("gmail_smsbower_timeout"), 120),
            poll_interval=_float_or_default(settings.get("gmail_smsbower_poll_interval"), 5.0),
        )

    @property
    def exhausted(self) -> bool:
        return self._dead

    def _request(self, endpoint: str, params: dict) -> dict:
        query = dict(params or {})
        query["api_key"] = self.api_key
        url = urljoin(self.base_url, endpoint.lstrip("/"))
        try:
            response = self._session.get(url, params=query, timeout=20)
        except Exception as exc:
            raise GmailSmsBowerError(
                f"SmsBower Gmail 网络错误: {exc}", kind="network"
            ) from exc
        raw = (getattr(response, "text", "") or "").strip()
        if getattr(response, "status_code", 0) != 200:
            raise GmailSmsBowerError(
                f"SmsBower Gmail HTTP {getattr(response, 'status_code', 0)}: {raw[:180]}",
                kind="network",
            )
        try:
            data = response.json()
        except Exception as exc:
            try:
                data = json.loads(raw)
            except Exception:
                raise GmailSmsBowerError(
                    f"SmsBower Gmail 返回非 JSON: {raw[:180]}", kind="protocol"
                ) from exc
        if not isinstance(data, dict):
            raise GmailSmsBowerError("SmsBower Gmail 返回格式错误", kind="protocol")
        status = data.get("status")
        if status is not None and str(status).lower() not in {"1", "true"}:
            message = str(data.get("error") or data.get("message") or raw[:180])
            low = message.lower()
            if any(token in low for token in ("not been received", "not received", "try again", "no code")):
                raise TimeoutError(message)
            fatal = any(token in low for token in ("bad_key", "invalid key", "unauthorized", "no balance"))
            kind = "config" if fatal else "provider"
            raise GmailSmsBowerError(message, fatal=fatal, kind=kind)
        return data

    @staticmethod
    def _payload(data: dict) -> dict:
        payload = data.get("data")
        return payload if isinstance(payload, dict) else data

    def create_mailbox(self) -> str:
        params = {"service": self.service, "domain": self.domain}
        if self.max_price is not None:
            params["maxPrice"] = str(self.max_price)
        data = self._request("getActivation", params)
        payload = self._payload(data)
        activation_id = str(payload.get("mailId") or payload.get("mail_id") or payload.get("id") or "").strip()
        email = str(payload.get("mail") or payload.get("email") or "").strip().lower()
        if not activation_id or "@" not in email:
            raise GmailSmsBowerError(
                f"SmsBower Gmail 租号响应缺少 mailId/mail: {data}", kind="protocol"
            )
        with self._lock:
            self._activation_id = activation_id
            self._email = email
            self._finalized = False
            self._cancel_event.clear()
        logger.info("[gmail] SmsBower 租取临时邮箱: %s", email)
        return email

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        with self._lock:
            activation_id = self._activation_id
        if not activation_id:
            raise GmailSmsBowerError("Gmail SmsBower 尚未租取邮箱", fatal=True, kind="state")
        deadline = time.monotonic() + max(1, min(int(timeout or self.timeout), 900))
        last_error = ""
        while time.monotonic() < deadline:
            if self._cancel_event.is_set():
                raise RuntimeError("Gmail SmsBower 邮箱轮询已取消")
            try:
                data = self._request("getCode", {"mailId": activation_id})
                payload = self._payload(data)
                raw_code = str(payload.get("code") or payload.get("message") or payload.get("text") or "")
                code = extract_otp(raw_code) or (raw_code.strip() if _OTP_RE.fullmatch(raw_code.strip()) else None)
                if code:
                    logger.info("[gmail] 收到 SmsBower OTP: %s**", code[:2])
                    # 5 = 保持激活，允许同一 Gmail 在本轮重发/二次验证时继续收码。
                    try:
                        self._request("setStatus", {"id": activation_id, "status": "5"})
                    except Exception as exc:
                        logger.debug("[gmail] setStatus(5) 失败: %s", exc)
                    return code
                if raw_code:
                    last_error = f"响应无有效 6 位码: {raw_code[:100]}"
            except TimeoutError as exc:
                last_error = str(exc)
            except GmailSmsBowerError as exc:
                if exc.fatal:
                    self._dead = True
                    raise
                last_error = str(exc)
            self._cancel_event.wait(min(self.poll_interval, max(0.2, deadline - time.monotonic())))
        raise TimeoutError(
            f"Gmail SmsBower 邮件 OTP 超时 ({timeout}s){(': ' + last_error) if last_error else ''}"
        )

    def mark_dead(self, reason: str = "") -> None:
        self._dead = True

    def finalize(self, success: bool = False) -> None:
        """完成流程时扣费，失败时释放；可重复调用。"""
        self._cancel_event.set()
        with self._lock:
            activation_id = self._activation_id
            already_finalized = self._finalized
            self._finalized = True
        try:
            if activation_id and not already_finalized:
                status = "3" if success else "2"
                self._request("setStatus", {"id": activation_id, "status": status})
                logger.info(
                    "[gmail] SmsBower 邮箱已%s: id=%s",
                    "完成" if success else "释放",
                    activation_id,
                )
        except Exception as exc:
            logger.warning("[gmail] SmsBower 邮箱状态更新失败 id=%s: %s", activation_id, exc)
        finally:
            try:
                self._session.close()
            except Exception:
                pass

    def self_test(self) -> dict:
        try:
            data = self._request(
                "getPriceRests",
                {"service": self.service, "domain": self.domain},
            )
            payload = self._payload(data)
            return {"ok": True, "message": f"SmsBower Gmail 连通正常: {payload}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        finally:
            try:
                self._session.close()
            except Exception:
                pass


@register
class GmailLinkMailProvider(MailProvider):
    """导入主 Gmail + 取码链接；同一主邮箱可展开 4 个 plus alias。"""

    kind = "gmail_link"
    display_name = "Gmail 接码链接（主邮箱 + 4 个分裂邮箱）"
    pooled = True
    ephemeral = False
    line_segments = 2
    import_hint = "每行：主 Gmail----接码链接；导入时自动生成 4 个 +随机别名，共 5 个邮箱"
    import_placeholder = (
        "name@gmail.com----https://gapi.mailsapi.com/api/get-code?uid=xxxxxxxx"
    )
    config_fields = [
        ConfigField(
            "gmail_link_timeout",
            "接码链接超时（秒）",
            type="number",
            required=False,
            placeholder="120",
        ),
        ConfigField(
            "gmail_link_poll_interval",
            "接码链接轮询间隔（秒）",
            type="number",
            required=False,
            placeholder="3",
        ),
    ]

    def __init__(
        self,
        email: str,
        relay_url: str,
        *,
        timeout: int = 120,
        poll_interval: float = 3.0,
        session=None,
    ):
        self.email = (email or "").strip().lower()
        self.relay_url = (relay_url or "").strip()
        if not self.email.endswith("@gmail.com") or self.email.count("@") != 1:
            raise GmailSmsBowerError("Gmail 接码链接来源只接受 @gmail.com", fatal=True, kind="config")
        if not self.relay_url.lower().startswith(("https://", "http://")):
            raise GmailSmsBowerError("Gmail 接码链接必须是 HTTP(S) URL", fatal=True, kind="config")
        self.timeout = max(10, min(int(timeout or 120), 900))
        self.poll_interval = max(1.0, min(float(poll_interval or 3), 30.0))
        self._session = session or requests.Session()
        self._session.trust_env = False
        self._dead = False
        self._cancel_event = threading.Event()
        self._baseline_code = ""
        self._last_returned_code = ""

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        if not account:
            raise GmailSmsBowerError("Gmail 接码链接需要从邮箱池领取账号", fatal=True, kind="config")
        try:
            timeout = int(str(settings.get("gmail_link_timeout") or "120"))
        except (TypeError, ValueError):
            timeout = 120
        try:
            interval = float(str(settings.get("gmail_link_poll_interval") or "3"))
        except (TypeError, ValueError):
            interval = 3.0
        return cls(
            account.get("email", ""),
            account.get("relay_url", ""),
            timeout=timeout,
            poll_interval=interval,
        )

    @classmethod
    def parse_line(cls, line: str) -> dict:
        parts = [part.strip() for part in (line or "").split("----")]
        if len(parts) != 2:
            raise ValueError(
                f"需要 2 段（gmail----接码链接），实际 {len(parts)} 段"
            )
        email, relay_url = parts
        if not email.lower().endswith("@gmail.com") or email.count("@") != 1:
            raise ValueError("第一段必须是 @gmail.com 主邮箱")
        if "+" in email.rsplit("@", 1)[0]:
            raise ValueError("请导入主 Gmail，不要导入已经带 + 的别名")
        if not relay_url.lower().startswith(("https://", "http://")):
            raise ValueError("第二段必须是 HTTP(S) 接码链接")
        return {
            "email": email.lower(),
            "relay_url": relay_url,
            "kind": cls.kind,
        }

    @classmethod
    def expand_import_row(cls, row: dict) -> list[dict]:
        """把主 Gmail 展开为主地址 + 4 个稳定、不重复的 plus alias。

        后缀只由主地址稳定派生。更新接码链接再导入时会更新同一组 5 行，
        不会额外生成第二组别名而突破每主邮箱最多 4 个的约束。
        """
        email = str(row.get("email") or "").lower()
        local, domain = email.rsplit("@", 1)
        seed = email.encode("utf-8")
        digest = hashlib.blake2b(seed, digest_size=16).hexdigest()
        aliases = []
        for index in range(4):
            suffix = digest[index * 8 : (index + 1) * 8]
            alias = f"{local}+{suffix}@{domain}"
            aliases.append({**row, "email": alias})
        return [dict(row), *aliases]

    def create_mailbox(self) -> str:
        # plus aliases 共用同一个收件箱链接。先记住接口当前残留的旧码，
        # 后续只接受变化后的码，避免下一个 alias 误拿上一账号的验证码。
        try:
            baseline, _ = self._fetch_code()
            self._baseline_code = baseline
        except Exception as exc:
            logger.debug("[gmail-link] 预读旧码失败（不阻断注册）: %s", exc)
        logger.info("[gmail-link] 使用 Gmail: %s", self.email)
        return self.email

    @staticmethod
    def _extract_payload_code(payload) -> str:
        if isinstance(payload, dict):
            candidates = [
                payload.get("code"),
                payload.get("verification_code"),
                payload.get("message"),
            ]
            data = payload.get("data")
            if isinstance(data, dict):
                candidates.extend((data.get("code"), data.get("verification_code"), data.get("message")))
            elif data is not None:
                candidates.append(data)
            for candidate in candidates:
                code = extract_otp(str(candidate or ""))
                if code:
                    return code
        return extract_otp(str(payload or "")) or ""

    def _fetch_code(self) -> tuple[str, str]:
        response = self._session.get(self.relay_url, timeout=20)
        raw = (getattr(response, "text", "") or "").strip()
        if getattr(response, "status_code", 0) != 200:
            return "", f"HTTP {getattr(response, 'status_code', 0)}"
        try:
            payload = response.json()
        except Exception:
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw
        code = self._extract_payload_code(payload)
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or "等待验证码")[:160]
        return code, message

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        # Gmail 链接只有“最新验证码”一个槽位，传播通常比普通邮箱慢。
        # provider 专用超时是这个链路的下限，不能被 WebUI 通用 OTP_TIMEOUT
        # （历史默认 10 秒）意外截短；用户仍可在 Gmail 设置中调小/调大它。
        effective_timeout = max(
            1,
            min(max(int(timeout or 0), self.timeout), 900),
        )
        started_at = time.monotonic()
        deadline = started_at + effective_timeout
        reference_code = self._last_returned_code or self._baseline_code

        # 这个接口只有“最新验证码”，没有邮件时间或消息 ID，无法从响应本身
        # 区分旧邮件与 OpenAI 在新 challenge 中复用的相同六位数。每个 relay URL
        # 已由 DB claim + registrar group lock 保证独占，因此先给接口最多 1 秒刷新；
        # 若数字仍相同，也应接受，而不是把有效的复用码一直等到超时。
        same_code_grace = min(1.0, effective_timeout / 2.0)
        if issued_after is not None:
            try:
                same_code_grace = max(
                    0.0,
                    same_code_grace - max(0.0, time.time() - float(issued_after)),
                )
            except (TypeError, ValueError):
                pass
        same_code_not_before = started_at + same_code_grace
        last_error = ""
        while time.monotonic() < deadline:
            if self._cancel_event.is_set():
                raise RuntimeError("Gmail 接码链接轮询已取消")
            code = ""
            try:
                code, message = self._fetch_code()
                if code and (code != reference_code or time.monotonic() >= same_code_not_before):
                    if reference_code and code == reference_code:
                        logger.info(
                            "[gmail-link] OTP 数字与上一轮相同；接码链接已独占，接受复用验证码"
                        )
                    self._last_returned_code = code
                    logger.info("[gmail-link] 收到 OTP: %s**", code[:2])
                    return code
                if code:
                    last_error = "验证码数字与上一轮相同，短暂等待接码接口刷新"
                elif message:
                    last_error = message
            except Exception as exc:
                last_error = str(exc)
            now = time.monotonic()
            sleep_for = min(self.poll_interval, max(0.0, deadline - now))
            if code and reference_code and code == reference_code and now < same_code_not_before:
                sleep_for = min(sleep_for, max(0.0, same_code_not_before - now))
            self._cancel_event.wait(max(0.01, sleep_for))
        raise TimeoutError(
            f"Gmail 接码链接 OTP 超时 ({effective_timeout}s)"
            f"{(': ' + last_error) if last_error else ''}"
        )

    def mark_dead(self, reason: str = "") -> None:
        self._dead = True

    def finalize(self, success: bool = False) -> None:
        self._cancel_event.set()
        try:
            self._session.close()
        except Exception:
            pass
__all__ = [
    "GmailMailProvider",
    "GmailLinkMailProvider",
    "GmailSmsBowerError",
]
