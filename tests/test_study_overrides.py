from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agents.study_manager.agent import StudyManager
from agents.study_manager.logic import (
    OverrideValidationError,
    apply_overrides_to_task,
    task_view,
    validate_override_field,
    validate_target_type,
)

_NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _insert_task(db, id="t1", course="CSE4112 Тест (Лаб)", status="upcoming", due_at=None, submitted_at=None):
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, course, title, status, due_at, submitted_at, source, updated_at) "
            "VALUES (?, ?, 'ЛР №3', ?, ?, ?, 'teams', ?)",
            (id, course, status, due_at, submitted_at, _iso(_NOW)),
        )


# ===========================================================================
# Field/value allowlist validation (spec §16)
# ===========================================================================


@pytest.mark.parametrize(
    "field,value",
    [
        ("academic_week", 5),
        ("activity_number", 3),
        ("is_current", True),
        ("local_due_date", "2026-10-02T00:00:00+00:00"),
        ("local_completion_status", "COMPLETED"),
        ("user_note", "Преподаватель сказал сдать до пятницы"),
    ],
)
def test_validate_override_field_accepts_allowed_fields(field, value):
    assert validate_override_field(field, value) == value


def test_validate_override_field_rejects_unknown_field():
    with pytest.raises(OverrideValidationError):
        validate_override_field("some_internal_database_column", "hacked")


@pytest.mark.parametrize(
    "field,value",
    [
        ("academic_week", "five"),
        ("academic_week", -1),
        ("academic_week", True),  # bool is not an acceptable int here
        ("is_current", "true"),
        ("local_due_date", "not a date"),
        ("local_completion_status", "DONE"),  # not one of the 3 allowed values
        ("user_note", ""),
    ],
)
def test_validate_override_field_rejects_wrong_shaped_values(field, value):
    with pytest.raises(OverrideValidationError):
        validate_override_field(field, value)


def test_validate_target_type_rejects_anything_not_task_activity_course():
    validate_target_type("task")
    validate_target_type("activity")
    validate_target_type("course")
    with pytest.raises(OverrideValidationError):
        validate_target_type("wishlist_item")


# ===========================================================================
# StudyManager.set_task_override / clear_override / get_overrides (§74)
# ===========================================================================


def test_set_task_override_rejects_disallowed_field_before_touching_db(tmp_db):
    sm = StudyManager(db=tmp_db)
    with pytest.raises(OverrideValidationError):
        sm.set_task_override("t1", "some_internal_database_column", "value")
    assert tmp_db.get_overrides() == []


def test_set_task_override_stores_and_get_overrides_lists_it(tmp_db):
    sm = StudyManager(db=tmp_db)
    sm.set_task_override("t1", "academic_week", 5, course_code="CSE4112")
    overrides = sm.get_overrides()
    assert len(overrides) == 1
    assert overrides[0]["field"] == "academic_week"
    assert overrides[0]["value"] == 5
    assert overrides[0]["active"] is True


def test_set_task_override_on_same_target_field_upserts_in_place(tmp_db):
    sm = StudyManager(db=tmp_db)
    sm.set_task_override("t1", "activity_number", 2)
    sm.set_task_override("t1", "activity_number", 3)
    overrides = sm.get_overrides(target_type="task")
    assert len(overrides) == 1
    assert overrides[0]["value"] == 3


def test_set_activity_override_uses_a_distinct_target_type(tmp_db):
    sm = StudyManager(db=tmp_db)
    sm.set_activity_override("schedule-42", "is_current", True, course_code="CSE4112")
    overrides = sm.get_overrides(target_type="activity")
    assert len(overrides) == 1
    assert overrides[0]["target_id"] == "schedule-42"


def test_clear_override_removes_only_the_named_field(tmp_db):
    sm = StudyManager(db=tmp_db)
    sm.set_task_override("t1", "academic_week", 5)
    sm.set_task_override("t1", "is_current", True)
    result = sm.clear_override("task", "t1", field="academic_week")
    assert result["cleared"] == 1
    remaining = sm.get_overrides(target_type="task")
    assert [o["field"] for o in remaining] == ["is_current"]


def test_clear_override_with_no_field_clears_everything_on_that_target(tmp_db):
    sm = StudyManager(db=tmp_db)
    sm.set_task_override("t1", "academic_week", 5)
    sm.set_task_override("t1", "is_current", True)
    result = sm.clear_override("task", "t1")
    assert result["cleared"] == 2
    assert sm.get_overrides(target_type="task") == []


def test_clear_override_never_touches_a_different_targets_overrides(tmp_db):
    sm = StudyManager(db=tmp_db)
    sm.set_task_override("t1", "academic_week", 5)
    sm.set_task_override("t2", "academic_week", 7)
    sm.clear_override("task", "t1")
    remaining = sm.get_overrides(target_type="task")
    assert [o["target_id"] for o in remaining] == ["t2"]


