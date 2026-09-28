from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agents.base import AgentResult, AgentStatus, BaseAgent
from agents.study_manager.agent import StudyManager
from agents.study_manager.logic import (
    CourseMatchResult,
    StudyManagerStatus,
    combine_source_status,
    course_matches,
    course_tasks_for,
    expand_weekly_schedule,
    extract_course_code,
    filter_overdue_tasks,
    filter_upcoming_tasks,
    is_task_completed,
    is_task_overdue,
    normalize_class_type,
    normalize_course_code,
    normalize_title,
    resolve_course,
    task_view,
)

_NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _task(
    id: str = "t1",
    course: str = "CSE4112 Администрирование систем и сетей (Лаб)",
    title: str = "ЛР №3",
    status: str = "upcoming",
    due_at: str | None = None,
    submitted_at: str | None = None,
) -> dict:
    return {
        "id": id,
        "course": course,
        "title": title,
        "description": None,
        "status": status,
        "due_at": due_at,
        "submitted_at": submitted_at,
        "source": "teams",
    }


class _StubAgent(BaseAgent):
    """A minimal fake source/checker/auth-checker agent for refresh()
    integration tests - records that it ran, and can be told to raise
    (spec §28's "one source's exception must not take down the rest")."""

    def __init__(self, name: str, *, raises: Exception | None = None, on_run=None):
        super().__init__(name=name)
        self.raises = raises
        self.on_run = on_run
        self.call_count = 0

    async def run(self) -> AgentResult:
        self.call_count += 1
        if self.raises is not None:
            raise self.raises
        if self.on_run is not None:
            self.on_run()
        return AgentResult(agent=self.name, status=AgentStatus.WORKING, data={})


# ===========================================================================
# 1. Source status (5 scenarios)
# ===========================================================================


def test_source_status_teams_ok_sso_ok_gives_overall_ok():
    assert combine_source_status("OK", "OK") == StudyManagerStatus.OK.value


def test_source_status_teams_auth_required_sso_ok_gives_partial():
    assert combine_source_status("AUTH_REQUIRED", "OK") == StudyManagerStatus.PARTIAL.value


def test_source_status_teams_error_sso_ok_gives_partial_result():
    assert combine_source_status("ERROR", "OK") == StudyManagerStatus.PARTIAL.value


def test_source_status_sso_auth_required_means_schedule_ops_cant_trust_current_data():
    # combine_source_status() itself reports PARTIAL (Teams still usable);
    # get_upcoming_schedule()'s own gate is "is there any SSO snapshot at
    # all", covered separately below - this asserts the top-level status
    # a caller sees never claims OK while SSO can't be trusted.
    assert combine_source_status("OK", "AUTH_REQUIRED") == StudyManagerStatus.PARTIAL.value


def test_source_status_both_unavailable_gives_source_unavailable():
    assert combine_source_status("UNAVAILABLE", "UNAVAILABLE") == StudyManagerStatus.SOURCE_UNAVAILABLE.value
    assert combine_source_status(None, None) == StudyManagerStatus.SOURCE_UNAVAILABLE.value


def test_source_status_both_auth_required_gives_auth_required():
    assert combine_source_status("AUTH_REQUIRED", "AUTH_REQUIRED") == StudyManagerStatus.AUTH_REQUIRED.value


# ===========================================================================
# 2. Tasks (5 scenarios)
# ===========================================================================


def test_upcoming_tasks_includes_task_due_within_window():
    due = _iso(_NOW + timedelta(days=2))
    view = task_view(_task(due_at=due))
    upcoming = filter_upcoming_tasks([view], days_ahead=7, now=_NOW)
    assert [t["id"] for t in upcoming] == ["t1"]


def test_overdue_tasks_includes_task_past_due_and_not_completed():
    due = _iso(_NOW - timedelta(days=1))
    view = task_view(_task(due_at=due, status="overdue"))
    overdue = filter_overdue_tasks([view], now=_NOW)
    assert [t["id"] for t in overdue] == ["t1"]


