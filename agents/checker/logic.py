"""Checker Agent's pure analytical logic (bro's ТЗ, "обновлённая логика
сверки" spec, 2026-09-28) - normalization, course/activity matching, and
the actual MATCH/PARTIAL_MATCH/NO_MATCH sweep.

Everything here is a pure function of already-normalized SSO/Teams rows
(storage/models.py's sso_courses/sso_schedule_entries + tasks) - nothing
in this module touches the database, the network, or a browser. See
agents/checker/agent.py for the thin BaseAgent wrapper that reads real
rows and calls into this module.

TWO HONEST LIMITATIONS baked into this module's design, both because the
spec's examples assume data this project's real, already-captured shape
does not actually carry:

1. storage/models.py's sso_schedule_entries has NO absolute calendar
   date - only day_title ("MONDAY_SHORT" etc., a real value confirmed by
   agents/sso/parser.py's docstring) + start_time/end_time. It is a
   WEEKLY RECURRING template (one Monday-13:00-LAB row represents every
   Monday of the semester), not a list of one-off dated events. The
   spec's own examples ("15.09 → LAB", schedule_entry_id + date both
   present in its JSON shape) only make sense once a recurring template
   row is expanded into concrete calendar-date OCCURRENCES - so that is
   exactly what expand_occurrences() below does, and every downstream
   "activity" in this module's output is really one (schedule_entry_id,
   concrete date) occurrence, not the raw template row itself.
2. There is no confirmed real capture of ScheduleEntry.class_type's
   actual shape (an int per one test fixture, a string per the spec's
   own "Лабораторная"/"Практика" examples - see agents/sso/parser.py's
   docstring, which never says which). normalize_activity_type() below
   handles either: it stringifies whatever it's given and matches
   Russian/English stems/abbreviations - a bare integer with no known
   mapping normalizes to OTHER (never a guessed LAB/PRACTICE/LECTURE),
   which is the same "don't invent data" discipline the rest of this
   project already follows for genuinely unconfirmed API shapes.

Nothing here ever asserts a student definitely missed an assignment -
see NO_MATCH's docstring and this project's Checker spec §5/§22: an
activity this module can't find a Teams match for is reported as
"no confirmed match found in the data available", not "confirmed
missing".
"""
from __future__ import annotations

import enum
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable


class ActivityType(str, enum.Enum):
    LECTURE = "LECTURE"
    PRACTICE = "PRACTICE"
    LAB = "LAB"
    #: Used both when class_type couldn't be classified at all AND when
    #: nothing in the matched text hints at a specific type - never a
    #: guess, see this module's docstring.
    OTHER = "OTHER"


