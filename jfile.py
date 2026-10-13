"""
jfile.py — SQLite storage helpers for F###.

Users (email/password), per-user telegram id, download log, app settings.
"""

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime
from app_paths import data_file

DB_FILE = str(data_file("info.db"))


def get_connection() -> sqlite3.Connection:
    """Get a database connection with row factory for dict-like access."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create all tables if they don't exist."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                email         TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                salt          TEXT NOT NULL,
                telegram_id   TEXT,
                last_seen     TEXT,
                created_at    TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_log (
                url         TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                media       TEXT NOT NULL,
                downloaded  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS link_confirmations (
                token       TEXT PRIMARY KEY,
                email       TEXT NOT NULL,
                telegram_id TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_queue (
                id            TEXT PRIMARY KEY,
                url           TEXT NOT NULL,
                fmt           TEXT NOT NULL,
                mode          TEXT NOT NULL,
                quality       TEXT NOT NULL,
                requested_by  TEXT,
                title         TEXT,
                progress      REAL DEFAULT 0,
                speed         TEXT DEFAULT '',
                eta           TEXT DEFAULT '',
                status        TEXT NOT NULL,
                error         TEXT DEFAULT '',
                started       TEXT DEFAULT '',
                created       TEXT NOT NULL,
                finished_at   TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS download_history (
                id            TEXT PRIMARY KEY,
                url           TEXT NOT NULL,
                fmt           TEXT NOT NULL,
                mode          TEXT NOT NULL,
                quality       TEXT NOT NULL,
                requested_by  TEXT,
                title         TEXT,
                status        TEXT NOT NULL,
                error         TEXT DEFAULT '',
                started       TEXT DEFAULT '',
                created       TEXT NOT NULL,
                finished_at   TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO app_settings (key, value) VALUES ('firsttime', 'True')
        """)
        conn.commit()


# -----------------------------------------------------------------------------
# Users
# -----------------------------------------------------------------------------


def hash_password(password: str, salt: str = None) -> tuple[str, str]:
    """Hash a password using SHA-256 with a salt. Returns (hash, salt)."""
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return hashed, salt


def create_user(email: str, password: str) -> bool:
    """Create a new user. Returns True if created, False if email exists."""
    email = email.strip().lower()
    if not email or "@" not in email:
        return False
    init_db()
    with get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
        if exists:
            return False
        hashed, salt = hash_password(password)
        conn.execute(
            "INSERT INTO users (email, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (email, hashed, salt, datetime.now().isoformat()),
        )
        conn.execute("UPDATE app_settings SET value = 'False' WHERE key = 'firsttime'")
        conn.commit()
    return True


def check_user(email: str, password: str) -> bool:
    """Verify credentials. Returns True if valid."""
    email = email.strip().lower()
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT password_hash, salt FROM users WHERE email = ?", (email,)
        ).fetchone()
    if row is None:
        return False
    hashed_input, _ = hash_password(password, row["salt"])
    return hashed_input == row["password_hash"]


def user_exists(email: str) -> bool:
    email = email.strip().lower()
    init_db()
    with get_connection() as conn:
        return conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None


def get_user(email: str) -> dict | None:
    email = email.strip().lower()
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row is None:
        return None
    return {
        "email": row["email"],
        "telegram_id": row["telegram_id"],
        "last_seen": row["last_seen"],
        "created_at": row["created_at"],
    }


def set_telegram_id(email: str, telegram_id: str) -> None:
    email = email.strip().lower()
    telegram_id = telegram_id.strip()
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET telegram_id = ? WHERE email = ?",
            (telegram_id or None, email),
        )
        conn.commit()


def touch_user(email: str) -> None:
    email = email.strip().lower()
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET last_seen = ? WHERE email = ?",
            (datetime.now().isoformat(), email),
        )
        conn.commit()


def change_password(email: str, old_password: str, new_password: str) -> bool:
    """Change a user's password. Returns True on success."""
    email = email.strip().lower()
    if not check_user(email, old_password):
        return False
    if len(new_password) < 6:
        return False
    hashed, salt = hash_password(new_password)
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE email = ?",
            (hashed, salt, email),
        )
        conn.commit()
    return True


def delete_user(email: str) -> bool:
    """Delete a user account and their link confirmations. Returns True if deleted."""
    email = email.strip().lower()
    init_db()
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM users WHERE email = ?", (email,))
        conn.execute("DELETE FROM link_confirmations WHERE email = ?", (email,))
        conn.commit()
    return cursor.rowcount > 0


def online_users(within_seconds: int = 300) -> list[dict]:
    """Users active within the last `within_seconds` seconds."""
    init_db()
    cutoff = datetime.now().timestamp() - within_seconds
    with get_connection() as conn:
        rows = conn.execute("SELECT email, telegram_id, last_seen FROM users").fetchall()
    online = []
    for row in rows:
        if row["last_seen"]:
            try:
                ts = datetime.fromisoformat(row["last_seen"]).timestamp()
            except ValueError:
                continue
            if ts >= cutoff:
                online.append({
                    "email": row["email"],
                    "telegram_id": row["telegram_id"],
                    "last_seen": row["last_seen"],
                })
    return online


