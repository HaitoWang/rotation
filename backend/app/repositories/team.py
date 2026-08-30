from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.models import (
    Account,
    RegisteredCredential,
    TeamMother,
    TeamRotationEvent,
    TeamRotationMember,
    TeamRotationMemberHistory,
)


def mother_dict(mother: TeamMother, *, include_secret: bool = False) -> Dict[str, Any]:
    value = {
        "id": mother.id,
        "name": mother.name,
        "email": mother.email or "",
        "workspace_id": mother.workspace_id,
        "enabled": bool(mother.enabled),
        "join_mode": mother.join_mode,
        "preferred_seat_type": mother.preferred_seat_type,
        "auto_accept_configured": bool(mother.auto_accept_configured),
        "seat_capacity": mother.seat_capacity or {},
        "seats_entitled": mother.seats_entitled,
        "seats_in_use": mother.seats_in_use,
        "seats_remaining": mother.seats_remaining,
        "last_checked_at": mother.last_checked_at,
        "last_error": mother.last_error or "",
        "next_rotation_at": mother.next_rotation_at,
        "rotation_stage": mother.rotation_stage,
        "rotation_attempts": mother.rotation_attempts,
        "rotation_lease_until": mother.rotation_lease_until,
        "seat_cache_updated_at": mother.seat_cache_updated_at,
        "member_cache_updated_at": mother.member_cache_updated_at,
        "created_at": mother.created_at,
        "updated_at": mother.updated_at,
    }
    if include_secret:
        value.update({"access_token": mother.access_token, "cookie_header": mother.cookie_header})
    else:
        value.update({
            "has_access_token": bool(mother.access_token),
            "has_cookie": bool(mother.cookie_header),
        })
    return value


