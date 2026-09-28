from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from agents.checker.agent import CheckerAgent
from agents.checker.logic import (
    ActivityStatus,
    CheckerStatus,
    course_matches_task,
    evaluate_course,
    expand_occurrences,
    extract_course_code,
    extract_teams_course_title,
    find_candidates,
    normalize_activity_type,
    normalize_course_code,
    normalize_title,
    pick_nearest,
    task_activity_type,
    task_due_date,
)
from agents.base import AgentStatus

_MONDAY = "MONDAY_SHORT"
_WEDNESDAY = "WEDNESDAY_SHORT"


def _row(id: int, day_title: str, class_type, course_code: str = "CSE4112") -> dict:
    return {"id": id, "day_title": day_title, "class_type": class_type, "course_code": course_code}


def _task(id: int, course: str, title: str = "", description: str | None = None, due_at: str | None = None) -> dict:
    return {"id": id, "course": course, "title": title, "description": description, "due_at": due_at}


def _iso(d: date) -> str:
    return f"{d.isoformat()}T00:00:00+00:00"


# A fixed window (Mon 2026-09-14 .. Sun 2026-10-18) covering both target
# weekdays several times over - the pure logic functions never read the
# real clock, so tests can use literal dates rather than mocking "now".
_WINDOW_START = date(2026, 9, 14)
_WINDOW_END = date(2026, 10, 18)
# The single Monday-of-week-1 occurrence inside that window, used as
# "the" occurrence date in single-activity tests.
_MONDAY_DATE = date(2026, 9, 14)
_WEDNESDAY_DATE = date(2026, 9, 16)


# -- §19: LAB/PRACTICE/LECTURE aliases (test 19) ----------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Лабораторная", "LAB"),
        ("Лабораторная работа", "LAB"),
        ("Лаб. работа", "LAB"),
        ("ЛР", "LAB"),
        ("Lab", "LAB"),
        ("Lab work", "LAB"),
        ("Практика", "PRACTICE"),
        ("Практическое занятие", "PRACTICE"),
        ("Практическое задание", "PRACTICE"),
        ("ПЗ", "PRACTICE"),
        ("Practice", "PRACTICE"),
        ("Лекция", "LECTURE"),
        ("Lecture", "LECTURE"),
        (None, "OTHER"),
        ("", "OTHER"),
        ("Консультация", "OTHER"),
        (3, "OTHER"),  # unconfirmed int shape - never guessed into a real type
    ],
)
def test_normalize_activity_type_aliases(raw, expected):
    assert normalize_activity_type(raw) == expected


# -- §6.3: course code normalization (test 9) -------------------------------

@pytest.mark.parametrize("raw", ["CSE4112", "cse-4112", "CSE 4112", "cse 4112"])
def test_normalize_course_code_treats_these_as_one_code(raw):
    assert normalize_course_code(raw) == "CSE4112"


def test_normalize_course_code_none_for_empty():
    assert normalize_course_code(None) is None
    assert normalize_course_code("") is None


# -- §6.4: exact normalized title matching (test 10) ------------------------

def test_normalize_title_collapses_whitespace_and_case():
    assert normalize_title("  Администрирование   систем и сетей ") == normalize_title(
        "администрирование систем и сетей"
    )


def test_course_matches_task_by_code():
    course = {"code": "cse-4112", "title": "Администрирование систем и сетей"}
    task = _task(1, course="CSE4112 Администрирование систем и сетей (Лаб)")
    assert course_matches_task(course, task) is True


def test_course_matches_task_falls_back_to_exact_title_when_no_code():
    course = {"code": None, "title": "Основы научно-исследовательской работы студентов"}
    task = _task(1, course="Основы научно-исследовательской работы студентов")
    assert course_matches_task(course, task) is True


def test_course_matches_task_false_for_unrelated_course():
    course = {"code": "CSE9999", "title": "Совсем другой предмет"}
    task = _task(1, course="CSE4112 Администрирование систем и сетей (Лаб)")
    assert course_matches_task(course, task) is False


def test_extract_course_code_from_teams_display_name():
    assert extract_course_code("CSE4112 Администрирование систем и сетей (Лаб)") == "CSE4112"
    assert extract_course_code("ВиАУ Вт 7.50") is None


def test_extract_teams_course_title_strips_code_and_type_suffix():
    assert (
        extract_teams_course_title("CSE4112 Администрирование систем и сетей (Лаб)")
        == normalize_title("Администрирование систем и сетей")
    )


# -- §6.1: Teams assignment type detection (uses real embedded-in-course-name shape) --