def telegram_ids() -> list[int]:
    """All telegram ids linked to user accounts."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT telegram_id FROM users WHERE telegram_id IS NOT NULL AND telegram_id != ''"
        ).fetchall()
    ids = []
    for row in rows:
        try:
            ids.append(int(row["telegram_id"]))
        except (TypeError, ValueError):
            continue
    return ids


# -----------------------------------------------------------------------------
# Telegram link confirmations
# -----------------------------------------------------------------------------


def create_link_confirmation(email: str, telegram_id: str) -> str:
    """Create a pending link confirmation. Returns the verification token."""
    init_db()
    token = secrets.token_hex(16)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO link_confirmations (token, email, telegram_id, status, created_at) "
            "VALUES (?, ?, ?, 'pending', ?)",
            (token, email.strip().lower(), telegram_id.strip(), datetime.now().isoformat()),
        )
        conn.commit()
    return token


def get_link_confirmation(token: str) -> dict | None:
    """Fetch a link confirmation row by token."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT token, email, telegram_id, status FROM link_confirmations WHERE token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def set_link_confirmation_status(token: str, status: str) -> None:
    """Update the status of a link confirmation."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            "UPDATE link_confirmations SET status = ? WHERE token = ?",
            (status, token),
        )
        conn.commit()


# -----------------------------------------------------------------------------
# App settings
# -----------------------------------------------------------------------------


def settings_get(key: str, default=None):
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    val = row["value"]
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


def settings_set(key: str, value) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value) if not isinstance(value, str) else value),
        )
        conn.commit()


# -----------------------------------------------------------------------------
# Download log
# -----------------------------------------------------------------------------


def download_log_get() -> dict:
    init_db()
    with get_connection() as conn:
        rows = conn.execute("SELECT url, title, media, downloaded FROM download_log").fetchall()
    return {
        row["url"]: {
            "title": row["title"],
            "media": row["media"],
            "downloaded": row["downloaded"],
        }
        for row in rows
    }


def download_log_add(url: str, title: str, media: str) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO download_log (url, title, media, downloaded) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET title = excluded.title, media = excluded.media, "
            "downloaded = excluded.downloaded",
            (url, title, media, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()


def download_log_exists(url: str) -> bool:
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM download_log WHERE url = ?", (url,)).fetchone()
    return row is not None


def download_log_get_one(url: str) -> dict | None:
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT title, media, downloaded FROM download_log WHERE url = ?", (url,)
        ).fetchone()
    if row is None:
        return None
    return {"title": row["title"], "media": row["media"], "downloaded": row["downloaded"]}


def download_log_remove(url: str) -> bool:
    init_db()
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM download_log WHERE url = ?", (url,))
        conn.commit()
    return cursor.rowcount > 0


def download_log_clear() -> int:
    init_db()
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM download_log")
        conn.commit()
    return cursor.rowcount


# -----------------------------------------------------------------------------
# Persistent download queue (survives restarts)
# -----------------------------------------------------------------------------


def dlq_reset_orphan_running() -> int:
    """On startup: any job left in 'downloading' state had its process killed.
    Reset to 'queued' so it gets retried. Returns the number reset."""
    init_db()
    with get_connection() as conn:
        cursor = conn.execute(
            "UPDATE download_queue SET status='queued', started='' WHERE status='downloading'"
        )
        conn.commit()
    return cursor.rowcount


def dlq_insert(job: dict) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO download_queue
               (id, url, fmt, mode, quality, requested_by, title,
                progress, speed, eta, status, error, started, created, finished_at)
               VALUES (:id, :url, :fmt, :mode, :quality, :requested_by, :title,
                       :progress, :speed, :eta, :status, :error, :started, :created, :finished_at)""",
            job,
        )
        conn.commit()


def dlq_get_next() -> dict | None:
    """Oldest queued job, or None if empty."""
    init_db()
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM download_queue WHERE status='queued' ORDER BY created ASC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def dlq_update_progress(job_id: str, progress: float, speed: str, eta: str,
                        title: str, status: str) -> None:
    init_db()
    with get_connection() as conn:
        conn.execute(
            """UPDATE download_queue
               SET progress=?, speed=?, eta=?, title=?, status=?
               WHERE id=?""",
            (progress, speed, eta, title, status, job_id),
        )
        conn.commit()


def dlq_archive(job: dict) -> None:
    """Move a finished job out of the active queue into history."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO download_history
               (id, url, fmt, mode, quality, requested_by, title,
                status, error, started, created, finished_at)
               VALUES (:id, :url, :fmt, :mode, :quality, :requested_by, :title,
                       :status, :error, :started, :created, :finished_at)""",
            job,
        )
        conn.execute("DELETE FROM download_queue WHERE id=?", (job["id"],))
        conn.commit()


def dlq_list_queued() -> list[dict]:
    """Jobs waiting to be picked up by the worker.

    The currently-downloading job is intentionally excluded — the dashboard
    shows that one under `current` so it doesn't appear twice (once stale,
    once live) in the queue list."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM download_queue WHERE status='queued' ORDER BY created ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def dlq_list_active() -> list[dict]:
    """All non-archived jobs (queued + currently downloading).
    Kept for back-compat; prefer dlq_list_queued() for the dashboard."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM download_queue ORDER BY created ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def dlq_clear_finished() -> int:
    init_db()
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM download_queue WHERE status IN ('done', 'error')")
        conn.commit()
    return cursor.rowcount


def dlh_list_recent(limit: int = 50) -> list[dict]:
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM download_history ORDER BY finished_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def dlh_count() -> int:
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM download_history").fetchone()
    return int(row["n"]) if row else 0
