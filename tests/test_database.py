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

    required = {
        "tasks", "weather_snapshots", "valorant_store", "notifications", "agent_runs",
        "sso_courses", "sso_schedule_entries", "sso_study_materials",
    }
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


def test_save_valorant_store_writes_items_and_reset_time(tmp_db):
    import json

    tmp_db.save_valorant_store(
        [{"uuid": "abc-123", "name": "Апертура", "price_vp": 1275, "image_url": "https://x/y.png"}],
        reset_in="7 часов и 49 минут",
    )

    with tmp_db.connect() as conn:
        row = conn.execute(
            "SELECT skins_json, checked_at FROM valorant_store ORDER BY checked_at DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    payload = json.loads(row["skins_json"])
    assert payload["items"][0]["name"] == "Апертура"
    assert payload["items"][0]["price_vp"] == 1275
    assert payload["reset_in"] == "7 часов и 49 минут"


def test_cleanup_removes_only_old_rows_from_valorant_store(tmp_db):
    old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()

    with tmp_db.connect() as conn:
        conn.execute(
            "INSERT INTO valorant_store (skins_json, checked_at) VALUES (?, ?)",
            ('{"items": [], "reset_in": null}', old),
        )
        conn.execute(
            "INSERT INTO valorant_store (skins_json, checked_at) VALUES (?, ?)",
            ('{"items": [], "reset_in": null}', recent),
        )

    deleted = tmp_db.cleanup(retention_days=30)
    assert deleted["valorant_store"] == 1

    with tmp_db.connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) AS c FROM valorant_store").fetchone()["c"]
    assert remaining == 1


def test_save_sso_snapshot_writes_courses_schedule_and_materials(tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "CSE5472", "title": "НИРС", "total_credits": 3}],
        schedule_entries=[{"class_id": 222374, "course_code": "CSE5472", "day_title": "MONDAY_SHORT"}],
        materials=[{"file_id": 1, "folder_id": 490059, "file_name": "Практика 1.docx"}],
    )

    assert len(tmp_db.get_sso_courses(85)) == 1
    assert tmp_db.get_sso_courses(85)[0]["code"] == "CSE5472"
    assert len(tmp_db.get_sso_schedule(85)) == 1
    assert tmp_db.get_sso_schedule(85)[0]["class_id"] == 222374
    assert len(tmp_db.get_sso_materials()) == 1
    assert tmp_db.get_sso_materials()[0]["file_name"] == "Практика 1.docx"


def test_save_sso_snapshot_replaces_the_previous_snapshot_for_that_semester(tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "OLD1", "title": "Old course"}],
        schedule_entries=[{"class_id": 1, "day_title": "MONDAY_SHORT"}],
        materials=[{"file_id": 1, "folder_id": 1, "file_name": "old.docx"}],
    )
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "NEW1", "title": "New course"}],
        schedule_entries=[{"class_id": 2, "day_title": "TUESDAY_SHORT"}],
        materials=[{"file_id": 2, "folder_id": 2, "file_name": "new.docx"}],
    )

    courses = tmp_db.get_sso_courses(85)
    assert [c["code"] for c in courses] == ["NEW1"]
    schedule = tmp_db.get_sso_schedule(85)
    assert [s["class_id"] for s in schedule] == [2]
    materials = tmp_db.get_sso_materials()
    assert [m["file_name"] for m in materials] == ["new.docx"]


def test_save_sso_snapshot_does_not_touch_a_different_semesters_courses(tmp_db):
    tmp_db.save_sso_snapshot(semester_id=80, courses=[{"code": "OLD", "title": "Old sem"}], schedule_entries=[], materials=[])
    tmp_db.save_sso_snapshot(semester_id=85, courses=[{"code": "NEW", "title": "New sem"}], schedule_entries=[], materials=[])

    assert [c["code"] for c in tmp_db.get_sso_courses(80)] == ["OLD"]
    assert [c["code"] for c in tmp_db.get_sso_courses(85)] == ["NEW"]


def test_get_latest_sso_semester_id_is_none_before_any_snapshot(tmp_db):
    assert tmp_db.get_latest_sso_semester_id() is None


def test_get_latest_sso_semester_id_returns_the_highest_seen(tmp_db):
    tmp_db.save_sso_snapshot(semester_id=80, courses=[{"code": "OLD", "title": "Old sem"}], schedule_entries=[], materials=[])
    tmp_db.save_sso_snapshot(semester_id=85, courses=[{"code": "NEW", "title": "New sem"}], schedule_entries=[], materials=[])

    assert tmp_db.get_latest_sso_semester_id() == 85


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
