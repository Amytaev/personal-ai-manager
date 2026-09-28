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
from typing import Any, Iterator

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

    def last_successful_agent_run(self, agent: str) -> sqlite3.Row | None:
        """The most recent agent_runs row for ``agent`` that actually did
        the agent's real job - status "working" or "degraded" (some real
        data was fetched/processed), never "failing" (bro's live bug,
        2026-09-28: Auth Checker's is_logged_in() reported teams -> OK a
        few seconds after the SAME run's actual TeamsAgent.run() had
        already failed to reach the "Задания" list - is_logged_in() only
        proves the session/cookie is valid, not that the last real data
        fetch succeeded, and those two can genuinely disagree in a given
        cycle). This is the timestamp that answers "when did we last
        actually get real data from this source", which source_status's
        own last_successful_sync (an Auth Checker session check, see
        agents/auth_checker.py) does NOT answer - see
        agents/study_manager/agent.py's get_source_status() for how the
        two are combined for a caller."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM agent_runs WHERE agent = ? AND status IN ('working', 'degraded') "
                "ORDER BY finished_at DESC LIMIT 1",
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

    def get_tasks(self, source: str | None = None) -> list[sqlite3.Row]:
        """All stored tasks, optionally filtered by ``source`` (e.g.
        "teams") - used by agents/checker/agent.py to read Teams'
        already-normalized assignments without importing anything from
        agents/teams/* itself (Checker only ever reads storage, per its
        spec §2/§19 - it must never call TeamsAgent/SsoAgent or open a
        session of its own)."""
        with self.connect() as conn:
            if source is None:
                return conn.execute("SELECT * FROM tasks ORDER BY due_at").fetchall()
            return conn.execute(
                "SELECT * FROM tasks WHERE source = ? ORDER BY due_at", (source,)
            ).fetchall()

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

    def get_latest_sso_semester_id(self) -> int | None:
        """save_sso_snapshot() never records a separate "current semester"
        pointer of its own - courses/schedule are simply stored per
        semester_id (see save_sso_snapshot's docstring). Telegram commands
        (telegram_bot/handlers.py's schedule()/status use) need SOME
        semester to show without the user having to know an id, so this
        just takes the highest semester_id seen - SsoAgent only ever
        fetches parser.pick_current_semester_id()'s pick (the one the
        portal itself flags as current), and Satbayev's ids increase over
        time in every real capture so far, so the latest stored id is
        always that same current semester. Returns None if the SSO Agent
        has never successfully saved a snapshot. Checks both sso_courses
        and sso_schedule_entries (not just courses) since either table
        alone is enough evidence a semester_id exists - a capture could
        in principle populate one and not the other."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(semester_id) AS semester_id FROM ("
                "SELECT semester_id FROM sso_courses "
                "UNION ALL SELECT semester_id FROM sso_schedule_entries"
                ")"
            ).fetchone()
            return row["semester_id"] if row and row["semester_id"] is not None else None

    # -- source_status (Auth Checker Agent) -------------------------------

    def record_source_status(
        self,
        source: str,
        status: str,
        checked_at: str,
        error: str | None = None,
    ) -> None:
        """Upserts the live auth/health flag for one source (agents/
        auth_checker.py). Two fields are deliberately NOT just "whatever
        was passed this call":

        - last_successful_sync only ever moves forward on status="OK" -
          a later AUTH_REQUIRED/ERROR/UNAVAILABLE check reuses whatever
          was already stored, so "when did this last actually work"
          survives the source being temporarily down instead of getting
          wiped to NULL/stale the moment it fails once.
        - last_error keeps the most recent non-None ``error`` ever
          passed for this source - a clean status="OK" call (error=None)
          does not clear it, so a recovered source can still say what
          broke last time instead of going silent about its own history.

        Never pass anything password/cookie/token/MFA-shaped as
        ``error`` - see this table's schema comment in storage/models.py.
        """
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT last_successful_sync, last_error FROM source_status WHERE source = ?",
                (source,),
            ).fetchone()
            last_successful_sync = existing["last_successful_sync"] if existing else None
            if status == "OK":
                last_successful_sync = checked_at
            last_error = error if error is not None else (existing["last_error"] if existing else None)
            conn.execute(
                """
                INSERT INTO source_status
                    (source, status, checked_at, last_successful_sync, last_error)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                    status=excluded.status,
                    checked_at=excluded.checked_at,
                    last_successful_sync=excluded.last_successful_sync,
                    last_error=excluded.last_error
                """,
                (source, status, checked_at, last_successful_sync, last_error),
            )

    def get_source_status(self, source: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM source_status WHERE source = ?", (source,)
            ).fetchone()

    def get_all_source_status(self) -> list[sqlite3.Row]:
        """The single health/status report a future Study Manager (or a
        Telegram /status extension) can read without knowing about
        Teams/SSO/Auth Checker internals at all - just "what sources
        exist and can their data currently be trusted" (bro's ТЗ, Auth
        Checker Agent step, point 12)."""
        with self.connect() as conn:
            return conn.execute("SELECT * FROM source_status ORDER BY source").fetchall()

    # -- checker_findings (Checker Agent) ---------------------------------

    def upsert_checker_finding(
        self,
        course_code: str,
        course_title: str | None,
        overall_status: str,
        checked_at: str,
        window_start: str,
        window_end: str,
        details: dict,
        source: str = "checker",
    ) -> None:
        """One row per (course_code, window_start, window_end) - a
        re-run with the SAME window (agents/checker/agent.py's spec §17
        dedup rule) updates that row in place via the UNIQUE constraint
        in storage/models.py rather than inserting a duplicate. No
        history table on this first pass, same as source_status."""
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO checker_findings
                    (course_code, course_title, overall_status, checked_at,
                     window_start, window_end, source, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(course_code, window_start, window_end) DO UPDATE SET
                    course_title=excluded.course_title,
                    overall_status=excluded.overall_status,
                    checked_at=excluded.checked_at,
                    source=excluded.source,
                    details=excluded.details
                """,
                (
                    course_code,
                    course_title,
                    overall_status,
                    checked_at,
                    window_start,
                    window_end,
                    source,
                    json.dumps(details, ensure_ascii=False),
                ),
            )

    def get_checker_findings(self, course_code: str | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if course_code is None:
                return conn.execute(
                    "SELECT * FROM checker_findings ORDER BY course_code"
                ).fetchall()
            return conn.execute(
                "SELECT * FROM checker_findings WHERE course_code = ? ORDER BY window_start",
                (course_code,),
            ).fetchall()

    # -- study_overrides (AI Manager ТЗ v3 §12-16) ------------------------

    def upsert_override(
        self,
        target_type: str,
        target_id: str,
        field: str,
        value: Any,
        course_code: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Upserts the one active row for (target_type, target_id, field) -
        a repeated "set" on the same target/field replaces its value in
        place (via the UNIQUE constraint in storage/models.py) rather
        than growing a duplicate row, and reactivates it (active=1) if
        it had previously been cleared. ``field`` is NOT validated here
        - agents/study_manager/logic.py's validate_override_field() is
        the single point that enforces the allowlist, called by
        StudyManager BEFORE this method is ever reached, so a bad field
        name never gets this far in the first place."""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM study_overrides WHERE target_type = ? AND target_id = ? AND field = ?",
                (target_type, target_id, field),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO study_overrides
                    (target_type, target_id, course_code, field, value_json,
                     reason, created_at, updated_at, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(target_type, target_id, field) DO UPDATE SET
                    course_code=excluded.course_code,
                    value_json=excluded.value_json,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at,
                    active=1
                """,
                (
                    target_type,
                    target_id,
                    course_code,
                    field,
                    json.dumps(value, ensure_ascii=False),
                    reason,
                    created_at,
                    now,
                ),
            )

    def get_overrides(
        self,
        target_type: str | None = None,
        target_id: str | None = None,
        course_code: str | None = None,
        active_only: bool = True,
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM study_overrides WHERE 1=1"
        params: list[Any] = []
        if target_type is not None:
            query += " AND target_type = ?"
            params.append(target_type)
        if target_id is not None:
            query += " AND target_id = ?"
            params.append(str(target_id))
        if course_code is not None:
            query += " AND course_code = ?"
            params.append(course_code)
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY course_code, target_id, field"
        with self.connect() as conn:
            return conn.execute(query, params).fetchall()

    def clear_overrides(self, target_type: str, target_id: str, field: str | None = None) -> int:
        """Soft-deletes (active=0) either one field's override or, when
        ``field`` is None, every active override on that target
        ("верни всё как было в Teams" - spec §21). Never deletes the
        row outright - see storage/models.py's schema comment on why.
        Returns how many rows were cleared, so a caller can tell "there
        was nothing to clear" apart from "cleared 1"."""
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            if field is None:
                cur = conn.execute(
                    "UPDATE study_overrides SET active = 0, updated_at = ? "
                    "WHERE target_type = ? AND target_id = ? AND active = 1",
                    (now, target_type, target_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE study_overrides SET active = 0, updated_at = ? "
                    "WHERE target_type = ? AND target_id = ? AND field = ? AND active = 1",
                    (now, target_type, target_id, field),
                )
            return cur.rowcount

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
