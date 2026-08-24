"""SQLite 号池 + 注册结果存储。

表结构：
  outlook_accounts: 接码号池（多种邮箱混放，kind 列区分 + 状态机）
  registered:       注册成功结果（凭证 JSON）

关于 outlook_accounts 这个表名：
    它现在装的不止 outlook（还有 gmail / icloud / qq ...），名字已经不准，
    但改表名要动迁移和一堆 SQL，收益只是好看一点，风险不值。
    真正区分类型的是 kind 列。

凭证字段用「并集列」而不是 extra_json：
    outlook/gmail 用 password+client_id+refresh_token，
    icloud 这类中转只用 relay_url，各自把不用的列留空。
    几种邮箱的规模下，并集列比 JSON 好 —— 能建索引、能加约束、
    SQL 里直接看得见。加新邮箱时如果要新字段，就再 ALTER 加一列。
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = Path(os.getenv("WEBUI_DB_PATH") or (Path(__file__).resolve().parent / "webui.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()  # SQLite 写入串行化
_db_local = threading.local()

# These failures describe a durable account/mailbox state, not a transient
# registration attempt.  Bulk reset must not silently requeue them; a single
# account reset remains available after the operator repairs its credentials.
_TERMINAL_FAILURE_MARKERS = (
    "mfa_challenge_missing_totp_secret",
    "totp_activated_but_persistence_failed",
    "existing_account_missing_password",
    "invalid_username_or_password",
    "invalid username or password",
    "deleted or deactivated",
    "account because it has been deleted",
    "account has been deactivated",
    "account is deactivated",
    "imap xoauth2",
    "outlook imap account unusable",
    "authentication failed",
    "authenticate failed",
    "outlook refresh failed",
    "refresh_token 失效",
)


def _bulk_reset_terminal_filter() -> tuple[str, list[str]]:
    """Return SQL clauses excluding known terminal failure reasons."""
    clauses = [
        "instr(lower(coalesce(fail_reason, '')), ?) = 0"
        for _ in _TERMINAL_FAILURE_MARKERS
    ]
    return " AND " + " AND ".join(clauses), list(_TERMINAL_FAILURE_MARKERS)


def _new_connection() -> sqlite3.Connection:
    """创建连接；并发启动时对瞬时文件/SQLite 错误做短退避重试。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    last_error: Optional[Exception] = None
    for attempt in range(6):
        con = None
        try:
            con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=60)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=60000")
            con.execute("PRAGMA synchronous=NORMAL")
            return con
        except (sqlite3.OperationalError, OSError) as exc:
            last_error = exc
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
            if attempt < 5:
                time.sleep(0.05 * (2 ** attempt))
    raise last_error or sqlite3.OperationalError("SQLite connection failed")


def _conn() -> sqlite3.Connection:
    """每个线程复用一个连接，避免高并发下反复打开数据库文件。"""
    path = str(DB_PATH.resolve())
    con = getattr(_db_local, "connection", None)
    if con is not None and getattr(_db_local, "path", "") == path:
        try:
            con.execute("SELECT 1")
            return con
        except sqlite3.ProgrammingError:
            pass
    if con is not None:
        try:
            con.close()
        except Exception:
            pass
    con = _new_connection()
    _db_local.connection = con
    _db_local.path = path
    return con


