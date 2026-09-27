from __future__ import annotations

from datetime import date

import pytest

from utils.academic_calendar import (
    FIRST_ATTESTATION_WEEK,
    TOTAL_TEACHING_WEEKS,
    current_teaching_week,
    teaching_week_bounds,
)

# 2026-09-01 is a Tuesday - the real anchor Desae confirmed directly:
# week 1 = Tue 2026-09-01 .. Sun 2026-09-06 (short, 6 days), week 2
# starts Mon 2026-09-07 and every week after aligns to real Mon-Sun
# calendar weeks.
WEEK1_START = date(2026, 9, 1)


def test_before_semester_start_is_none():
    assert current_teaching_week(date(2026, 8, 31), WEEK1_START) is None


def test_week1_start_day_itself():
    assert current_teaching_week(date(2026, 9, 1), WEEK1_START) == 1


def test_week1_runs_through_the_sunday_before_the_next_monday():
    assert current_teaching_week(date(2026, 9, 6), WEEK1_START) == 1


def test_week2_starts_on_the_following_monday():
    assert current_teaching_week(date(2026, 9, 7), WEEK1_START) == 2


def test_week2_runs_a_full_calendar_week():
    assert current_teaching_week(date(2026, 9, 13), WEEK1_START) == 2
    assert current_teaching_week(date(2026, 9, 14), WEEK1_START) == 3


def test_first_attestation_week_is_8():
    # week 8 starts Mon 2026-09-07 + 6*7 days = 2026-10-19
    assert FIRST_ATTESTATION_WEEK == 8
    assert current_teaching_week(date(2026, 10, 19), WEEK1_START) == 8
    assert current_teaching_week(date(2026, 10, 25), WEEK1_START) == 8


def test_week15_is_the_last_teaching_week():
    start, end = teaching_week_bounds(TOTAL_TEACHING_WEEKS, WEEK1_START)
    assert current_teaching_week(start, WEEK1_START) == TOTAL_TEACHING_WEEKS
    assert current_teaching_week(end, WEEK1_START) == TOTAL_TEACHING_WEEKS


def test_after_week15_is_none():
    _, week15_end = teaching_week_bounds(TOTAL_TEACHING_WEEKS, WEEK1_START)
    day_after = date.fromordinal(week15_end.toordinal() + 1)
    assert current_teaching_week(day_after, WEEK1_START) is None


def test_teaching_week_bounds_week1_is_short():
    start, end = teaching_week_bounds(1, WEEK1_START)
    assert start == date(2026, 9, 1)
    assert end == date(2026, 9, 6)


def test_teaching_week_bounds_week2_is_a_full_week():
    start, end = teaching_week_bounds(2, WEEK1_START)
    assert start == date(2026, 9, 7)
    assert end == date(2026, 9, 13)


def test_teaching_week_bounds_rejects_out_of_range():
    with pytest.raises(ValueError):
        teaching_week_bounds(0, WEEK1_START)
    with pytest.raises(ValueError):
        teaching_week_bounds(16, WEEK1_START)


def test_week1_start_on_a_monday_gives_a_full_first_week():
    # Defensive case: if a future semester happens to start on a Monday,
    # week 1 should be a normal full 7-day week, not accidentally
    # collapse to 1 day.
    monday_start = date(2027, 8, 30)  # a Monday
    start, end = teaching_week_bounds(1, monday_start)
    assert start == monday_start
    assert end == date(2027, 9, 5)
    assert current_teaching_week(date(2027, 9, 5), monday_start) == 1
    assert current_teaching_week(date(2027, 9, 6), monday_start) == 2
