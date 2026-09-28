"""Study Manager Agent (bro's ТЗ, "Техническое задание — Study Manager
Agent для Personal AI Manager", 36 sections) - pure orchestration logic.

Everything here is a pure function of already-normalized rows this
project's other agents already wrote to SQLite (tasks/sso_courses/
sso_schedule_entries/sso_study_materials/checker_findings/source_status)
- nothing in this module touches the database, the network, a browser,
Telegram, or any LLM SDK (spec §23/§24/§27). See agents/study_manager/
agent.py for the thin BaseAgent wrapper that reads real rows, calls the
other agents' public run() methods for a sync, and calls into this
module for every read.

DELIBERATE DUPLICATION, NOT AN OVERSIGHT: normalize_course_code(),
normalize_title(), extract_course_code() and extract_teams_course_title()
below are near-identical to the ones in agents/checker/logic.py. Spec
§4/§31 repeatedly says Study Manager may only use Teams/SSO/Checker/
Auth Checker via their PUBLIC agent interfaces, naming Checker
explicitly alongside the source agents as something not to reach
"inside" of. Read strictly, that bars importing from
agents.checker.logic too - it is Checker's own internal module, not
part of CheckerAgent's public surface (which is just
`await CheckerAgent(...).run()` writing to checker_findings). A few
small pure functions duplicated here is a much smaller cost than
quietly coupling Study Manager's behavior to Checker's internal
implementation details.

WHY THIS MODULE ALWAYS EXPECTS PLAIN dict ROWS, NEVER sqlite3.Row:
several functions below call `.get(...)` on the rows they're handed
(sqlite3.Row only supports bracket access, not `.get()` - this project
already hit that exact AttributeError once building Checker Agent, see
agents/checker/agent.py's own comment on the same pitfall). To make
that mistake structurally harder to repeat, EVERY function in this
module takes/returns plain dicts, and agents/study_manager/agent.py is
responsible for `dict(row)`-converting anything it reads from
storage/database.py before calling in here.
"""
from __future__ import annotations

import enum
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable


class StudyManagerStatus(str, enum.Enum):
    #: Every source this result depends on was OK.
    OK = "OK"
    #: At least one source was unavailable/needs auth/errored, but useful
    #: data still came from whatever WAS available (spec §8/§19) - the
    #: normal, expected "something's down but you're not blind" state.
    PARTIAL = "PARTIAL"
    #: A source this result critically needs is AUTH_REQUIRED on both
    #: sides (see combine_source_status() below) - a human needs to log
    #: back in before anything useful can come out of a refresh.
    AUTH_REQUIRED = "AUTH_REQUIRED"
    #: Both sources are down and neither is simply AUTH_REQUIRED (e.g.
    #: UNAVAILABLE/UNKNOWN) - nothing usable right now, but not
    #: necessarily anyone's fault (no session wired up, a first-ever run).
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    #: An internal processing exception happened during refresh() itself
    #: - reserved for that, never used for an ordinary "source is down"
    #: (spec §19: "не сообщать OK если нужный для результата источник
    #: недоступен" cuts the other way too - a merely-down source is
    #: PARTIAL/AUTH_REQUIRED/SOURCE_UNAVAILABLE, not ERROR).
    ERROR = "ERROR"


class CourseMatchResult(str, enum.Enum):
    """resolve_course()'s explicit "don't guess" signal (spec §16: "при
    неоднозначном совпадении вернуть отсутствие однозначного курса, а не
    случайно выбрать один"). Never raised as an exception - callers check
    `isinstance(result, CourseMatchResult)` before treating a result as a
    real course row."""

    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"


# Real Teams status vocabulary (agents/teams/parser.py's compute_status):
# returned/submitted/completed all mean "this task is done"; overdue/
# upcoming both mean "not done yet". Deliberately NOT trusted directly
# for "is this overdue right now" (see is_task_overdue() below) - Teams'
# own "overdue" string is a freshness judgement made AT THE LAST SYNC,
# which can go stale the moment a source is AUTH_REQUIRED for a while
# (spec §15: recompute at query time from the structured due_at field
# instead of trusting a possibly-stale stored status string).
_DONE_STATUSES = {"returned", "submitted", "completed"}

