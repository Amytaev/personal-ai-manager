"""SQLite connection management + retention cleanup (TZ v4 §12).

One connection per call is intentional and sufficient for this project's
scale (a handful of agents polling every 30-120 minutes) — no connection
pool needed, and it keeps SQLite's locking model simple.
"""
from __future__ import annotations

import json
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

    # -- tasks (Teams Agent, Phase 5) ------------------------------------

    def get_task_fingerprint(self, task_id: str) -> sqlite3.Row | None:
        """The subset of a task's stored fields that matter for change
        detection (agents/teams/agent.py decides new/changed/unchanged
        by comparing this against a freshly parsed row)."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT status, due_at, submitted_at, title FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()

    def upsert_task(self, task: dict, state: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, course, title, description, status, created_at, due_at,
                     submitted_at, source, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    course=excluded.course,
                    title=excluded.title,
                    description=excluded.description,
                    status=excluded.status,
                    due_at=excluded.due_at,
                    submitted_at=excluded.submitted_at,
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """,
                (
                    task["id"],
                    task["course"],
                    task["title"],
                    task.get("description"),
                    task["status"],
                    task.get("created_at"),
                    task.get("due_at"),
                    task.get("submitted_at"),
                    task["source"],
                    state,
                    now,
                ),
            )

    # -- valorant_store (VALORANT Agent, Phase 6) ------------------------

    def save_valorant_store(self, items: list[dict], reset_in: str | None = None) -> None:
        """Stores one snapshot of the daily store as a single JSON row -
        unlike tasks (which are upserted by id for per-task change
        detection), the store is a whole-day snapshot with no natural
        per-item identity to track across days, so it's simply appended,
        same shape as weather_snapshots. `reset_in` is folded into the
        stored payload rather than a separate column, so /store and
        /briefing (telegram_bot/handlers.py) can show it without a
        schema change."""
        payload = {"items": items, "reset_in": reset_in}
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO valorant_store (skins_json, checked_at) VALUES (?, ?)",
                (json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
            )

    # -- sso_courses / sso_schedule_entries / sso_study_materials (SSO Agent, Этап B) --

    def save_sso_snapshot(
        self,
        semester_id: int,
        courses: list[dict],
        schedule_entries: list[dict],
        materials: list[dict],
    ) -> None:
        """Replaces the whole normalized snapshot for ``semester_id`` in
        one transaction - see storage/models.py's schema comment for why
        this is wipe-and-replace rather than a per-row upsert (no stable
        per-row identity worth tracking yet at this stage). Materials
        aren't semester-scoped (real UMKD folders span multiple academic
        years per course), so they're wiped and re-inserted in full on
        every run regardless of which semester was fetched.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("DELETE FROM sso_courses WHERE semester_id = ?", (semester_id,))
            for course in courses:
                conn.execute(
                    """
                    INSERT INTO sso_courses
                        (code, semester_id, title, discipline_type_title, cycle_title,
                         lecture_credits, practice_credits, lab_credits, total_credits,
                         reading_chair_title, description, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        course["code"],
                        semester_id,
                        course["title"],
                        course.get("discipline_type_title"),
                        course.get("cycle_title"),
                        course.get("lecture_credits"),
                        course.get("practice_credits"),
                        course.get("lab_credits"),
                        course.get("total_credits"),
                        course.get("reading_chair_title"),
                        course.get("description"),
                        now,
                    ),
                )

            conn.execute("DELETE FROM sso_schedule_entries WHERE semester_id = ?", (semester_id,))
            for entry in schedule_entries:
                conn.execute(
                    """
                    INSERT INTO sso_schedule_entries
                        (semester_id, class_id, course_code, course_title, instructor_name,
                         room_title, class_type, day_title, start_time, end_time,
                         group_number, students_count, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        semester_id,
                        entry.get("class_id"),
                        entry.get("course_code"),
                        entry.get("course_title"),
                        entry.get("instructor_name"),
                        entry.get("room_title"),
                        entry.get("class_type"),
                        entry.get("day_title"),
                        entry.get("start_time"),
                        entry.get("end_time"),
                        entry.get("group_number"),
                        entry.get("students_count"),
                        now,
                    ),
                )

            conn.execute("DELETE FROM sso_study_materials")
            for material in materials:
                conn.execute(
                    """
                    INSERT INTO sso_study_materials
                        (file_id, folder_id, file_name, file_category_title,
                         course_title, instructor_name, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        material["file_id"],
                        material["folder_id"],
                        material["file_name"],
                        material.get("file_category_title"),
                        material.get("course_title"),
                        material.get("instructor_name"),
                        now,
                    ),
                )

    def get_sso_courses(self, semester_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sso_courses WHERE semester_id = ? ORDER BY code", (semester_id,)
            ).fetchall()

    def get_sso_schedule(self, semester_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM sso_schedule_entries WHERE semester_id = ? ORDER BY day_title, start_time",
                (semester_id,),
            ).fetchall()

    def get_sso_materials(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM sso_study_materials ORDER BY file_name").fetchall()

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
                ("valorant_store", "checked_at"),
                ("agent_runs", "finished_at"),
            ):
                cur = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                deleted[table] = cur.rowcount
        return deleted