def test_completed_task_is_never_counted_as_overdue_even_with_past_due_date():
    due = _iso(_NOW - timedelta(days=5))
    view = task_view(_task(due_at=due, status="submitted", submitted_at=_iso(_NOW - timedelta(days=6))))
    assert is_task_completed(view) is True
    assert is_task_overdue(view, now=_NOW) is False
    assert filter_overdue_tasks([view], now=_NOW) == []


def test_task_without_due_date_does_not_break_processing():
    view = task_view(_task(due_at=None))
    # Neither list crashes nor wrongly claims this task - it just isn't
    # in either date-bounded view (spec §14).
    assert filter_upcoming_tasks([view], now=_NOW) == []
    assert filter_overdue_tasks([view], now=_NOW) == []
    assert is_task_overdue(view, now=_NOW) is False


def test_course_filtering_only_returns_tasks_for_that_course():
    cse = task_view(_task(id="t1", course="CSE4112 Администрирование систем и сетей (Лаб)"))
    other = task_view(_task(id="t2", course="CSE5472 Основы научно-исследовательской работы студентов"))
    courses = [{"code": "CSE4112", "title": "Администрирование систем и сетей"}]
    course, tasks = course_tasks_for("CSE4112", courses, [cse, other])
    assert not isinstance(course, CourseMatchResult)
    assert [t["id"] for t in tasks] == ["t1"]


# ===========================================================================
# 3. Schedule (4 scenarios)
# ===========================================================================


def _schedule_row(id: int, day_title: str, start_time: str = "10:00", course_code: str = "CSE4112") -> dict:
    return {
        "id": id,
        "course_code": course_code,
        "course_title": "Администрирование систем и сетей",
        "instructor_name": "Иванов И.И.",
        "room_title": "301",
        "class_type": "Лаб",
        "day_title": day_title,
        "start_time": start_time,
        "end_time": "11:50",
    }


def test_nearest_lesson_is_the_first_occurrence_in_the_expanded_schedule():
    # _NOW is Monday 2026-09-28.
    rows = [_schedule_row(1, "MONDAY_SHORT"), _schedule_row(2, "WEDNESDAY_SHORT", start_time="14:00")]
    occurrences = expand_weekly_schedule(rows, days_ahead=7, now=_NOW)
    assert occurrences[0]["schedule_entry_id"] == 1
    assert occurrences[0]["date"] == _NOW.date().isoformat()


def test_weekly_template_expands_into_real_calendar_dates():
    rows = [_schedule_row(1, "WEDNESDAY_SHORT")]
    occurrences = expand_weekly_schedule(rows, days_ahead=14, now=_NOW)
    dates = [o["date"] for o in occurrences]
    # Two Wednesdays inside a 14-day window starting on a Monday.
    assert dates == ["2026-09-30", "2026-10-07"]


def test_schedule_is_sorted_by_date_then_start_time():
    rows = [
        _schedule_row(1, "MONDAY_SHORT", start_time="14:00"),
        _schedule_row(2, "MONDAY_SHORT", start_time="09:00"),
    ]
    occurrences = expand_weekly_schedule(rows, days_ahead=0, now=_NOW)
    assert [o["schedule_entry_id"] for o in occurrences] == [2, 1]


def test_schedule_expansion_never_produces_duplicate_occurrences():
    rows = [_schedule_row(1, "MONDAY_SHORT")]
    occurrences = expand_weekly_schedule(rows, days_ahead=30, now=_NOW)
    keys = [(o["schedule_entry_id"], o["date"]) for o in occurrences]
    assert len(keys) == len(set(keys))


# ===========================================================================
# 4. Courses (4 scenarios)
# ===========================================================================


def test_course_code_matching_resolves_by_normalized_code():
    courses = [{"code": "CSE4112", "title": "Администрирование систем и сетей"}]
    course = resolve_course(courses, "cse-4112")
    assert not isinstance(course, CourseMatchResult)
    assert course["code"] == "CSE4112"


