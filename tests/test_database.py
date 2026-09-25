from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_schema_creates_all_required_tables(tmp_db):
    with tmp_db.connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    required = {"tasks", "weather_snapshots", "valorant_store", "notifications", "agent_runs"}
    assert required.issubset(tables)


def test_record_and_read_agent_run(tmp_db):
    now = datetime.now(timezone.utc).isoformat()
    tmp_db.record_agent_run("weather", "working", now, now, None)

    last = tmp_db.last_run("weather")
    assert last is not None
    assert last["status"] == "working"
    assert last["error"] is None


def test_notification_dedup_prevents_duplicate_sends(tmp_db):
    assert tmp_db.was_notified("task_overdue", "task-123") is False

    tmp_db.mark_notified("task_overdue", "task-123")

    assert tmp_db.was_notified("task_overdue", "task-123") is True
    # Marking the same (kind, dedupe_key) again must not raise (INSERT OR IGNORE).
    tmp_db.mark_notified("task_overdue", "task-123")


def test_cleanup_removes_only_old_rows_from_prunable_tables(tmp_db):
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()

    with tmp_db.connect() as conn:
        conn.execute(
            "INSERT INTO weather_snapshots (location, payload_json, created_at) VALUES (?, ?, ?)",
            ("Almaty", "{}", old),
        )
        conn.execute(
            "INSERT INTO weather_snapshots (location, payload_json, created_at) VALUES (?, ?, ?)",
            ("Almaty", "{}", recent),
        )

    deleted = tmp_db.cleanup(retention_days=30)
    assert deleted["weather_snapshots"] == 1

    with tmp_db.connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) AS c FROM weather_snapshots").fetchone()["c"]
    assert remaining == 1


def test_cleanup_does_not_touch_tasks_or_notifications(tmp_db):
    tmp_db.mark_notified("agent_status_change", "teams:failing")

    with tmp_db.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, course, title, status, source, updated_at) "
            "VALUES ('t1', 'CS101', 'Lab 1', 'overdue', 'teams', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )

    tmp_db.cleanup(retention_days=0)  # even an aggressive cutoff shouldn't touch these

    assert tmp_db.was_notified("agent_status_change", "teams:failing") is True
    with tmp_db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 1
