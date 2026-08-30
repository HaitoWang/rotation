"""create async service tables

Revision ID: 0001_initial
Revises:
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("relay_url", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("pooled", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fail_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_index("ix_accounts_status_kind", "accounts", ["status", "kind"])
    op.create_index("ix_accounts_claimed_at", "accounts", ["claimed_at"])
    op.create_table(
        "registered_credentials",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password", sa.Text(), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("session_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("id_token", sa.Text(), nullable=False),
        sa.Column("device_id", sa.String(length=255), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("cookie_header", sa.Text(), nullable=False),
        sa.Column("totp_secret", sa.String(length=255), nullable=False),
        sa.Column("totp_factor_id", sa.String(length=255), nullable=False),
        sa.Column("mail_provider", sa.String(length=64), nullable=False),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_table(
        "registration_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("log_path", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_registration_runs_status_started", "registration_runs", ["status", "started_at"])
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "team_sso_sync_queue",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("email"),
    )
    op.create_index("ix_team_sso_sync_due", "team_sso_sync_queue", ["next_attempt_at", "lease_until"])
    op.create_table(
        "sms_activation_cleanup",
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("activation_id", sa.String(length=255), nullable=False),
        sa.Column("phone_number", sa.String(length=64), nullable=True),
        sa.Column("acquired_at", sa.Float(), nullable=False),
        sa.Column("cancel_after", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("platform", "activation_id"),
    )
    op.create_index(
        "ix_sms_activation_cleanup_due",
        "sms_activation_cleanup",
        ["status", "next_attempt_at", "cancel_after", "lease_until"],
    )
    op.create_table(
        "team_mothers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("workspace_id", sa.String(length=200), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("cookie_header", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("join_mode", sa.String(length=64), nullable=False),
        sa.Column("preferred_seat_type", sa.String(length=32), nullable=False),
        sa.Column("auto_accept_configured", sa.Integer(), nullable=False),
        sa.Column("seat_capacity", sa.JSON(), nullable=False),
        sa.Column("seats_entitled", sa.Integer(), nullable=True),
        sa.Column("seats_in_use", sa.Integer(), nullable=True),
        sa.Column("seats_remaining", sa.Integer(), nullable=True),
        sa.Column("last_checked_at", sa.Float(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id"),
    )
    op.create_table(
        "team_rotation_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("mother_id", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("member_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("seat_type", sa.String(length=32), nullable=False),
        sa.Column("primary_used_percent", sa.Float(), nullable=True),
        sa.Column("secondary_used_percent", sa.Float(), nullable=True),
        sa.Column("joined_at", sa.Float(), nullable=True),
        sa.Column("last_checked_at", sa.Float(), nullable=True),
        sa.Column("removed_at", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("hub_status", sa.String(length=32), nullable=False),
        sa.Column("hub_pushed_at", sa.Float(), nullable=True),
        sa.Column("hub_last_attempt_at", sa.Float(), nullable=True),
        sa.Column("hub_error", sa.Text(), nullable=True),
        sa.Column("hub_account_id", sa.String(length=255), nullable=False),
        sa.Column("reauth_failure_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_team_rotation_members_mother", "team_rotation_members", ["mother_id", "status", "updated_at"])
    op.create_table(
        "team_rotation_member_history",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("mother_id", sa.String(length=64), nullable=False),
        sa.Column("joined_at", sa.Float(), nullable=False),
        sa.Column("removed_at", sa.Float(), nullable=True),
        sa.Column("cooldown_until", sa.Float(), nullable=True),
        sa.Column("permanently_excluded", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", "mother_id", name="uq_team_rotation_history_email_mother"),
    )
    op.create_index("ix_team_rotation_history_email", "team_rotation_member_history", ["email", "joined_at"])
    op.create_index("ix_team_rotation_history_mother", "team_rotation_member_history", ["mother_id", "joined_at"])
    op.create_table(
        "team_rotation_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("mother_id", sa.String(length=64), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_team_rotation_events_created", "team_rotation_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_team_rotation_events_created", table_name="team_rotation_events")
    op.drop_table("team_rotation_events")
    op.drop_index("ix_team_rotation_history_mother", table_name="team_rotation_member_history")
    op.drop_index("ix_team_rotation_history_email", table_name="team_rotation_member_history")
    op.drop_table("team_rotation_member_history")
    op.drop_index("ix_team_rotation_members_mother", table_name="team_rotation_members")
    op.drop_table("team_rotation_members")
    op.drop_table("team_mothers")
    op.drop_index("ix_sms_activation_cleanup_due", table_name="sms_activation_cleanup")
    op.drop_table("sms_activation_cleanup")
    op.drop_index("ix_team_sso_sync_due", table_name="team_sso_sync_queue")
    op.drop_table("team_sso_sync_queue")
    op.drop_table("settings")
    op.drop_index("ix_registration_runs_status_started", table_name="registration_runs")
    op.drop_table("registration_runs")
    op.drop_table("registered_credentials")
    op.drop_index("ix_accounts_claimed_at", table_name="accounts")
    op.drop_index("ix_accounts_status_kind", table_name="accounts")
    op.drop_table("accounts")