class TeamRepository:
    async def resume_after_reauthorization(self, session: AsyncSession, *, email: str, now: Optional[float] = None) -> int:
        """Release a join lease after the credential worker successfully refreshed it."""
        now = float(now or time.time())
        result = await session.execute(
            update(TeamRotationMember)
            .where(
                TeamRotationMember.email == str(email or "").strip().lower(),
                TeamRotationMember.status == "auth_required",
            )
            .values(status="pending", stage="joining", next_attempt_at=now, lease_until=0, error="")
        )
        rows = int(result.rowcount or 0)
        if rows:
            await session.execute(
                update(TeamMother)
                .where(TeamMother.id == select(TeamRotationMember.mother_id).where(TeamRotationMember.email == str(email or "").strip().lower()).scalar_subquery())
                .values(next_rotation_at=now, rotation_stage="queued", rotation_lease_until=0)
            )
        return rows

    async def list_due_mothers(
        self, session: AsyncSession, *, now: float, limit: int = 100, force: bool = False, mother_id: str = ""
    ) -> List[Dict[str, Any]]:
        query = select(TeamMother).where(TeamMother.enabled == 1)
        if mother_id:
            query = query.where(TeamMother.id == str(mother_id))
        lease_available = or_(
            TeamMother.rotation_lease_until.is_(None), TeamMother.rotation_lease_until <= now
        )
        if force:
            query = query.where(lease_available)
        else:
            query = query.where(
                or_(
                    TeamMother.next_rotation_at.is_(None),
                    TeamMother.next_rotation_at <= now,
                    TeamMother.rotation_stage.in_(["queued", "running"]),
                ),
                lease_available,
            )
        rows = (await session.scalars(query.order_by(TeamMother.next_rotation_at.asc()).limit(limit))).all()
        return [mother_dict(row, include_secret=False) for row in rows]

    async def schedule_mothers(self, session: AsyncSession, *, when: float) -> int:
        result = await session.execute(
            update(TeamMother)
            .where(TeamMother.enabled == 1)
            .values(next_rotation_at=float(when), rotation_lease_until=0, rotation_stage="queued")
        )
        return int(result.rowcount or 0)

    async def schedule_mother(self, session: AsyncSession, mother_id: str, *, when: float) -> bool:
        result = await session.execute(
            update(TeamMother)
            .where(TeamMother.id == mother_id)
            .values(next_rotation_at=float(when), rotation_lease_until=0, rotation_stage="queued")
        )
        return bool(result.rowcount)

    async def counts(self, session: AsyncSession) -> dict[str, int]:
        rows = (
            await session.execute(
                select(TeamRotationMember.status, func.count()).group_by(TeamRotationMember.status)
            )
        ).all()
        return {str(status): int(count) for status, count in rows}

    async def mark_member_removed(self, session: AsyncSession, mother_id: str, member_id: str) -> bool:
        result = await session.execute(
            update(TeamRotationMember)
            .where(
                TeamRotationMember.mother_id == mother_id,
                TeamRotationMember.member_id == member_id,
                TeamRotationMember.status.in_(["pending", "active", "owner"]),
            )
            .values(
                status="removed", stage="done", removed_at=time.time(),
                next_attempt_at=0, lease_until=0, error="管理员手动移出",
            )
        )
        return result.rowcount > 0
    async def list_mothers(
        self, session: AsyncSession, *, enabled_only: bool = False, include_secret: bool = False
    ) -> List[Dict[str, Any]]:
        query = select(TeamMother).order_by(TeamMother.created_at.asc())
        if enabled_only:
            query = query.where(TeamMother.enabled == 1)
        rows = (await session.scalars(query)).all()
        return [mother_dict(row, include_secret=include_secret) for row in rows]

    async def get_mother(
        self, session: AsyncSession, mother_id: str, *, include_secret: bool = False
    ) -> Optional[Dict[str, Any]]:
        row = await session.get(TeamMother, mother_id)
        return mother_dict(row, include_secret=include_secret) if row else None

    async def create_mother(self, session: AsyncSession, values: Dict[str, Any]) -> Dict[str, Any]:
        preferred = str(values.get("preferred_seat_type") or "standard").lower()
        if preferred not in {"standard", "advanced"}:
            raise ValueError("母号席位类型只能是 standard/advanced")
        mode = "auto_accept_request" if preferred == "advanced" else str(
            values.get("join_mode") or "invite_accept"
        )
        mother = TeamMother(
            id=str(values.get("id") or uuid.uuid4()),
            name=str(values.get("name") or "").strip(),
            email=str(values.get("email") or "").strip().lower(),
            workspace_id=str(values.get("workspace_id") or "").strip(),
            access_token=str(values.get("access_token") or "").strip(),
            cookie_header=str(values.get("cookie_header") or "").strip(),
            owner_user_id=str(values.get("owner_user_id") or "").strip(),
            enabled=1 if values.get("enabled", True) else 0,
            join_mode=mode,
            preferred_seat_type=preferred,
            auto_accept_configured=1 if values.get("auto_accept_configured") else 0,
            seat_capacity=values.get("seat_capacity") or {},
        )
        session.add(mother)
        await session.flush()
        return mother_dict(mother)

    async def update_mother(
        self,
        session: AsyncSession,
        mother_id: str,
        values: Optional[Dict[str, Any]] = None,
        **extra_values: Any,
    ) -> Optional[Dict[str, Any]]:
        values = {**(values or {}), **extra_values}
        mother = await session.get(TeamMother, mother_id)
        if mother is None:
            return None
        for field in (
            "name", "workspace_id", "access_token", "cookie_header", "owner_user_id",
            "join_mode", "preferred_seat_type", "seat_capacity", "seats_entitled",
            "seats_in_use", "seats_remaining", "last_checked_at", "last_error",
            "next_rotation_at", "rotation_stage", "rotation_attempts", "rotation_lease_until",
            "seat_cache_updated_at", "member_cache_updated_at",
        ):
            if field in values:
                setattr(mother, field, values[field])
        for field in ("enabled", "auto_accept_configured"):
            if field in values:
                setattr(mother, field, 1 if values[field] else 0)
        if "email" in values:
            mother.email = str(values["email"] or "").strip().lower()
        if mother.preferred_seat_type == "advanced":
            mother.join_mode = "auto_accept_request"
        await session.flush()
        # PostgreSQL's onupdate expression expires updated_at; explicitly
        # refresh before building the response so async callers never trigger
        # an implicit IO from mother_dict().
        await session.refresh(mother)
        return mother_dict(mother)

    async def delete_mother(self, session: AsyncSession, mother_id: str) -> bool:
        active = await session.scalar(
            select(func.count())
            .select_from(TeamRotationMember)
            .where(
                TeamRotationMember.mother_id == mother_id,
                TeamRotationMember.status.in_(["pending", "active"]),
            )
        )
        if active:
            raise ValueError("母号仍有轮转中的子号，请先停用并移出成员")
        result = await session.execute(delete(TeamMother).where(TeamMother.id == mother_id))
        return result.rowcount > 0

    async def list_members(
        self, session: AsyncSession, *, mother_id: str = "", status: str = "", limit: int = 500
    ) -> List[Dict[str, Any]]:
        query = (
            select(TeamRotationMember, TeamMother.name, TeamMother.workspace_id)
            .outerjoin(TeamMother, TeamMother.id == TeamRotationMember.mother_id)
            .order_by(TeamRotationMember.updated_at.desc())
            .limit(max(1, min(limit, 5000)))
        )
        if mother_id:
            query = query.where(TeamRotationMember.mother_id == mother_id)
        if status:
            query = query.where(TeamRotationMember.status == status)
        result = await session.execute(query)
        output = []
        for member, mother_name, workspace_id in result.all():
            item = {column.name: getattr(member, column.name) for column in TeamRotationMember.__table__.columns}
            item.update({"mother_name": mother_name or "", "workspace_id": workspace_id or ""})
            output.append(item)
        return output

    async def record_event(
        self,
        session: AsyncSession,
        *,
        level: str,
        action: str,
        message: str,
        mother_id: str = "",
        email: str = "",
    ) -> None:
        session.add(
            TeamRotationEvent(
                level=(level or "INFO").upper(),
                action=action or "flow",
                mother_id=mother_id or None,
                email=email.lower() or None,
                message=(message or "")[:2000],
                created_at=time.time(),
            )
        )

    async def list_events(self, session: AsyncSession, *, limit: int = 100) -> List[Dict[str, Any]]:
        query = (
            select(TeamRotationEvent)
            .order_by(TeamRotationEvent.id.desc())
            .limit(max(1, min(limit, 500)))
        )
        rows = (await session.scalars(query)).all()
        return [
            {column.name: getattr(row, column.name) for column in TeamRotationEvent.__table__.columns}
            for row in rows
        ]

    async def update_member(self, session: AsyncSession, row_id: int, **values: Any) -> bool:
        allowed = {
            "member_id", "status", "seat_type", "primary_used_percent", "secondary_used_percent",
            "joined_at", "last_checked_at", "removed_at", "error", "hub_status", "hub_pushed_at",
            "hub_last_attempt_at", "hub_error", "hub_account_id", "reauth_failure_count",
            "stage", "attempts", "next_attempt_at", "lease_until", "quota_checked_at", "quota_status",
        }
        changes = {key: value for key, value in values.items() if key in allowed}
        if not changes:
            return False
        changes["updated_at"] = time.time()
        result = await session.execute(
            update(TeamRotationMember).where(TeamRotationMember.id == int(row_id)).values(**changes)
        )
        return result.rowcount > 0

    async def claim_candidate(
        self,
        session: AsyncSession,
        *,
        mother_id: str,
        mother_email: str = "",
        now: Optional[float] = None,
        lease_seconds: float = 180,
    ) -> Optional[Dict[str, Any]]:
        """Atomically reserve one complete registered credential for joining."""
        now = float(now or time.time())
        domain = str(mother_email or "").strip().lower().rsplit("@", 1)[-1]
        query = (
            select(RegisteredCredential)
            .where(
                RegisteredCredential.deleted_at.is_(None),
                RegisteredCredential.email != "",
                RegisteredCredential.access_token != "",
                RegisteredCredential.session_token != "",
                RegisteredCredential.refresh_token != "",
                ~exists(
                    select(Account.email).where(
                        Account.email == RegisteredCredential.email,
                        Account.status != "done",
                    )
                ),
                ~exists(
                    select(TeamRotationMember.id).where(
                        TeamRotationMember.email == RegisteredCredential.email,
                        or_(
                            TeamRotationMember.status.in_(["active", "owner", "auth_required"]),
                            and_(
                                TeamRotationMember.status == "pending",
                                or_(
                                    TeamRotationMember.stage != "joining",
                                    TeamRotationMember.lease_until > now,
                                ),
                            ),
                            TeamRotationMember.next_attempt_at > now,
                        ),
                    )
                ),
                ~exists(
                    select(TeamRotationMemberHistory.id).where(
                        TeamRotationMemberHistory.email == RegisteredCredential.email,
                        TeamRotationMemberHistory.mother_id == mother_id,
                        or_(
                            TeamRotationMemberHistory.permanently_excluded == 1,
                            TeamRotationMemberHistory.cooldown_until > now,
                        ),
                    )
                ),
            )
            .order_by(RegisteredCredential.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if domain:
            query = query.where(
                func.lower(func.substr(
                    RegisteredCredential.email,
                    func.instr(RegisteredCredential.email, "@") + 1,
                )) == domain
            )
        candidate = (await session.scalars(query)).first()
        if candidate is None:
            return None

        assignment = (
            await session.scalars(
                select(TeamRotationMember)
                .where(TeamRotationMember.email == candidate.email)
                .with_for_update()
            )
        ).first()
        if assignment is None:
            assignment = TeamRotationMember(
                mother_id=mother_id,
                email=candidate.email,
                status="pending",
                stage="joining",
                attempts=1,
                next_attempt_at=now,
                lease_until=now + max(30, float(lease_seconds)),
                created_at=now,
                updated_at=now,
            )
            session.add(assignment)
        else:
            assignment.mother_id = mother_id
            assignment.status = "pending"
            assignment.stage = "joining"
            assignment.attempts = int(assignment.attempts or 0) + 1
            assignment.next_attempt_at = now
            assignment.lease_until = now + max(30, float(lease_seconds))
            assignment.member_id = ""
            assignment.seat_type = "unknown"
            assignment.primary_used_percent = None
            assignment.secondary_used_percent = None
            assignment.joined_at = None
            assignment.last_checked_at = None
            assignment.removed_at = None
            assignment.error = ""
            assignment.hub_status = "paused" if assignment.hub_account_id else "pending"
            assignment.hub_pushed_at = None
            assignment.hub_last_attempt_at = None
            assignment.hub_error = ""
            assignment.reauth_failure_count = 0
            assignment.quota_checked_at = None
            assignment.quota_status = "unknown"
            assignment.updated_at = now
        await session.flush()
        return {
            "id": int(assignment.id),
            "mother_id": mother_id,
            "email": candidate.email,
            "status": "pending",
            "stage": "joining",
            "attempts": int(assignment.attempts or 0),
        }

    async def recover_expired_leases(self, session: AsyncSession, *, now: float) -> int:
        """Make side-effect stages runnable again after a killed worker."""
        result = await session.execute(
            update(TeamRotationMember)
            .where(
                TeamRotationMember.lease_until > 0,
                TeamRotationMember.lease_until <= now,
                TeamRotationMember.stage.in_(["joining", "hub_push", "removing"]),
            )
            .values(lease_until=0, next_attempt_at=now)
        )
        return int(result.rowcount or 0)

    async def record_join(
        self, session: AsyncSession, *, email: str, mother_id: str, joined_at: Optional[float] = None
    ) -> bool:
        normalized = email.strip().lower()
        now = float(joined_at or time.time())
        history = (
            await session.scalars(
                select(TeamRotationMemberHistory).where(
                    TeamRotationMemberHistory.email == normalized,
                    TeamRotationMemberHistory.mother_id == mother_id,
                )
            )
        ).first()
        if history and history.permanently_excluded:
            return False
        if history is None:
            history = TeamRotationMemberHistory(
                email=normalized, mother_id=mother_id, joined_at=now,
                created_at=now, updated_at=now, reason="",
            )
            session.add(history)
        else:
            history.joined_at = now
            history.removed_at = None
            history.cooldown_until = None
            history.reason = ""
            history.updated_at = now
        return True

    async def record_removal(
        self,
        session: AsyncSession,
        *,
        email: str,
        mother_id: str,
        reason: str = "",
        cooldown_until: Optional[float] = None,
        permanently_excluded: bool = False,
    ) -> bool:
        normalized = str(email or "").strip().lower()
        if not normalized or not mother_id:
            return False
        now = time.time()
        history = (
            await session.scalars(
                select(TeamRotationMemberHistory).where(
                    TeamRotationMemberHistory.email == normalized,
                    TeamRotationMemberHistory.mother_id == mother_id,
                )
            )
        ).first()
        if history is None:
            history = TeamRotationMemberHistory(
                email=normalized,
                mother_id=mother_id,
                joined_at=now,
                created_at=now,
                updated_at=now,
                reason=str(reason or "")[:1000],
            )
            session.add(history)
        history.removed_at = now
        history.cooldown_until = cooldown_until
        history.permanently_excluded = 1 if permanently_excluded else int(history.permanently_excluded or 0)
        history.reason = str(reason or "")[:1000]
        history.updated_at = now
        await session.flush()
        return True