def test_exact_title_matching_used_as_fallback_when_code_lookup_fails():
    courses = [{"code": None, "title": "Основы научно-исследовательской работы студентов"}]
    course = resolve_course(courses, "Основы научно-исследовательской работы студентов")
    assert not isinstance(course, CourseMatchResult)


def test_unmatched_course_returns_not_found_sentinel():
    courses = [{"code": "CSE4112", "title": "Администрирование систем и сетей"}]
    course = resolve_course(courses, "XYZ9999")
    assert course is CourseMatchResult.NOT_FOUND


def test_ambiguous_course_is_never_randomly_picked():
    courses = [
        {"code": "CSE4112", "title": "Курс А"},
        {"code": "CSE4112", "title": "Курс Б"},
    ]
    course = resolve_course(courses, "CSE4112")
    assert course is CourseMatchResult.AMBIGUOUS


# ===========================================================================
# 5. Checker (3 scenarios)
# ===========================================================================


def test_checker_findings_are_available_via_study_manager(tmp_db):
    tmp_db.upsert_checker_finding(
        course_code="CSE4112",
        course_title="Администрирование систем и сетей",
        overall_status="MATCH",
        checked_at=_iso(_NOW),
        window_start="2026-09-21",
        window_end="2026-10-28",
        details={"activities": []},
    )
    sm = StudyManager(db=tmp_db)
    findings = sm.get_checker_findings()
    assert len(findings) == 1
    assert findings[0]["course_code"] == "CSE4112"
    assert findings[0]["details"] == {"activities": []}


@pytest.mark.asyncio
async def test_checker_is_not_called_when_sso_status_is_not_ok(tmp_db):
    tmp_db.record_source_status("teams", "OK", _iso(_NOW))
    tmp_db.record_source_status("sso", "AUTH_REQUIRED", _iso(_NOW))
    checker = _StubAgent("checker")
    sm = StudyManager(db=tmp_db, checker_agent=checker)
    await sm.refresh()
    assert checker.call_count == 0


def test_existing_finding_is_included_in_the_dashboard(tmp_db):
    tmp_db.upsert_checker_finding(
        course_code="CSE4112",
        course_title="X",
        overall_status="PARTIAL_MATCH",
        checked_at=_iso(_NOW),
        window_start="2026-09-21",
        window_end="2026-10-28",
        details={"activities": []},
    )
    sm = StudyManager(db=tmp_db)
    dashboard = sm.get_dashboard()
    assert [f["course_code"] for f in dashboard["checker_findings"]] == ["CSE4112"]


# ===========================================================================
# 6. Freshness (3 scenarios)
# ===========================================================================


def test_last_successful_sync_is_saved_and_readable_via_get_source_status(tmp_db):
    tmp_db.record_source_status("teams", "OK", _iso(_NOW))
    sm = StudyManager(db=tmp_db)
    rows = sm.get_source_status()
    teams = next(r for r in rows if r["source"] == "teams")
    assert teams["last_successful_sync"] == _iso(_NOW)


def test_auth_required_does_not_delete_old_task_or_schedule_data(tmp_db):
    with tmp_db.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, course, title, status, source, updated_at) "
            "VALUES ('t1', 'CSE4112', 'ЛР №3', 'upcoming', 'teams', ?)",
            (_iso(_NOW),),
        )
    tmp_db.record_source_status("teams", "AUTH_REQUIRED", _iso(_NOW))
    sm = StudyManager(db=tmp_db)
    # Old Teams data must still be readable - AUTH_REQUIRED never wipes tasks.
    tasks = sm.get_upcoming_tasks(days_ahead=3650)
    assert len(tmp_db.get_tasks(source="teams")) == 1
    assert isinstance(tasks, list)


