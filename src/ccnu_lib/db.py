"""SQLite 持久层（同步，单进程低并发，配合 per-user 锁足够）。"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_accounts (
    user_key     TEXT PRIMARY KEY,
    username     TEXT,
    password     TEXT,
    phone_hint   TEXT,
    profile_dir  TEXT,
    login_status TEXT DEFAULT 'unknown',
    updated_at   INTEGER
);

CREATE TABLE IF NOT EXISTS library_sessions (
    user_key      TEXT PRIMARY KEY,
    last_login_at INTEGER,
    last_check_at INTEGER,
    status        TEXT DEFAULT 'unknown',
    last_error    TEXT
);

CREATE TABLE IF NOT EXISTS library_challenges (
    challenge_id    TEXT PRIMARY KEY,
    user_key        TEXT,
    type            TEXT,
    prompt          TEXT,
    screenshot_path TEXT,
    status          TEXT DEFAULT 'pending',
    created_at      INTEGER,
    expires_at      INTEGER
);
"""


class Database:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query_one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    # ----- accounts -----
    def upsert_account(self, user_key: str, username: str, password: str,
                       phone_hint: str | None, profile_dir: str) -> None:
        self._exec(
            """INSERT INTO library_accounts
               (user_key, username, password, phone_hint, profile_dir, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(user_key) DO UPDATE SET
                 username=excluded.username,
                 password=excluded.password,
                 phone_hint=excluded.phone_hint,
                 profile_dir=excluded.profile_dir,
                 updated_at=excluded.updated_at""",
            (user_key, username, password, phone_hint, profile_dir, int(time.time())),
        )

    def get_account(self, user_key: str) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM library_accounts WHERE user_key=?", (user_key,)
        )

    def set_login_status(self, user_key: str, status: str) -> None:
        self._exec(
            "UPDATE library_accounts SET login_status=?, updated_at=? WHERE user_key=?",
            (status, int(time.time()), user_key),
        )

    # ----- sessions -----
    def update_session(self, user_key: str, *, status: str | None = None,
                       logged_in: bool = False, error: str | None = None) -> None:
        now = int(time.time())
        prev = self._query_one(
            "SELECT * FROM library_sessions WHERE user_key=?", (user_key,)
        )
        last_login = now if logged_in else (prev or {}).get("last_login_at")
        self._exec(
            """INSERT INTO library_sessions
               (user_key, last_login_at, last_check_at, status, last_error)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_key) DO UPDATE SET
                 last_login_at=excluded.last_login_at,
                 last_check_at=excluded.last_check_at,
                 status=excluded.status,
                 last_error=excluded.last_error""",
            (user_key, last_login, now, status, error),
        )

    def get_session(self, user_key: str) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM library_sessions WHERE user_key=?", (user_key,)
        )

    # ----- challenges -----
    def create_challenge(self, challenge_id: str, user_key: str, ctype: str,
                         prompt: str, screenshot_path: str | None,
                         expires_at: int) -> None:
        self._exec(
            """INSERT INTO library_challenges
               (challenge_id, user_key, type, prompt, screenshot_path,
                status, created_at, expires_at)
               VALUES (?,?,?,?,?, 'pending', ?, ?)""",
            (challenge_id, user_key, ctype, prompt, screenshot_path,
             int(time.time()), expires_at),
        )

    def get_challenge(self, challenge_id: str) -> dict[str, Any] | None:
        return self._query_one(
            "SELECT * FROM library_challenges WHERE challenge_id=?", (challenge_id,)
        )

    def set_challenge_status(self, challenge_id: str, status: str) -> None:
        self._exec(
            "UPDATE library_challenges SET status=? WHERE challenge_id=?",
            (status, challenge_id),
        )

    def expire_stale_challenges(self, user_key: str) -> None:
        self._exec(
            """UPDATE library_challenges SET status='expired'
               WHERE user_key=? AND status='pending' AND expires_at < ?""",
            (user_key, int(time.time())),
        )
