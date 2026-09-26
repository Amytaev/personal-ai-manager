"""SQLite schema for the Personal AI Manager (TZ v4 §12).

Tables: tasks, weather_snapshots, valorant_store, notifications,
agent_runs, sso_courses, sso_schedule_entries, sso_study_materials.
Retention/cleanup logic lives in storage/database.py, not here — this
module only defines shape.
"""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    course          TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT NOT NULL,
    created_at      TEXT,
    due_at          TEXT,
    submitted_at    TEXT,
    source          TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'new',  -- new | changed | resolved | unchanged
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    location        TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS valorant_store (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    skins_json      TEXT NOT NULL,
    checked_at      TEXT NOT NULL
);

-- Deduplication ledger for Telegram notifications (TZ v4 §20/§22):
-- a (kind, dedupe_key) pair is sent at most once.
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,       -- e.g. 'task_overdue', 'agent_status_change'
    dedupe_key      TEXT NOT NULL,       -- identifies the specific event instance
    sent_at         TEXT NOT NULL,
    UNIQUE(kind, dedupe_key)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT NOT NULL,
    status          TEXT NOT NULL,       -- working | degraded | failing
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    error           TEXT
);

-- SSO Agent (Этап B) - Schedule + UMKD normalized data. Snapshot-replace
-- semantics (storage/database.py's save_sso_snapshot): every successful
-- run wipes and re-inserts these three tables, same "whole-snapshot,
-- no natural per-row identity to track across runs" reasoning as
-- valorant_store above - a schedule can legitimately change room/time/
-- teacher between runs with no stable "this changed" signal worth
-- tracking yet at this stage.
CREATE TABLE IF NOT EXISTS sso_courses (
    code                    TEXT NOT NULL,
    semester_id             INTEGER NOT NULL,
    title                   TEXT NOT NULL,
    discipline_type_title   TEXT,
    cycle_title             TEXT,
    lecture_credits         INTEGER,
    practice_credits        INTEGER,
    lab_credits             INTEGER,
    total_credits           INTEGER,
    reading_chair_title     TEXT,
    description             TEXT,
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (code, semester_id)
);

CREATE TABLE IF NOT EXISTS sso_schedule_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    semester_id         INTEGER NOT NULL,
    class_id            INTEGER,
    course_code         TEXT,
    course_title        TEXT,
    instructor_name     TEXT,
    room_title          TEXT,
    class_type          INTEGER,
    day_title           TEXT,
    start_time          TEXT,
    end_time            TEXT,
    group_number        INTEGER,
    students_count      INTEGER,
    updated_at          TEXT NOT NULL
);

-- Metadata only (fileId/fileName/category) - the actual file bytes are
-- never fetched/stored at this stage (agents/sso/parser.py's docstring).
-- Not semester-scoped: GetFoldersForStudent's real capture spans
-- multiple academic years per course (УМКД 2024-2025/2025-2026/
-- 2026-2027 folders all under the same course node), so this is wiped
-- and fully re-inserted on every run rather than filtered by semester.
CREATE TABLE IF NOT EXISTS sso_study_materials (
    file_id                 INTEGER PRIMARY KEY,
    folder_id               INTEGER NOT NULL,
    file_name               TEXT NOT NULL,
    file_category_title     TEXT,
    course_title            TEXT,
    instructor_name         TEXT,
    updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_time ON agent_runs(agent, finished_at);
CREATE INDEX IF NOT EXISTS idx_weather_created ON weather_snapshots(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_sso_schedule_semester ON sso_schedule_entries(semester_id);
CREATE INDEX IF NOT EXISTS idx_sso_materials_folder ON sso_study_materials(folder_id);
"""
