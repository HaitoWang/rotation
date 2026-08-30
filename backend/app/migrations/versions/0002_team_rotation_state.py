"""Add durable cursors and leases for resumable Team rotation.

Revision ID: 0002_team_rotation_state
Revises: 0001_initial
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_team_rotation_state"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("team_mothers", sa.Column("next_rotation_at", sa.Float(), nullable=True))
    op.add_column(
        "team_mothers",
        sa.Column("rotation_stage", sa.String(length=32), nullable=False, server_default="idle"),
    )
    op.add_column(
        "team_mothers",
        sa.Column("rotation_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "team_mothers",
        sa.Column("rotation_lease_until", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column("team_mothers", sa.Column("seat_cache_updated_at", sa.Float(), nullable=True))
    op.add_column("team_mothers", sa.Column("member_cache_updated_at", sa.Float(), nullable=True))
    op.create_index(
        "ix_team_mothers_rotation_due",
        "team_mothers",
        ["enabled", "next_rotation_at", "rotation_lease_until"],
    )

    op.add_column(
        "team_rotation_members",
        sa.Column("stage", sa.String(length=32), nullable=False, server_default="candidate"),
    )
    op.add_column(
        "team_rotation_members",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "team_rotation_members",
        sa.Column("next_attempt_at", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "team_rotation_members",
        sa.Column("lease_until", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column("team_rotation_members", sa.Column("quota_checked_at", sa.Float(), nullable=True))
    op.add_column(
        "team_rotation_members",
        sa.Column("quota_status", sa.String(length=32), nullable=False, server_default="unknown"),
    )
    op.create_index(
        "ix_team_rotation_members_due",
        "team_rotation_members",
        ["mother_id", "status", "next_attempt_at", "lease_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_team_rotation_members_due", table_name="team_rotation_members")
    op.drop_column("team_rotation_members", "quota_status")
    op.drop_column("team_rotation_members", "quota_checked_at")
    op.drop_column("team_rotation_members", "lease_until")
    op.drop_column("team_rotation_members", "next_attempt_at")
    op.drop_column("team_rotation_members", "attempts")
    op.drop_column("team_rotation_members", "stage")
    op.drop_index("ix_team_mothers_rotation_due", table_name="team_mothers")
    op.drop_column("team_mothers", "member_cache_updated_at")
    op.drop_column("team_mothers", "seat_cache_updated_at")
    op.drop_column("team_mothers", "rotation_lease_until")
    op.drop_column("team_mothers", "rotation_attempts")
    op.drop_column("team_mothers", "rotation_stage")
    op.drop_column("team_mothers", "next_rotation_at")