def test_stale_data_is_explicitly_marked_via_source_status_not_hidden(tmp_db):
    tmp_db.record_source_status("teams", "OK", "2026-09-01T00:00:00+00:00")
    tmp_db.record_source_status("teams", "AUTH_REQUIRED", "2026-09-28T00:00:00+00:00")
    sm = StudyManager(db=tmp_db)
    rows = sm.get_source_status()
    teams = next(r for r in rows if r["source"] == "teams")
    assert teams["status"] == "AUTH_REQUIRED"
    # The last time it actually worked is preserved, not silently presented as "now".
    assert teams["last_successful_sync"] == "2026-09-01T00:00:00+00:00"


def test_get_source_status_distinguishes_session_validity_from_real_data_sync(tmp_db):
    """Live bug, 2026-09-28: Auth Checker's is_logged_in() reported
    teams -> OK a few seconds after that SAME source's real TeamsAgent
    run had already failed to fetch data ("Задания" list never loaded).
    get_dashboard/get_source_status must expose BOTH signals separately
    so a caller (AI Manager) can never say "synced just now" based only
    on the session-validity check."""
    # Auth Checker's own session check says the session is fine...
    tmp_db.record_source_status("teams", "OK", "2026-09-28T19:02:33+00:00")
    # ...but the real TeamsAgent run in that same cycle actually failed,
    # and its last genuinely successful data fetch was earlier.
    tmp_db.record_agent_run(
        "teams", "working", "2026-09-28T10:00:00+00:00", "2026-09-28T10:00:00+00:00", None
    )
    tmp_db.record_agent_run(
        "teams", "failing", "2026-09-28T19:02:51+00:00", "2026-09-28T19:02:51+00:00", "session expired"
    )

    sm = StudyManager(db=tmp_db)
    teams = next(r for r in sm.get_source_status() if r["source"] == "teams")

    assert teams["status"] == "OK"
    assert teams["last_successful_sync"] == "2026-09-28T19:02:33+00:00"
    # The two fields below are what tell the real story - session is
    # fine, but the last REAL fetch that actually worked was earlier,
    # and the most recent attempt outright failed.
    assert teams["last_data_sync"] == "2026-09-28T10:00:00+00:00"
    assert teams["last_data_status"] == "failing"


def test_get_source_status_reports_none_for_last_data_sync_when_never_successful(tmp_db):
    tmp_db.record_source_status("teams", "OK", "2026-09-28T00:00:00+00:00")
    sm = StudyManager(db=tmp_db)
    teams = next(r for r in sm.get_source_status() if r["source"] == "teams")
    assert teams["last_data_sync"] is None
    assert teams["last_data_status"] is None


# ===========================================================================
# 7. Partial failure (2 scenarios)
# ===========================================================================


@pytest.mark.asyncio
async def test_teams_failure_still_returns_sso_data(tmp_db):
    teams = _StubAgent("teams", raises=RuntimeError("boom"))
    sso = _StubAgent("sso", on_run=lambda: tmp_db.record_source_status("sso", "OK", _iso(_NOW)))
    sm = StudyManager(db=tmp_db, teams_agent=teams, sso_agent=sso)
    result = await sm.refresh()
    assert sso.call_count == 1
    assert any("teams" in e for e in result["errors"])
    assert result["sso_status"] == "OK"


@pytest.mark.asyncio
async def test_sso_failure_still_returns_teams_data(tmp_db):
    teams = _StubAgent("teams", on_run=lambda: tmp_db.record_source_status("teams", "OK", _iso(_NOW)))
    sso = _StubAgent("sso", raises=RuntimeError("boom"))
    sm = StudyManager(db=tmp_db, teams_agent=teams, sso_agent=sso)
    result = await sm.refresh()
    assert teams.call_count == 1
    assert any("sso" in e for e in result["errors"])
    assert result["teams_status"] == "OK"
    assert result["status"] == StudyManagerStatus.ERROR.value  # an internal exception did happen


