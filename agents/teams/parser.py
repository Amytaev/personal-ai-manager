"""Normalize raw Teams Education assignment JSON into Task rows (Phase 5).

Written against REAL captured data from the actual Satbayev Teams
tenant (see README "Реальный API заданий"), not a guess at the shape -
scripts/teams_capture_assignments.py captured 239+ real network
responses from assignments.edu.cloud.microsoft/api/v1.0/edu/me/work
before this file was written.

Two honest limitations, both already documented in README:
1. assignments.edu.cloud.microsoft/api is an undocumented internal
   backend for the Teams Education web client, not the versioned
   public Graph API - it can change shape without notice. Parsing here
   is defensive (missing/None fields degrade a row, they don't crash
   the agent) for exactly that reason.
2. classId -> course display name only resolves for classes actually
   seen in this project's own captures (COURSE_NAMES below) - an
   unresolved classId falls back to a shortened id rather than a
   guessed name.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# classId (GUID) -> real course display name, resolved from REAL Teams
# network captures (the assignments.edu.cloud.microsoft/api responses
# never include a course name, only this GUID - the name had to be
# cross-referenced against a separate teams.cloud.microsoft endpoint and,
# for one course, a Teams deep-link the user pasted directly). See the
# "Реальный API заданий" section of README for how each of these was
# confirmed. A handful of other classIds show up in captured assignment
# data but never resolved to a name (old/hidden semesters) - those fall
# back to resolve_course_name()'s shortened-id behavior below rather
# than a guessed name.
COURSE_NAMES: dict[str, str] = {
    "e3f75118-bae6-4636-87c7-b2e91b63f913": "CSE4112 Администрирование систем и сетей (Лаб)",
    "d11a5ce4-a152-4345-917c-0c037db728d4": "CSE4112 Администрирование систем и сетей (Лекция)",
    "e70bd8af-0e7f-478d-bfa1-2f059cc3cc1d": "CSE5472 Основы научно-исследовательской работы студентов",
    "162b0fa2-6461-4eb0-aef3-da996e67e7e7": "CSE8092 Проектирование и защита серверных баз данных",
    "44ca09e5-d245-4a03-80e6-bac2bc205525": "ВиАУ Вт 7.50",
}


def resolve_course_name(class_id: str | None) -> str:
    if not class_id:
        return "Неизвестный курс"
    return COURSE_NAMES.get(class_id, f"Курс {class_id[:8]}")


def _extract_description(instructions: Any) -> str | None:
    """`instructions` was null in every assignment this project has
    actually captured, but Microsoft's own docs for the equivalent Graph
    field describe it as a rich-content block ({"content": ..., "contentType":
    ...}), not a plain string - handle both shapes rather than assuming
    the field is always a string once it's finally seen non-null."""
    if instructions is None:
        return None
    if isinstance(instructions, str):
        return instructions or None
    if isinstance(instructions, dict):
        content = instructions.get("content")
        return content or None
    return None


def parse_due(due_iso: str | None) -> datetime | None:
    """Parses a raw `dueDateTime` ISO string into an aware datetime, or
    None for a missing/malformed value. Public (not `_parse_due`) since
    agents/teams/agent.py also needs it, to filter out assignments from
    stale/old semesters by their real due date before saving anything
    to the DB - not just this module's own status computation."""
    if not due_iso:
        return None
    try:
        return datetime.fromisoformat(due_iso.replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_status(assignment: dict, submission: dict, *, now: datetime | None = None) -> str:
    """One of: returned, submitted, overdue, upcoming, completed.

    Order matters: a returned/submitted submission takes priority over
    the assignment-level isCompleted/dueDateTime flags, because those
    reflect where the assignment currently sits for the *class*, while
    the submission is specific to this student.
    """
    if submission.get("returnedDateTime"):
        return "returned"
    if submission.get("submittedDateTime"):
        return "submitted"

    if assignment.get("isCompleted"):
        return "completed"

    due = parse_due(assignment.get("dueDateTime"))
    if due is not None:
        now = now or datetime.now(timezone.utc)
        if due < now:
            return "overdue"
    return "upcoming"


def parse_assignment(assignment: dict, *, now: datetime | None = None) -> dict:
    """Normalize one raw educationAssignment object (as returned by
    .../edu/me/work) into the shape storage.models.SCHEMA's `tasks` table
    expects. Does not touch the database - agents/teams/agent.py does
    the DB write and change-state (new/changed/unchanged) computation,
    this function is pure and independently testable."""
    submissions = assignment.get("submissions") or []
    submission = submissions[0] if submissions else {}

    class_id = assignment.get("classId")
    return {
        "id": assignment.get("id", ""),
        "course": resolve_course_name(class_id),
        "title": assignment.get("displayName") or "(без названия)",
        "description": _extract_description(assignment.get("instructions")),
        "status": compute_status(assignment, submission, now=now),
        "created_at": assignment.get("createdDateTime"),
        "due_at": assignment.get("dueDateTime"),
        "submitted_at": submission.get("submittedDateTime"),
        "source": "teams",
    }


def parse_work_response(body: dict, *, now: datetime | None = None) -> list[dict]:
    """Parse one full .../edu/me/work response body (the "value" list) into
    Task rows. Returns [] for a malformed/empty body rather than raising -
    callers decide what an empty-but-200 response means."""
    items = body.get("value") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    return [parse_assignment(item, now=now) for item in items]
