from __future__ import annotations

from datetime import datetime, timezone

from agents.teams.parser import (
    compute_status,
    parse_assignment,
    parse_due,
    parse_work_response,
    resolve_course_name,
)

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)


def _assignment(**overrides) -> dict:
    """A minimal educationAssignment shaped like the real captured data
    (agents/teams/parser.py's module docstring / README explain where
    this shape came from) - only the fields parse_assignment() actually
    reads, with sane defaults a test can override."""
    base = {
        "id": "assignment-1",
        "classId": "e3f75118-bae6-4636-87c7-b2e91b63f913",
        "displayName": "Лабораторная работа №3",
        "dueDateTime": "2026-10-03T14:30:00Z",
        "createdDateTime": "2026-09-14T04:45:48.682Z",
        "isCompleted": False,
        "instructions": None,
        "submissions": [
            {
                "status": "working",
                "submittedDateTime": None,
                "returnedDateTime": None,
            }
        ],
    }
    base.update(overrides)
    return base


def test_resolve_course_name_known_class_id():
    assert resolve_course_name("162b0fa2-6461-4eb0-aef3-da996e67e7e7") == (
        "CSE8092 Проектирование и защита серверных баз данных"
    )


def test_resolve_course_name_falls_back_for_unknown_class_id():
    name = resolve_course_name("00000000-1111-2222-3333-444444444444")
    assert name == "Курс 00000000"


def test_resolve_course_name_handles_missing_class_id():
    assert resolve_course_name(None) == "Неизвестный курс"
    assert resolve_course_name("") == "Неизвестный курс"


def test_status_upcoming_when_not_due_yet():
    assignment = _assignment(dueDateTime="2026-10-03T14:30:00Z")
    status = compute_status(assignment, assignment["submissions"][0], now=NOW)
    assert status == "upcoming"


def test_status_overdue_when_past_due_and_not_completed():
    assignment = _assignment(dueDateTime="2026-09-01T00:00:00Z", isCompleted=False)
    status = compute_status(assignment, assignment["submissions"][0], now=NOW)
    assert status == "overdue"


def test_status_submitted_takes_priority_over_due_date():
    assignment = _assignment(dueDateTime="2026-09-01T00:00:00Z")  # in the past
    submission = {"submittedDateTime": "2026-08-30T10:00:00Z", "returnedDateTime": None}
    status = compute_status(assignment, submission, now=NOW)
    assert status == "submitted"


def test_status_returned_takes_priority_over_submitted():
    submission = {
        "submittedDateTime": "2026-08-30T10:00:00Z",
        "returnedDateTime": "2026-09-02T10:00:00Z",
    }
    status = compute_status(_assignment(), submission, now=NOW)
    assert status == "returned"


def test_status_completed_when_isCompleted_true_and_no_submission_dates():
    assignment = _assignment(isCompleted=True)
    submission = {"submittedDateTime": None, "returnedDateTime": None}
    status = compute_status(assignment, submission, now=NOW)
    assert status == "completed"


def test_status_upcoming_when_no_deadline_at_all():
    assignment = _assignment(dueDateTime=None, isCompleted=False)
    status = compute_status(assignment, assignment["submissions"][0], now=NOW)
    assert status == "upcoming"


def test_status_ignores_malformed_due_date_instead_of_raising():
    assignment = _assignment(dueDateTime="not-a-real-date", isCompleted=False)
    status = compute_status(assignment, assignment["submissions"][0], now=NOW)
    assert status == "upcoming"


def test_parse_assignment_maps_all_fields():
    assignment = _assignment()
    task = parse_assignment(assignment, now=NOW)

    assert task["id"] == "assignment-1"
    assert task["course"] == "CSE4112 Администрирование систем и сетей (Лаб)"
    assert task["title"] == "Лабораторная работа №3"
    assert task["description"] is None
    assert task["status"] == "upcoming"
    assert task["due_at"] == "2026-10-03T14:30:00Z"
    assert task["source"] == "teams"


def test_parse_assignment_handles_missing_title():
    task = parse_assignment(_assignment(displayName=None), now=NOW)
    assert task["title"] == "(без названия)"


def test_parse_assignment_handles_dict_shaped_instructions():
    task = parse_assignment(
        _assignment(instructions={"content": "Сдать до пятницы", "contentType": "text"}), now=NOW
    )
    assert task["description"] == "Сдать до пятницы"


def test_parse_assignment_handles_no_submissions_at_all():
    assignment = _assignment(submissions=[])
    task = parse_assignment(assignment, now=NOW)
    assert task["submitted_at"] is None
    assert task["status"] == "upcoming"


def test_parse_work_response_handles_multiple_courses():
    body = {
        "value": [
            _assignment(id="a1", classId="e3f75118-bae6-4636-87c7-b2e91b63f913"),
            _assignment(id="a2", classId="162b0fa2-6461-4eb0-aef3-da996e67e7e7"),
        ]
    }
    tasks = parse_work_response(body, now=NOW)
    courses = {t["course"] for t in tasks}
    assert len(tasks) == 2
    assert len(courses) == 2


def test_parse_work_response_empty_or_malformed_body_returns_empty_list():
    assert parse_work_response({}, now=NOW) == []
    assert parse_work_response({"value": "not-a-list"}, now=NOW) == []
    assert parse_work_response(None, now=NOW) == []  # type: ignore[arg-type]


def test_parse_due_parses_a_real_zulu_timestamp():
    due = parse_due("2026-10-03T14:30:00Z")
    assert due == datetime(2026, 10, 3, 14, 30, 0, tzinfo=timezone.utc)


def test_parse_due_handles_missing_or_malformed_values():
    assert parse_due(None) is None
    assert parse_due("") is None
    assert parse_due("not-a-real-date") is None
