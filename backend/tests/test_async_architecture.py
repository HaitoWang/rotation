from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.core.config import Settings
from app.infrastructure.database import Database
from app.infrastructure.models import (
    Account,
    RegisteredCredential,
    RegistrationRun,
    TeamSSOSyncQueue,
    SMSActivationCleanup,
)
from app.infrastructure.redis import RedisManager
from app.repositories.accounts import AccountRepository
from app.repositories.queues import QueueRepository
from app.services.registration import RegistrationService, RegistrationWorker
from app.services.sms_cleanup import SMSCleanupService
from sqlalchemy import select


class FakeRedis:
    def __init__(self, fail_enqueue: bool = False):
        self.fail_enqueue = fail_enqueue
        self.events = []

    async def enqueue(self, function: str, *, job_id: str, **kwargs):
        if self.fail_enqueue:
            raise ConnectionError("redis unavailable")
        return job_id

    async def publish_event(self, run_id: str, event: str, data: dict):
        self.events.append((run_id, event, data))


class FakeStreamClient:
    async def xread(self, streams, *, block, count):
        key = next(iter(streams))
        return [[key, [("1710000000000-0", {"payload": json.dumps({"event": "done", "data": {"ok": True}})})]]]

    async def aclose(self):
        return None


async def make_database(tmp_path: Path) -> Database:
    database = Database(
        Settings(
            _env_file=None,
            app_env="test",
            database_url="sqlite+aiosqlite:///" + str(tmp_path / "async.db"),
        )
    )
    await database.create_schema()
    return database


@pytest.mark.asyncio
async def test_claim_is_transactional_and_kind_aware(tmp_path: Path):
    database = await make_database(tmp_path)
    try:
        accounts = AccountRepository()
        async with database.session() as session:
            async with session.begin():
                await accounts.upsert(session, email="one@example.com", kind="outlook")
                await accounts.upsert(session, email="two@example.com", kind="gmail")

        async with database.session() as session:
            async with session.begin():
                claimed = await accounts.claim(session, kind="gmail")
                assert claimed is not None
                assert claimed.email == "two@example.com"

        async with database.session() as session:
            assert await accounts.claim(session, email="two@example.com") is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_queue_failure_releases_account_and_finishes_run(tmp_path: Path):
    database = await make_database(tmp_path)
    try:
        accounts = AccountRepository()
        async with database.session() as session:
            async with session.begin():
                await accounts.upsert(session, email="one@example.com")

        redis = FakeRedis(fail_enqueue=True)
        service = RegistrationService(database.session_factory, redis)
        with pytest.raises(ConnectionError):
            await service.enqueue(email="one@example.com", kind="outlook", options={})
        assert [item[1] for item in redis.events] == ["queued", "error"]

        async with database.session() as session:
            account = await accounts.get(session, "one@example.com")
            assert account is not None
            assert account.status == "available"
            run = (await session.scalars(select(RegistrationRun))).first()
            assert run is not None
            assert run.status == "failed"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_non_pooled_provider_gets_ephemeral_placeholder(tmp_path: Path):
    database = await make_database(tmp_path)
    try:
        redis = FakeRedis()
        service = RegistrationService(database.session_factory, redis)
        result = await service.enqueue(email=None, kind="cf_temp", options={})
        assert result["email"].endswith("@placeholder.local")
        async with database.session() as session:
            account = await AccountRepository().get(session, result["email"])
            assert account is not None and account.kind == "cf_temp" and account.status == "in_use"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_redis_stream_event_contains_resume_id():
    manager = RedisManager(Settings(_env_file=None, redis_url="redis://127.0.0.1:63999/0"))
    manager.client = FakeStreamClient()
    event = await anext(manager.events("run-1", block_ms=1))
    assert event["id"] == "1710000000000-0"
    assert event["event"] == "done"
    await manager.close()


@pytest.mark.asyncio
async def test_sms_cleanup_is_noop_when_disabled_or_empty(tmp_path: Path):
    database = await make_database(tmp_path)
    try:
        assert await SMSCleanupService(database.session_factory).dispatch_once() == 0
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_sso_queue_claim_uses_a_lease(tmp_path: Path):
    database = await make_database(tmp_path)
    try:
        queue = QueueRepository()
        async with database.session() as session:
            async with session.begin():
                session.add(
                    TeamSSOSyncQueue(
                        email="one@example.com",
                        content="{}",
                        attempts=0,
                        next_attempt_at=0,
                        lease_until=0,
                        updated_at=0,
                    )
                )
        async with database.session() as session:
            async with session.begin():
                claimed = await queue.claim_sso(session, limit=1, lease_seconds=60)
                assert len(claimed) == 1
                assert claimed[0].attempts == 1
                assert claimed[0].lease_until > 0
        async with database.session() as session:
            async with session.begin():
                assert await queue.complete_sso(session, "one@example.com")
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_expired_active_sms_rental_is_claimed_for_cleanup(tmp_path: Path):
    database = await make_database(tmp_path)
    try:
        queue = QueueRepository()
        async with database.session() as session:
            async with session.begin():
                session.add(
                    SMSActivationCleanup(
                        platform="smsbower",
                        activation_id="stale-1",
                        phone_number="+661",
                        acquired_at=0,
                        cancel_after=0,
                        status="active",
                        attempts=0,
                        next_attempt_at=0,
                        lease_until=0,
                        updated_at=0,
                    )
                )
        async with database.session() as session:
            async with session.begin():
                claimed = await queue.claim_sms_cleanup(session, limit=1, lease_seconds=60)
                assert [row.activation_id for row in claimed] == ["stale-1"]
                assert claimed[0].status == "pending_cancel"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_worker_commits_credential_and_sso_side_effect_atomically(tmp_path: Path):
    database = await make_database(tmp_path)
    redis = FakeRedis()
    try:
        async with database.session() as session:
            async with session.begin():
                session.add(
                    Account(
                        email="one@example.com",
                        kind="outlook",
                        password="mail-password",
                        client_id="client",
                        refresh_token="mail-refresh-token",
                        status="in_use",
                    )
                )
                session.add(
                    RegistrationRun(
                        run_id="run-1",
                        email="one@example.com",
                        status="queued",
                        options={},
                        result={},
                        log_path="",
                    )
                )
        worker = RegistrationWorker(
            database.session_factory,
            redis,
            Settings(_env_file=None, team_sso_enabled=True),
        )

        async def fake_native(*_args, **_kwargs):
            return {
                "email": "one@example.com",
                "password": "openai-password",
                "access_token": "access",
                "session_token": "session",
                "refresh_token": "codex-refresh",
            }

        worker._run_native = fake_native
        await worker.execute("run-1")
        async with database.session() as session:
            account = await session.get(Account, "one@example.com")
            run = await session.get(RegistrationRun, "run-1")
            credential = await session.get(RegisteredCredential, "one@example.com")
            sso = await session.get(TeamSSOSyncQueue, "one@example.com")
            assert account.status == "done"
            assert run.status == "done"
            assert credential.access_token == "access"
            assert sso is not None
    finally:
        await database.close()


def test_registration_error_categories_keep_network_accounts_reusable():
    assert RegistrationWorker._error_category("proxy connection timed out") == "network"
    assert RegistrationWorker._error_category("HTTP 429 rate_limit_exceeded") == "rate_limit"
    assert RegistrationWorker._error_category("invalid username or password") == "account"