# ===========================================================================
# 8. Dashboard (2 scenarios)
# ===========================================================================


def test_dashboard_combines_tasks_schedule_checker_and_source_status(tmp_db):
    tmp_db.record_source_status("teams", "OK", _iso(_NOW))
    tmp_db.record_source_status("sso", "OK", _iso(_NOW))
    with tmp_db.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, course, title, status, due_at, source, updated_at) "
            "VALUES ('t1', 'CSE4112 Тест (Лаб)', 'ЛР №3', 'upcoming', ?, 'teams', ?)",
            (_iso(_NOW + timedelta(days=1)), _iso(_NOW)),
        )
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "CSE4112", "title": "Тест"}],
        schedule_entries=[_schedule_row(1, "MONDAY_SHORT")],
        materials=[],
    )
    tmp_db.upsert_checker_finding(
        course_code="CSE4112", course_title="Тест", overall_status="MATCH",
        checked_at=_iso(_NOW), window_start="2026-09-21", window_end="2026-10-28",
        details={"activities": []},
    )
    sm = StudyManager(db=tmp_db)
    dashboard = sm.get_dashboard()
    assert dashboard["status"] == StudyManagerStatus.OK.value
    assert len(dashboard["upcoming_tasks"]) == 1
    assert len(dashboard["checker_findings"]) == 1
    assert {r["source"] for r in dashboard["source_status"]} == {"teams", "sso"}


def test_dashboard_never_returns_secret_shaped_fields(tmp_db):
    tmp_db.record_source_status("teams", "ERROR", _iso(_NOW), error="RuntimeError: session expired")
    sm = StudyManager(db=tmp_db)
    dashboard = sm.get_dashboard()
    blob = str(dashboard)
    for forbidden in ("password", "cookie", "access_token", "mfa", "iin"):
        assert forbidden not in blob.lower()


# ===========================================================================
# Extra: constructor/BaseAgent contract, refresh() sequencing, resolve helpers
# ===========================================================================


def test_extract_course_code_pulls_code_out_of_teams_display_name():
    assert extract_course_code("CSE4112 Администрирование систем и сетей (Лаб)") == "CSE4112"


# ===========================================================================
# normalize_class_type() - live bug, 2026-09-29: this crashed with
# NameError: name '_LECTURE_ABBR' is not defined for any schedule row
# whose class_type wasn't a lab/practice stem or abbreviation (i.e. most
# lecture rows) - the constant was silently dropped when this function
# was duplicated from agents/checker/logic.py's normalize_activity_type().
# It broke get_upcoming_schedule()/get_dashboard() end-to-end in
# production (a live Telegram AI Manager reply) despite 366+ passing
# tests, because nothing called this function directly with a
# non-lab/practice value before. These are the direct unit tests that
# should have caught it, plus one through expand_weekly_schedule() (what
# get_upcoming_schedule() actually calls) so a future regression here
# fails loudly again.
# ===========================================================================


def test_normalize_class_type_recognizes_a_lab_by_stem():
    assert normalize_class_type("Лабораторная работа") == "LAB"


def test_normalize_class_type_recognizes_a_lab_by_abbreviation():
    assert normalize_class_type("ЛР") == "LAB"


def test_normalize_class_type_recognizes_a_practice_by_abbreviation():
    assert normalize_class_type("ПЗ") == "PRACTICE"


def test_normalize_class_type_recognizes_a_lecture_by_stem_and_does_not_crash():
    # This is the exact shape that crashed in production - a value that
    # is NOT a lab/practice stem or abbreviation, forcing evaluation of
    # the (previously undefined) _LECTURE_ABBR branch.
    assert normalize_class_type("Лекция") == "LECTURE"


def test_normalize_class_type_falls_back_to_other_without_crashing():
    assert normalize_class_type("Семинар") == "OTHER"
    assert normalize_class_type(None) == "OTHER"
    assert normalize_class_type("") == "OTHER"


