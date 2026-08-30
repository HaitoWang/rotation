#!/usr/bin/env python3
"""One-time migration from the legacy WebUI SQLite database.

Usage:
    uv run python scripts/migrate_sqlite_to_postgres.py \
      --sqlite backend/data/webui.db

The source database is read-only. Run this before switching production traffic
to ``/api/v1``; re-running is idempotent and refreshes account credentials.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings
from app.infrastructure.database import Database
from app.infrastructure.models import (
    RegisteredCredential,
    RegistrationRun,
    Setting,
    SMSActivationCleanup,
    TeamMother,
    TeamRotationEvent,
    TeamRotationMember,
    TeamRotationMemberHistory,
    TeamSSOSyncQueue,
)
from app.repositories.accounts import AccountRepository
from sqlalchemy import text


def rows(connection: sqlite3.Connection, query: str):
    connection.row_factory = sqlite3.Row
    return connection.execute(query).fetchall()


def epoch(value):
    return datetime.fromtimestamp(value, timezone.utc) if value else None


def json_object(value, default=None):
    try:
        parsed = json.loads(value) if value else default
        return parsed if isinstance(parsed, dict) else (default or {})
    except (TypeError, ValueError, json.JSONDecodeError):
        return default or {}


async def migrate(source: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(source)
    settings = get_settings()
    database = Database(settings)
    account_repo = AccountRepository()
    connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    try:
        async with database.session() as session:
            async with session.begin():
                account_count = 0
                for row in rows(connection, "SELECT * FROM outlook_accounts"):
                    account, _created = await account_repo.upsert(
                        session,
                        email=row["email"],
                        kind=row["kind"] or "outlook",
                        password=row["password"] or "",
                        client_id=row["client_id"] or "",
                        refresh_token=row["refresh_token"] or "",
                        relay_url=row["relay_url"] or "",
                    )
                    # Preserve lifecycle state; upsert intentionally resets
                    # changed credentials to available for normal imports.
                    account.status = row["status"] or "available"
                    account.claimed_at = epoch(row["claimed_at"])
                    account.finished_at = epoch(row["finished_at"])
                    account.fail_reason = row["fail_reason"] or None
                    if row["imported_at"]:
                        account.created_at = epoch(row["imported_at"])
                    account_count += 1

                credential_count = 0
                for row in rows(connection, "SELECT * FROM registered"):
                    email = str(row["email"] or "").strip().lower()
                    if not email:
                        continue
                    credential = await session.get(RegisteredCredential, email)
                    if credential is None:
                        credential = RegisteredCredential(email=email)
                        session.add(credential)
                    for field in (
                        "password", "access_token", "session_token", "refresh_token", "id_token",
                        "device_id", "csrf_token", "cookie_header", "totp_secret", "totp_factor_id",
                    ):
                        if field in row.keys():
                            setattr(credential, field, row[field] or "")
                    legacy_extra = json_object(row["extra_json"], {})
                    legacy_extra["legacy_import"] = True
                    credential.extra = legacy_extra
                    if row["deleted_at"]:
                        credential.deleted_at = epoch(row["deleted_at"])
                    credential_count += 1

                for row in rows(
                    connection,
                    "SELECT run_id, email, status, started_at, finished_at, log_path, error, error_category FROM runs",
                ):
                    source_status = row["status"] or "failed"
                    if source_status == "running":
                        source_status = "failed"
                    run = await session.get(RegistrationRun, str(row["run_id"]))
                    if run is None:
                        run = RegistrationRun(
                            run_id=str(row["run_id"]),
                            email=str(row["email"] or ""),
                            status=source_status,
                            options={},
                            result={},
                            log_path=row["log_path"] or "",
                        )
                        session.add(run)
                    else:
                        run.status = source_status
                        run.log_path = row["log_path"] or run.log_path
                    run.error = row["error"] or (
                        "legacy run was running during migration" if row["status"] == "running" else None
                    )
                    run.error_category = row["error_category"] or None
                    run.started_at = epoch(row["started_at"])
                    run.finished_at = epoch(row["finished_at"])

                for row in rows(connection, "SELECT key, value FROM settings"):
                    setting = await session.get(Setting, str(row["key"]))
                    if setting is None:
                        setting = Setting(key=str(row["key"]), value={})
                        session.add(setting)
                    setting.value = {"value": row["value"]}

                for row in rows(connection, "SELECT * FROM team_mothers"):
                    mother = await session.get(TeamMother, str(row["id"]))
                    values = {
                        "name": row["name"] or "",
                        "email": row["email"] or None,
                        "workspace_id": row["workspace_id"] or "",
                        "access_token": row["access_token"] or "",
                        "cookie_header": row["cookie_header"] or "",
                        "owner_user_id": row["owner_user_id"] or "",
                        "enabled": int(row["enabled"] or 0),
                        "join_mode": row["join_mode"] or "invite_accept",
                        "preferred_seat_type": row["preferred_seat_type"] or "standard",
                        "auto_accept_configured": int(row["auto_accept_configured"] or 0),
                        "seat_capacity": json_object(row["seat_capacity_json"]),
                        "seats_entitled": row["seats_entitled"],
                        "seats_in_use": row["seats_in_use"],
                        "seats_remaining": row["seats_remaining"],
                        "last_checked_at": row["last_checked_at"],
                        "last_error": row["last_error"] or None,
                        "rotation_stage": "idle",
                        "rotation_attempts": 0,
                        "rotation_lease_until": 0,
                        "created_at": epoch(row["created_at"]) or datetime.now(timezone.utc),
                        "updated_at": epoch(row["updated_at"]) or datetime.now(timezone.utc),
                    }
                    if mother is None:
                        mother = TeamMother(id=str(row["id"]), **values)
                        session.add(mother)
                    else:
                        for key, value in values.items():
                            setattr(mother, key, value)

                for row in rows(connection, "SELECT * FROM team_rotation_members"):
                    member = await session.get(TeamRotationMember, int(row["id"]))
                    values = {
                        "mother_id": row["mother_id"] or "",
                        "email": str(row["email"] or "").lower(),
                        "member_id": row["member_id"] or "",
                        "status": row["status"] or "pending",
                        "seat_type": row["seat_type"] or "unknown",
                        "primary_used_percent": row["primary_used_percent"],
                        "secondary_used_percent": row["secondary_used_percent"],
                        "joined_at": row["joined_at"],
                        "last_checked_at": row["last_checked_at"],
                        "removed_at": row["removed_at"],
                        "error": row["error"] or None,
                        "hub_status": row["hub_status"] or "pending",
                        "hub_pushed_at": row["hub_pushed_at"],
                        "hub_last_attempt_at": row["hub_last_attempt_at"],
                        "hub_error": row["hub_error"] or None,
                        "hub_account_id": row["hub_account_id"] or "",
                        "reauth_failure_count": int(row["reauth_failure_count"] or 0),
                        "stage": "active" if row["status"] in {"active", "owner"} else "done" if row["status"] in {"removed", "exhausted", "cooldown"} else "candidate",
                        "attempts": 0,
                        "next_attempt_at": 0,
                        "lease_until": 0,
                        "quota_checked_at": row["last_checked_at"] if row["last_checked_at"] else None,
                        "quota_status": "unknown",
                        "created_at": row["created_at"] or 0,
                        "updated_at": row["updated_at"] or 0,
                    }
                    if member is None:
                        session.add(TeamRotationMember(id=int(row["id"]), **values))
                    else:
                        for key, value in values.items():
                            setattr(member, key, value)

                for row in rows(connection, "SELECT * FROM team_rotation_member_history"):
                    history = await session.get(TeamRotationMemberHistory, int(row["id"]))
                    values = {
                        "email": str(row["email"] or "").lower(),
                        "mother_id": row["mother_id"] or "",
                        "joined_at": row["joined_at"] or 0,
                        "removed_at": row["removed_at"],
                        "cooldown_until": row["cooldown_until"],
                        "permanently_excluded": int(row["permanently_excluded"] or 0),
                        "reason": row["reason"] or "",
                        "created_at": row["created_at"] or 0,
                        "updated_at": row["updated_at"] or 0,
                    }
                    if history is None:
                        session.add(TeamRotationMemberHistory(id=int(row["id"]), **values))
                    else:
                        for key, value in values.items():
                            setattr(history, key, value)

                for row in rows(connection, "SELECT * FROM team_rotation_events"):
                    event = await session.get(TeamRotationEvent, int(row["id"]))
                    values = {
                        "level": row["level"] or "info",
                        "action": row["action"] or "",
                        "mother_id": row["mother_id"] or None,
                        "email": row["email"] or None,
                        "message": row["message"] or "",
                        "created_at": row["created_at"] or 0,
                    }
                    if event is None:
                        session.add(TeamRotationEvent(id=int(row["id"]), **values))
                    else:
                        for key, value in values.items():
                            setattr(event, key, value)

                for row in rows(connection, "SELECT * FROM team_sso_sync_queue"):
                    item = await session.get(TeamSSOSyncQueue, str(row["email"]).lower())
                    values = {
                        "content": row["content"] or "",
                        "attempts": int(row["attempts"] or 0),
                        "next_attempt_at": row["next_attempt_at"] or 0,
                        "lease_until": row["lease_until"] or 0,
                        "last_error": row["last_error"] or None,
                        "updated_at": row["updated_at"] or 0,
                    }
                    if item is None:
                        session.add(TeamSSOSyncQueue(email=str(row["email"]).lower(), **values))
                    else:
                        for key, value in values.items():
                            setattr(item, key, value)

                for row in rows(connection, "SELECT * FROM sms_activation_cleanup"):
                    key = (str(row["platform"]), str(row["activation_id"]))
                    item = await session.get(SMSActivationCleanup, key)
                    values = {
                        "phone_number": row["phone_number"] or None,
                        "acquired_at": row["acquired_at"] or 0,
                        "cancel_after": row["cancel_after"] or 0,
                        "status": row["status"] or "active",
                        "attempts": int(row["attempts"] or 0),
                        "next_attempt_at": row["next_attempt_at"] or 0,
                        "lease_until": row["lease_until"] or 0,
                        "last_error": row["last_error"] or None,
                        "updated_at": row["updated_at"] or 0,
                    }
                    if item is None:
                        session.add(SMSActivationCleanup(platform=key[0], activation_id=key[1], **values))
                    else:
                        for field, value in values.items():
                            setattr(item, field, value)

                # Explicitly imported integer IDs do not advance PostgreSQL's
                # serial sequences; advance them before new writes arrive.
                for table in (
                    "team_rotation_members",
                    "team_rotation_member_history",
                    "team_rotation_events",
                ):
                    await session.execute(
                        text(
                            "SELECT setval(pg_get_serial_sequence(:table, 'id'), "
                            "COALESCE((SELECT MAX(id) FROM " + table + "), 1), true)"
                        ),
                        {"table": table},
                    )
        print(f"migrated accounts={account_count} credentials={credential_count}")
    finally:
        connection.close()
        await database.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sqlite", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(migrate(args.sqlite))


if __name__ == "__main__":
    main()
