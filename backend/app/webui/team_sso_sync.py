"""Reliable delivery of successful registrations to team-sso's free pool."""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Optional

from . import db

logger = logging.getLogger("team_sso_sync")

_wake = threading.Event()
_stop = threading.Event()
_thread_lock = threading.Lock()
_thread: Optional[threading.Thread] = None


def _clean(value) -> str:
    return str(value or "").strip()


def build_account_content(credential: dict) -> str:
    """Render one account with mailbox metadata preserved for every provider."""
    required = ("email", "password", "access_token", "refresh_token")
    missing = [name for name in required if not _clean(credential.get(name))]
    if missing:
        raise ValueError("缺少同步字段: " + ", ".join(missing))

    payload = {
        "name": _clean(credential.get("email")),
        "credentials": {
            "email": _clean(credential.get("email")),
            "openai_password": _clean(credential.get("password")),
            "chatgpt_access_token": _clean(credential.get("access_token")),
            # team-sso accepts a legacy access token alongside the Codex RT.
            "access_token": _clean(credential.get("access_token")),
            "refresh_token": _clean(credential.get("refresh_token")),
            "id_token": _clean(credential.get("id_token")),
            "totp_secret": _clean(credential.get("totp_secret")),
            "mail_provider": _clean(credential.get("mail_provider")) or "outlook",
            "mail_url": _clean(credential.get("mail_url")),
            "mail_password": _clean(credential.get("mail_password")),
            "mail_client_id": _clean(credential.get("mail_client_id")),
            "mail_refresh_token": _clean(credential.get("mail_refresh_token")),
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def enqueue_registered_account(email: str) -> dict:
    """Persist the account for delivery after registration has committed."""
    cfg = db.get_export_internal_config().get("team_sso", {})
    if not cfg.get("enabled"):
        return {"ok": True, "queued": False, "message": "team-sso 同步未启用"}
    credential = db.get_registered_for_export(email)
    if not credential:
        raise ValueError(f"未找到注册结果: {email}")
    if not _clean(credential.get("refresh_token")):
        return {"ok": True, "queued": False, "message": "账号没有 GPT refresh_token，跳过"}
    content = build_account_content(credential)
    db.enqueue_team_sso_sync(credential["email"], content)
    start_dispatcher()
    _wake.set()
    return {"ok": True, "queued": True, "format": "json"}


def _request(cfg: dict, method: str, content: str = "") -> dict:
    url = _clean(cfg.get("url"))
    key = _clean(cfg.get("sync_key"))
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("team-sso URL 必须以 http:// 或 https:// 开头")
    if not key:
        raise ValueError("team-sso 同步密钥未配置")
    try:
        timeout = max(2.0, min(float(cfg.get("timeout") or 10), 120.0))
    except (TypeError, ValueError):
        timeout = 10.0
    data = None
    headers = {
        "Accept": "application/json",
        "X-Team-SSO-Sync-Key": key,
        "User-Agent": "gpt-outlook-register/team-sso-sync",
    }
    if method == "POST":
        data = json.dumps({"content": content}, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1 << 20)
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"team-sso HTTP {exc.code}: {detail[:500]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"team-sso 连接失败: {exc}") from exc
    if status != 200:
        raise RuntimeError(f"team-sso HTTP {status}")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("team-sso 返回非 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("team-sso 返回格式异常")
    return payload


def test_connection(cfg: dict) -> dict:
    _request(cfg, "GET")
    return {"ok": True, "message": "team-sso free 账号池连通和密钥校验正常"}


def _dispatch_loop() -> None:
    while not _stop.is_set():
        try:
            cfg = db.get_export_internal_config().get("team_sso", {})
            if not cfg.get("enabled"):
                _wake.wait(2.0)
                _wake.clear()
                continue
            rows = db.claim_team_sso_sync(limit=16, lease_seconds=45)
            if not rows:
                _wake.wait(1.0)
                _wake.clear()
                continue
            for row in rows:
                if _stop.is_set():
                    return
                try:
                    result = _request(cfg, "POST", row["content"])
                    db.complete_team_sso_sync(row["email"], row["content"])
                    logger.info(
                        "[team-sso] free 账号同步成功 email=%s added=%s updated=%s skipped=%s",
                        row["email"], result.get("added", 0), result.get("updated", 0),
                        result.get("skipped", 0),
                    )
                except Exception as exc:  # noqa: BLE001
                    db.fail_team_sso_sync(row["email"], row["content"], str(exc))
                    logger.warning("[team-sso] free 账号同步失败，将自动重试 email=%s: %s", row["email"], exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[team-sso] 同步调度异常: %s", exc)
            _stop.wait(1.0)


def start_dispatcher() -> None:
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_dispatch_loop, daemon=True, name="team-sso-sync")
        _thread.start()


def stop_dispatcher() -> None:
    _stop.set()
    _wake.set()