# Same real day_title values as agents/checker/logic.py's own copy
# (confirmed by agents/sso/parser.py's docstring, reused verbatim by
# telegram_bot/handlers.py's _SSO_WEEKDAY_LABELS) - Monday=0 matches
# Python's date.weekday(). Duplicated here for the same reason as the
# rest of this module (see the file docstring).
DAY_TITLE_TO_WEEKDAY: dict[str, int] = {
    "MONDAY_SHORT": 0,
    "TUESDAY_SHORT": 1,
    "WEDNESDAY_SHORT": 2,
    "THURSDAY_SHORT": 3,
    "FRIDAY_SHORT": 4,
    "SATURDAY_SHORT": 5,
    "SUNDAY_SHORT": 6,
}

_LAB_STEMS = ("лаборатор", "lab")
_LAB_ABBR = ("лаб", "лр")
_PRACTICE_STEMS = ("практи",)
_PRACTICE_ABBR = ("пз", "practice")
_LECTURE_STEMS = ("лекци", "lecture")


def _contains_any_stem(text: str, stems: tuple[str, ...]) -> bool:
    return any(stem in text for stem in stems)


def _contains_any_abbr(text: str, abbrs: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(abbr)}\b", text) for abbr in abbrs)


def normalize_class_type(raw: Any) -> str:
    """Best-effort LECTURE/PRACTICE/LAB/OTHER label for a schedule row's
    class_type - same word list and same "never guess, fall back to
    OTHER" discipline as agents/checker/logic.py's
    normalize_activity_type() (there is still no confirmed real capture
    of class_type's actual shape in this project - see that module's
    docstring). Used only for get_upcoming_schedule()'s display label;
    Checker's own findings (already computed, already stored in
    checker_findings) are read as-is via get_checker_findings(), never
    recomputed here."""
    if raw is None:
        return "OTHER"
    text = str(raw).strip().lower()
    if not text:
        return "OTHER"
    if _contains_any_stem(text, _LAB_STEMS) or _contains_any_abbr(text, _LAB_ABBR):
        return "LAB"
    if _contains_any_stem(text, _PRACTICE_STEMS) or _contains_any_abbr(text, _PRACTICE_ABBR):
        return "PRACTICE"
    if _contains_any_stem(text, _LECTURE_STEMS) or _contains_any_abbr(text, _LECTURE_ABBR):
        return "LECTURE"
    return "OTHER"


# -- course code / title normalization (duplicated from agents/checker/logic.py - see file docstring) --

_COURSE_CODE_PATTERN = re.compile(r"\b([A-Za-zА-Яа-яЁё]{2,6}\d{3,5})\b")
_TRAILING_PAREN_PATTERN = re.compile(r"\s*\([^)]*\)\s*$")


def normalize_course_code(code: Any) -> str | None:
    if not code:
        return None
    cleaned = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", str(code)).upper()
    return cleaned or None


def normalize_title(title: Any) -> str | None:
    if not title:
        return None
    cleaned = " ".join(str(title).strip().split())
    return cleaned.casefold() or None


def extract_course_code(text: Any) -> str | None:
    if not text:
        return None
    match = _COURSE_CODE_PATTERN.search(str(text))
    if not match:
        return None
    return normalize_course_code(match.group(1))


def extract_teams_course_title(text: Any) -> str | None:
    if not text:
        return None
    text = str(text)
    match = _COURSE_CODE_PATTERN.match(text.strip())
    if match:
        text = text[match.end():]
    text = _TRAILING_PAREN_PATTERN.sub("", text)
    return normalize_title(text)


