from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    consent_at   INTEGER,
    active_voice INTEGER,
    last_job_at  INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS voices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    name         TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_voices_user ON voices(user_id);
"""


@dataclass(frozen=True)
class Voice:
    id: int
    user_id: int
    name: str
    reference_id: str


class Storage:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # --- пользователи ---
    def ensure_user(self, user_id: int) -> None:
        self._write("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        rows = self._read("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return rows[0] if rows else None

    def has_consent(self, user_id: int) -> bool:
        row = self.get_user(user_id)
        return bool(row and row["consent_at"])

    def set_consent(self, user_id: int) -> None:
        self.ensure_user(user_id)
        self._write(
            "UPDATE users SET consent_at = ? WHERE user_id = ?",
            (int(time.time()), user_id),
        )

    def set_active_voice(self, user_id: int, voice_id: int | None) -> None:
        self._write(
            "UPDATE users SET active_voice = ? WHERE user_id = ?", (voice_id, user_id)
        )

    def touch_job(self, user_id: int) -> None:
        self._write(
            "UPDATE users SET last_job_at = ? WHERE user_id = ?",
            (int(time.time()), user_id),
        )

    # --- голоса ---
    def add_voice(self, user_id: int, name: str, reference_id: str) -> int:
        cur = self._write(
            "INSERT INTO voices(user_id, name, reference_id, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, name, reference_id, int(time.time())),
        )
        return int(cur.lastrowid)

    def list_voices(self, user_id: int) -> list[Voice]:
        rows = self._read(
            "SELECT * FROM voices WHERE user_id = ? ORDER BY id", (user_id,)
        )
        return [Voice(r["id"], r["user_id"], r["name"], r["reference_id"]) for r in rows]

    def get_voice(self, voice_id: int, user_id: int) -> Voice | None:
        rows = self._read(
            "SELECT * FROM voices WHERE id = ? AND user_id = ?", (voice_id, user_id)
        )
        if not rows:
            return None
        r = rows[0]
        return Voice(r["id"], r["user_id"], r["name"], r["reference_id"])

    def delete_voice(self, voice_id: int, user_id: int) -> bool:
        cur = self._write(
            "DELETE FROM voices WHERE id = ? AND user_id = ?", (voice_id, user_id)
        )
        return cur.rowcount > 0

    def purge_user(self, user_id: int) -> list[Voice]:
        voices = self.list_voices(user_id)
        self._write("DELETE FROM voices WHERE user_id = ?", (user_id,))
        self._write("DELETE FROM users WHERE user_id = ?", (user_id,))
        return voices