def init_db():
    con = _conn()
    # WAL 是数据库级设置，只在初始化时切换；不能让每个并发连接重复执行。
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outlook_accounts (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            client_id       TEXT,
            refresh_token   TEXT,
            relay_url       TEXT,       -- 中转取码 URL（icloud 类用，其余留空）
            kind            TEXT NOT NULL DEFAULT 'outlook',
                            -- 邮箱类型，对应 mail_providers 注册表的 kind
            status          TEXT NOT NULL DEFAULT 'available',
                            -- available / in_use / done / failed
            imported_at     REAL,
            claimed_at      REAL,
            finished_at     REAL,
            fail_reason     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_outlook_status ON outlook_accounts(status);
        -- idx_outlook_kind 不在这里建：老库此刻还没有 kind 列，
        -- 建索引会当场报错。放到下面补完列之后再建。

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT
        );

        CREATE TABLE IF NOT EXISTS registered (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            access_token    TEXT,
            session_token   TEXT,
            refresh_token   TEXT,
            id_token        TEXT,
            device_id       TEXT,
            csrf_token      TEXT,
            cookie_header   TEXT,
            totp_secret     TEXT,
            totp_factor_id  TEXT,
            extra_json      TEXT,
            created_at      REAL,
            deleted_at      REAL
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id          TEXT PRIMARY KEY,
            email           TEXT,
            status          TEXT,        -- running / done / failed
            started_at      REAL,
            finished_at     REAL,
            log_path        TEXT,
            error           TEXT,
            error_category  TEXT         -- network / account / unknown
        );

        CREATE TABLE IF NOT EXISTS team_sso_sync_queue (
            email           TEXT PRIMARY KEY,
            content         TEXT NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            lease_until     REAL NOT NULL DEFAULT 0,
            last_error      TEXT,
            updated_at      REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_team_sso_sync_due
        ON team_sso_sync_queue(next_attempt_at, lease_until);

        CREATE TABLE IF NOT EXISTS sms_activation_cleanup (
            platform        TEXT NOT NULL,
            activation_id   TEXT NOT NULL,
            phone_number    TEXT,
            acquired_at     REAL NOT NULL,
            cancel_after    REAL NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
                            -- active / pending_cancel
            attempts        INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            lease_until     REAL NOT NULL DEFAULT 0,
            last_error      TEXT,
            updated_at      REAL NOT NULL,
            PRIMARY KEY(platform, activation_id)
        );

        CREATE INDEX IF NOT EXISTS idx_sms_activation_cleanup_due
        ON sms_activation_cleanup(status, next_attempt_at, cancel_after, lease_until);

        CREATE TABLE IF NOT EXISTS team_mothers (
            id                TEXT PRIMARY KEY,
            name              TEXT NOT NULL,
            email             TEXT,
            workspace_id      TEXT NOT NULL UNIQUE,
            access_token      TEXT,
            cookie_header     TEXT,
            owner_user_id     TEXT,
            enabled           INTEGER NOT NULL DEFAULT 1,
            seats_entitled    INTEGER,
            seats_in_use      INTEGER,
            seats_remaining   INTEGER,
            last_checked_at   REAL,
            last_error        TEXT,
            created_at        REAL NOT NULL,
            updated_at        REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS team_rotation_members (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            mother_id                TEXT NOT NULL,
            email                    TEXT NOT NULL UNIQUE,
            member_id                TEXT,
            status                   TEXT NOT NULL DEFAULT 'pending',
            primary_used_percent     REAL,
            secondary_used_percent   REAL,
            joined_at                REAL,
            last_checked_at          REAL,
            removed_at               REAL,
            error                    TEXT,
            hub_status               TEXT NOT NULL DEFAULT 'pending',
            hub_pushed_at            REAL,
            hub_last_attempt_at       REAL,
            hub_error                TEXT,
            created_at               REAL NOT NULL,
            updated_at               REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_team_rotation_members_mother
        ON team_rotation_members(mother_id, status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS team_rotation_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            level       TEXT NOT NULL,
            action      TEXT NOT NULL,
            mother_id   TEXT,
            email       TEXT,
            message     TEXT NOT NULL,
            created_at  REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_team_rotation_events_created
        ON team_rotation_events(created_at DESC);
    """)
    con.commit()
    # 老 DB migrate：error_category 在后期才加，对已建表补列
    cur = con.execute("PRAGMA table_info(runs)")
    cols = {r[1] for r in cur.fetchall()}
    if "error_category" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN error_category TEXT")
        con.commit()

    # 老 DB migrate：号池多邮箱混放（kind / relay_url 在后期才加）
    # 存量行全部是 outlook 时代导进去的，DEFAULT 'outlook' 正好把它们
    # 归位，不需要额外 UPDATE。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(outlook_accounts)")
    acc_cols = {r[1] for r in cur.fetchall()}
    if "kind" not in acc_cols:
        con.execute(
            "ALTER TABLE outlook_accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'outlook'"
        )
        con.commit()
    if "relay_url" not in acc_cols:
        con.execute("ALTER TABLE outlook_accounts ADD COLUMN relay_url TEXT")
        con.commit()
    # 索引建在补列之后，否则老库上 CREATE INDEX 会因为没有 kind 列而失败
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outlook_kind ON outlook_accounts(kind, status)"
    )
    con.commit()

    # 老 DB migrate：registered 的 2FA 两列（totp_secret / totp_factor_id）后期才加。
    # secret 一次性下发、服务端取不回，务必单独补列持久化。重复执行无副作用。
    cur = con.execute("PRAGMA table_info(registered)")
    reg_cols = {r[1] for r in cur.fetchall()}
    if "totp_secret" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_secret TEXT")
        con.commit()
    if "totp_factor_id" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_factor_id TEXT")
        con.commit()
    if "deleted_at" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN deleted_at REAL")
        con.commit()
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_registered_active_created "
        "ON registered(coalesce(deleted_at, 0), created_at DESC)"
    )
    con.commit()

    # Team 轮转初版没有 Hub 推送状态列。补列后，历史 active 成员会在下一轮自动推送。
    cur = con.execute("PRAGMA table_info(team_rotation_members)")
    rotation_cols = {r[1] for r in cur.fetchall()}
    if "hub_status" not in rotation_cols:
        con.execute(
            "ALTER TABLE team_rotation_members "
            "ADD COLUMN hub_status TEXT NOT NULL DEFAULT 'pending'"
        )
    if "hub_pushed_at" not in rotation_cols:
        con.execute("ALTER TABLE team_rotation_members ADD COLUMN hub_pushed_at REAL")
    if "hub_last_attempt_at" not in rotation_cols:
        con.execute("ALTER TABLE team_rotation_members ADD COLUMN hub_last_attempt_at REAL")
    if "hub_error" not in rotation_cols:
        con.execute("ALTER TABLE team_rotation_members ADD COLUMN hub_error TEXT")
    if "hub_account_id" not in rotation_cols:
        con.execute("ALTER TABLE team_rotation_members ADD COLUMN hub_account_id TEXT")
    con.commit()
    con.close()


# ──────────────────────── outlook 号池 ────────────────────────


def parse_lines(text: str, kind: str = "") -> list[dict]:
    """解析导入文本，委托给 mail_providers 注册表。

    kind 指定 → 用该 provider 的格式解析（推荐）
    kind 为空 → 按段数猜（段数重复时会猜不出）

    非法行抛 ImportValidationError（带行号和原因），**不再静默跳过**。
    以前这里是 `if len(parts) != 4: continue`，用户看到"导入成功"
    但号少了几个，完全没法排查。
    """
    from mail_providers import parse_import_text

    return parse_import_text(text or "", kind)


def import_accounts(text: str, kind: str = "") -> dict:
    """批量入库。已存在的 email 仅在凭证变化时更新。

    解析阶段全对才写：有一行非法就整批拒绝（抛 ImportValidationError），
    不会出现"写进去一半"对不上账的情况。
    """
    rows = parse_lines(text, kind)
    now = time.time()
    inserted = updated = skipped = 0
    with _lock:
        con = _conn()
        for r in rows:
            row_kind = r.get("kind") or kind or "outlook"
            # 凭证并集：不同 provider 用不同子集，没有的留空字符串
            password = r.get("password", "") or ""
            client_id = r.get("client_id", "") or ""
            refresh = r.get("refresh_token", "") or ""
            relay = r.get("relay_url", "") or ""

            cur = con.execute(
                "SELECT password, client_id, refresh_token, relay_url, kind "
                "FROM outlook_accounts WHERE email=?",
                (r["email"],),
            )
            existing = cur.fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                    "relay_url, kind, status, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'available', ?)",
                    (r["email"], password, client_id, refresh, relay, row_kind, now),
                )
                inserted += 1
            elif (
                (existing["password"] or "") != password
                or (existing["client_id"] or "") != client_id
                or (existing["refresh_token"] or "") != refresh
                or (existing["relay_url"] or "") != relay
                or (existing["kind"] or "") != row_kind
            ):
                # 凭证或类型变了 → 覆盖并重置为可用
                con.execute(
                    "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                    "relay_url=?, kind=?, status='available', imported_at=?, fail_reason=NULL "
                    "WHERE email=?",
                    (refresh, password, client_id, relay, row_kind, now, r["email"]),
                )
                updated += 1
            else:
                skipped += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def count_accounts(status: str = "", kind: str = "") -> int:
    con = _conn()
    sql = "SELECT COUNT(*) FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    return con.execute(sql, args).fetchone()[0]


def list_accounts(
    status: str = "", limit: int = 50, offset: int = 0, kind: str = ""
) -> list[dict]:
    con = _conn()
    sql = "SELECT * FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY imported_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def stats_by_kind() -> dict:
    """按邮箱类型分组统计，给 WebUI 顶部展示"每种邮箱各有多少号"。"""
    con = _conn()
    cur = con.execute(
        "SELECT kind, status, COUNT(*) AS n FROM outlook_accounts GROUP BY kind, status"
    )
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        k = r["kind"] or "outlook"
        slot = out.setdefault(
            k, {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
        )
        slot[r["status"]] = r["n"]
        slot["total"] += r["n"]
    return out


def get_account(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM outlook_accounts WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    return dict(row) if row else None


def claim_account(email: str) -> Optional[dict]:
    """原子 claim 指定邮箱（available -> in_use）。

    failed 是不可自动复用的终态。需要重试时必须先显式 reset 为 available，
    避免已删除、密码错误、缺 TOTP 或邮箱认证失效的账号被循环领取。
    网络/环境错误由 registrar.release_unused() 直接放回 available，不受影响。

    按 email 指定时不过滤 kind —— 用户点名要这个号，它是什么类型
    由记录自己的 kind 列说了算，调用方读 account["kind"] 即可。
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with _lock:
        con = _conn()
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE email=? AND status='available'",
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        if str(row["kind"] or "").strip().lower() == "gmail_link" and str(row["relay_url"] or "").strip():
            # 主 Gmail 和四个 plus aliases 共用一个“仅返回最新验证码”的 URL。
            # 点名领取、自动重试也必须遵守组互斥，不能只在 claim_next 中过滤。
            rc = con.execute(
                "UPDATE outlook_accounts SET status='in_use', claimed_at=?, fail_reason=NULL "
                "WHERE email=? AND status='available' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM outlook_accounts AS busy "
                "  WHERE busy.kind='gmail_link' AND busy.status='in_use' "
                "    AND busy.relay_url=?"
                ")",
                (time.time(), email, row["relay_url"]),
            )
        else:
            rc = con.execute(
                "UPDATE outlook_accounts SET status='in_use', claimed_at=?, fail_reason=NULL "
                "WHERE email=? AND status='available'",
                (time.time(), email),
            )
        con.commit()
        if rc.rowcount != 1:
            return None
        return dict(row)


def claim_next(kind: str = "") -> Optional[dict]:
    """原子 claim 任一 available 号。

    kind 指定 → 只从该类型里挑（"选了 gmail 就只跑 gmail 号"）
    kind 为空 → 全池子里挑最早导入的

    多类型混放的关键就在这里：号池里 outlook 和 gmail 并存，
    但当前配置选了哪种，就只 claim 哪种，不会串。
    """
    k = (kind or "").strip().lower()
    with _lock:
        con = _conn()
        for _ in range(50):  # 有限重试，避免并发抢号时无限递归爆栈
            now = time.time()
            if k:
                if k == "gmail_link":
                    # 主 Gmail + 4 个 aliases 共用 relay URL。同一组同时只能
                    # claim 一行，否则 100 worker 会把 5 行全抢走后在进程锁前
                    # 排队，号池看似 100 并发、实际 Gmail 吞吐只剩约 1/5。
                    cur = con.execute(
                        "SELECT a.* FROM outlook_accounts AS a "
                        "WHERE a.status='available' AND a.kind=? "
                        "AND (a.claimed_at IS NULL OR a.claimed_at<=?) "
                        "AND NOT EXISTS ("
                        "  SELECT 1 FROM outlook_accounts AS busy "
                        "  WHERE busy.kind='gmail_link' AND busy.status='in_use' "
                        "    AND busy.relay_url=a.relay_url"
                        ") ORDER BY a.imported_at ASC LIMIT 1",
                        (k, now),
                    )
                else:
                    cur = con.execute(
                        "SELECT * FROM outlook_accounts WHERE status='available' AND kind=? "
                        "AND (claimed_at IS NULL OR claimed_at<=?) "
                        "ORDER BY imported_at ASC LIMIT 1",
                        (k, now),
                    )
            else:
                cur = con.execute(
                    "SELECT a.* FROM outlook_accounts AS a WHERE a.status='available' "
                    "AND (a.claimed_at IS NULL OR a.claimed_at<=?) "
                    "AND (a.kind!='gmail_link' OR a.relay_url='' OR NOT EXISTS ("
                    "  SELECT 1 FROM outlook_accounts AS busy "
                    "  WHERE busy.kind='gmail_link' AND busy.status='in_use' "
                    "    AND busy.relay_url=a.relay_url"
                    ")) "
                    "ORDER BY a.imported_at ASC LIMIT 1",
                    (now,),
                )
            row = cur.fetchone()
            if not row:
                return None
            if str(row["kind"] or "").strip().lower() == "gmail_link" and str(row["relay_url"] or "").strip():
                # 把互斥条件放进 UPDATE，保证即便将来有多个 app 进程，两个
                # 进程同时 SELECT 到同组 aliases 时也只有一个能原子 claim 成功。
                rc = con.execute(
                    "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
                    "WHERE email=? AND status='available' "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM outlook_accounts AS busy "
                    "  WHERE busy.kind='gmail_link' AND busy.status='in_use' "
                    "    AND busy.relay_url=?"
                    ")",
                    (time.time(), row["email"], row["relay_url"]),
                )
            else:
                rc = con.execute(
                    "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
                    "WHERE email=? AND status='available'",
                    (time.time(), row["email"]),
                )
            con.commit()
            if rc.rowcount == 1:
                return dict(row)
            # 被别的线程抢走了，换下一个再试
        return None


def mark_done(email: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='done', finished_at=?, fail_reason=NULL WHERE email=?",
            (time.time(), email.lower()),
        )
        con.commit()


def mark_failed(email: str, reason: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='failed', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def release_unused(email: str) -> None:
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE email=? AND status='in_use'",
            (email.lower(),),
        )
        con.commit()


def reset_to_available(email: str) -> bool:
    """手动重置单个号：done / failed → available，清空时间戳和失败原因。

    场景：注册成功但 refresh_token 没拿到，主人想重新跑一遍这个号。
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            "finished_at=NULL, fail_reason=NULL "
            "WHERE lower(email)=lower(?)",
            (email,),
        )
        con.commit()
        return rc.rowcount > 0


def bulk_reset_to_available(emails: list[str]) -> int:
    """批量重置多个号，跳过已知终态失败号。返回实际被改的行数。"""
    if not emails:
        return 0
    with _lock:
        con = _conn()
        terminal_filter, terminal_args = _bulk_reset_terminal_filter()
        rc = con.execute(
            f"UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            f"finished_at=NULL, fail_reason=NULL "
            f"WHERE lower(email) IN ({','.join(['lower(?)'] * len(emails))}) "
            f"AND status IN ('done', 'failed'){terminal_filter}",
            [*emails, *terminal_args],
        )
        con.commit()
        return rc.rowcount


def reset_failed_to_available() -> int:
    """重置可恢复的 failed 号，跳过已知终态失败号。

    场景：代理短暂抽风导致一波号被冤枉标 failed，主人想给它们一次机会。
    """
    with _lock:
        con = _conn()
        terminal_filter, terminal_args = _bulk_reset_terminal_filter()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', fail_reason=NULL, "
            "finished_at=NULL WHERE status='failed'" + terminal_filter,
            terminal_args,
        )
        con.commit()
        return rc.rowcount


def release_stale_in_use(stale_seconds: float = 1800) -> int:
    """把 claimed_at 超过 N 秒还在 in_use 的号释放回 available。

    场景：上次 webui 强退/进程崩溃，号卡在 in_use 永远不释放。默认 30 分钟。
    """
    with _lock:
        con = _conn()
        cutoff = time.time() - stale_seconds
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE status='in_use' AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
        con.commit()
        return rc.rowcount


def defer_unused(email: str, defer_seconds: float = 120) -> None:
    """Release an unconsumed mailbox but hide it from auto-claim briefly.

    ``claimed_at`` doubles as the next eligible timestamp while a row is
    available. Normal release clears it; claim_next skips a future value.
    This avoids a rate-limited mailbox being reclaimed immediately by one of
    many workers and hammered in a tight loop.
    """
    delay = max(1.0, float(defer_seconds or 0))
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=? "
            "WHERE email=? AND status='in_use'",
            (time.time() + delay, email.lower()),
        )
        con.commit()


def recover_interrupted_runs(reason: str = "服务进程重启，任务已安全回收") -> int:
    """启动时关闭上个进程遗留的 running 记录，避免界面永久显示运行中。"""
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE runs SET status='failed', finished_at=?, error=?, error_category='network' "
            "WHERE status='running'",
            (time.time(), reason),
        )
        con.commit()
        return rc.rowcount


def delete_account(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM outlook_accounts WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_accounts_by_status(status: str) -> int:
    """按状态批量删除。status 必须是 available/in_use/done/failed 之一；
    传 'all' 删全部。返回受影响行数。"""
    valid = {"available", "in_use", "done", "failed", "all"}
    s = (status or "").strip().lower()
    if s not in valid:
        return 0
    with _lock:
        con = _conn()
        if s == "all":
            rc = con.execute("DELETE FROM outlook_accounts")
        else:
            rc = con.execute("DELETE FROM outlook_accounts WHERE status=?", (s,))
        con.commit()
        return rc.rowcount


def delete_accounts_by_emails(emails: list[str]) -> int:
    """按 email 列表批量删除。返回受影响行数。"""
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM outlook_accounts WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def stats() -> dict:
    con = _conn()
    cur = con.execute(
        "SELECT status, COUNT(*) AS n FROM outlook_accounts GROUP BY status"
    )
    out = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
    for r in cur.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


# ──────────────────────── 注册结果存储 ────────────────────────


def save_registered(d: dict) -> None:
    """保存注册成功（或部分成功）的凭证。覆盖同邮箱旧记录。

    凭证三件套（access_token / session_token / refresh_token）单独存列；
    其余字段（如 device_id / cookie_header / id_token / 自定义元数据）打包进 extra_json。
    """
    email = (d.get("email") or "").lower()
    if not email:
        return
    password = d.get("password", "") or ""
    extra = {k: v for k, v in d.items() if k not in {
        "email", "password", "access_token", "session_token", "refresh_token",
        "id_token", "device_id", "csrf_token", "cookie_header",
        "totp_secret", "totp_factor_id",
    }}
    with _lock:
        con = _conn()
        # ⚠️ INSERT OR REPLACE 是**整行替换**，不是按字段合并 —— 没写的列会被清空。
        #    重跑同一个邮箱时这会咬人：第一轮 register_password 设了密码但 OTP 超时，
        #    save_password_early 把密码存下了；第二轮 OpenAI 已经认识这个邮箱了，
        #    走 passwordless_login 分支根本不调 register_password，
        #    这一轮的 d["password"] 是空的 —— 直接 REPLACE 就把上一轮的密码冲没了。
        #    密码是 OpenAI 侧的**持久状态**，"这一轮没设" ≠ "这个号没有密码"，
        #    所以空值不覆盖非空旧值。
        #    refresh_token 例外：OAuth 失败时本轮为空，不能抹掉上一轮已验证的 RT。
        # totp_secret 和密码同理，甚至更严：secret【一次性下发、服务端取不回】，
        #    丢了 = 该号 2FA 永久锁死。重跑同邮箱（已绑过 2FA）时这一轮不会再绑，
        #    d 里没有 secret —— 绝不能拿空值把库里已存的 secret 冲没。
        #    与密码合成一次 SELECT，顺带把两列旧值一起兜住。
        totp_secret = (d.get("totp_secret") or "").strip()
        refresh_token = (d.get("refresh_token") or "").strip()
        totp_factor_id = (d.get("totp_factor_id") or "").strip()
        if not password or not totp_secret or not refresh_token:
            row = con.execute(
                "SELECT password, totp_secret, totp_factor_id, refresh_token FROM registered WHERE email=?",
                (email,),
            ).fetchone()
            if row:
                if not password and (row["password"] or "").strip():
                    password = row["password"]
                if not totp_secret and (row["totp_secret"] or "").strip():
                    totp_secret = row["totp_secret"]
                    # factor_id 跟着 secret 走：本轮没绑就沿用旧的
                    totp_factor_id = totp_factor_id or (row["totp_factor_id"] or "")
                if not refresh_token and (row["refresh_token"] or "").strip():
                    refresh_token = row["refresh_token"]
        con.execute(
            "INSERT OR REPLACE INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                password,
                d.get("access_token", ""),
                d.get("session_token", ""),
                refresh_token,
                d.get("id_token", ""),
                d.get("device_id", ""),
                d.get("csrf_token", ""),
                d.get("cookie_header", ""),
                totp_secret,
                totp_factor_id,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                time.time(),
            ),
        )
        con.commit()
        con.close()


def save_password_early(email: str, password: str) -> None:
    """密码一在 OpenAI 侧生效就落盘，不等整个注册流程跑完。

    由 AuthFlow 的 on_password 回调触发（register_password 里 POST 200 之后）。
    此刻账号+密码在 OpenAI 那边已经建好，但本地还要过发码/验证/建账户三关，
    挂在任何一关都走不到 save_registered ——
    密码只活在内存里，进程一退号就成了谁也登不进去的孤儿。

    只写 email + password；token 三件套留空，等流程跑通后 save_registered
    用同一个 email 主键覆盖同一行补上。extra_json 打 pending 标记，
    方便一眼认出"有密码没凭证"的半成品行（跑通后会被 save_registered 清掉）。

    ⚠️ 行已存在时**只 UPDATE password**，绝不动已有的 token：
       重跑一个之前跑通过的邮箱时，不能把人家的凭证清空。
    """
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        return
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, extra_json, created_at) "
            "VALUES (?, ?, '', '', '', '', '', '', '', ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET password=excluded.password",
            (
                email,
                password,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def save_totp_early(email: str, secret: str, factor_id: str = "") -> None:
    """Persist a newly activated TOTP factor before any later OAuth/SMS work.

    OpenAI only returns the secret during enrollment.  Once activation succeeds,
    a later failure cannot fetch it again, so this write must happen immediately
    and must merge with (rather than replace) an early password or valid tokens.
    """
    email = (email or "").strip().lower()
    secret = normalize_totp_secret(secret)
    factor_id = (factor_id or "").strip()
    if not email or not secret:
        return
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, totp_secret, "
            "totp_factor_id, extra_json, created_at) "
            "VALUES (?, '', '', '', '', '', '', '', '', ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "totp_secret=excluded.totp_secret, "
            "totp_factor_id=CASE WHEN excluded.totp_factor_id<>'' "
            "THEN excluded.totp_factor_id ELSE registered.totp_factor_id END",
            (
                email,
                secret,
                factor_id,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def normalize_totp_secret(raw: str) -> str:
    """把用户手填的 TOTP secret 规范化成可用的 base32，非法值抛 ValueError。

    登录侧（auth_flow._totp_now）拿到 secret 直接 b32decode，**不做任何校验** ——
    脏值存进去要等到真登录时才炸，那时只看到一句 base32 解码异常，
    根本看不出是手填填错了。所以校验必须挡在写库这一关。

    接受的输入：
      - 裸 base32:  JBSWY3DPEHPK3PXP / jbswy3dp ehpk 3pxp / JBSW-Y3DP-EHPK
      - otpauth URI: otpauth://totp/ChatGPT:a@b.com?secret=JBSWY3DP&issuer=...
        （从手机 App 导出/二维码解码出来的就是这个格式，直接粘进来很常见）
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # otpauth:// URI 抽 secret 参数
    if s.lower().startswith("otpauth://"):
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(s).query)
            s = (qs.get("secret") or [""])[0]
        except Exception:
            raise ValueError("otpauth 链接解析失败，请直接填 secret")
        if not s:
            raise ValueError("otpauth 链接里没有 secret 参数")
    # 去掉分隔符（手机 App 展示时常带空格/连字符）并统一大写
    s = s.replace(" ", "").replace("-", "").replace("_", "").upper()
    # base32 只有 A-Z 和 2-7，先挡掉明显非法字符再解码，报错更好懂
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in s):
        raise ValueError("TOTP secret 含非法字符（base32 只允许 A-Z 和 2-7）")
    try:
        # 补 padding 后试解，解得开才算合法。auth_flow 那边也是这么补的。
        decoded = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        raise ValueError("TOTP secret 不是合法的 base32")
    if len(decoded) < 10:
        raise ValueError(f"TOTP secret 太短（解出 {len(decoded)} 字节，通常应为 20 字节）")
    return s


def update_registered_manual(email: str, password: Optional[str] = None,
                             totp_secret: Optional[str] = None) -> bool:
    """手动修正某个已注册账号的密码 / TOTP secret。

    ⚠️ 只改**本地库**，不会同步到 OpenAI —— 这里改密码不等于改了账号密码。
       用途是把外部已知的凭证补进来，或修正记录错误。

    传 None = 该字段不动（不是清空）。用 None 而不是空串做"不修改"的标记，
    是为了留出"主人真想清空某字段"的余地（传空串即清空）。

    totp_secret 会先过 normalize_totp_secret 校验，非法直接抛 ValueError；
    宁可这里报错，也不能让脏值躺进库里等登录时才炸。

    返回 False 表示该邮箱不存在（不会凭空插入新行 —— 手填是"修正已有记录"，
    真要新增外部账号是另一件事，走单独的导入功能）。
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    sets, vals = [], []
    if password is not None:
        sets.append("password=?")
        vals.append(password)
    if totp_secret is not None:
        # 空串 = 主人主动清空；非空则必须过校验
        sets.append("totp_secret=?")
        vals.append(normalize_totp_secret(totp_secret) if totp_secret.strip() else "")
    if not sets:
        return False
    with _lock:
        con = _conn()
        row = con.execute("SELECT email FROM registered WHERE email=?", (email,)).fetchone()
        if not row:
            return False
        vals.append(email)
        con.execute(f"UPDATE registered SET {', '.join(sets)} WHERE email=?", vals)
        con.commit()
        return True


def update_registered_codex_tokens(
    email: str,
    *,
    refresh_token: str = "",
    id_token: str = "",
) -> bool:
    """Persist rotated Codex OAuth tokens without touching ChatGPT web tokens."""
    email = (email or "").strip().lower()
    refresh_token = (refresh_token or "").strip()
    id_token = (id_token or "").strip()
    if not email or not (refresh_token or id_token):
        return False

    sets: list[str] = []
    values: list[str] = []
    if refresh_token:
        sets.append("refresh_token=?")
        values.append(refresh_token)
    if id_token:
        sets.append("id_token=?")
        values.append(id_token)
    values.append(email)

    with _lock:
        con = _conn()
        cur = con.execute(
            f"UPDATE registered SET {', '.join(sets)} WHERE email=?",
            values,
        )
        con.commit()
        return cur.rowcount > 0


def update_plus_check(email: str, plus_info: dict) -> None:
    """把 Plus 检查结果写入 extra_json.plus_check。"""
    email = email.lower()
    con = _conn()
    cur = con.execute("SELECT extra_json FROM registered WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return
    extra = {}
    if row["extra_json"]:
        try:
            extra = json.loads(row["extra_json"])
        except Exception:
            extra = {}
    extra["plus_check"] = plus_info
    with _lock:
        con.execute(
            "UPDATE registered SET extra_json=? WHERE email=?",
            (json.dumps(extra, ensure_ascii=False), email),
        )
        con.commit()


def _registered_where(filt: str) -> str:
    clauses = ["coalesce(deleted_at, 0) = 0"]
    if filt == "has_rt":
        clauses.append("length(refresh_token) > 0")
    elif filt == "no_rt":
        clauses.append("coalesce(length(refresh_token),0) = 0")
    elif filt == "unchecked":
        clauses.append("(extra_json IS NULL OR extra_json NOT LIKE '%\"plus_check\"%')")
    elif filt == "free":
        clauses.append("extra_json LIKE '%\"free\"%'")
    elif filt == "plus":
        clauses.append("(extra_json LIKE '%\"plus_eligible\"%' OR extra_json LIKE '%\"plus_active\"%')")
    elif filt == "banned":
        clauses.append("extra_json LIKE '%\"banned\"%'")
    return "WHERE " + " AND ".join(clauses)


def _registered_export_where(filt: str) -> str:
    """把注册结果筛选条件限定到 registered 别名。"""
    return (
        _registered_where(filt)
        .replace("deleted_at", "r.deleted_at")
        .replace("refresh_token", "r.refresh_token")
        .replace("extra_json", "r.extra_json")
    )


def count_registered(filter_rt: str = "all") -> int:
    con = _conn()
    cur = con.execute(f"SELECT COUNT(*) FROM registered {_registered_where(filter_rt)}")
    return cur.fetchone()[0]


def list_registered(limit: int = 20, offset: int = 0, filter_rt: str = "all") -> list[dict]:
    con = _conn()
    where = _registered_where(filter_rt)
    cur = con.execute(
        f"SELECT email, password, totp_secret, "
        f"length(access_token) AS at_len, length(session_token) AS st_len, "
        f"length(refresh_token) AS rt_len, extra_json, created_at FROM registered "
        f"{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        plus = None
        if d.get("extra_json"):
            try:
                extra = json.loads(d["extra_json"])
                plus = extra.get("plus_check")
            except Exception:
                pass
        d["plus_check"] = plus
        d.pop("extra_json", None)
        rows.append(d)
    return rows


def list_registered_full(limit: int = 5000, filter_rt: str = "all") -> list[dict]:
    """返回完整凭证（用于批量导出）。每行同 get_registered 的格式。"""
    con = _conn()
    cur = con.execute(
        "SELECT r.*, o.kind AS mail_provider, o.relay_url AS mail_url, "
        "o.password AS mail_password, o.client_id AS mail_client_id, "
        "o.refresh_token AS mail_refresh_token "
        "FROM registered AS r LEFT JOIN outlook_accounts AS o "
        "ON lower(o.email)=lower(r.email) "
        f"{_registered_export_where(filter_rt)} "
        "ORDER BY r.created_at DESC LIMIT ?",
        (max(0, min(int(limit or 0), 100000)),),
    )
    fetched = cur.fetchall()
    con.close()
    out = []
    for row in fetched:
        d = dict(row)
        if d.get("extra_json"):
            try:
                d["extra"] = json.loads(d["extra_json"])
            except Exception:
                d["extra"] = {}
        for key in ("mail_provider", "mail_url", "mail_password", "mail_client_id", "mail_refresh_token"):
            if not d.get(key) and d.get("extra", {}).get(key):
                d[key] = d["extra"][key]
        d.pop("extra_json", None)
        out.append(d)
    return out


def list_registered_by_emails(emails: list[str]) -> list[dict]:
    """按 email 列表返回完整凭证（批量导出勾选的号用）。

    - 行序 = created_at 倒序，和「注册结果」表格里看到的一致，方便核对。
    - 查不到的 email 直接不出现（号已被删掉的情况），不报错。
    - SQLite 单条语句变量数有上限（默认 999），所以分批查。
    """
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return []

    con = _conn()
    out = []
    CHUNK = 500
    for i in range(0, len(cleaned), CHUNK):
        part = cleaned[i:i + CHUNK]
        placeholders = ",".join("?" * len(part))
        cur = con.execute(
            f"SELECT r.*, o.kind AS mail_provider, o.relay_url AS mail_url, "
            f"o.password AS mail_password, o.client_id AS mail_client_id, "
            f"o.refresh_token AS mail_refresh_token "
            f"FROM registered AS r LEFT JOIN outlook_accounts AS o "
            f"ON lower(o.email)=lower(r.email) "
            f"WHERE r.email IN ({placeholders}) AND coalesce(r.deleted_at, 0) = 0",
            part,
        )
        for row in cur.fetchall():
            d = dict(row)
            if d.get("extra_json"):
                try:
                    d["extra"] = json.loads(d["extra_json"])
                except Exception:
                    d["extra"] = {}
            for key in ("mail_provider", "mail_url", "mail_password", "mail_client_id", "mail_refresh_token"):
                if not d.get(key) and d.get("extra", {}).get(key):
                    d[key] = d["extra"][key]
            d.pop("extra_json", None)
            out.append(d)

    out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return out


def get_registered(email: str) -> Optional[dict]:
    con = _conn()
    try:
        cur = con.execute(
            "SELECT * FROM registered WHERE email=? AND coalesce(deleted_at, 0)=0",
            (email.lower(),),
        )
        row = cur.fetchone()
    finally:
        con.close()
    if not row:
        return None
    out = dict(row)
    if out.get("extra_json"):
        try:
            out["extra"] = json.loads(out["extra_json"])
        except Exception:
            out["extra"] = {}
    out.pop("extra_json", None)
    return out


# ──────────────────────── Team 轮转 ────────────────────────


def create_team_mother(data: dict) -> dict:
    now = time.time()
    mother_id = str(data.get("id") or uuid.uuid4())
    with _lock:
        con = _conn()
        try:
            con.execute(
                "INSERT INTO team_mothers "
                "(id, name, email, workspace_id, access_token, cookie_header, "
                "owner_user_id, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mother_id,
                    str(data.get("name") or "").strip(),
                    str(data.get("email") or "").strip().lower(),
                    str(data.get("workspace_id") or "").strip(),
                    str(data.get("access_token") or "").strip(),
                    str(data.get("cookie_header") or "").strip(),
                    str(data.get("owner_user_id") or "").strip(),
                    1 if data.get("enabled", True) else 0,
                    now,
                    now,
                ),
            )
            con.commit()
        finally:
            con.close()
    return get_team_mother(mother_id, include_secret=False) or {}


def update_team_mother(mother_id: str, data: dict) -> Optional[dict]:
    allowed = {
        "name", "email", "workspace_id", "access_token", "cookie_header",
        "owner_user_id", "enabled",
    }
    sets = []
    values = []
    for key, value in data.items():
        if key not in allowed:
            continue
        sets.append(f"{key}=?")
        if key == "enabled":
            values.append(1 if value else 0)
        elif key == "email":
            values.append(str(value or "").strip().lower())
        else:
            values.append(str(value or "").strip())
    if not sets:
        return get_team_mother(mother_id, include_secret=False)
    sets.append("updated_at=?")
    values.extend((time.time(), mother_id))
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                f"UPDATE team_mothers SET {', '.join(sets)} WHERE id=?", values
            )
            con.commit()
            if not cur.rowcount:
                return None
        finally:
            con.close()
    return get_team_mother(mother_id, include_secret=False)


def get_team_mother(mother_id: str, include_secret: bool = True) -> Optional[dict]:
    con = _conn()
    try:
        row = con.execute(
            "SELECT * FROM team_mothers WHERE id=?", (mother_id,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    out = dict(row)
    out["enabled"] = bool(out.get("enabled"))
    if not include_secret:
        out["has_access_token"] = bool(out.get("access_token"))
        out["has_cookie"] = bool(out.get("cookie_header"))
        out.pop("access_token", None)
        out.pop("cookie_header", None)
    return out


def list_team_mothers(include_secret: bool = False, enabled_only: bool = False) -> list[dict]:
    con = _conn()
    try:
        where = "WHERE enabled=1" if enabled_only else ""
        rows = con.execute(
            f"SELECT * FROM team_mothers {where} ORDER BY created_at ASC"
        ).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        item = dict(row)
        item["enabled"] = bool(item.get("enabled"))
        if not include_secret:
            item["has_access_token"] = bool(item.get("access_token"))
            item["has_cookie"] = bool(item.get("cookie_header"))
            item.pop("access_token", None)
            item.pop("cookie_header", None)
        out.append(item)
    return out


def delete_team_mother(mother_id: str) -> bool:
    with _lock:
        con = _conn()
        try:
            active = con.execute(
                "SELECT COUNT(*) FROM team_rotation_members "
                "WHERE mother_id=? AND status IN ('pending','active')",
                (mother_id,),
            ).fetchone()[0]
            if active:
                raise ValueError("母号仍有轮转中的子号，请先停用并移出成员")
            cur = con.execute("DELETE FROM team_mothers WHERE id=?", (mother_id,))
            con.commit()
            return bool(cur.rowcount)
        finally:
            con.close()


def record_team_mother_check(
    mother_id: str,
    *,
    entitled: Optional[int] = None,
    in_use: Optional[int] = None,
    remaining: Optional[int] = None,
    error: str = "",
) -> None:
    now = time.time()
    with _lock:
        con = _conn()
        try:
            con.execute(
                "UPDATE team_mothers SET seats_entitled=?, seats_in_use=?, "
                "seats_remaining=?, last_checked_at=?, last_error=?, updated_at=? "
                "WHERE id=?",
                (entitled, in_use, remaining, now, str(error or "")[:1000], now, mother_id),
            )
            con.commit()
        finally:
            con.close()


def claim_team_rotation_candidate(mother_id: str) -> Optional[dict]:
    """Atomically reserve a fresh account or recycle one from another mother."""
    now = time.time()
    with _lock:
        con = _conn()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT r.email, tm.id AS assignment_id, "
                "tm.mother_id AS previous_mother_id "
                "FROM registered AS r "
                "LEFT JOIN outlook_accounts AS o "
                "ON lower(o.email)=lower(r.email) "
                "LEFT JOIN team_rotation_members AS tm "
                "ON lower(tm.email)=lower(r.email) "
                "WHERE coalesce(r.deleted_at, 0)=0 "
                "AND length(trim(coalesce(r.access_token, '')))>0 "
                "AND length(trim(coalesce(r.session_token, '')))>0 "
                "AND length(trim(coalesce(r.refresh_token, '')))>0 "
                "AND (o.email IS NULL OR o.status='done') "
                "AND (tm.id IS NULL OR ("
                "  tm.status IN ('exhausted','removed') AND tm.mother_id<>?"
                ")) "
                "ORDER BY CASE WHEN tm.id IS NULL THEN 0 ELSE 1 END, "
                "r.created_at ASC LIMIT 1",
                (mother_id,),
            ).fetchone()
            if not row:
                con.rollback()
                return None
            email = str(row["email"]).lower()
            assignment_id = row["assignment_id"]
            if assignment_id is not None:
                con.execute(
                    "UPDATE team_rotation_members SET mother_id=?, member_id=NULL, "
                    "status='pending', primary_used_percent=NULL, "
                    "secondary_used_percent=NULL, joined_at=NULL, "
                    "last_checked_at=NULL, removed_at=NULL, error='', "
                    "hub_status='pending', hub_pushed_at=NULL, "
                    "hub_last_attempt_at=NULL, hub_error='', hub_account_id=NULL, "
                    "created_at=?, updated_at=? WHERE id=?",
                    (mother_id, now, now, int(assignment_id)),
                )
                con.commit()
                return {
                    "id": int(assignment_id),
                    "mother_id": mother_id,
                    "previous_mother_id": str(row["previous_mother_id"] or ""),
                    "email": email,
                    "status": "pending",
                    "recycled": True,
                    "created_at": now,
                    "updated_at": now,
                }
            cur = con.execute(
                "INSERT INTO team_rotation_members "
                "(mother_id, email, status, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (mother_id, email, now, now),
            )
            con.commit()
            return {
                "id": cur.lastrowid,
                "mother_id": mother_id,
                "email": email,
                "status": "pending",
                "recycled": False,
                "created_at": now,
                "updated_at": now,
            }
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()


def has_team_rotation_candidate(mother_id: str) -> bool:
    """Return whether this mother can claim a fresh or recycled account."""
    con = _conn()
    try:
        row = con.execute(
            "SELECT 1 FROM registered AS r "
            "LEFT JOIN outlook_accounts AS o "
            "ON lower(o.email)=lower(r.email) "
            "LEFT JOIN team_rotation_members AS tm "
            "ON lower(tm.email)=lower(r.email) "
            "WHERE coalesce(r.deleted_at, 0)=0 "
            "AND length(trim(coalesce(r.access_token, '')))>0 "
            "AND length(trim(coalesce(r.session_token, '')))>0 "
            "AND length(trim(coalesce(r.refresh_token, '')))>0 "
            "AND (o.email IS NULL OR o.status='done') "
            "AND (tm.id IS NULL OR ("
            "  tm.status IN ('exhausted','removed') AND tm.mother_id<>?"
            ")) LIMIT 1",
            (mother_id,),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def update_team_rotation_member(member_row_id: int, **fields) -> None:
    allowed = {
        "member_id", "status", "primary_used_percent", "secondary_used_percent",
        "joined_at", "last_checked_at", "removed_at", "error",
        "hub_status", "hub_pushed_at", "hub_last_attempt_at", "hub_error",
        "hub_account_id",
    }
    sets = []
    values = []
    for key, value in fields.items():
        if key in allowed:
            sets.append(f"{key}=?")
            values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.extend((time.time(), int(member_row_id)))
    with _lock:
        con = _conn()
        try:
            con.execute(
                f"UPDATE team_rotation_members SET {', '.join(sets)} WHERE id=?",
                values,
            )
            con.commit()
        finally:
            con.close()


def release_team_rotation_auth_required(email: str) -> bool:
    """Return a manually reauthorized child to the normal candidate pool."""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return False
    with _lock:
        con = _conn()
        try:
            rc = con.execute(
                "DELETE FROM team_rotation_members "
                "WHERE lower(email)=lower(?) AND status='auth_required'",
                (normalized,),
            )
            con.commit()
            return rc.rowcount > 0
        finally:
            con.close()


def find_team_rotation_member(mother_id: str, email: str) -> Optional[dict]:
    con = _conn()
    try:
        row = con.execute(
            "SELECT * FROM team_rotation_members WHERE mother_id=? AND lower(email)=lower(?)",
            (mother_id, email),
        ).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def list_team_rotation_members(
    mother_id: str = "", status: str = "", limit: int = 500
) -> list[dict]:
    clauses = []
    values = []
    if mother_id:
        clauses.append("tm.mother_id=?")
        values.append(mother_id)
    if status:
        clauses.append("tm.status=?")
        values.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    values.append(max(1, min(int(limit or 500), 5000)))
    con = _conn()
    try:
        rows = con.execute(
            "SELECT tm.*, m.name AS mother_name, m.workspace_id "
            "FROM team_rotation_members AS tm "
            "LEFT JOIN team_mothers AS m ON m.id=tm.mother_id "
            f"{where} ORDER BY tm.updated_at DESC LIMIT ?",
            values,
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def team_rotation_counts() -> dict:
    out = {"total": 0, "pending": 0, "active": 0, "exhausted": 0, "removed": 0, "failed": 0}
    con = _conn()
    try:
        rows = con.execute(
            "SELECT status, COUNT(*) AS n FROM team_rotation_members GROUP BY status"
        ).fetchall()
    finally:
        con.close()
    for row in rows:
        out[str(row["status"])] = int(row["n"])
        out["total"] += int(row["n"])
    return out


def add_team_rotation_event(
    level: str,
    action: str,
    message: str,
    mother_id: str = "",
    email: str = "",
) -> None:
    with _lock:
        con = _conn()
        try:
            con.execute(
                "INSERT INTO team_rotation_events "
                "(level, action, mother_id, email, message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(level or "INFO").upper(),
                    str(action or "flow"),
                    str(mother_id or ""),
                    str(email or "").lower(),
                    str(message or "")[:2000],
                    time.time(),
                ),
            )
            con.execute(
                "DELETE FROM team_rotation_events WHERE id NOT IN "
                "(SELECT id FROM team_rotation_events ORDER BY id DESC LIMIT 1000)"
            )
            con.commit()
        finally:
            con.close()


def list_team_rotation_events(limit: int = 100) -> list[dict]:
    con = _conn()
    try:
        rows = con.execute(
            "SELECT e.*, m.name AS mother_name FROM team_rotation_events AS e "
            "LEFT JOIN team_mothers AS m ON m.id=e.mother_id "
            "ORDER BY e.id DESC LIMIT ?",
            (max(1, min(int(limit or 100), 500)),),
        ).fetchall()
    finally:
        con.close()
    return [dict(row) for row in rows]


def count_registered_by_emails(emails: list[str]) -> int:
    """Count active registered rows for a selected email list without loading credentials."""
    cleaned = list(dict.fromkeys(e.strip().lower() for e in (emails or []) if e and e.strip()))
    if not cleaned:
        return 0
    con = _new_connection()
    try:
        total = 0
        for i in range(0, len(cleaned), 500):
            part = cleaned[i:i + 500]
            placeholders = ",".join("?" * len(part))
            row = con.execute(
                "SELECT COUNT(*) FROM registered "
                f"WHERE coalesce(deleted_at, 0)=0 AND email IN ({placeholders})",
                part,
            ).fetchone()
            total += int(row[0] or 0)
        return total
    finally:
        con.close()


def iter_registered_export_rows(
    format_id: str,
    *,
    limit: int = 100000,
    filter_rt: str = "all",
    emails: Optional[list[str]] = None,
):
    """Yield export rows from SQLite without building a result list.

    The built-in text formats only need four credential columns. Keeping the
    lightweight projection here avoids reading large cookie/token JSON blobs
    for every row and lets the HTTP layer send each rendered line immediately.
    """
    cleaned = list(dict.fromkeys(e.strip().lower() for e in (emails or []) if e and e.strip()))
    capped = max(0, min(int(limit or 100000), 100000))
    lightweight = format_id in {"at", "email_pw", "email_pw_2fa"}
    if lightweight:
        select = "r.email, r.password, r.access_token, r.totp_secret, r.created_at"
    else:
        select = (
            "r.*, o.kind AS mail_provider, o.relay_url AS mail_url, "
            "o.password AS mail_password, o.client_id AS mail_client_id, "
            "o.refresh_token AS mail_refresh_token"
        )

    con = _new_connection()
    try:
        source = "registered AS r"
        if not lightweight:
            # Both email columns are normalized to lowercase on write. Keep
            # the join sargable so SQLite can use outlook_accounts' primary
            # key instead of scanning the mailbox table for every row.
            source += " LEFT JOIN outlook_accounts AS o ON o.email=r.email"
        params: list = []
        if cleaned:
            con.execute("CREATE TEMP TABLE export_emails (email TEXT PRIMARY KEY)")
            con.executemany(
                "INSERT OR IGNORE INTO export_emails(email) VALUES (?)",
                ((email,) for email in cleaned),
            )
            source += " JOIN export_emails AS e ON e.email=r.email"
        where = _registered_export_where(filter_rt)
        cur = con.execute(
            f"SELECT {select} FROM {source} {where} "
            "ORDER BY r.created_at DESC LIMIT ?",
            [*params, capped],
        )
        for row in cur:
            d = dict(row)
            if not lightweight and d.get("extra_json"):
                try:
                    d["extra"] = json.loads(d["extra_json"])
                except Exception:
                    d["extra"] = {}
                for key in (
                    "mail_provider", "mail_url", "mail_password",
                    "mail_client_id", "mail_refresh_token",
                ):
                    if not d.get(key) and d.get("extra", {}).get(key):
                        d[key] = d["extra"][key]
                d.pop("extra_json", None)
            yield d
    finally:
        con.close()


def get_registered_for_export(email: str) -> Optional[dict]:
    """Return one registered row with its mailbox OAuth material."""
    rows = list_registered_by_emails([email])
    return rows[0] if rows else None


def enqueue_team_sso_sync(email: str, content: str) -> None:
    """Durably enqueue the latest credential line for one account."""
    normalized = str(email or "").strip().lower()
    payload = str(content or "").strip()
    if not normalized or not payload:
        raise ValueError("email/content 不能为空")
    now = time.time()
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO team_sso_sync_queue "
            "(email, content, attempts, next_attempt_at, lease_until, last_error, updated_at) "
            "VALUES (?, ?, 0, ?, 0, '', ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "content=excluded.content, attempts=0, next_attempt_at=excluded.next_attempt_at, "
            "lease_until=0, last_error='', updated_at=excluded.updated_at",
            (normalized, payload, now, now),
        )
        con.commit()


def claim_team_sso_sync(limit: int = 8, lease_seconds: float = 30.0) -> list[dict]:
    """Lease due sync rows so only one dispatcher attempt owns each row."""
    now = time.time()
    count = max(1, min(int(limit or 1), 100))
    with _lock:
        con = _conn()
        con.execute("BEGIN IMMEDIATE")
        try:
            rows = con.execute(
                "SELECT email, content, attempts FROM team_sso_sync_queue "
                "WHERE next_attempt_at<=? AND lease_until<=? "
                "ORDER BY next_attempt_at, updated_at LIMIT ?",
                (now, now, count),
            ).fetchall()
            if rows:
                emails = [row["email"] for row in rows]
                placeholders = ",".join("?" * len(emails))
                con.execute(
                    f"UPDATE team_sso_sync_queue SET lease_until=? "
                    f"WHERE email IN ({placeholders})",
                    (now + max(5.0, float(lease_seconds)), *emails),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
    return [dict(row) for row in rows]


def complete_team_sso_sync(email: str, content: str) -> bool:
    """Delete only the delivered version; a newer payload remains queued."""
    with _lock:
        con = _conn()
        cur = con.execute(
            "DELETE FROM team_sso_sync_queue WHERE email=? AND content=?",
            (str(email or "").strip().lower(), str(content or "").strip()),
        )
        con.commit()
        return cur.rowcount > 0


def fail_team_sso_sync(email: str, content: str, error: str) -> None:
    """Release a failed lease with bounded exponential retry backoff."""
    normalized = str(email or "").strip().lower()
    payload = str(content or "").strip()
    with _lock:
        con = _conn()
        row = con.execute(
            "SELECT attempts FROM team_sso_sync_queue WHERE email=? AND content=?",
            (normalized, payload),
        ).fetchone()
        if not row:
            return
        attempts = int(row["attempts"] or 0) + 1
        delay = min(300.0, max(2.0, float(2 ** min(attempts, 8))))
        con.execute(
            "UPDATE team_sso_sync_queue SET attempts=?, next_attempt_at=?, lease_until=0, "
            "last_error=?, updated_at=? WHERE email=? AND content=?",
            (attempts, time.time() + delay, str(error or "")[:1000], time.time(), normalized, payload),
        )
        con.commit()


def team_sso_sync_pending_count() -> int:
    con = _conn()
    row = con.execute("SELECT COUNT(*) FROM team_sso_sync_queue").fetchone()
    return int(row[0] or 0)


# ──────────────────────── SMS 租号清理队列 ────────────────────────


def track_sms_activation(
    platform: str,
    activation_id: str,
    *,
    phone_number: str = "",
    acquired_at: Optional[float] = None,
    lifetime_seconds: float = 20 * 60,
) -> None:
    """Persist a fresh rental so an interrupted process cannot orphan it."""
    platform_key = str(platform or "").strip().lower()
    activation_key = str(activation_id or "").strip()
    if not platform_key or not activation_key:
        raise ValueError("platform/activation_id 不能为空")
    acquired = float(acquired_at or time.time())
    now = time.time()
    # Leave a short grace after the advertised lifetime so a final reuse near
    # the boundary is not cancelled underneath an in-flight verification.
    cancel_after = acquired + max(60.0, float(lifetime_seconds)) + 60.0
    with _lock:
        con = _conn()
        try:
            con.execute(
                "INSERT INTO sms_activation_cleanup "
                "(platform, activation_id, phone_number, acquired_at, cancel_after, status, "
                " attempts, next_attempt_at, lease_until, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', 0, 0, 0, '', ?) "
                "ON CONFLICT(platform, activation_id) DO UPDATE SET "
                "phone_number=CASE WHEN excluded.phone_number<>'' THEN excluded.phone_number "
                "                  ELSE sms_activation_cleanup.phone_number END, "
                "updated_at=excluded.updated_at",
                (
                    platform_key,
                    activation_key,
                    str(phone_number or "").strip(),
                    acquired,
                    cancel_after,
                    now,
                ),
            )
            con.commit()
        finally:
            con.close()


def queue_sms_activation_cancel(
    platform: str,
    activation_id: str,
    *,
    phone_number: str = "",
    acquired_at: Optional[float] = None,
    not_before: Optional[float] = None,
    error: str = "",
) -> None:
    """Mark a rental for background cancellation without resetting retry state."""
    platform_key = str(platform or "").strip().lower()
    activation_key = str(activation_id or "").strip()
    if not platform_key or not activation_key:
        return
    now = time.time()
    acquired = float(acquired_at or now)
    due = max(now, float(not_before or now))
    with _lock:
        con = _conn()
        try:
            con.execute(
                "INSERT INTO sms_activation_cleanup "
                "(platform, activation_id, phone_number, acquired_at, cancel_after, status, "
                " attempts, next_attempt_at, lease_until, last_error, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending_cancel', 0, ?, 0, ?, ?) "
                "ON CONFLICT(platform, activation_id) DO UPDATE SET "
                "status='pending_cancel', "
                "phone_number=CASE WHEN excluded.phone_number<>'' THEN excluded.phone_number "
                "                  ELSE sms_activation_cleanup.phone_number END, "
                "next_attempt_at=CASE "
                "  WHEN sms_activation_cleanup.status='active' THEN excluded.next_attempt_at "
                "  ELSE min(sms_activation_cleanup.next_attempt_at, excluded.next_attempt_at) END, "
                "last_error=excluded.last_error, updated_at=excluded.updated_at",
                (
                    platform_key,
                    activation_key,
                    str(phone_number or "").strip(),
                    acquired,
                    acquired + 20 * 60,
                    due,
                    str(error or "")[:1000],
                    now,
                ),
            )
            con.commit()
        finally:
            con.close()


def claim_sms_activation_cancellations(
    limit: int = 16, lease_seconds: float = 45.0
) -> list[dict]:
    """Lease due failures and stale active rentals for one cleanup worker."""
    now = time.time()
    count = max(1, min(int(limit or 1), 100))
    with _lock:
        con = _conn()
        con.execute("BEGIN IMMEDIATE")
        try:
            rows = con.execute(
                "SELECT platform, activation_id, phone_number, acquired_at, attempts, status "
                "FROM sms_activation_cleanup "
                "WHERE lease_until<=? AND ("
                " (status='pending_cancel' AND next_attempt_at<=?) OR "
                " (status='active' AND cancel_after<=?)"
                ") ORDER BY CASE status WHEN 'pending_cancel' THEN 0 ELSE 1 END, "
                "next_attempt_at, cancel_after LIMIT ?",
                (now, now, now, count),
            ).fetchall()
            for row in rows:
                con.execute(
                    "UPDATE sms_activation_cleanup "
                    "SET status='pending_cancel', lease_until=?, updated_at=? "
                    "WHERE platform=? AND activation_id=?",
                    (
                        now + max(5.0, float(lease_seconds)),
                        now,
                        row["platform"],
                        row["activation_id"],
                    ),
                )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
    return [dict(row) for row in rows]


def complete_sms_activation_cleanup(platform: str, activation_id: str) -> bool:
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                "DELETE FROM sms_activation_cleanup WHERE platform=? AND activation_id=?",
                (str(platform or "").strip().lower(), str(activation_id or "").strip()),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def fail_sms_activation_cleanup(platform: str, activation_id: str, error: str) -> None:
    """Release a failed cancellation lease with bounded exponential backoff."""
    platform_key = str(platform or "").strip().lower()
    activation_key = str(activation_id or "").strip()
    with _lock:
        con = _conn()
        try:
            row = con.execute(
                "SELECT attempts FROM sms_activation_cleanup "
                "WHERE platform=? AND activation_id=?",
                (platform_key, activation_key),
            ).fetchone()
            if not row:
                return
            attempts = int(row["attempts"] or 0) + 1
            delay = min(15 * 60.0, max(15.0, float(15 * (2 ** min(attempts - 1, 6)))))
            now = time.time()
            con.execute(
                "UPDATE sms_activation_cleanup SET status='pending_cancel', attempts=?, "
                "next_attempt_at=?, lease_until=0, last_error=?, updated_at=? "
                "WHERE platform=? AND activation_id=?",
                (
                    attempts,
                    now + delay,
                    str(error or "")[:1000],
                    now,
                    platform_key,
                    activation_key,
                ),
            )
            con.commit()
        finally:
            con.close()


def sms_activation_cleanup_pending_count() -> int:
    con = _conn()
    try:
        row = con.execute("SELECT COUNT(*) FROM sms_activation_cleanup").fetchone()
        return int(row[0] or 0)
    finally:
        con.close()


def delete_registered(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_registered_by_emails(emails: list[str]) -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM registered WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def delete_all_registered() -> int:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered")
        con.commit()
        return rc.rowcount


def soft_delete_registered_by_emails(emails: list[str]) -> int:
    cleaned = sorted({e.strip().lower() for e in (emails or []) if e and e.strip()})
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        now = time.time()
        total = 0
        for i in range(0, len(cleaned), 500):
            part = cleaned[i:i + 500]
            placeholders = ",".join("?" * len(part))
            cur = con.execute(
                f"UPDATE registered SET deleted_at=? WHERE coalesce(deleted_at, 0)=0 "
                f"AND email IN ({placeholders})",
                [now, *part],
            )
            total += cur.rowcount
        con.commit()
        return total


# ──────────────────────── 运行记录 ────────────────────────


def create_run(run_id: str, email: str, log_path: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO runs(run_id, email, status, started_at, log_path) "
            "VALUES (?, ?, 'running', ?, ?)",
            (run_id, email.lower(), time.time(), log_path),
        )
        con.commit()


def update_run_email(run_id: str, email: str) -> None:
    """邮箱 provider 实际租到/展开地址后更新运行记录展示值。"""
    value = str(email or "").strip().lower()
    if not value:
        return
    with _lock:
        con = _conn()
        con.execute("UPDATE runs SET email=? WHERE run_id=?", (value, run_id))
        con.commit()
        con.close()


def finish_run(run_id: str, status: str, error: str = "", category: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE runs SET status=?, finished_at=?, error=?, error_category=? WHERE run_id=?",
            (status, time.time(), (error or "")[:500], category or None, run_id),
        )
        con.commit()


def get_run(run_id: str) -> Optional[dict]:
    con = _conn()
    try:
        row = con.execute(
            "SELECT * FROM runs WHERE run_id=?", (str(run_id or "").strip(),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def list_runs(limit: int = 50) -> list[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


# ──────────────────────── settings (KV) ────────────────────────


def get_setting(key: str, default: str = "") -> str:
    con = _conn()
    try:
        cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default
    finally:
        con.close()


def set_setting(key: str, value) -> None:
    with _lock:
        con = _conn()
        try:
            con.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            con.commit()
        finally:
            con.close()


# ──────────────────────── 邮箱来源配置 ────────────────────────


def get_mail_config() -> dict:
    """返回邮箱来源配置（密码类字段隐藏明文）。

    provider 声明的配置项自动带出来 —— 加新邮箱时这里不用改，
    新 provider 的 config_fields 会自动出现在返回值里。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if f.get("type") == "password":
                configured = get_setting(key)
                # Gmail SmsBower 可复用手机号接码 Key。前端必须看到掩码，
                # 否则会把可用的回退凭证误判成未配置并要求重复填写。
                if key == "gmail_smsbower_api_key" and not configured:
                    configured = get_setting("sms_smsbower_api_key") or (
                        get_setting("sms_api_key")
                        if get_setting("sms_provider", "smsbower") == "smsbower"
                        else ""
                    )
                out[key] = "***" if configured else ""
            else:
                out[key] = get_setting(key, "")
    return out


def save_mail_config(data: dict) -> None:
    """保存邮箱配置。password 类字段传 '***' 表示不修改。

    mail_source 校验改成查 mail_providers 注册表：
        以前是写死的白名单 ("outlook", "cf_temp")，选了别的会被
        **静默改回 outlook** —— 用户看到的是"保存成功但选择没生效"。
        现在未知来源直接抛错，问题当场暴露。
    """
    from mail_providers import get_provider_class, list_providers

    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        get_provider_class(src)  # 未注册的 kind 会抛 MailProviderError
        set_setting("mail_source", src)

    # 按 provider 声明的字段保存，加新邮箱时这里零改动
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if key not in data:
                continue
            val = data[key]
            if f.get("type") == "password":
                if not val or val == "***":
                    continue  # 没填 / 是掩码 → 保持原值
            set_setting(key, str(val).strip())


def get_secret_setting(key: str) -> str:
    """内部用：拿密码类配置的明文。"""
    return get_setting(key, "")


def get_mail_settings() -> dict:
    """内部用：给 create_mail_provider 的 settings（含明文密钥）。

    跟 get_mail_config 的区别：这个不打码，只在服务端构造 provider 时用，
    绝不能直接返回给前端。
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            out[f["key"]] = get_setting(f["key"], "")
    # Gmail 邮箱与手机号接码共用 SmsBower 账户时，无需重复粘贴 Key。
    # 显式填写 gmail_smsbower_api_key 始终优先；这里只做服务端兼容兜底，
    # 不把 SMS Key 回显到邮箱配置 API。
    if not out.get("gmail_smsbower_api_key"):
        legacy_provider = get_setting("sms_provider", "smsbower")
        out["gmail_smsbower_api_key"] = get_setting("sms_smsbower_api_key", "") or (
            get_setting("sms_api_key", "") if legacy_provider == "smsbower" else ""
        )
    return out


def get_cf_admin_token() -> str:
    """内部用：拿明文 admin_token。"""
    return get_setting("cf_admin_token", "")


# ──────────────────────── SMS 接码配置 ────────────────────────


def get_sms_config() -> dict:
    """返回 SMS 接码配置（api_key 隐藏明文）。

    sms_enabled:        '0'/'1' 是否启用接码（命中 add-phone 时才会用）
    sms_provider:       smsbower
    sms_country:        国家代码或 ID（推荐 '52' = Thailand，OpenAI 走 SMS 的唯一稳定国家）
    sms_service:        服务代码（OpenAI = 'dr'）
    sms_max_price:      号码硬性最高单价（SmsBower / HeroSMS；空 / -1 = 不限）
    sms_reuse_phone:    '0'/'1' 同号复用（SmsBower / SmsBower 支持，省钱）
    sms_phone_success_max: 同号最多复用几次（默认 3）
    sms_auto_country:   '0'/'1' 自动选国家
    sms_auto_min_stock: 自动选国家最低库存（默认 20）
    sms_auto_max_price: HeroSMS 优先价格阈值（默认 0 = 不限；阈值外作兜底）
    """
    legacy_provider = get_setting("sms_provider", "smsbower")
    legacy_key = get_setting("sms_api_key", "")
    smsbower_key = get_setting("sms_smsbower_api_key", "") or (
        legacy_key if legacy_provider == "smsbower" else ""
    )
    herosms_key = get_setting("sms_herosms_api_key", "") or (
        legacy_key if legacy_provider == "herosms" else ""
    )
    return {
        "sms_enabled":             get_setting("sms_enabled", "0"),
        "sms_provider":            legacy_provider,
        "sms_api_key":             "***" if legacy_key else "",
        "sms_mode":                get_setting("sms_mode", "single"),
        "sms_supplier_strategy":   get_setting("sms_supplier_strategy", "success_first"),
        "sms_smsbower_enabled":    get_setting(
            "sms_smsbower_enabled", "1" if legacy_provider == "smsbower" and bool(smsbower_key) else "0"
        ),
        "sms_smsbower_api_key":    "***" if smsbower_key else "",
        "sms_smsbower_api_url":    get_setting("sms_smsbower_api_url", ""),
        "sms_herosms_enabled":     get_setting(
            "sms_herosms_enabled", "1" if legacy_provider == "herosms" and bool(herosms_key) else "0"
        ),
        "sms_herosms_api_key":     "***" if herosms_key else "",
        "sms_herosms_api_url":     get_setting("sms_herosms_api_url", ""),
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
    }


def save_sms_config(data: dict) -> None:
    """保存 SMS 配置。sms_api_key 传 '***' 表示不修改。"""
    # 校验 provider
    valid_providers = {"smsbower", "herosms"}
    if "sms_provider" in data:
        p = str(data["sms_provider"]).strip().lower()
        if p not in valid_providers:
            p = "smsbower"
        set_setting("sms_provider", p)
    if "sms_mode" in data:
        mode = str(data["sms_mode"]).strip().lower().replace("-", "_")
        if mode in {"balanced", "round_robin", "roundrobin", "non_race", "sequential"}:
            mode = "split"
        set_setting(
            "sms_mode",
            mode if mode in {"single", "race", "split", "session_race"} else "single",
        )
    if "sms_supplier_strategy" in data:
        strategy = str(data["sms_supplier_strategy"]).strip().lower()
        set_setting(
            "sms_supplier_strategy",
            strategy if strategy in {"balanced", "success_first"} else "success_first",
        )
    # 字符串字段直接落
    for key in (
        "sms_country", "sms_service", "sms_max_price",
        "sms_phone_success_max", "sms_auto_min_stock", "sms_auto_max_price",
        "sms_per_phone_timeout", "sms_max_phone_attempts",
        "sms_allowed_countries",
        "sms_smsbower_api_url", "sms_herosms_api_url",
    ):
        if key in data:
            set_setting(key, str(data[key]).strip())
    # 布尔字段（前端传 '0'/'1' 或 bool）
    for key in (
        "sms_enabled", "sms_reuse_phone", "sms_auto_country", "sms_strict_whitelist",
        "sms_smsbower_enabled", "sms_herosms_enabled",
    ):
        if key in data:
            v = data[key]
            if isinstance(v, bool):
                set_setting(key, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key, "1" if s in ("1", "true", "yes", "on") else "0")
    # API key（'***' 不修改）
    if data.get("sms_api_key") and data["sms_api_key"] != "***":
        set_setting("sms_api_key", str(data["sms_api_key"]).strip())
    for key in ("sms_smsbower_api_key", "sms_herosms_api_key"):
        if data.get(key) and data[key] != "***":
            set_setting(key, str(data[key]).strip())


def get_sms_internal_config() -> dict:
    """内部用：拿明文 sms_api_key,供 sms_provider 实例化使用。"""
    legacy_provider = get_setting("sms_provider", "smsbower")
    legacy_key = get_setting("sms_api_key", "")
    smsbower_key = get_setting("sms_smsbower_api_key", "") or (
        legacy_key if legacy_provider == "smsbower" else ""
    )
    herosms_key = get_setting("sms_herosms_api_key", "") or (
        legacy_key if legacy_provider == "herosms" else ""
    )
    return {
        "sms_enabled":             get_setting("sms_enabled", "0") in ("1", "true"),
        "sms_provider":            legacy_provider,
        "sms_api_key":             legacy_key,
        "sms_mode":                get_setting("sms_mode", "single"),
        "sms_supplier_strategy":   get_setting("sms_supplier_strategy", "success_first"),
        "sms_smsbower_enabled":    get_setting(
            "sms_smsbower_enabled", "1" if legacy_provider == "smsbower" and bool(smsbower_key) else "0"
        ) in ("1", "true"),
        "sms_smsbower_api_key":    smsbower_key,
        "sms_smsbower_api_url":    get_setting("sms_smsbower_api_url", ""),
        "sms_herosms_enabled":     get_setting(
            "sms_herosms_enabled", "1" if legacy_provider == "herosms" and bool(herosms_key) else "0"
        ) in ("1", "true"),
        "sms_herosms_api_key":     herosms_key,
        "sms_herosms_api_url":     get_setting("sms_herosms_api_url", ""),
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0") in ("1", "true"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0") in ("1", "true"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0") in ("1", "true"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
    }


# ──────────────────────── 自动导出配置 (CPA / SUB2API) ────────────────────────


def get_export_config() -> dict:
    """返回导出配置（敏感字段做明文/'***' 占位）。

    给前端展示用：
      cpa_mgmt_key / sub2api_api_key 已设置时返回 '***'，未设置返回 ''。
      保存时传 '***' 代表不修改。
    """
    from .exporter import DEFAULT_SUB2API_MODELS, parse_sub2api_models

    default_models_json = json.dumps(list(DEFAULT_SUB2API_MODELS), ensure_ascii=False)
    return {
        # CPA
        "cpa_enabled":     get_setting("export_cpa_enabled", "0"),
        "cpa_url":         get_setting("export_cpa_url", ""),
        "cpa_mgmt_key":    "***" if get_setting("export_cpa_mgmt_key") else "",
        "cpa_timeout":     get_setting("export_cpa_timeout", "30"),
        # SUB2API
        "sub2api_enabled":    get_setting("export_sub2api_enabled", "0"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    "***" if get_setting("export_sub2api_api_key") else "",
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_default_model": get_setting("export_sub2api_default_model", "gpt-5.4"),
        "sub2api_models": parse_sub2api_models(
            get_setting("export_sub2api_models", default_models_json), fallback=()
        ),
        "sub2api_concurrency": get_setting("export_sub2api_concurrency", "3"),
        "sub2api_fingerprint_mode": get_setting("export_sub2api_fingerprint_mode", "session"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
        # team-sso free 账号池
        "team_sso_enabled":  get_setting("export_team_sso_enabled", os.getenv("TEAM_SSO_SYNC_ENABLED", "0")),
        "team_sso_url":      get_setting("export_team_sso_url", os.getenv("TEAM_SSO_SYNC_URL", "")),
        "team_sso_sync_key": "***" if get_setting("export_team_sso_sync_key", os.getenv("TEAM_SSO_SYNC_KEY", "")) else "",
        "team_sso_timeout":  get_setting("export_team_sso_timeout", "10"),
        "team_sso_pending":  team_sso_sync_pending_count(),
    }


def save_export_config(data: dict) -> None:
    """保存导出配置。密文字段传 '***' 表示不修改。"""
    from .exporter import parse_sub2api_models

    # 布尔开关
    for key_in, key_out in (
        ("cpa_enabled",     "export_cpa_enabled"),
        ("sub2api_enabled", "export_sub2api_enabled"),
        ("team_sso_enabled", "export_team_sso_enabled"),
    ):
        if key_in in data:
            v = data[key_in]
            if isinstance(v, bool):
                set_setting(key_out, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key_out, "1" if s in ("1", "true", "yes", "on") else "0")
    # 字符串字段（明文）
    for key_in, key_out in (
        ("cpa_url",            "export_cpa_url"),
        ("cpa_timeout",        "export_cpa_timeout"),
        ("sub2api_url",        "export_sub2api_url"),
        ("sub2api_group_ids",  "export_sub2api_group_ids"),
        ("sub2api_default_model", "export_sub2api_default_model"),
        ("sub2api_concurrency", "export_sub2api_concurrency"),
        ("sub2api_fingerprint_mode", "export_sub2api_fingerprint_mode"),
        ("sub2api_timeout",    "export_sub2api_timeout"),
        ("team_sso_url",       "export_team_sso_url"),
        ("team_sso_timeout",   "export_team_sso_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, str(data[key_in] or "").strip())
    if "sub2api_models" in data:
        models = parse_sub2api_models(data.get("sub2api_models"), fallback=())
        default_model = str(
            data.get("sub2api_default_model")
            or get_setting("export_sub2api_default_model", "gpt-5.4")
        ).strip() or "gpt-5.4"
        if default_model not in models:
            models.append(default_model)
        set_setting("export_sub2api_models", json.dumps(models, ensure_ascii=False))
    if "sub2api_fingerprint_mode" in data:
        mode = str(data.get("sub2api_fingerprint_mode") or "session").strip().lower()
        if mode not in ("off", "device", "session", "full"):
            mode = "session"
        set_setting("export_sub2api_fingerprint_mode", mode)
    # 密文字段（'***' 不修改）
    if data.get("cpa_mgmt_key") and data["cpa_mgmt_key"] != "***":
        set_setting("export_cpa_mgmt_key", str(data["cpa_mgmt_key"]).strip())
    if data.get("sub2api_api_key") and data["sub2api_api_key"] != "***":
        set_setting("export_sub2api_api_key", str(data["sub2api_api_key"]).strip())
    if data.get("team_sso_sync_key") and data["team_sso_sync_key"] != "***":
        set_setting("export_team_sso_sync_key", str(data["team_sso_sync_key"]).strip())


def get_export_internal_config() -> dict:
    """内部用：拿明文密钥 + 解析后的 enabled 布尔。供 registrar / app.test 调用。

    返回两个子配置 dict，可分别传给 exporter.export_to_cpa / export_to_sub2api。
    """
    from .exporter import DEFAULT_SUB2API_MODELS, parse_sub2api_models

    default_models_json = json.dumps(list(DEFAULT_SUB2API_MODELS), ensure_ascii=False)
    cpa = {
        "enabled":      get_setting("export_cpa_enabled", "0") in ("1", "true"),
        "cpa_url":      get_setting("export_cpa_url", ""),
        "cpa_mgmt_key": get_setting("export_cpa_mgmt_key", ""),
        "cpa_timeout":  get_setting("export_cpa_timeout", "30"),
    }
    sub2api = {
        "enabled":            get_setting("export_sub2api_enabled", "0") in ("1", "true"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    get_setting("export_sub2api_api_key", ""),
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_default_model": get_setting("export_sub2api_default_model", "gpt-5.4"),
        "sub2api_models": parse_sub2api_models(
            get_setting("export_sub2api_models", default_models_json), fallback=()
        ),
        "sub2api_concurrency": get_setting("export_sub2api_concurrency", "3"),
        "sub2api_fingerprint_mode": get_setting("export_sub2api_fingerprint_mode", "session"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }
    team_sso = {
        "enabled":  get_setting("export_team_sso_enabled", os.getenv("TEAM_SSO_SYNC_ENABLED", "0")) in ("1", "true"),
        "url":      get_setting("export_team_sso_url", os.getenv("TEAM_SSO_SYNC_URL", "")),
        "sync_key": get_setting("export_team_sso_sync_key", os.getenv("TEAM_SSO_SYNC_KEY", "")),
        "timeout":  get_setting("export_team_sso_timeout", "10"),
    }
    return {"cpa": cpa, "sub2api": sub2api, "team_sso": team_sso}


# 模块加载时自动建表
init_db()