def test_task_activity_type_reads_the_resolved_course_name_suffix():
    # Real captured data (agents/teams/parser.py's COURSE_NAMES) embeds
    # the type hint in the resolved `course` field, not title/description.
    task = _task(1, course="CSE4112 Администрирование систем и сетей (Лаб)", title="Задание 3")
    assert task_activity_type(task) == "LAB"


def test_task_activity_type_reads_title_when_course_has_no_hint():
    task = _task(1, course="CSE4112 Администрирование систем и сетей", title="Практическая работа №1")
    assert task_activity_type(task) == "PRACTICE"


# -- §11/§22: statuses never claim a confirmed miss -------------------------

def test_no_match_status_is_spelled_no_match_not_missing_from_teams():
    assert ActivityStatus.NO_MATCH.value == "NO_MATCH"
    assert {s.value for s in CheckerStatus} == {
        "MATCH", "PARTIAL_MATCH", "NO_MATCH", "UNMATCHED_COURSE",
        "AUTH_REQUIRED", "SOURCE_UNAVAILABLE", "ERROR",
    }
    assert "MISSING_FROM_TEAMS" not in {s.value for s in CheckerStatus}


# -- §7/§13: recurring schedule -> dated occurrences -------------------------

def test_expand_occurrences_generates_one_per_matching_weekday_in_window():
    rows = [_row(101, _MONDAY, "Лабораторная")]
    occurrences = expand_occurrences(rows, _WINDOW_START, _WINDOW_END)
    dates = [o["date"] for o in occurrences]
    assert _MONDAY_DATE in dates
    assert all(d.weekday() == 0 for d in dates)
    assert all(_WINDOW_START <= d <= _WINDOW_END for d in dates)


def test_expand_occurrences_skips_unrecognized_day_title():
    rows = [_row(101, "NOT_A_REAL_DAY", "Лабораторная")]
    assert expand_occurrences(rows, _WINDOW_START, _WINDOW_END) == []


# -- §16/§20: window filtering + "old assignment doesn't match new class" (tests 16, 20) --

def test_find_candidates_excludes_a_task_outside_the_window():
    course_tasks = [_task(1, "CSE4112 X (Лаб)", due_at=_iso(date(2026, 4, 1)))]  # way before window
    candidates = find_candidates(course_tasks, "LAB", _WINDOW_START, _WINDOW_END)
    assert candidates == []


def test_old_assignment_does_not_match_new_occurrence_just_because_course_matches():
    # Same course, same type, but an April due date - must never be
    # picked for a September occurrence just because the course lines up.
    schedule = [_row(101, _MONDAY, "Лабораторная")]
    tasks = [_task(1, "CSE4112 X (Лаб)", due_at=_iso(date(2026, 4, 1)))]
    activities, overall = evaluate_course(schedule, tasks, _WINDOW_START, _WINDOW_END)
    assert all(a["status"] != ActivityStatus.MATCH.value for a in activities)
    assert overall == CheckerStatus.NO_MATCH.value


# -- §17: assignment without a due date is an "active" candidate (test 17) --

def test_task_due_date_is_none_for_missing_due_at():
    assert task_due_date(_task(1, "X")) is None


def test_find_candidates_includes_a_task_with_no_due_date():
    course_tasks = [_task(1, "CSE4112 X (Лаб)", due_at=None)]
    candidates = find_candidates(course_tasks, "LAB", _WINDOW_START, _WINDOW_END)
    assert len(candidates) == 1
    assert candidates[0][1] is None


def test_pick_nearest_prefers_a_dated_candidate_over_an_undated_one():
    dated = _task(1, "CSE4112 X (Лаб)")
    undated = _task(2, "CSE4112 X (Лаб)")
    candidates = [(undated, None), (dated, _MONDAY_DATE)]
    assert pick_nearest(candidates, _MONDAY_DATE) == [1]


# -- §8-10, §21: evaluate_course overall-status rules (tests 1-8, 21) -------

def test_lab_only_with_teams_lab_is_match():
    schedule = [_row(101, _MONDAY, "Лабораторная")]
    tasks = [_task(1, "CSE4112 X (Лаб)", due_at=_iso(_MONDAY_DATE))]
    activities, overall = evaluate_course(schedule, tasks, _WINDOW_START, _WINDOW_END)
    assert overall == CheckerStatus.MATCH.value
    assert all(a["status"] == ActivityStatus.MATCH.value for a in activities)


def test_practice_only_with_teams_practice_is_match():
    schedule = [_row(101, _MONDAY, "Практика")]
    tasks = [_task(1, "CSE4112 X (Практика)", due_at=_iso(_MONDAY_DATE))]
    activities, overall = evaluate_course(schedule, tasks, _WINDOW_START, _WINDOW_END)
    assert overall == CheckerStatus.MATCH.value


