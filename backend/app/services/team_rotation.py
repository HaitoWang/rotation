from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.infrastructure.models import TeamMother, TeamRotationMember
from app.infrastructure.redis import RedisManager
from app.repositories.credentials import CredentialRepository, credential_dict
from app.repositories.settings import SettingsRepository
from app.repositories.team import TeamRepository, mother_dict
from app.services.team_state import TeamStateStore

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "interval_seconds": 300,
    "quota_threshold": 100.0,
    "quota_concurrency": 8,
    "mother_concurrency": 10,
    "join_concurrency": 4,
    "hub_concurrency": 8,
    "seat_cache_ttl": 300,
    "member_refresh_interval": 900,
    "operation_lease_seconds": 240,
    "retry_max_seconds": 1800,
}


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


class TeamRotationService:
    """Durable, per-mother asynchronous Team rotation state machine.

    ARQ calls ``claim_due`` frequently, but only a mother whose durable cursor
    is due gets a job. Redis leases serialize remote side effects while
    PostgreSQL records every stage, so a worker can die between any two calls
    and the next worker can reconcile and continue.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        settings,
        proxy: str = "",
        redis: Optional[RedisManager] = None,
    ):
        self.sessions = sessions
        self.settings = settings
        self.proxy = proxy or settings.team_rotation_proxy
        self.state = TeamStateStore(redis) if redis is not None else None
        self.team = TeamRepository()
        self.credentials = CredentialRepository()
        self.settings_repo = SettingsRepository()

    async def _config(self, session: AsyncSession) -> dict[str, Any]:
        values = await self.settings_repo.all(session)
        config = {**DEFAULTS}
        for key in config:
            stored = values.get(f"team_rotation_{key}", values.get(key))
            if stored is not None:
                config[key] = stored
        config["interval_seconds"] = int(_number(config["interval_seconds"], 300, 5, 86400))
        config["quota_threshold"] = _number(config["quota_threshold"], 100, 1, 100)
        config["quota_concurrency"] = int(_number(config["quota_concurrency"], 8, 1, 32))
        config["mother_concurrency"] = int(_number(config["mother_concurrency"], 10, 1, 16))
        config["join_concurrency"] = int(_number(config["join_concurrency"], 4, 1, 32))
        config["hub_concurrency"] = int(_number(config["hub_concurrency"], 8, 1, 32))
        config["seat_cache_ttl"] = int(_number(config["seat_cache_ttl"], 300, 30, 86400))
        config["member_refresh_interval"] = int(_number(config["member_refresh_interval"], 900, 60, 86400))
        config["operation_lease_seconds"] = int(_number(config["operation_lease_seconds"], 240, 30, 3600))
        config["retry_max_seconds"] = int(_number(config["retry_max_seconds"], 1800, 60, 86400))
        config["proxy"] = str(values.get("team_rotation_proxy") or self.proxy or "").strip()
        return config

    async def runtime_state(self, session: Optional[AsyncSession] = None) -> str:
        owns = session is None
        if owns:
            session = self.sessions()
        try:
            value = await self.settings_repo.get_value(session, "team_rotation_state", None)
            if value is None:
                return "running" if self.settings.team_rotation_enabled else "stopped"
            return str(value or "stopped").lower()
        finally:
            if owns:
                await session.close()

    async def claim_due(self, *, force: bool = False, limit: int = 100, mother_id: str = "") -> list[str]:
        """Advance durable cursors before enqueueing jobs, preventing duplicate ticks."""
        now = time.time()
        async with self.sessions() as session:
            state = await self.runtime_state(session)
            if state != "running":
                return []
            config = await self._config(session)
            rows = await self.team.list_due_mothers(
                session, now=now, limit=max(1, min(limit, 500)), force=force, mother_id=mother_id
            )
            if not rows:
                return []
            await session.commit()
            ids = []
            async with session.begin():
                for row in rows:
                    ids.append(row["id"])
                    await self.team.update_mother(
                        session,
                        row["id"],
                        next_rotation_at=now + config["interval_seconds"],
                        rotation_stage="queued",
                        rotation_lease_until=now + config["operation_lease_seconds"],
                    )
            return ids

    async def process_mother(self, mother_id: str) -> bool:
        config: dict[str, Any] = dict(DEFAULTS)
        lock_token: Optional[str] = None
        dispatch_slot: Optional[str] = None
        heartbeat: Optional[asyncio.Task] = None
        if self.state is not None:
            async with self.sessions() as session:
                config = await self._config(session)
            dispatch_slot = await self.state.acquire_dispatch_slot(
                limit=int(config["mother_concurrency"]),
                ttl=max(60, int(config["operation_lease_seconds"])),
            )
            if not dispatch_slot:
                async with self.sessions() as session:
                    async with session.begin():
                        await self.team.schedule_mother(session, mother_id, when=time.time() + 5)
                return False
            lock_token = await self.state.acquire(
                mother_id, ttl=max(60, int(config["operation_lease_seconds"]))
            )
            if not lock_token:
                await self.state.release_dispatch_slot(dispatch_slot)
                async with self.sessions() as session:
                    async with session.begin():
                        await self.team.schedule_mother(session, mother_id, when=time.time() + 5)
                return False
            heartbeat = asyncio.create_task(self._lock_heartbeat(mother_id, lock_token, dispatch_slot))
        try:
            mother, config = await self._lease_mother(mother_id)
            if mother is None:
                return False
            return bool(await self._run_mother(mother, config))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Team rotation failed mother=%s", mother_id)
            await self._finish_mother(mother_id, config, error=str(exc))
            return False
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            if self.state is not None:
                await self.state.release(mother_id, lock_token)
                await self.state.release_dispatch_slot(dispatch_slot)

    async def _lock_heartbeat(self, mother_id: str, token: str, dispatch_slot: str) -> None:
        while True:
            await asyncio.sleep(30)
            if self.state is None or not await self.state.extend(mother_id, token, ttl=300):
                return
            if not await self.state.extend_dispatch_slot(dispatch_slot, ttl=300):
                return
            try:
                async with self.sessions() as session:
                    async with session.begin():
                        row = await session.get(TeamMother, mother_id)
                        if row is not None and row.rotation_stage == "running":
                            row.rotation_lease_until = time.time() + 300
            except Exception:
                logger.debug("could not extend Team mother lease", exc_info=True)

    async def _lease_mother(self, mother_id: str) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
        now = time.time()
        async with self.sessions() as session:
            config = await self._config(session)
            await session.commit()
            async with session.begin():
                row = await session.scalar(
                    select(TeamMother)
                    .where(TeamMother.id == mother_id, TeamMother.enabled == 1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return None, config
                state = await self.settings_repo.get_value(session, "team_rotation_state", None)
                if state is not None and str(state).lower() != "running":
                    return None, config
                row.rotation_lease_until = now + config["operation_lease_seconds"]
                row.rotation_stage = "running"
                row.rotation_attempts = int(row.rotation_attempts or 0) + 1
                row.last_error = ""
                await session.flush()
                await session.refresh(row)
                return mother_dict(row, include_secret=True), config

    async def _is_running(self) -> bool:
        return await self.runtime_state() == "running"

    async def _run_mother(self, mother: dict[str, Any], config: dict[str, Any]) -> bool:
        mother_id = mother["id"]
        now = time.time()
        client = None
        try:
            from app.integrations.team.client import TeamApiClient

            client = TeamApiClient(config.get("proxy", ""))
            snapshot = await self._load_seat_snapshot(mother, client, config, now)
            if snapshot is None:
                raise RuntimeError("母号席位状态不可用，等待下一次重试")
            await self._recover_and_reconcile(mother, client, config, now)
            if not await self._is_running():
                return False
            await self._resume_removals(mother, client, config)
            if not await self._is_running():
                return False
            await self._check_active_quotas(mother, config)
            if not await self._is_running():
                return False
            # Redis reflects quota removals immediately. Without Redis (unit
            # tests or a degraded cache), keep the freshly fetched snapshot
            # rather than falling back to the stale mother dict.
            if self.state is not None:
                snapshot = await self._load_cached_or_persisted_seats(mother)
            await self._fill_seats(mother, client, snapshot, config)
            if not await self._is_running():
                return False
            await self._push_active_members(mother, config)
            await self._finish_mother(mother_id, config)
            return True
        finally:
            if client is not None:
                client.close()

    async def _load_cached_or_persisted_seats(self, mother: dict[str, Any]) -> dict[str, Any]:
        snapshot = await self.state.get_snapshot(mother["id"]) if self.state is not None else None
        if snapshot:
            return snapshot
        capacity = mother.get("seat_capacity") if isinstance(mother.get("seat_capacity"), dict) else {}
        return {
            "state": "stale",
            "entitled": mother.get("seats_entitled"),
            "in_use": mother.get("seats_in_use"),
            "remaining_configured": mother.get("seats_remaining"),
            "preferred_seat_type": mother.get("preferred_seat_type") or "standard",
            "pools": capacity.get("pools", {}),
            "cached_at": mother.get("seat_cache_updated_at") or 0,
        }

    async def _load_seat_snapshot(self, mother: dict[str, Any], client: Any, config: dict[str, Any], now: float) -> Optional[dict[str, Any]]:
        snapshot = await self._load_cached_or_persisted_seats(mother)
        cached_at = float(snapshot.get("cached_at") or 0)
        fresh = snapshot.get("remaining_configured") is not None and now - cached_at < config["seat_cache_ttl"]
        if fresh:
            return snapshot
        seats = await asyncio.to_thread(client.get_team_seats, mother)
        snapshot = {
            "state": "healthy",
            "entitled": seats.get("entitled"),
            "in_use": seats.get("in_use"),
            "remaining_configured": seats.get("remaining_configured"),
            "preferred_seat_type": seats.get("preferred_seat_type") or mother.get("preferred_seat_type") or "standard",
            "pools": seats.get("pools", {}),
            "cached_at": now,
            "last_error": "",
        }
        async with self.sessions() as session:
            async with session.begin():
                await self.team.update_mother(
                    session, mother["id"], seats_entitled=seats.get("entitled"), seats_in_use=seats.get("in_use"),
                    seats_remaining=seats.get("remaining_configured"), seat_capacity={"pools": seats.get("pools", {})},
                    last_checked_at=now, last_error="", seat_cache_updated_at=now,
                )
        if self.state is not None:
            await self.state.put_snapshot(mother["id"], snapshot, ttl=max(config["seat_cache_ttl"], 60))
        return snapshot

    async def _recover_and_reconcile(self, mother: dict[str, Any], client: Any, config: dict[str, Any], now: float) -> None:
        async with self.sessions() as session:
            async with session.begin():
                await self.team.recover_expired_leases(session, now=now)
                row = await session.get(TeamMother, mother["id"])
                last_sync = float(row.member_cache_updated_at or 0) if row else 0
                assignments = await self.team.list_members(session, mother_id=mother["id"], limit=5000)
        needs_sync = now - last_sync >= config["member_refresh_interval"] or any(
            (item.get("stage") == "joining" and float(item.get("lease_until") or 0) <= now)
            or (item.get("status") == "active" and not item.get("member_id"))
            for item in assignments
        )
        if not needs_sync:
            return
        detail = await asyncio.to_thread(client.get_team_members, mother)
        upstream = {str(item.get("email") or "").lower(): item for item in detail.get("members", []) if item.get("email")}
        async with self.sessions() as session:
            async with session.begin():
                current = await self.team.list_members(session, mother_id=mother["id"], limit=5000)
                for assignment in current:
                    item = upstream.get(str(assignment.get("email") or "").lower())
                    resumable = assignment.get("status") in {"pending", "active"} or assignment.get("stage") in {"joining", "hub_push", "removing"}
                    if item and resumable:
                        await self.team.update_member(
                            session, assignment["id"], status="active", stage="hub_push" if assignment.get("stage") == "joining" else assignment.get("stage") or "active",
                            member_id=item.get("id") or assignment.get("member_id") or "", seat_type=item.get("seat_type") or assignment.get("seat_type") or "unknown",
                            joined_at=assignment.get("joined_at") or now, lease_until=0, next_attempt_at=now, error="",
                        )
                    elif assignment.get("status") == "active" and assignment.get("stage") not in {"removing", "done", "hub_push"}:
                        await self.team.update_member(session, assignment["id"], status="removed", stage="done", removed_at=now, lease_until=0, error="成员已不在 Team")
                        await self.team.record_removal(session, email=assignment["email"], mother_id=mother["id"], reason="成员已不在 Team")
                    elif assignment.get("stage") == "removing" and assignment.get("status") in {"active", "pending"}:
                        permanent = assignment.get("quota_status") == "weekly_exhausted"
                        cooldown = None if permanent else now + 5 * 3600
                        await self.team.update_member(session, assignment["id"], status="exhausted" if permanent else "cooldown", stage="done", removed_at=now, lease_until=0, next_attempt_at=cooldown or 0)
                        await self.team.record_removal(session, email=assignment["email"], mother_id=mother["id"], reason=assignment.get("error") or "恢复已完成的移出操作", cooldown_until=cooldown, permanently_excluded=permanent)
                await self.team.update_mother(session, mother["id"], member_cache_updated_at=now)

    async def _check_active_quotas(self, mother: dict[str, Any], config: dict[str, Any]) -> None:
        async with self.sessions() as session:
            active = await self.team.list_members(session, mother_id=mother["id"], status="active", limit=5000)
        export_cfg = await self._export_config()
        now = time.time()
        due = [item for item in active if now - float(item.get("quota_checked_at") or 0) >= config["interval_seconds"]]
        if not due:
            return
        semaphore = asyncio.Semaphore(config["quota_concurrency"])

        async def check(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            async with semaphore:
                async with self.sessions() as session:
                    row = await self.credentials.get(session, item["email"])
                    account = credential_dict(row) if row else None
                if account is None:
                    return item, {"status": "unknown", "error": "本地凭证不存在"}
                def run() -> dict[str, Any]:
                    hub_id = str(item.get("hub_account_id") or "").strip()
                    if export_cfg.get("enabled") and hub_id:
                        from app.integrations.panels.exporter import get_sub2api_account_status

                        result = get_sub2api_account_status(
                            export_cfg,
                            hub_id,
                            expected_workspace_id=mother["workspace_id"],
                            expected_plan_type=(
                                "self_serve_business_prolite"
                                if str(mother.get("preferred_seat_type") or "standard").lower() == "advanced"
                                else "team"
                            ),
                        )
                        classification = str(result.get("classification") or "unknown")
                        if classification == "weekly_exhausted":
                            return {"status": "alive", "primary_used_percent": 0, "secondary_used_percent": 100, "quota_window": "weekly", "remove": True, "error": result.get("error") or "7d 额度达到阈值"}
                        if classification in {"short_rate_limited", "inactive"}:
                            return {"status": "alive", "primary_used_percent": 100, "secondary_used_percent": 0, "quota_window": "short", "remove": True, "error": result.get("error") or "账号当前不可调度"}
                        if classification in {"auth_required", "error", "missing", "team_mismatch", "hub_error"}:
                            return {"status": "auth_required" if classification == "auth_required" else "unknown", "primary_used_percent": None, "secondary_used_percent": None, "hub_classification": classification, "error": result.get("error") or "Hub 状态异常"}
                        return {"status": "alive", "primary_used_percent": None, "secondary_used_percent": None, "hub_classification": classification, "error": ""}

                    from app.integrations.team.client import TeamApiClient

                    probe = TeamApiClient(config.get("proxy", ""))
                    try:
                        return probe.check_quota(account, mother["workspace_id"])
                    finally:
                        probe.close()

                return item, await asyncio.to_thread(run)

        results = await asyncio.gather(*(check(item) for item in due), return_exceptions=True)
        threshold = float(config["quota_threshold"])
        for result in results:
            if isinstance(result, Exception):
                logger.warning("Team quota probe failed: %s", result)
                continue
            item, quota = result
            checked_at = time.time()
            primary = quota.get("primary_used_percent")
            secondary = quota.get("secondary_used_percent")
            exhausted = bool(quota.get("remove")) or any(value is not None and float(value) >= threshold for value in (primary, secondary))
            if exhausted:
                quota["quota_window"] = "weekly" if secondary is not None and float(secondary) >= threshold and not (primary is not None and float(primary) >= threshold) else "short"
            status = (
                "weekly_exhausted"
                if exhausted and quota.get("quota_window") == "weekly"
                else "exhausted"
                if exhausted
                else str(quota.get("status") or "unknown")
            )
            async with self.sessions() as session:
                async with session.begin():
                    changes = {
                        "quota_checked_at": checked_at,
                        "quota_status": status,
                        "primary_used_percent": primary,
                        "secondary_used_percent": secondary,
                        "last_checked_at": checked_at,
                        "error": str(quota.get("error") or ("额度达到阈值" if exhausted else ""))[:1000],
                    }
                    if quota.get("hub_classification") in {"missing", "team_mismatch"}:
                        changes.update({"hub_status": "pending", "hub_account_id": "", "stage": "hub_push", "next_attempt_at": checked_at})
                    await self.team.update_member(session, item["id"], **changes)
                    token_updates = {}
                    if quota.get("refresh_token"):
                        token_updates["refresh_token"] = quota["refresh_token"]
                    if quota.get("id_token"):
                        token_updates["id_token"] = quota["id_token"]
                    if token_updates:
                        await self.credentials.update_fields(session, item["email"], **token_updates)
            if exhausted:
                await self._remove_exhausted(mother, item, config, quota)

    async def _resume_removals(self, mother: dict[str, Any], client: Any, config: dict[str, Any]) -> None:
        """Retry a Team DELETE left half-complete by a killed worker."""
        async with self.sessions() as session:
            rows = await self.team.list_members(session, mother_id=mother["id"], limit=5000)
        for item in rows:
            if item.get("stage") != "removing" or item.get("status") not in {"active", "pending"}:
                continue
            await self._remove_exhausted(mother, item, config, {
                "error": item.get("error") or "恢复未完成的移出操作",
                "quota_window": "weekly" if item.get("quota_status") == "weekly_exhausted" else "",
            }, client=client)

    async def _remove_exhausted(self, mother: dict[str, Any], item: dict[str, Any], config: dict[str, Any], quota: dict[str, Any], *, client: Any = None) -> None:
        if not await self._is_running():
            return
        now = time.time()
        async with self.sessions() as session:
            async with session.begin():
                await self.team.update_member(session, item["id"], stage="removing", lease_until=now + config["operation_lease_seconds"], next_attempt_at=now, error=str(quota.get("error") or "额度达到阈值")[:1000])
        from app.integrations.panels import exporter

        export_cfg = await self._export_config()
        try:
            if item.get("hub_account_id") and export_cfg.get("enabled"):
                await asyncio.to_thread(exporter.set_sub2api_account_schedulable, export_cfg, item["hub_account_id"], False)
            if client is None:
                from app.integrations.team.client import TeamApiClient
                client = TeamApiClient(config.get("proxy", ""))
                try:
                    await asyncio.to_thread(client.remove_member, mother, item.get("member_id") or "")
                finally:
                    client.close()
            else:
                await asyncio.to_thread(client.remove_member, mother, item.get("member_id") or "")
        except Exception as exc:
            async with self.sessions() as session:
                async with session.begin():
                    await self.team.update_member(session, item["id"], stage="removing", lease_until=0, next_attempt_at=time.time() + self._retry_delay(item, config), error=str(exc)[:1000])
            return
        permanent = (
            str(quota.get("quota_window") or "") == "weekly"
            and str(mother.get("preferred_seat_type") or "standard").lower() != "advanced"
        )
        status = "exhausted" if permanent else "cooldown"
        cooldown = None if permanent else time.time() + 5 * 3600
        async with self.sessions() as session:
            async with session.begin():
                await self.team.update_member(session, item["id"], status=status, stage="done", removed_at=time.time(), lease_until=0, next_attempt_at=cooldown or 0, hub_status="paused", error=str(quota.get("error") or "额度达到阈值")[:1000])
                await self.team.record_removal(session, email=item["email"], mother_id=mother["id"], reason=str(quota.get("error") or "额度达到阈值"), cooldown_until=cooldown, permanently_excluded=permanent)
                current_mother = await session.get(TeamMother, mother["id"])
                if current_mother is not None:
                    await self.team.update_mother(
                        session,
                        mother["id"],
                        seats_remaining=max(0, int(current_mother.seats_remaining or 0) + 1),
                        seats_in_use=max(0, int(current_mother.seats_in_use or 0) - 1),
                        seat_cache_updated_at=time.time(),
                    )
        if self.state is not None:
            await self.state.adjust_remaining(mother["id"], 1)

    async def _fill_seats(self, mother: dict[str, Any], client: Any, snapshot: dict[str, Any], config: dict[str, Any]) -> None:
        remaining = max(0, int(snapshot.get("remaining_configured") or 0))
        now = time.time()
        async with self.sessions() as session:
            reservations = await session.scalar(
                select(func.count()).select_from(TeamRotationMember).where(
                    TeamRotationMember.mother_id == mother["id"],
                    TeamRotationMember.status == "pending",
                    or_(TeamRotationMember.stage != "joining", TeamRotationMember.lease_until > now),
                )
            )
        available = max(0, remaining - int(reservations or 0))
        claims: list[dict[str, Any]] = []
        for _ in range(available):
            if not await self._is_running():
                return
            async with self.sessions() as session:
                async with session.begin():
                    claim = await self.team.claim_candidate(
                        session,
                        mother_id=mother["id"],
                        mother_email=mother.get("email", ""),
                        lease_seconds=config["operation_lease_seconds"],
                    )
            if not claim:
                break
            claims.append(claim)
        if not claims:
            return

        semaphore = asyncio.Semaphore(config["join_concurrency"])

        async def join_one(claim: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            async with semaphore:
                async with self.sessions() as session:
                    credential = await self.credentials.get(session, claim["email"])
                    account = credential_dict(credential) if credential else None
                if account is None:
                    await self._mark_join_failure(claim, "本地凭证不存在", config, auth=False)
                    return claim, False

                def run() -> dict[str, Any]:
                    from app.integrations.team.client import TeamApiClient

                    probe = TeamApiClient(config.get("proxy", ""))
                    try:
                        return probe.invite_and_accept(mother, account, confirm=False)
                    finally:
                        probe.close()

                try:
                    joined = await asyncio.to_thread(run)
                except Exception as exc:  # noqa: BLE001
                    auth = exc.__class__.__name__ == "TeamChildAuthInvalidError"
                    await self._mark_join_failure(claim, str(exc), config, auth=auth)
                    return claim, False
                joined_at = time.time()
                async with self.sessions() as session:
                    async with session.begin():
                        await self.team.update_member(
                            session,
                            claim["id"],
                            status="active",
                            stage="hub_push",
                            member_id=joined.get("member_id", ""),
                            seat_type=joined.get("seat_type") or mother.get("preferred_seat_type") or "standard",
                            joined_at=joined_at,
                            lease_until=0,
                            next_attempt_at=joined_at,
                            error="",
                        )
                        await self.team.record_join(session, email=claim["email"], mother_id=mother["id"], joined_at=joined_at)
                return claim, True

        results = await asyncio.gather(*(join_one(claim) for claim in claims), return_exceptions=True)
        successful_results = [result for result in results if isinstance(result, tuple) and result[1] is True]
        successful = len(successful_results)
        if not successful:
            return
        try:
            detail = await asyncio.to_thread(client.get_team_members, mother)
            upstream = {
                str(item.get("email") or "").lower(): item
                for item in detail.get("members", [])
                if item.get("email")
            }
        except Exception as exc:  # noqa: BLE001
            upstream = {}
            logger.warning("Team member confirmation failed mother=%s: %s", mother["id"], exc)
        async with self.sessions() as session:
            async with session.begin():
                for claim, _ok in successful_results:
                    member = upstream.get(str(claim["email"]).lower())
                    if member:
                        await self.team.update_member(
                            session,
                            claim["id"],
                            member_id=member.get("id") or "",
                            seat_type=member.get("seat_type") or "unknown",
                            next_attempt_at=time.time(),
                            error="",
                        )
                await self.team.update_mother(session, mother["id"], member_cache_updated_at=time.time())
        joined_at = time.time()
        async with self.sessions() as session:
            async with session.begin():
                current = await session.get(TeamMother, mother["id"])
                if current is not None:
                    await self.team.update_mother(
                        session,
                        mother["id"],
                        seats_remaining=max(0, int(current.seats_remaining or remaining) - successful),
                        seats_in_use=int(current.seats_in_use or snapshot.get("in_use") or 0) + successful,
                        seat_cache_updated_at=joined_at,
                    )
        if self.state is not None:
            await self.state.adjust_remaining(mother["id"], -successful)

    async def _mark_join_failure(self, claim: dict[str, Any], error: str, config: dict[str, Any], *, auth: bool) -> None:
        now = time.time()
        async with self.sessions() as session:
            async with session.begin():
                await self.team.update_member(session, claim["id"], status="auth_required" if auth else "failed", stage="awaiting_auth" if auth else "candidate", lease_until=0, next_attempt_at=now + (3600 if auth else self._retry_delay(claim, config)), error=error[:1000])

    async def _push_active_members(self, mother: dict[str, Any], config: dict[str, Any]) -> None:
        export_cfg = await self._export_config()
        if not export_cfg.get("enabled"):
            return
        now = time.time()
        async with self.sessions() as session:
            active = await self.team.list_members(session, mother_id=mother["id"], status="active", limit=5000)
        semaphore = asyncio.Semaphore(config["hub_concurrency"])

        async def push_one(item: dict[str, Any]) -> None:
            if item.get("hub_status") == "success" and item.get("hub_account_id"):
                return
            if not item.get("member_id"):
                return
            if float(item.get("next_attempt_at") or 0) > now:
                return
            async with semaphore:
                if not await self._is_running():
                    return
                async with self.sessions() as session:
                    credential = await self.credentials.get(session, item["email"])
                    account = credential_dict(credential) if credential else None
                    await session.commit()
                    async with session.begin():
                        await self.team.update_member(session, item["id"], stage="hub_push", hub_status="pushing", hub_last_attempt_at=now, lease_until=now + config["operation_lease_seconds"], attempts=int(item.get("attempts") or 0) + 1)
                if account is None:
                    await self._mark_push_failure(item, "本地凭证不存在", config)
                    return
                from app.integrations.panels import exporter

                def push() -> dict[str, Any]:
                    return exporter.run_exports({**account, "account_id": mother["workspace_id"], "plan_type": "team", "seat_type": mother.get("preferred_seat_type") or "standard", "notes": "Team 轮转"}, cpa_cfg=None, sub2api_cfg=export_cfg, sub2api_account_id=item.get("hub_account_id"), sub2api_reactivate_schedulable=item.get("hub_status") == "paused").get("sub2api") or {}

                try:
                    result = await asyncio.to_thread(push)
                except Exception as exc:  # noqa: BLE001
                    result = {"ok": False, "error": str(exc)}
                if result.get("ok"):
                    async with self.sessions() as session:
                        async with session.begin():
                            await self.team.update_member(session, item["id"], stage="active", hub_status="success", hub_account_id=str(result.get("account_id") or item.get("hub_account_id") or ""), hub_pushed_at=time.time(), hub_error="", lease_until=0, next_attempt_at=0, error="")
                else:
                    await self._mark_push_failure(item, str(result.get("error") or "Hub 推送失败"), config)

        await asyncio.gather(*(push_one(item) for item in active), return_exceptions=True)

    async def _mark_push_failure(self, item: dict[str, Any], error: str, config: dict[str, Any]) -> None:
        async with self.sessions() as session:
            async with session.begin():
                await self.team.update_member(session, item["id"], stage="hub_push", hub_status="failed", hub_error=error[:1000], lease_until=0, next_attempt_at=time.time() + self._retry_delay(item, config), error=error[:1000])

    async def _export_config(self) -> dict[str, Any]:
        async with self.sessions() as session:
            values = await self.settings_repo.all(session)
        return {key: value for key, value in values.items() if key.startswith("sub2api_")} | {"enabled": str(values.get("sub2api_enabled", "")).lower() in {"1", "true", "yes", "on"}}

    @staticmethod
    def _retry_delay(item: dict[str, Any], config: dict[str, Any]) -> int:
        attempts = max(0, int(item.get("attempts") or 0))
        return min(int(config["retry_max_seconds"]), max(30, 2 ** min(attempts, 10)))

    async def _finish_mother(self, mother_id: str, config: dict[str, Any], *, error: str = "") -> None:
        now = time.time()
        delay = min(config["retry_max_seconds"], config["interval_seconds"])
        async with self.sessions() as session:
            async with session.begin():
                await self.team.update_mother(session, mother_id, next_rotation_at=now + (delay if error else config["interval_seconds"]), rotation_stage="error" if error else "idle", rotation_lease_until=0, last_error=error[:1000], last_checked_at=now)
                await self.team.record_event(session, level="ERROR" if error else "INFO", action="rotation_error" if error else "cycle", message=error[:1000] if error else "Team 轮转完成一轮", mother_id=mother_id)
        if self.state is not None:
            snapshot = await self.state.get_snapshot(mother_id)
            if snapshot is None:
                snapshot = {"state": "error" if error else "healthy", "remaining_configured": None, "pools": {}}
            snapshot["state"] = "error" if error else "healthy"
            snapshot["last_error"] = error[:1000]
            snapshot["last_checked_at"] = now
            await self.state.put_snapshot(mother_id, snapshot, ttl=max(config["seat_cache_ttl"], 60))
