"""SQLite connection management + retention cleanup (TZ v4 §12).

One connection per call is intentional and sufficient for this project's
scale (a handful of agents polling every 30-120 minutes) — no connection
pool needed, and it keeps SQLite's locking model simple.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from storage.models import SCHEMA


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- agent_runs -----------------------------------------------------

    def record_agent_run(
        self, agent: str, status: str, started_at: str, finished_at: str, error: str | None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO agent_runs (agent, status, started_at, finished_at, error) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent, status, started_at, finished_at, error),
            )

    def last_run(self, agent: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM agent_runs WHERE agent = ? ORDER BY finished_at DESC LIMIT 1",
                (agent,),
            ).fetchone()

    # -- notifications (dedup, TZ v4 §20/§22) ----------------------------

    def was_notified(self, kind: str, dedupe_key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM notifications WHERE kind = ? AND dedupe_key = ?",
                (kind, dedupe_key),
            ).fetchone()
            return row is not None

    def mark_notified(self, kind: str, dedupe_key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO notifications (kind, dedupe_key, sent_at) VALUES (?, ?, ?)",
                (kind, dedupe_key, datetime.now(timezone.utc).isoformat()),
            )

    # -- retention (TZ v4 §12) ------------------------------------------

    def cleanup(self, retention_days: int) -> dict[str, int]:
        """Delete rows older than ``retention_days`` from the tables that
        are allowed to be pruned on a blanket time cutoff.

        tasks/notifications are intentionally NOT touched here — tasks
        follow the academic-period lifecycle (not a time cutoff), and a
        deleted notification row would let a duplicate alert through.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        deleted: dict[str, int] = {}
        with self.connect() as conn:
            for table, column in (
                ("weather_snapshots", "created_at"),
                ("agent_runs", "finished_at"),
            ):
                cur = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                deleted[table] = cur.rowcount
        return deleted
