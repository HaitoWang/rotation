from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        Index("ix_accounts_status_kind", "status", "kind"),
        Index("ix_accounts_claimed_at", "claimed_at"),
    )

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    password: Mapped[str] = mapped_column(Text, default="", nullable=False)
    client_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    relay_url: Mapped[str] = mapped_column(Text, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String(64), default="outlook", nullable=False)
    pooled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="available", nullable=False)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    fail_reason: Mapped[Optional[str]] = mapped_column(Text)


class RegisteredCredential(TimestampMixin, Base):
    __tablename__ = "registered_credentials"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    password: Mapped[str] = mapped_column(Text, default="", nullable=False)
    access_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    session_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    id_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    device_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    csrf_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    cookie_header: Mapped[str] = mapped_column(Text, default="", nullable=False)
    totp_secret: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    totp_factor_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    mail_provider: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class RegistrationRun(Base):
    __tablename__ = "registration_runs"
    __table_args__ = (Index("ix_registration_runs_status_started", "status", "started_at"),)

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    log_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)
    error_category: Mapped[Optional[str]] = mapped_column(String(64))


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class TeamSSOSyncQueue(Base):
    __tablename__ = "team_sso_sync_queue"
    __table_args__ = (Index("ix_team_sso_sync_due", "next_attempt_at", "lease_until"),)

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    lease_until: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class SMSActivationCleanup(Base):
    __tablename__ = "sms_activation_cleanup"
    __table_args__ = (
        Index("ix_sms_activation_cleanup_due", "status", "next_attempt_at", "cancel_after", "lease_until"),
    )

    platform: Mapped[str] = mapped_column(String(64), primary_key=True)
    activation_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(64))
    acquired_at: Mapped[float] = mapped_column(Float, nullable=False)
    cancel_after: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    lease_until: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class TeamMother(TimestampMixin, Base):
    __tablename__ = "team_mothers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(320))
    workspace_id: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    access_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    cookie_header: Mapped[str] = mapped_column(Text, default="", nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    enabled: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    join_mode: Mapped[str] = mapped_column(String(64), default="invite_accept", nullable=False)
    preferred_seat_type: Mapped[str] = mapped_column(String(32), default="standard", nullable=False)
    auto_accept_configured: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    seat_capacity: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    seats_entitled: Mapped[Optional[int]] = mapped_column(Integer)
    seats_in_use: Mapped[Optional[int]] = mapped_column(Integer)
    seats_remaining: Mapped[Optional[int]] = mapped_column(Integer)
    last_checked_at: Mapped[Optional[float]] = mapped_column(Float)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    # Durable scheduler cursor. Redis mirrors the hot cache, while these
    # fields let a new worker resume after a restart or a lost Redis key.
    next_rotation_at: Mapped[Optional[float]] = mapped_column(Float)
    rotation_stage: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)
    rotation_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rotation_lease_until: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    seat_cache_updated_at: Mapped[Optional[float]] = mapped_column(Float)
    member_cache_updated_at: Mapped[Optional[float]] = mapped_column(Float)


class TeamRotationMember(Base):
    __tablename__ = "team_rotation_members"
    __table_args__ = (Index("ix_team_rotation_members_mother", "mother_id", "status", "updated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mother_id: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    member_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    seat_type: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    primary_used_percent: Mapped[Optional[float]] = mapped_column(Float)
    secondary_used_percent: Mapped[Optional[float]] = mapped_column(Float)
    joined_at: Mapped[Optional[float]] = mapped_column(Float)
    last_checked_at: Mapped[Optional[float]] = mapped_column(Float)
    removed_at: Mapped[Optional[float]] = mapped_column(Float)
    error: Mapped[Optional[str]] = mapped_column(Text)
    hub_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    hub_pushed_at: Mapped[Optional[float]] = mapped_column(Float)
    hub_last_attempt_at: Mapped[Optional[float]] = mapped_column(Float)
    hub_error: Mapped[Optional[str]] = mapped_column(Text)
    hub_account_id: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    reauth_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), default="candidate", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    lease_until: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    quota_checked_at: Mapped[Optional[float]] = mapped_column(Float)
    quota_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class TeamRotationMemberHistory(Base):
    __tablename__ = "team_rotation_member_history"
    __table_args__ = (
        UniqueConstraint("email", "mother_id", name="uq_team_rotation_history_email_mother"),
        Index("ix_team_rotation_history_email", "email", "joined_at"),
        Index("ix_team_rotation_history_mother", "mother_id", "joined_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    mother_id: Mapped[str] = mapped_column(String(64), nullable=False)
    joined_at: Mapped[float] = mapped_column(Float, nullable=False)
    removed_at: Mapped[Optional[float]] = mapped_column(Float)
    cooldown_until: Mapped[Optional[float]] = mapped_column(Float)
    permanently_excluded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class TeamRotationEvent(Base):
    __tablename__ = "team_rotation_events"
    __table_args__ = (Index("ix_team_rotation_events_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    mother_id: Mapped[Optional[str]] = mapped_column(String(64))
    email: Mapped[Optional[str]] = mapped_column(String(320))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
