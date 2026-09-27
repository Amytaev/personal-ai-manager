"""Academic teaching-week calculator (Desae's own request, 2026-09-27 -
not part of bro's ТЗ, a personal add-on). Pure date math only - no
scraping of the official academic calendar site, which is both blocked
from this environment (robots.txt + network policy) and, per Desae,
unnecessary: he confirmed the real rule directly instead.

THE RULE (as given, real/confirmed, not guessed):
- Teaching week 1 starts on the semester's fixed first day
  (2026-09-01, a Tuesday - "1 неделя обучения это 1 сентября") and runs
  through the Sunday right before the next Monday. This can be shorter
  than 7 days if the semester doesn't start on a Monday (2026: Tue-Sun,
  6 days).
- Teaching week 2 onward aligns to real Monday-Sunday calendar weeks,
  starting the first Monday on/after week 1's start ("2 неделя обучения
  это 7 сентября" - 2026-09-07 is exactly that Monday).
- 15 teaching weeks total. What happens after week 15 (exam session,
  break) is deliberately NOT modeled here - Desae wasn't sure of the
  exact session start date, and "what teaching week is it" doesn't need
  it: current_teaching_week() just returns None past week 15.
- One lab and one practice per course per teaching week, so "current
  teaching week number" directly IS "current lab/practice number" -
  used by telegram_bot/handlers.py's week() command.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

TOTAL_TEACHING_WEEKS = 15

# Kazakhstan runs a single UTC+5 zone nationwide since 2024 (covers both
# Almaty and Qyzylorda, where Desae actually is) - a fixed offset avoids
# depending on the `tzdata` package being installed (Python's zoneinfo
# needs it on Windows, where this project actually runs), at the cost of
# being wrong for the ~1-2 hours around a DST-style change - which
# Kazakhstan hasn't had since abolishing seasonal clock changes.
_KZ_UTC_OFFSET = timedelta(hours=5)

# Per Desae directly: week 8 is when the first attestation happens - by
# then, 8 labs and 8 practices (one per week) should be done per course.
FIRST_ATTESTATION_WEEK = 8


def today_in_kz() -> date:
    """"Today" as a calendar date in Kazakhstan's timezone, not the
    server's own (this runs in a UTC cloud container / on Desae's own
    Windows machine, either way not guaranteed to already be KZ time)."""
    return (datetime.now(timezone.utc) + _KZ_UTC_OFFSET).date()


def _first_monday_on_or_after(d: date) -> date:
    days_until_monday = (7 - d.weekday()) % 7  # Monday == 0
    return d + timedelta(days=days_until_monday)


def _week2_start(semester_week1_start: date) -> date:
    return _first_monday_on_or_after(semester_week1_start + timedelta(days=1))


def current_teaching_week(today: date, semester_week1_start: date) -> int | None:
    """Returns the current teaching week (1..TOTAL_TEACHING_WEEKS), or
    None if `today` is before the semester started or on/after teaching
    week 16 (exam session, break, or anything else past the 15 teaching
    weeks - not modeled here, see this module's docstring)."""
    if today < semester_week1_start:
        return None

    week2_start = _week2_start(semester_week1_start)
    if today < week2_start:
        return 1

    week = 2 + (today - week2_start).days // 7
    if week > TOTAL_TEACHING_WEEKS:
        return None
    return week


def teaching_week_bounds(week: int, semester_week1_start: date) -> tuple[date, date]:
    """Returns the (start, end) dates (both inclusive) of a given
    teaching week number - lets a caller show "неделя 5 (8-14 сент)"
    rather than just the bare number."""
    if week < 1 or week > TOTAL_TEACHING_WEEKS:
        raise ValueError(f"week must be between 1 and {TOTAL_TEACHING_WEEKS}, got {week}")

    week2_start = _week2_start(semester_week1_start)
    if week == 1:
        return semester_week1_start, week2_start - timedelta(days=1)
    start = week2_start + timedelta(days=(week - 2) * 7)
    return start, start + timedelta(days=6)
