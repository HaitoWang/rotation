from __future__ import annotations

import threading
import time

import pytest
from app.core.config import Settings
from app.infrastructure.models import (
    Account,
    Base,
    RegisteredCredential,
    Setting,
    TeamMother,
    TeamRotationMember,
)
from app.integrations.team.client import TeamApiClient
from app.repositories.team import TeamRepository
from app.services.team_rotation import TeamRotationService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory):
    async with factory() as session:
        async with session.begin():
            session.add(Setting(key="team_rotation_state", value={"value": "running"}))
            session.add(
                TeamMother(
                    id="mother-1",
                    name="母号",
                    email="mother@example.com",
                    workspace_id="workspace-1",
                    access_token="mother-token",
                    cookie_header="",
                    owner_user_id="owner-1",
                    enabled=1,
                    join_mode="invite_accept",
                    preferred_seat_type="standard",
                    auto_accept_configured=0,
                    seat_capacity={},
                )
            )
            session.add(
                Account(
                    email="child@example.com",
                    password="mail-password",
                    client_id="client",
                    refresh_token="mail-refresh",
                    relay_url="",
                    kind="outlook",
                    pooled=1,
                    status="done",
                )
            )
            session.add(
                RegisteredCredential(
                    email="child@example.com",
                    password="password",
                    access_token="access",
                    session_token="session",
                    refresh_token="refresh",
                    id_token="",
                    device_id="",
                    csrf_token="",
                    cookie_header="",
                    totp_secret="",
                    totp_factor_id="",
                    mail_provider="outlook",
                    extra={},
                )
            )


@pytest.mark.asyncio
async def test_claim_candidate_is_atomic_and_domain_aware(session_factory):
    await _seed(session_factory)
    repository = TeamRepository()
    async with session_factory() as session:
        async with session.begin():
            claim = await repository.claim_candidate(
                session,
                mother_id="mother-1",
                mother_email="mother@example.com",
            )
    assert claim["email"] == "child@example.com"
    assert claim["stage"] == "joining"
    async with session_factory() as session:
        async with session.begin():
            assert await repository.claim_candidate(
                session,
                mother_id="mother-1",
                mother_email="mother@example.com",
            ) is None


@pytest.mark.asyncio
async def test_rotation_resumes_after_join_and_persists_next_cursor(session_factory, monkeypatch):
    await _seed(session_factory)
    monkeypatch.setattr(
        TeamApiClient,
        "get_team_seats",
        lambda self, mother: {
            "entitled": 2,
            "in_use": 0,
            "remaining_configured": 1,
            "preferred_seat_type": "standard",
            "pools": {"standard": {"available": 1}},
        },
    )
    monkeypatch.setattr(
        TeamApiClient,
        "get_team_members",
        lambda self, mother: {"members": [{"id": "child-1", "email": "child@example.com", "seat_type": "default"}]},
    )
    monkeypatch.setattr(
        TeamApiClient,
        "invite_and_accept",
        lambda self, mother, account, **kwargs: {"member_id": "child-1", "seat_type": "standard"},
    )
    service = TeamRotationService(
        session_factory,
        Settings(app_env="test", team_rotation_enabled=False),
    )
    assert await service.process_mother("mother-1") is True
    async with session_factory() as session:
        member = (await session.scalars(select(TeamRotationMember))).one()
        mother = await session.get(TeamMother, "mother-1")
    assert member.status == "active"
    assert member.stage == "hub_push"
    assert member.member_id == "child-1"
    assert mother.rotation_stage == "idle"
    assert mother.next_rotation_at > time.time()


@pytest.mark.asyncio
async def test_rotation_interval_supports_five_seconds_and_clamps_lower_values(session_factory):
    from app.repositories.settings import SettingsRepository

    settings_repo = SettingsRepository()
    async with session_factory() as session:
        async with session.begin():
            session.add(Setting(key="team_rotation_interval_seconds", value={"value": 5}))
        service = TeamRotationService(session_factory, Settings(app_env="test"))
        assert (await service._config(session))["interval_seconds"] == 5

    async with session_factory() as session:
        async with session.begin():
            await settings_repo.set(session, "team_rotation_interval_seconds", 1)
        service = TeamRotationService(session_factory, Settings(app_env="test"))
        assert (await service._config(session))["interval_seconds"] == 5


@pytest.mark.asyncio
async def test_seat_filling_runs_join_operations_concurrently(session_factory, monkeypatch):
    await _seed(session_factory)
    async with session_factory() as session:
        async with session.begin():
            for index in range(2, 5):
                email = f"child{index}@example.com"
                session.add(
                    Account(
                        email=email,
                        password="mail-password",
                        client_id="client",
                        refresh_token="mail-refresh",
                        relay_url="",
                        kind="outlook",
                        pooled=1,
                        status="done",
                    )
                )
                session.add(
                    RegisteredCredential(
                        email=email,
                        password="password",
                        access_token="access",
                        session_token="session",
                        refresh_token="refresh",
                        id_token="",
                        device_id="",
                        csrf_token="",
                        cookie_header="",
                        totp_secret="",
                        totp_factor_id="",
                        mail_provider="outlook",
                        extra={},
                    )
                )
    active = 0
    maximum = 0
    guard = threading.Lock()

    def invite(self, mother, account, **kwargs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return {"member_id": account["email"], "seat_type": "standard"}

    monkeypatch.setattr(TeamApiClient, "invite_and_accept", invite)
    monkeypatch.setattr(
        TeamApiClient,
        "get_team_members",
        lambda self, mother: {
            "members": [
                {"id": f"child{index}@example.com", "email": f"child{index}@example.com", "seat_type": "default"}
                for index in range(1, 5)
            ]
        },
    )

    class ConfirmClient:
        def get_team_members(self, mother):
            return TeamApiClient.get_team_members(self, mother)

    service = TeamRotationService(session_factory, Settings(app_env="test"))
    await service._fill_seats(
        {
            "id": "mother-1",
            "email": "mother@example.com",
            "workspace_id": "workspace-1",
            "preferred_seat_type": "standard",
        },
        ConfirmClient(),
        {"remaining_configured": 4, "in_use": 0},
        {"join_concurrency": 4, "operation_lease_seconds": 240},
    )
    assert maximum > 1