def test_clear_override_rejects_unknown_target_type(tmp_db):
    sm = StudyManager(db=tmp_db)
    with pytest.raises(OverrideValidationError):
        sm.clear_override("wishlist_item", "t1")


# ===========================================================================
# Effective value precedence + conflicts (spec §19-20, §75)
# ===========================================================================


def test_effective_equals_source_when_no_override_exists():
    view = task_view({"id": "t1", "course": "X", "status": "upcoming", "due_at": "2026-09-30T23:59:00+00:00"})
    result = apply_overrides_to_task(view, [])
    assert result["source"]["due_date"] == "2026-09-30T23:59:00+00:00"
    assert result["effective"]["due_date"] == "2026-09-30T23:59:00+00:00"
    assert result["override"] == {}


def test_effective_due_date_uses_override_but_source_due_date_is_preserved():
    view = task_view({"id": "t1", "course": "X", "status": "upcoming", "due_at": "2026-09-30T00:00:00+00:00"})
    overrides = [{"field": "local_due_date", "value": "2026-10-02T00:00:00+00:00"}]
    result = apply_overrides_to_task(view, overrides)
    assert result["source"]["due_date"] == "2026-09-30T00:00:00+00:00"
    assert result["override"]["local_due_date"] == "2026-10-02T00:00:00+00:00"
    assert result["effective"]["due_date"] == "2026-10-02T00:00:00+00:00"


def test_clearing_override_makes_effective_fall_back_to_source(tmp_db):
    _insert_task(tmp_db, due_at="2026-09-30T00:00:00+00:00")
    sm = StudyManager(db=tmp_db)
    sm.set_task_override("t1", "local_due_date", "2026-10-02T00:00:00+00:00")
    before = sm.get_effective_task("t1")
    assert before["effective"]["due_date"] == "2026-10-02T00:00:00+00:00"

    sm.clear_override("task", "t1", field="local_due_date")
    after = sm.get_effective_task("t1")
    assert after["effective"]["due_date"] == "2026-09-30T00:00:00+00:00"
    assert after["override"] == {}


def test_local_completion_status_never_overwrites_teams_own_status_in_source():
    # spec §50: Teams says NOT_SUBMITTED, user says "I already submitted it" -
    # `source` must keep reporting Teams' own truth untouched.
    view = task_view({"id": "t1", "course": "X", "status": "upcoming", "due_at": None})
    overrides = [{"field": "local_completion_status", "value": "COMPLETED"}]
    result = apply_overrides_to_task(view, overrides)
    assert result["source"]["is_completed"] is False
    assert result["source"]["status"] == "upcoming"
    assert result["effective"]["completion_status"] == "COMPLETED"


def test_get_effective_task_returns_none_for_an_unknown_task_id(tmp_db):
    sm = StudyManager(db=tmp_db)
    assert sm.get_effective_task("does-not-exist") is None


def test_get_effective_task_end_to_end_matches_spec_example_shape(tmp_db):
    _insert_task(tmp_db, id="123", course="CSE4112 Тест (Лаб)", status="upcoming", due_at="2026-09-30T00:00:00+00:00")
    sm = StudyManager(db=tmp_db)
    sm.set_task_override("123", "academic_week", 5, course_code="CSE4112")
    sm.set_task_override("123", "is_current", True, course_code="CSE4112")
    sm.set_task_override("123", "local_completion_status", "COMPLETED", course_code="CSE4112")

    result = sm.get_effective_task("123")
    assert result["task_id"] == "123"
    assert result["course_code"] == "CSE4112"
    assert result["source"]["is_completed"] is False
    assert result["effective"]["academic_week"] == 5
    assert result["effective"]["is_current"] is True
    assert result["effective"]["completion_status"] == "COMPLETED"


# ===========================================================================
# Storage layer directly (DB CRUD)
# ===========================================================================


def test_db_get_overrides_is_empty_before_any_write(tmp_db):
    assert tmp_db.get_overrides() == []


def test_db_upsert_override_is_idempotent_on_same_target_and_field(tmp_db):
    tmp_db.upsert_override("task", "t1", "academic_week", 5)
    tmp_db.upsert_override("task", "t1", "academic_week", 6)
    rows = tmp_db.get_overrides()
    assert len(rows) == 1
    assert rows[0]["value_json"] == "6"


def test_db_clear_overrides_is_a_soft_delete_not_a_real_delete(tmp_db):
    tmp_db.upsert_override("task", "t1", "academic_week", 5)
    tmp_db.clear_overrides("task", "t1")
    assert tmp_db.get_overrides() == []  # active_only=True by default
    assert tmp_db.get_overrides(active_only=False) != []


def test_db_clear_overrides_returns_zero_when_nothing_matched(tmp_db):
    assert tmp_db.clear_overrides("task", "does-not-exist") == 0