def course_matches(course: dict, task: dict) -> bool:
    """True if ``task`` (a Study Manager task view - see task_view()
    below) identifies the same course as ``course`` (an sso_courses
    row) - code first, exact normalized title fallback. Same matching
    rule as agents/checker/logic.py's course_matches_task(), duplicated
    per this module's docstring."""
    course_code = normalize_course_code(course.get("code"))
    task_code = task.get("course_code") or extract_course_code(task.get("course"))
    if course_code and task_code and course_code == task_code:
        return True
    course_title = normalize_title(course.get("title"))
    task_title = extract_teams_course_title(task.get("course"))
    if course_title and task_title and course_title == task_title:
        return True
    return False


def resolve_course(courses: list[dict], identifier: str | None):
    """spec §16/§17: resolves ``identifier`` (a course code, OR - the
    documented fallback - a course title) against ``courses`` (already
    dict()'d sso_courses rows). Code match is tried first; title is
    only tried as a fallback when the code lookup finds nothing at all.
    An ambiguous result at EITHER stage returns CourseMatchResult.
    AMBIGUOUS immediately rather than falling through to try the other
    stage - falling through would just risk picking a *different*
    guessed winner, still a guess. No fuzzy matching - see this
    project's Checker Agent for the same explicit rule (spec §6.4),
    reused here by policy, not by import (this module's docstring)."""
    if not identifier:
        return CourseMatchResult.NOT_FOUND

    norm_code = normalize_course_code(identifier)
    if norm_code:
        matches = [c for c in courses if normalize_course_code(c.get("code")) == norm_code]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return CourseMatchResult.AMBIGUOUS

    norm_title = normalize_title(identifier)
    if norm_title:
        matches = [c for c in courses if normalize_title(c.get("title")) == norm_title]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return CourseMatchResult.AMBIGUOUS

    return CourseMatchResult.NOT_FOUND


def course_tasks_for(
    identifier: str, courses: list[dict], tasks: list[dict]
) -> tuple[Any, list[dict]]:
    """spec §16's get_course_tasks(course_code) core: resolves
    ``identifier`` to one SSO course (see resolve_course()) and returns
    (course_or_CourseMatchResult, matching_task_views). An unresolved
    identifier (NOT_FOUND/AMBIGUOUS) returns that sentinel with an empty
    task list - callers must not fall back to guessing which tasks
    belong to it."""
    course = resolve_course(courses, identifier)
    if isinstance(course, CourseMatchResult):
        return course, []
    return course, [t for t in tasks if course_matches(course, t)]


# -- tasks (spec §9/§14/§15) -------------------------------------------------

def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_task_completed(task: dict) -> bool:
    """A task counts as done if Teams recorded a real submission
    timestamp OR its last-synced status string is one of the terminal
    "done" values (spec §9's `submitted_at`/`status` fields) - this is
    the one piece of Teams' own status this module DOES trust, since
    "was this ever submitted" doesn't go stale the way "is this overdue
    right now" does (see is_task_overdue() below)."""
    if task.get("submitted_at"):
        return True
    status = (task.get("status") or "").strip().lower()
    return status in _DONE_STATUSES


def is_task_overdue(task: dict, *, now: datetime | None = None) -> bool:
    """spec §15: overdue = due_date < now AND no confirmation of a
    successful submission - recomputed HERE at query time from the
    structured due_date, never taken from Teams' own possibly-stale
    `status` string (that string was computed once, at the last
    successful Teams sync - if Teams has been AUTH_REQUIRED for three
    days, a task that was "upcoming" at last sync may be overdue NOW,
    and this module must not keep reporting the stale answer)."""
    if is_task_completed(task):
        return False
    due = parse_iso(task.get("due_date"))
    if due is None:
        # spec §14: a task with no due date must never break processing
        # - and with nothing to compare against "now", it simply isn't
        # classified as overdue (it's also never "upcoming" for the
        # same reason - see filter_upcoming_tasks() below).
        return False
    now = now or datetime.now(timezone.utc)
    return due < now