def test_lab_and_practice_with_teams_lab_only_is_partial_match():
    schedule = [_row(101, _MONDAY, "Лабораторная"), _row(102, _WEDNESDAY, "Практика")]
    tasks = [_task(1, "CSE4112 X (Лаб)", due_at=_iso(_MONDAY_DATE))]
    activities, overall = evaluate_course(schedule, tasks, _WINDOW_START, _WINDOW_END)
    assert overall == CheckerStatus.PARTIAL_MATCH.value
    statuses = {a["type"]: a["status"] for a in activities}
    assert statuses["LAB"] == ActivityStatus.MATCH.value
    assert statuses["PRACTICE"] == ActivityStatus.NO_MATCH.value


def test_lab_and_practice_with_teams_practice_only_is_partial_match():
    schedule = [_row(101, _MONDAY, "Лабораторная"), _row(102, _WEDNESDAY, "Практика")]
    tasks = [_task(1, "CSE4112 X (Практика)", due_at=_iso(_WEDNESDAY_DATE))]
    activities, overall = evaluate_course(schedule, tasks, _WINDOW_START, _WINDOW_END)
    assert overall == CheckerStatus.PARTIAL_MATCH.value


def test_lab_and_practice_with_teams_both_is_match():
    schedule = [_row(101, _MONDAY, "Лабораторная"), _row(102, _WEDNESDAY, "Практика")]
    tasks = [
        _task(1, "CSE4112 X (Лаб)", due_at=_iso(_MONDAY_DATE)),
        _task(2, "CSE4112 X (Практика)", due_at=_iso(_WEDNESDAY_DATE)),
    ]
    activities, overall = evaluate_course(schedule, tasks, _WINDOW_START, _WINDOW_END)
    assert overall == CheckerStatus.MATCH.value


def test_lab_and_practice_with_teams_none_is_no_match():
    schedule = [_row(101, _MONDAY, "Лабораторная"), _row(102, _WEDNESDAY, "Практика")]
    activities, overall = evaluate_course(schedule, [], _WINDOW_START, _WINDOW_END)
    assert overall == CheckerStatus.NO_MATCH.value
    assert all(a["status"] == ActivityStatus.NO_MATCH.value for a in activities)


def test_unclassifiable_activity_type_is_unknown_not_a_false_match():
    schedule = [_row(101, _MONDAY, "Консультация")]  # normalizes to OTHER
    tasks = [_task(1, "CSE4112 X (Лаб)", due_at=_iso(_MONDAY_DATE))]  # would otherwise be unrelated anyway
    activities, overall = evaluate_course(schedule, tasks, _WINDOW_START, _WINDOW_END)
    assert activities[0]["status"] == ActivityStatus.UNKNOWN.value
    assert activities[0]["type"] == "OTHER"
    # UNKNOWN must never be silently treated as a MATCH at the course level.
    assert overall != CheckerStatus.MATCH.value


# -- Checker Agent: source-status gating (tests 12, 13, 14) -----------------

@pytest.mark.asyncio
async def test_run_reports_auth_required_when_teams_needs_reauth(tmp_db):
    tmp_db.record_source_status("teams", "AUTH_REQUIRED", datetime.now(timezone.utc).isoformat())
    tmp_db.record_source_status("sso", "OK", datetime.now(timezone.utc).isoformat())
    agent = CheckerAgent(db=tmp_db)

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data == {"blocked": True, "source": "teams", "reason": "AUTH_REQUIRED"}
    assert tmp_db.get_checker_findings() == []


@pytest.mark.asyncio
async def test_run_reports_error_and_degraded_when_teams_check_itself_errored(tmp_db):
    tmp_db.record_source_status("teams", "ERROR", datetime.now(timezone.utc).isoformat())
    tmp_db.record_source_status("sso", "OK", datetime.now(timezone.utc).isoformat())
    agent = CheckerAgent(db=tmp_db)

    result = await agent.run()

    assert result.status == AgentStatus.DEGRADED
    assert result.data["reason"] == "ERROR"


@pytest.mark.asyncio
async def test_run_skips_the_sweep_entirely_when_sso_is_unavailable(tmp_db):
    tmp_db.record_source_status("teams", "OK", datetime.now(timezone.utc).isoformat())
    tmp_db.record_source_status("sso", "AUTH_REQUIRED", datetime.now(timezone.utc).isoformat())
    # Even though a real SSO snapshot exists in the DB, the live
    # source_status gate must still block the sweep (spec §12) - cached
    # data being present doesn't mean the source is currently trustworthy.
    tmp_db.save_sso_snapshot(
        semester_id=85, courses=[{"code": "CSE4112", "title": "X"}], schedule_entries=[], materials=[]
    )
    agent = CheckerAgent(db=tmp_db)

    result = await agent.run()

    assert result.data == {"blocked": True, "source": "sso", "reason": "AUTH_REQUIRED"}
    assert tmp_db.get_checker_findings() == []