def test_expand_weekly_schedule_does_not_crash_on_a_lecture_row():
    """End-to-end through the actual function get_upcoming_schedule()
    calls (agents/study_manager/agent.py) - reproduces the live
    NameError from a real schedule row shape, not just the pure
    normalize_class_type() unit above."""
    rows = [{
        "id": 1,
        "course_code": "CSE4112",
        "course_title": "Тест",
        "class_type": "Лекция",
        "day_title": "MONDAY_SHORT",
        "start_time": "09:00",
        "end_time": "10:20",
        "instructor_name": "Иванов И.И.",
        "room_title": "101",
    }]
    occurrences = expand_weekly_schedule(rows, days_ahead=7, now=_NOW)
    assert occurrences  # at least one Monday in the window
    assert all(o["class_type"] == "LECTURE" for o in occurrences)


def test_normalize_course_code_and_title_are_case_and_punctuation_insensitive():
    assert normalize_course_code("cse-4112") == normalize_course_code("CSE 4112") == "CSE4112"
    assert normalize_title("  Тест   курс ") == normalize_title("тест курс")


def test_course_matches_by_code_first_then_title_fallback():
    course = {"code": "CSE4112", "title": "Администрирование систем и сетей"}
    task = task_view(_task(course="CSE4112 Администрирование систем и сетей (Лаб)"))
    assert course_matches(course, task) is True

    course_no_code = {"code": None, "title": "Администрирование систем и сетей"}
    task_no_code = task_view(_task(course="Администрирование систем и сетей (Лекция)"))
    assert course_matches(course_no_code, task_no_code) is True


@pytest.mark.asyncio
async def test_refresh_runs_auth_checker_before_and_after_source_agents(tmp_db):
    call_order: list[str] = []
    auth_checker = _StubAgent("auth_checker", on_run=lambda: call_order.append("auth_checker"))
    teams = _StubAgent("teams", on_run=lambda: call_order.append("teams"))
    sso = _StubAgent("sso", on_run=lambda: call_order.append("sso"))
    sm = StudyManager(db=tmp_db, teams_agent=teams, sso_agent=sso, auth_checker_agent=auth_checker)
    await sm.refresh()
    assert auth_checker.call_count == 2
    assert call_order[0] == "auth_checker"
    assert call_order[-1] == "auth_checker"
    assert "teams" in call_order and "sso" in call_order


@pytest.mark.asyncio
async def test_refresh_skips_a_source_whose_last_known_status_is_unavailable(tmp_db):
    tmp_db.record_source_status("teams", "UNAVAILABLE", _iso(_NOW))
    teams = _StubAgent("teams")
    sm = StudyManager(db=tmp_db, teams_agent=teams)
    await sm.refresh()
    assert teams.call_count == 0


@pytest.mark.asyncio
async def test_run_reports_working_status_when_refresh_succeeds_ok(tmp_db):
    auth_checker = _StubAgent(
        "auth_checker",
        on_run=lambda: (
            tmp_db.record_source_status("teams", "OK", _iso(_NOW)),
            tmp_db.record_source_status("sso", "OK", _iso(_NOW)),
        ),
    )
    sm = StudyManager(db=tmp_db, auth_checker_agent=auth_checker)
    result = await sm.run()
    assert result.status == AgentStatus.WORKING
    assert result.data["status"] == StudyManagerStatus.OK.value


def test_get_course_materials_returns_metadata_only_never_binary_content(tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "CSE4112", "title": "Тест"}],
        schedule_entries=[],
        materials=[{"file_id": 1, "folder_id": 10, "file_name": "Лекция 1.pdf", "course_title": "Тест"}],
    )
    sm = StudyManager(db=tmp_db)
    result = sm.get_course_materials("CSE4112")
    assert result["match"] == "MATCHED"
    assert result["materials"][0]["title"] == "Лекция 1.pdf"
    assert "content" not in result["materials"][0]
    assert "bytes" not in result["materials"][0]