def task_view(task: dict) -> dict:
    """spec §9's per-task field set, built from an already-normalized
    ``tasks`` row (never re-parses raw Teams API responses - this only
    ever sees what agents/teams/parser.py already wrote to SQLite)."""
    course_text = task.get("course")
    return {
        "id": task.get("id"),
        "course": course_text,
        "course_code": extract_course_code(course_text),
        "title": task.get("title"),
        "description": task.get("description"),
        "due_date": task.get("due_at"),
        "status": task.get("status"),
        "is_completed": is_task_completed(task),
        "submitted_at": task.get("submitted_at"),
        "source": task.get("source"),
    }


def filter_upcoming_tasks(
    task_views: list[dict], *, days_ahead: int = 7, now: datetime | None = None
) -> list[dict]:
    """spec §14: not completed, has a due date inside [now, now+days_ahead]
    - an overdue task is NEVER counted as upcoming (that's
    filter_overdue_tasks()'s job, spec §14 explicitly forbids double-
    counting one way; a task the caller explicitly wants as "overdue"
    must still be visible via that other function, spec §14's other
    explicit rule - this function simply never claims to be that list).
    A task with no due date is excluded from this date-bounded view
    (not silently treated as "due now") rather than raising."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=days_ahead)
    result = []
    for view in task_views:
        if view["is_completed"]:
            continue
        due = parse_iso(view["due_date"])
        if due is None:
            continue
        if due < now or due > horizon:
            continue
        result.append(view)
    result.sort(key=lambda v: v["due_date"])
    return result


def filter_overdue_tasks(task_views: list[dict], *, now: datetime | None = None) -> list[dict]:
    """spec §15: every not-completed task whose due_date is in the past
    - always available regardless of any "upcoming" time window a
    caller might separately be using (spec §14's "must not hide overdue
    tasks if the caller explicitly asked for overdue tasks")."""
    now = now or datetime.now(timezone.utc)
    result = [v for v in task_views if is_task_overdue(v, now=now)]
    result.sort(key=lambda v: v["due_date"])
    return result


def filter_tasks_by_course(task_views: list[dict], course_code: str) -> list[dict]:
    """A simple course_code-equality filter over already-built task
    views, for callers that just want "this course's tasks from the
    list I already have" without going through the full
    resolve_course()/course_tasks_for() SSO-matching path (spec §14's
    "filtering must consider ... course")."""
    norm = normalize_course_code(course_code)
    if not norm:
        return []
    return [v for v in task_views if v["course_code"] == norm]


# -- schedule (spec §10/§13) -------------------------------------------------

def expand_weekly_schedule(
    schedule_rows: list[dict], *, days_ahead: int = 7, now: datetime | None = None
) -> list[dict]:
    """spec §13: expands weekly-template sso_schedule_entries rows
    (day_title + time, no date of their own - same weekly-recurring
    shape agents/checker/logic.py's expand_occurrences() already
    established for this project's real DB schema) into one dict per
    concrete calendar date from today through today+days_ahead
    inclusive. Each (schedule_entry_id, date) pair appears at most once
    by construction (each row is walked across the window exactly once
    per matching weekday), so duplicates are structurally impossible -
    spec §13's explicit "must not create duplicate occurrences"."""
    now = now or datetime.now(timezone.utc)
    start = now.date()
    end = start + timedelta(days=days_ahead)
    occurrences: list[dict] = []
    if end < start:
        return occurrences
    for row in schedule_rows:
        weekday = DAY_TITLE_TO_WEEKDAY.get(row.get("day_title"))
        if weekday is None:
            # Unrecognized/missing day_title - nothing to expand against,
            # same "don't guess" discipline as the rest of this project.
            continue
        current = start
        while current <= end:
            if current.weekday() == weekday:
                occurrences.append({
                    "schedule_entry_id": row.get("id"),
                    "course_code": row.get("course_code"),
                    "course_title": row.get("course_title"),
                    "class_type": normalize_class_type(row.get("class_type")),
                    "date": current.isoformat(),
                    "start_time": row.get("start_time"),
                    "end_time": row.get("end_time"),
                    "instructor": row.get("instructor_name"),
                    "room": row.get("room_title"),
                })
            current += timedelta(days=1)
    # spec §13's "correct sort by date/time" - date first, then start_time
    # (missing start_time sorts first within a day rather than crashing
    # on a None/str comparison), then schedule_entry_id as a stable
    # deterministic tiebreaker.
    occurrences.sort(key=lambda o: (o["date"], o["start_time"] or "", o["schedule_entry_id"] or 0))
    return occurrences


# -- top-level status (spec §19) --------------------------------------------

# source_status.status values that mean "this source's data cannot be
# used at all right now" (mirrors agents/auth_checker.py's SourceStatus
# - AUTH_REQUIRED is broken out separately below since it gets its own
# StudyManagerStatus value when BOTH sources are AUTH_REQUIRED).
_NON_OK_STATUSES = {"AUTH_REQUIRED", "ERROR", "UNAVAILABLE", "UNKNOWN", None}


def combine_source_status(teams_status: str | None, sso_status: str | None) -> str:
    """spec §19's top-level OK/PARTIAL/AUTH_REQUIRED/SOURCE_UNAVAILABLE
    rule, from the two sources' raw source_status.status strings
    (``None`` means Auth Checker has never checked that source at all -
    treated the same as "not usable right now", same reasoning as
    agents/checker/agent.py's own _blocking_status()). ERROR (a genuine
    internal Study Manager exception) is deliberately never returned
    from here - only refresh() itself decides that, from its own
    try/except bookkeeping, per spec §19's "ERROR reserved for internal
    processing errors", not for an ordinary down source.
    """
    teams_ok = teams_status == "OK"
    sso_ok = sso_status == "OK"

    if teams_ok and sso_ok:
        return StudyManagerStatus.OK.value

    if not teams_ok and not sso_ok:
        # Neither source usable at all - if BOTH are specifically
        # AUTH_REQUIRED, surface that prominently (a human action -
        # logging back in - is exactly what would fix this); otherwise
        # nothing usable but no single clear human fix -> SOURCE_UNAVAILABLE.
        if teams_status == "AUTH_REQUIRED" and sso_status == "AUTH_REQUIRED":
            return StudyManagerStatus.AUTH_REQUIRED.value
        return StudyManagerStatus.SOURCE_UNAVAILABLE.value

    # Exactly one of the two is OK - useful data is available from that
    # one source (spec §8's own worked example: Teams=OK + SSO=ERROR ->
    # Study Manager status = PARTIAL), never OK (spec §19: never report
    # OK when a source needed for the full result is unavailable).
    return StudyManagerStatus.PARTIAL.value


# ===========================================================================
# Study Overrides (AI Manager ТЗ v3 §12-23) - a user-owned LOCAL correction
# layer, kept entirely separate from Teams/SSO's read-only source data (see
# storage/models.py's study_overrides table comment). Nothing below ever
# writes anywhere - these are pure validation/merge functions; agents/
# study_manager/agent.py's set_task_override()/clear_override()/
# get_effective_task() are the only things that touch storage/database.py's
# study_overrides table.
# ===========================================================================


class OverrideValidationError(ValueError):
    """spec §16's "LLM must not be able to pass an arbitrary field and
    change it" - raised for any field not on the exact allowlist below,
    or a value that doesn't fit that field's type. A caller (AI Manager
    tool layer, StudyManager itself) must treat this as a normal,
    expected rejection - never let it propagate as an unhandled crash."""


ALLOWED_TARGET_TYPES = frozenset({"task", "activity", "course"})

#: spec §14's exact, closed field list - nothing else is ever accepted,
#: however it's spelled or cased. Extending this list is a deliberate
#: code change, never a runtime decision.
ALLOWED_OVERRIDE_FIELDS = frozenset({
    "academic_week",
    "activity_number",
    "is_current",
    "local_due_date",
    "local_completion_status",
    "user_note",
})

_COMPLETION_STATUSES = frozenset({"UNKNOWN", "IN_PROGRESS", "COMPLETED"})


def validate_target_type(target_type: Any) -> str:
    if target_type not in ALLOWED_TARGET_TYPES:
        raise OverrideValidationError(
            f"unknown target_type {target_type!r} - must be one of {sorted(ALLOWED_TARGET_TYPES)}"
        )
    return target_type


def validate_override_field(field: Any, value: Any) -> Any:
    """spec §16's allowlist gate, run BEFORE any override ever reaches
    storage/database.py's upsert_override(). Returns the value in the
    exact form it should be stored as; raises OverrideValidationError
    (never a bare crash) for an unknown field or a value that doesn't
    fit that field's type - an int-shaped field never silently accepts
    a string, a bool-shaped field never accepts the string "true"."""
    if field == "academic_week" or field == "activity_number":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise OverrideValidationError(f"{field} must be a positive integer, got {value!r}")
        return value
    if field == "is_current":
        if not isinstance(value, bool):
            raise OverrideValidationError(f"is_current must be a boolean, got {value!r}")
        return value
    if field == "local_due_date":
        if parse_iso(value) is None:
            raise OverrideValidationError(f"local_due_date must be an ISO date/datetime string, got {value!r}")
        return str(value)
    if field == "local_completion_status":
        if value not in _COMPLETION_STATUSES:
            raise OverrideValidationError(
                f"local_completion_status must be one of {sorted(_COMPLETION_STATUSES)}, got {value!r}"
            )
        return value
    if field == "user_note":
        if not isinstance(value, str) or not value.strip():
            raise OverrideValidationError("user_note must be a non-empty string")
        return value.strip()
    raise OverrideValidationError(
        f"unknown override field {field!r} - must be one of {sorted(ALLOWED_OVERRIDE_FIELDS)}"
    )


def apply_overrides_to_task(task: dict, override_rows: list[dict]) -> dict:
    """spec §18/§19's {task_id, course_code, source, override, effective}
    shape for one task_view() dict + its active study_overrides rows
    (already filtered by the caller to target_type="task",
    target_id==this task's id - see agents/study_manager/agent.py's
    get_effective_task()). ``override_rows`` items are dicts with at
    least "field"/"value" keys (agent.py's _override_view()).

    Precedence (spec §19): an active override always wins for the
    field it covers; with no override at all for a field, effective ==
    source for it. spec §50's distinction is preserved explicitly:
    local_completion_status NEVER overwrites/erases Teams' own
    ``status``/``is_completed`` in ``source`` - it only changes what
    ``effective`` says, so a caller can still honestly say "you marked
    it done, but Teams hasn't registered a submission" (see this
    project's README for the exact wording bro's spec requires).
    """
    by_field = {row["field"]: row["value"] for row in override_rows}

    source = {
        "due_date": task.get("due_date"),
        "status": task.get("status"),
        "is_completed": task.get("is_completed"),
    }
    effective: dict[str, Any] = {
        "due_date": source["due_date"],
        "completion_status": "COMPLETED" if source["is_completed"] else "NOT_SUBMITTED",
    }
    override_out: dict[str, Any] = {}

    if "local_due_date" in by_field:
        override_out["local_due_date"] = by_field["local_due_date"]
        effective["due_date"] = by_field["local_due_date"]

    if "local_completion_status" in by_field:
        override_out["local_completion_status"] = by_field["local_completion_status"]
        effective["completion_status"] = by_field["local_completion_status"]

    # Passed straight through into `effective` when present - these three
    # have no equivalent field in Teams' own source data at all (Teams
    # has no concept of "academic week"/"activity number"/"is this the
    # one we're currently working on" - see spec §12), so there is
    # nothing for them to conflict with in `source`.
    for passthrough_field in ("academic_week", "activity_number", "is_current", "user_note"):
        if passthrough_field in by_field:
            override_out[passthrough_field] = by_field[passthrough_field]
            effective[passthrough_field] = by_field[passthrough_field]

    return {
        "task_id": task.get("id"),
        "course_code": task.get("course_code"),
        "source": source,
        "override": override_out,
        "effective": effective,
    }
