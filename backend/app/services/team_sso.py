from __future__ import annotations

import json
import logging
from typing import Any, Dict

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.repositories.queues import QueueRepository

logger = logging.getLogger(__name__)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def build_account_content(credential: Dict[str, Any]) -> str:
    required = ("email", "password", "access_token", "refresh_token")
    missing = [name for name in required if not _clean(credential.get(name))]
    if missing:
        raise ValueError("缺少同步字段: " + ", ".join(missing))
    payload = {
        "name": _clean(credential["email"]),
        "credentials": {
            "email": _clean(credential["email"]),
            "openai_password": _clean(credential["password"]),
            "chatgpt_access_token": _clean(credential["access_token"]),
            "access_token": _clean(credential["access_token"]),
            "refresh_token": _clean(credential["refresh_token"]),
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


class TeamSSOService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings):
        self.sessions = sessions
        self.settings = settings
        self.queue = QueueRepository()

    async def dispatch_once(self, *, limit: int = 16) -> int:
        if not self.settings.team_sso_enabled:
            return 0
        url = _clean(self.settings.team_sso_url)
        key = _clean(self.settings.team_sso_sync_key.get_secret_value())
        if not url.startswith(("http://", "https://")) or not key:
            raise RuntimeError("team-sso URL 或同步密钥未配置")
        async with self.sessions() as session:
            async with session.begin():
                rows = await self.queue.claim_sso(session, limit=limit, lease_seconds=45)
        if not rows:
            return 0
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Team-SSO-Sync-Key": key,
            "User-Agent": "regert-register/async-team-sso",
        }
        async with httpx.AsyncClient(timeout=self.settings.team_sso_timeout, trust_env=False) as client:
            for row in rows:
                try:
                    response = await client.post(url, headers=headers, json={"content": row.content})
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise RuntimeError("team-sso 返回格式异常")
                    async with self.sessions() as session:
                        async with session.begin():
                            await self.queue.complete_sso(session, row.email)
                    logger.info("team-sso sync ok email=%s", row.email)
                except Exception as exc:  # noqa: BLE001
                    async with self.sessions() as session:
                        async with session.begin():
                            await self.queue.fail_sso(session, row.email, str(exc))
                    logger.warning("team-sso sync failed email=%s: %s", row.email, exc)
        return len(rows)
