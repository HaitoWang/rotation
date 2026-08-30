from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.models import RegisteredCredential
from app.infrastructure.redis import RedisManager
from app.repositories.accounts import AccountRepository
from app.repositories.queues import QueueRepository
from app.repositories.runs import RunRepository
from app.repositories.settings import SettingsRepository
from app.repositories.team import TeamRepository
from app.services.sms_persistence import AsyncSmsPersistence
from app.services.team_sso import build_account_content

logger = logging.getLogger(__name__)


def serialize_account(account: Any) -> dict[str, Any]:
    return {
        "email": account.email,
        "password": account.password,
        "client_id": account.client_id,
        "refresh_token": account.refresh_token,
        "relay_url": account.relay_url,
        "kind": account.kind,
    }


class RegistrationService:
    """Queue registration work and expose durable run state."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], redis: RedisManager):
        self.sessions = sessions
        self.redis = redis
        self.accounts = AccountRepository()
        self.runs = RunRepository()
        self.queue = QueueRepository()

    async def enqueue(self, *, email: Optional[str], kind: Optional[str], options: dict) -> Dict[str, str]:
        run_id = uuid.uuid4().hex
        options = dict(options or {})
        # Side effects are handled by the PostgreSQL-backed worker.
        options.setdefault("skip_exports", True)
        provider_kind = (kind or "").strip().lower()
        async with self.sessions() as session:
            async with session.begin():
                pooled = True
                if provider_kind:
                    try:
                        from app.integrations.mail.providers import get_provider_class

                        pooled = bool(get_provider_class(provider_kind).pooled)
                    except Exception as exc:
                        raise LookupError(f"邮箱 provider 不可用: {provider_kind}") from exc
                if pooled:
                    account = await self.accounts.claim(session, email=email, kind=kind)
                    if account is None:
                        raise LookupError("没有可用账号")
                else:
                    # Ephemeral providers still need a durable run owner for
                    # lifecycle/status bookkeeping, but the placeholder is
                    # never presented as an importable mailbox credential.
                    placeholder = f"{provider_kind or 'mail'}_{uuid.uuid4().hex}@placeholder.local"
                    account, _created = await self.accounts.upsert(
                        session, email=placeholder, kind=provider_kind or "mail"
                    )
                    account.pooled = 0
                    account.status = "in_use"
                    await session.flush()
                await self.runs.create(
                    session, run_id=run_id, email=account.email, options=options
                )
                claimed_email = account.email
        try:
            # Persist the first stream event before publishing the job so a
            # very fast worker cannot make ``running`` appear before ``queued``.
            await self.redis.publish_event(run_id, "queued", {"run_id": run_id, "email": claimed_email})
            await self.redis.enqueue("run_registration", job_id=run_id, run_id=run_id)
        except Exception:
            async with self.sessions() as session:
                async with session.begin():
                    await self.accounts.release(session, claimed_email)
                    await self.runs.finish(
                        session, run_id, success=False, error="任务入队失败", category="queue"
                    )
            try:
                await self.redis.publish_event(
                    run_id, "error", {"message": "任务入队失败", "category": "queue"}
                )
            except Exception:
                logger.debug("could not publish queue failure for run %s", run_id, exc_info=True)
            raise
        return {"run_id": run_id, "email": claimed_email}


class RegistrationWorker:
    """Executes one run in a worker process.

    The protocol client is isolated here. The current AuthFlow/mail providers
    are synchronous, so only this boundary uses ``to_thread``; API, storage
    and queue contracts stay fully asynchronous.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], redis: RedisManager, settings):
        self.sessions = sessions
        self.redis = redis
        self.settings = settings
        self.accounts = AccountRepository()
        self.runs = RunRepository()
        self.queue = QueueRepository()
        self.settings_repo = SettingsRepository()
        self.team = TeamRepository()

    async def execute(self, run_id: str) -> None:
        async with self.sessions() as session:
            run = await self.runs.get(session, run_id)
            if run is None:
                logger.error("run %s does not exist", run_id)
                return
            account = await self.accounts.get(session, run.email)
            if account is None:
                await self.runs.finish(session, run_id, success=False, error="账号不存在", category="account")
                await session.commit()
                return
            options = dict(run.options or {})
            email = account.email
            account_data = serialize_account(account)
            runtime_settings = await self.settings_repo.all(session)
            saved_credential = await session.get(RegisteredCredential, email)
            account_data["_saved_password"] = (
                getattr(saved_credential, "password", "") if saved_credential else ""
            )
            account_data["_saved_totp_secret"] = (
                getattr(saved_credential, "totp_secret", "") if saved_credential else ""
            )
            await self.runs.mark_running(session, run_id)
            await session.commit()
        await self.redis.publish_event(run_id, "running", {"run_id": run_id, "email": email})

        try:
            result = await self._run_native(run_id, account_data, options, runtime_settings)
            async with self.sessions() as session:
                async with session.begin():
                    await self._save_credential(session, result)
                    await self.team.resume_after_reauthorization(
                        session, email=str(result.get("email") or email)
                    )
                    if self.settings.team_sso_enabled:
                        await self.queue.enqueue_sso(
                            session,
                            email=str(result.get("email") or email),
                            content=build_account_content({**account_data, **result}),
                        )
                    await self.accounts.finish(session, email, success=True)
                    await self.runs.finish(session, run_id, success=True, result=self._summary(result))
            await self.redis.publish_event(run_id, "done", self._summary(result))
        except Exception as exc:  # noqa: BLE001
            logger.exception("registration run %s failed", run_id)
            error = str(exc)[:4000]
            category = self._error_category(error)
            async with self.sessions() as session:
                async with session.begin():
                    if category == "network":
                        await self.accounts.release(session, email)
                    else:
                        await self.accounts.finish(session, email, success=False, reason=error)
                    await self.runs.finish(
                        session, run_id, success=False, error=error, category=category
                    )
            await self.redis.publish_event(run_id, "error", {"message": error, "category": category})

    async def _run_native(
        self, run_id: str, account: dict, options: dict, runtime_settings: dict
    ) -> dict:
        def blocking(loop) -> dict:
            from app.integrations.mail.providers import create_mail_provider
            from app.integrations.openai.auth_flow import AuthFlow
            from app.integrations.openai.config import Config

            provider_kind = str(
                account.get("kind") or runtime_settings.get("mail_source") or "outlook"
            )
            mail = create_mail_provider(provider_kind, runtime_settings, account)
            saved_password = str(account.get("_saved_password") or "").strip()
            env_overrides = {
                "WEBUI_ALLOW_LOGIN": "1",
                "OTP_TIMEOUT": str(int(options.get("otp_timeout") or 180)),
            }
            if options.get("want_refresh_token", True):
                env_overrides["REQUIRE_REFRESH_TOKEN"] = "1"
            else:
                env_overrides.update({
                    "SKIP_OAUTH_TOKEN_EXCHANGE": "1",
                    "OAUTH_CODEX_RT_EXCHANGE": "0",
                    "OAUTH_CODEX_RT_BEFORE_CALLBACK": "0",
                })
            if saved_password:
                env_overrides["REGISTER_PASSWORD"] = saved_password
            cfg = Config(proxy=str(options.get("proxy") or "").strip() or None)
            sms_callback = None
            sms_enabled = str(runtime_settings.get("sms_enabled", "0")).lower() in {
                "1", "true", "yes", "on"
            }
            if sms_enabled:
                from app.integrations.sms.provider import PhoneCallbackController

                sms_callback = PhoneCallbackController(
                    provider_key=str(runtime_settings.get("sms_provider") or "smsbower"),
                    config=runtime_settings,
                    service=str(runtime_settings.get("sms_service") or "dr"),
                    country=str(runtime_settings.get("sms_country") or "52"),
                    auto_select_country=str(runtime_settings.get("sms_auto_country", "0")).lower()
                    in {"1", "true", "yes", "on"},
                    persistence=AsyncSmsPersistence(self.sessions, loop),
                )
            flow = AuthFlow(
                cfg,
                sms_callback=sms_callback,
                env_overrides=env_overrides,
                account_callback=lambda _email: {
                    "password": saved_password,
                    "totp_secret": str(account.get("_saved_totp_secret") or ""),
                },
            )
            registration_succeeded = False
            root_logger = logging.getLogger()
            thread_id = threading.get_ident()

            class RunLogHandler(logging.Handler):
                def emit(self, record: logging.LogRecord) -> None:
                    if record.thread != thread_id:
                        return
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self.redis.publish_event(
                                run_id,
                                "log",
                                {"line": self.format(record)},
                            ),
                            loop,
                        )
                        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
                    except Exception:
                        pass

            log_handler = RunLogHandler()
            log_handler.redis = self.redis
            log_handler.setLevel(logging.INFO)
            log_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            ))
            root_logger.addHandler(log_handler)
            try:
                try:
                    result = flow.run_register(mail).to_dict()
                except RuntimeError:
                    # Keep the protocol's partial result only when every
                    # credential requested by the caller is already present.
                    result = flow.result.to_dict()
                    wanted = (
                        (not options.get("want_access_token", True) or result.get("access_token"))
                        and (not options.get("want_session_token", True) or result.get("session_token"))
                        and (not options.get("want_refresh_token", True) or result.get("refresh_token"))
                    )
                    if not wanted:
                        raise
                if options.get("want_2fa") and result.get("access_token") and result.get("password"):
                    try:
                        from app.integrations.openai.totp import bind_totp_2fa_inline

                        two_factor = bind_totp_2fa_inline(flow, result["access_token"])
                        if two_factor and two_factor.get("secret"):
                            result["totp_secret"] = two_factor["secret"]
                            result["totp_factor_id"] = two_factor.get("factor_id", "")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("native 2FA binding failed email=%s: %s", account["email"], exc)
                result["mail_provider"] = provider_kind
                result["extra"] = {
                    "mail_url": account.get("relay_url", ""),
                    "mail_password": account.get("password", ""),
                    "mail_client_id": account.get("client_id", ""),
                    "mail_refresh_token": account.get("refresh_token", ""),
                }
                registration_succeeded = True
                return result
            finally:
                try:
                    root_logger.removeHandler(log_handler)
                    if mail is not None and hasattr(mail, "finalize"):
                        mail.finalize(registration_succeeded)
                    if sms_callback is not None and hasattr(sms_callback, "report_rt_result"):
                        sms_callback.report_rt_result(bool(flow.result.refresh_token))
                    flow.session.close()
                except Exception:
                    pass

        await self.redis.publish_event(run_id, "phase", {"phase": "native_registration"})
        return await asyncio.to_thread(blocking, asyncio.get_running_loop())

    async def _save_credential(self, session: AsyncSession, data: dict) -> None:
        email = str(data.get("email") or "").strip().lower()
        if not email:
            raise RuntimeError("registration result has no email")
        credential = await session.get(RegisteredCredential, email)
        if credential is None:
            credential = RegisteredCredential(email=email)
            session.add(credential)
        for field in (
            "password", "access_token", "session_token", "refresh_token", "id_token",
            "device_id", "csrf_token", "cookie_header", "totp_secret", "totp_factor_id",
            "mail_provider",
        ):
            if field in data:
                setattr(credential, field, data.get(field) or "")
        merged_extra = dict(credential.extra or {})
        merged_extra.update(data.get("extra") or {})
        credential.extra = merged_extra

    @staticmethod
    def _summary(data: dict) -> dict:
        return {
            "email": data.get("email", ""),
            "password": data.get("password", ""),
            "access_token_len": len(data.get("access_token") or ""),
            "session_token_len": len(data.get("session_token") or ""),
            "refresh_token_len": len(data.get("refresh_token") or ""),
        }

    @staticmethod
    def _error_category(error: str) -> str:
        lowered = error.lower()
        if any(marker in lowered for marker in (
            "timeout", "timed out", "connection", "connect error", "proxy", "socks",
            "dns", "tls", "ssl", "cloudflare", "remote disconnected", "reset by peer",
        )):
            return "network"
        if any(marker in lowered for marker in ("429", "rate_limit", "too many requests")):
            return "rate_limit"
        if "sms" in lowered and any(marker in lowered for marker in ("exhaust", "no available", "无可用")):
            return "sms_exhausted"
        return "account"