# -- Checker Agent: end-to-end with a real (mocked-source-status) DB --------

def _mark_both_sources_ok(db) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.record_source_status("teams", "OK", now)
    db.record_source_status("sso", "OK", now)


def _today_day_title() -> str:
    from agents.checker.logic import DAY_TITLE_TO_WEEKDAY

    today_weekday = datetime.now(timezone.utc).date().weekday()
    for title, weekday in DAY_TITLE_TO_WEEKDAY.items():
        if weekday == today_weekday:
            return title
    raise AssertionError("no day_title maps to today's weekday")


@pytest.mark.asyncio
async def test_run_writes_a_match_finding_for_a_fully_matched_course(tmp_db):
    _mark_both_sources_ok(tmp_db)
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "CSE4112", "title": "Администрирование систем и сетей"}],
        schedule_entries=[
            {
                "class_id": 1, "course_code": "CSE4112", "course_title": "X",
                "class_type": "Лабораторная", "day_title": _today_day_title(),
                "start_time": "8:55", "end_time": "9:45",
            }
        ],
        materials=[],
    )
    today = datetime.now(timezone.utc).date()
    tmp_db.upsert_task(
        {
            "id": "t1", "course": "CSE4112 Администрирование систем и сетей (Лаб)",
            "title": "Задание 1", "status": "upcoming", "source": "teams",
            "due_at": f"{today.isoformat()}T00:00:00+00:00",
        },
        state="new",
    )
    agent = CheckerAgent(db=tmp_db)

    result = await agent.run()

    assert result.data["blocked"] is False
    assert result.data["findings_written"] == 1
    findings = tmp_db.get_checker_findings()
    assert len(findings) == 1
    assert findings[0]["course_code"] == "CSE4112"
    assert findings[0]["overall_status"] == CheckerStatus.MATCH.value


@pytest.mark.asyncio
async def test_run_reports_unmatched_course_when_teams_has_no_evidence_of_it(tmp_db):
    _mark_both_sources_ok(tmp_db)
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "CSE9999", "title": "Совсем неизвестный курс"}],
        schedule_entries=[
            {
                "class_id": 1, "course_code": "CSE9999", "course_title": "X",
                "class_type": "Лабораторная", "day_title": _today_day_title(),
                "start_time": "8:55", "end_time": "9:45",
            }
        ],
        materials=[],
    )
    # A real Teams task, but for a completely different course.
    tmp_db.upsert_task(
        {
            "id": "t1", "course": "CSE4112 Администрирование систем и сетей (Лаб)",
            "title": "Задание 1", "status": "upcoming", "source": "teams",
        },
        state="new",
    )
    agent = CheckerAgent(db=tmp_db)

    result = await agent.run()

    findings = tmp_db.get_checker_findings()
    assert len(findings) == 1
    assert findings[0]["overall_status"] == CheckerStatus.UNMATCHED_COURSE.value


@pytest.mark.asyncio
async def test_run_ignores_courses_from_an_old_semester(tmp_db):
    _mark_both_sources_ok(tmp_db)
    tmp_db.save_sso_snapshot(
        semester_id=80,
        courses=[{"code": "OLD1", "title": "Старый курс"}],
        schedule_entries=[
            {
                "class_id": 1, "course_code": "OLD1", "course_title": "X",
                "class_type": "Лабораторная", "day_title": _today_day_title(),
                "start_time": "8:55", "end_time": "9:45",
            }
        ],
        materials=[],
    )
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "CSE4112", "title": "Новый курс"}],
        schedule_entries=[],
        materials=[],
    )
    agent = CheckerAgent(db=tmp_db)

    await agent.run()

    findings = tmp_db.get_checker_findings()
    assert all(f["course_code"] != "OLD1" for f in findings)


@pytest.mark.asyncio
async def test_run_twice_does_not_create_duplicate_findings(tmp_db):
    _mark_both_sources_ok(tmp_db)
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "CSE4112", "title": "Администрирование систем и сетей"}],
        schedule_entries=[
            {
                "class_id": 1, "course_code": "CSE4112", "course_title": "X",
                "class_type": "Лабораторная", "day_title": _today_day_title(),
                "start_time": "8:55", "end_time": "9:45",
            }
        ],
        materials=[],
    )
    agent = CheckerAgent(db=tmp_db)

    await agent.run()
    await agent.run()

    findings = tmp_db.get_checker_findings()
    assert len(findings) == 1  # same (course_code, window) upserted in place, not duplicated
