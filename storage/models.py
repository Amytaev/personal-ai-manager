"""SQLite schema for the Personal AI Manager (TZ v4 §12).

Tables: tasks, weather_snapshots, valorant_store, notifications,
agent_runs. Retention/cleanup logic lives in storage/database.py, not
here — this module only defines shape.
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

CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_time ON agent_runs(agent, finished_at);
CREATE INDEX IF NOT EXISTS idx_weather_created ON weather_snapshots(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
"""