class ActivityStatus(str, enum.Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    #: The activity's own type couldn't be determined (OTHER) - spec §21:
    #: this must never be reported as a false MATCH, and must never be
    #: reported as NO_MATCH either (that would falsely imply "confirmed
    #: missing" for a class type this module doesn't even understand).
    UNKNOWN = "UNKNOWN"


class CheckerStatus(str, enum.Enum):
    MATCH = "MATCH"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    NO_MATCH = "NO_MATCH"
    UNMATCHED_COURSE = "UNMATCHED_COURSE"
    #: The four below mirror agents/auth_checker.py's SourceStatus, but
    #: spelled exactly the way bro's Checker spec §11 names them
    #: (SOURCE_UNAVAILABLE, not Auth Checker's own UNAVAILABLE) - see
    #: agent.py's _map_source_status() for the translation between the
    #: two vocabularies.
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    ERROR = "ERROR"


# Real day_title values, confirmed by agents/sso/parser.py's docstring
# and reused verbatim by telegram_bot/handlers.py's own
# _SSO_WEEKDAY_LABELS - Monday=0 matches Python's date.weekday().
DAY_TITLE_TO_WEEKDAY: dict[str, int] = {
    "MONDAY_SHORT": 0,
    "TUESDAY_SHORT": 1,
    "WEDNESDAY_SHORT": 2,
    "THURSDAY_SHORT": 3,
    "FRIDAY_SHORT": 4,
    "SATURDAY_SHORT": 5,
    "SUNDAY_SHORT": 6,
}

# -- §4/§6.1: activity-type normalization -----------------------------------

# Stems match anywhere in the (lowercased) text - long enough that a
# false hit inside an unrelated word is not a realistic risk in this
# narrow academic-schedule/assignment-title domain. Abbreviations are
# short enough that they DO need a word boundary (checked via
# \b<abbr>\b below) so "лаб" doesn't fire on some unrelated word that
# merely contains those three letters.
_LAB_STEMS = ("лаборатор", "lab")
_LAB_ABBR = ("лаб", "лр")
_PRACTICE_STEMS = ("практи",)
_PRACTICE_ABBR = ("пз", "practice")
_LECTURE_STEMS = ("лекци", "lecture")
_LECTURE_ABBR = ()


def _contains_any_stem(text: str, stems: tuple[str, ...]) -> bool:
    return any(stem in text for stem in stems)


def _contains_any_abbr(text: str, abbrs: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(abbr)}\b", text) for abbr in abbrs)


def normalize_activity_type(raw: Any) -> str:
    """Normalizes an SSO ScheduleEntry.class_type value OR any free text
    (a Teams assignment's course/title/description) into one of
    LECTURE/PRACTICE/LAB/OTHER (spec §4/§6.1 share the same word list,
    so this one function serves both callers - agent.py uses it on
    class_type, task_activity_type() below uses it on Teams text).

    Checked in LAB -> PRACTICE -> LECTURE order (arbitrary but fixed,
    since a real title is very unlikely to contain more than one of
    these word groups). Anything that matches none of them - including
    None, an empty string, or a bare number this project has no
    confirmed class_type mapping for - is OTHER, never a guess (see
    this module's docstring's honest limitation #2).
    """
    if raw is None:
        return ActivityType.OTHER.value
    text = str(raw).strip().lower()
    if not text:
        return ActivityType.OTHER.value
    if _contains_any_stem(text, _LAB_STEMS) or _contains_any_abbr(text, _LAB_ABBR):
        return ActivityType.LAB.value
    if _contains_any_stem(text, _PRACTICE_STEMS) or _contains_any_abbr(text, _PRACTICE_ABBR):
        return ActivityType.PRACTICE.value
    if _contains_any_stem(text, _LECTURE_STEMS) or _contains_any_abbr(text, _LECTURE_ABBR):
        return ActivityType.LECTURE.value
    return ActivityType.OTHER.value


def task_activity_type(task: dict) -> str:
    """Spec §6.1's "explicit type in name/description" step - but real
    captured Teams data (agents/teams/parser.py's COURSE_NAMES) shows
    the type hint often lives in the *resolved course name* instead
    ("CSE4112 Администрирование систем и сетей (Лаб)"), not the
    assignment's own title/description - so this scans all three
    fields together rather than just title+description as the spec's
    prose literally says, to actually work against real data."""
    combined = " ".join(
        part for part in (task.get("course"), task.get("title"), task.get("description")) if part
    )
    return normalize_activity_type(combined)


# -- §6.3/§6.4: course code / title normalization + matching ---------------

_COURSE_CODE_PATTERN = re.compile(r"\b([A-Za-zА-Яа-яЁё]{2,6}\d{3,5})\b")
_TRAILING_PAREN_PATTERN = re.compile(r"\s*\([^)]*\)\s*$")


def normalize_course_code(code: Any) -> str | None:
    """'CSE4112' / 'cse-4112' / 'CSE 4112' all normalize to the same
    'CSE4112' (spec §6.3) - strips everything but letters/digits and
    uppercases. None for a missing/empty code, never an empty string,
    so callers can tell "no code" apart from a real one."""
    if not code:
        return None
    cleaned = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", str(code)).upper()
    return cleaned or None


def normalize_title(title: Any) -> str | None:
    """Case-insensitive, whitespace-collapsed exact-title comparison key
    (spec §6.4 - no fuzzy matching on the first pass, by explicit
    instruction)."""
    if not title:
        return None
    cleaned = " ".join(str(title).strip().split())
    return cleaned.casefold() or None


def extract_course_code(text: Any) -> str | None:
    """Best-effort course-code extraction from free text (a Teams
    assignment's resolved `course` display name) - e.g. pulls "CSE4112"
    out of "CSE4112 Администрирование систем и сетей (Лаб)". None if no
    code-shaped token is found (spec §6.4's fallback then applies)."""
    if not text:
        return None
    match = _COURSE_CODE_PATTERN.search(str(text))
    if not match:
        return None
    return normalize_course_code(match.group(1))


def extract_teams_course_title(text: Any) -> str | None:
    """Strips a leading course-code token and a trailing "(Лаб)"/
    "(Лекция)"-style parenthetical (both real, observed shapes in
    agents/teams/parser.py's COURSE_NAMES) off Teams' resolved `course`
    field, leaving just the title for spec §6.4's exact normalized-title
    fallback comparison against SSO's own course.title."""
    if not text:
        return None
    text = str(text)
    match = _COURSE_CODE_PATTERN.match(text.strip())
    if match:
        text = text[match.end():]
    text = _TRAILING_PAREN_PATTERN.sub("", text)
    return normalize_title(text)


def course_matches_task(course: dict, task: dict) -> bool:
    """True if ``task`` (a Teams tasks row) identifies the same course as
    ``course`` (an sso_courses row) - by code first (spec §6.3), falling
    back to an exact normalized title (spec §6.4). A course this can
    never confirm against ANY task is what makes a course
    UNMATCHED_COURSE (see evaluate_course() below) - this function is
    also, deliberately, the only place that decision is made."""
    course_code = normalize_course_code(course.get("code"))
    task_code = extract_course_code(task.get("course"))
    if course_code and task_code and course_code == task_code:
        return True
    course_title = normalize_title(course.get("title"))
    task_title = extract_teams_course_title(task.get("course"))
    if course_title and task_title and course_title == task_title:
        return True
    return False


# -- §7/§13: recurring schedule -> concrete dated occurrences --------------

def expand_occurrences(
    schedule_rows: Iterable[Any], window_start: date, window_end: date
) -> list[dict]:
    """Expands weekly-recurring sso_schedule_entries rows (day_title +
    time, no date of their own - this module's docstring's honest
    limitation #1) into one dict per concrete calendar date inside
    [window_start, window_end] that falls on that row's weekday.
    ``schedule_entry_id`` in the result is the original template row's
    own id (sqlite3.Row supports ["id"]), so a finding can always be
    traced back to it; ``date`` is the real occurrence date this
    instance stands for."""
    occurrences: list[dict] = []
    if window_end < window_start:
        return occurrences
    for row in schedule_rows:
        weekday = DAY_TITLE_TO_WEEKDAY.get(row["day_title"])
        if weekday is None:
            # An unrecognized day_title (future API change, bad data) -
            # nothing to expand against, same "don't guess" discipline
            # as everywhere else in this module.
            continue
        current = window_start
        while current <= window_end:
            if current.weekday() == weekday:
                occurrences.append({
                    "schedule_entry_id": row["id"],
                    "date": current,
                    "class_type_raw": row["class_type"],
                })
            current += timedelta(days=1)
    return occurrences


# -- §7/§16/§20: matching one occurrence against candidate Teams tasks -----

def task_due_date(task: dict) -> date | None:
    """The calendar date a Teams task's due_at falls on, or None if it
    has no due date at all - spec §17 says an assignment with no due
    date is treated as an "active" assignment (always a valid
    candidate), not silently dropped."""
    due_raw = task.get("due_at")
    if not due_raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(due_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.date()


def find_candidates(
    course_tasks: Iterable[dict],
    activity_type: str,
    window_start: date,
    window_end: date,
) -> list[tuple[dict, date | None]]:
    """Every task in ``course_tasks`` (already course-matched by the
    caller) whose inferred type equals ``activity_type`` AND whose due
    date (when it has one) falls inside [window_start, window_end] -
    spec §16/§20: a task outside the window (an old assignment from a
    past semester, say) is never a candidate for ANY occurrence, however
    well its course/type otherwise line up. A task with no due date at
    all (§17) is never excluded by the window check - it's paired with
    None here and evaluate_course()/pick_nearest() rank it after any
    dated candidate rather than dropping it."""
    candidates: list[tuple[dict, date | None]] = []
    for task in course_tasks:
        if task_activity_type(task) != activity_type:
            continue
        due = task_due_date(task)
        if due is not None and not (window_start <= due <= window_end):
            continue
        candidates.append((task, due))
    return candidates


def pick_nearest(candidates: list[tuple[dict, date | None]], occurrence_date: date) -> list[Any]:
    """Picks the candidate(s) - spec §7's "ближайшее соответствующее
    задание" - closest in date to ``occurrence_date``; a task with no
    due date at all ranks behind every dated candidate (it's still a
    valid match if it's the only one available, just never preferred
    over a task that actually names a date). Ties (equal distance, or
    several undated candidates when none are dated) all come back
    together, sorted by id for a deterministic result."""

    def sort_key(item: tuple[dict, date | None]) -> tuple[int, int, Any]:
        task, due = item
        if due is None:
            return (1, 0, task["id"])
        return (0, abs((due - occurrence_date).days), task["id"])

    ranked = sorted(candidates, key=sort_key)
    if not ranked:
        return []
    best_key = sort_key(ranked[0])
    return [task["id"] for task, due in ranked if sort_key((task, due)) == best_key]


# -- §8-§14: per-course evaluation ------------------------------------------

def evaluate_course(
    schedule_rows: Iterable[Any],
    course_tasks: list[dict],
    window_start: date,
    window_end: date,
) -> tuple[list[dict], str]:
    """The actual sweep for one already course-matched course (see
    course_matches_task()/agent.py for how "matched" is decided).
    Returns (activities, overall_status) - activities is the per-
    occurrence detail spec §13 describes, overall_status is one of
    MATCH/PARTIAL_MATCH/NO_MATCH per spec §14's literal counting rule
    (an UNKNOWN activity counts the same as NO_MATCH for that rule - it
    is, after all, not a MATCH).
    """
    activities: list[dict] = []
    occurrences = expand_occurrences(schedule_rows, window_start, window_end)
    for occ in sorted(occurrences, key=lambda o: (o["date"], o["schedule_entry_id"])):
        activity_type = normalize_activity_type(occ["class_type_raw"])
        if activity_type == ActivityType.OTHER.value:
            # spec §21: an activity whose own type can't be determined
            # must never become a false MATCH, and must never become a
            # NO_MATCH claim either (that would assert "confirmed
            # missing" for a class type this module doesn't understand
            # in the first place) - UNKNOWN, not a guess either way.
            activities.append({
                "schedule_entry_id": occ["schedule_entry_id"],
                "type": activity_type,
                "date": occ["date"].isoformat(),
                "status": ActivityStatus.UNKNOWN.value,
                "matched_assignment_ids": [],
            })
            continue

        candidates = find_candidates(course_tasks, activity_type, window_start, window_end)
        if candidates:
            matched_ids = pick_nearest(candidates, occ["date"])
            status = ActivityStatus.MATCH.value
        else:
            matched_ids = []
            # spec §5/§22: NO_MATCH means "no confirmed match found in
            # the data available", not "confirmed missing assignment" -
            # callers (Study Manager) must not upgrade this into an
            # accusation on their own.
            status = ActivityStatus.NO_MATCH.value

        activities.append({
            "schedule_entry_id": occ["schedule_entry_id"],
            "type": activity_type,
            "date": occ["date"].isoformat(),
            "status": status,
            "matched_assignment_ids": matched_ids,
        })

    overall = _overall_status(activities)
    return activities, overall


def _overall_status(activities: list[dict]) -> str:
    """Spec §14's literal rule: all MATCH -> MATCH; some (but not all)
    MATCH -> PARTIAL_MATCH; none MATCH -> NO_MATCH. Only ever called
    with a non-empty ``activities`` list by evaluate_course() above -
    an empty schedule for an otherwise-matched course is the caller's
    problem to decide whether to record at all."""
    statuses = [a["status"] for a in activities]
    if all(s == ActivityStatus.MATCH.value for s in statuses):
        return CheckerStatus.MATCH.value
    if any(s == ActivityStatus.MATCH.value for s in statuses):
        return CheckerStatus.PARTIAL_MATCH.value
    return CheckerStatus.NO_MATCH.value
