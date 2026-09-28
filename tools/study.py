"""Study Manager tools (AI Manager ТЗ v3 §11/§24) - each Tool below is a
thin wrapper around one of StudyManager's PUBLIC methods (agents/
study_manager/agent.py), never agents/study_manager/logic.py directly
(spec §11's explicit "не импортировать внутренние, если для операции
существует публичный интерфейс StudyManager"). No handler here touches
SQLite, a browser, or the network itself - StudyManager already does
that job; this module only describes each call for the LLM and passes
validated arguments through.
"""
from __future__ import annotations

from typing import Any

from agents.study_manager.agent import StudyManager
from tools.base import Tool

#: spec §14's exact allowed field list, mirrored here (not imported from
#: agents/study_manager/logic.py, same "small pure list duplicated across
#: a module boundary is cheaper than a coupling" call already made for
#: agents/study_manager/logic.py itself) so the LLM sees the real
#: allowed values directly in the tool schema, not just a free-form
#: string it has to guess at.
_OVERRIDE_FIELD_ENUM = [
    "academic_week",
    "activity_number",
    "is_current",
    "local_due_date",
    "local_completion_status",
    "user_note",
]
_TARGET_TYPE_ENUM = ["task", "activity", "course"]


class ToolNotFoundError(Exception):
    """Raised by a handler below when the requested id doesn't exist -
    tools/base.py's ToolRegistry.execute() turns this into
    ToolResult.fail("ToolNotFoundError", ...), never a silently-empty
    OK result an LLM could mistake for "no data at all" (spec §37 cuts
    both ways: an unknown id is a real error, not silence)."""


def build_study_tools(study_manager: StudyManager) -> list[Tool]:
    """Builds every Study Manager tool, each closed over THIS
    ``study_manager`` instance - callers (run.py/AIManager wiring) hand
    in the SAME StudyManager already registered on the scheduler,
    rather than this module constructing its own copy."""

    def _get_dashboard() -> dict:
        return study_manager.get_dashboard()

    def _get_upcoming_schedule(days_ahead: int | None = None) -> list[dict]:
        return study_manager.get_upcoming_schedule(days_ahead=days_ahead)

    def _get_upcoming_tasks(days_ahead: int | None = None) -> list[dict]:
        return study_manager.get_upcoming_tasks(days_ahead=days_ahead)

    def _get_overdue_tasks() -> list[dict]:
        return study_manager.get_overdue_tasks()

    def _get_course_tasks(course_code: str) -> dict:
        return study_manager.get_course_tasks(course_code)

    def _get_course_materials(course_code: str) -> dict:
        return study_manager.get_course_materials(course_code)

    def _get_checker_findings(course_code: str | None = None) -> list[dict]:
        return study_manager.get_checker_findings(course_code)

    def _get_source_status() -> list[dict]:
        return study_manager.get_source_status()

    async def _refresh_study_data() -> dict:
        return await study_manager.refresh()

    def _set_task_override(
        task_id: str,
        field: str,
        value: Any,
        course_code: str | None = None,
        reason: str | None = None,
    ) -> dict:
        return study_manager.set_task_override(task_id, field, value, course_code=course_code, reason=reason)

    def _set_activity_override(
        activity_id: str,
        field: str,
        value: Any,
        course_code: str | None = None,
        reason: str | None = None,
    ) -> dict:
        return study_manager.set_activity_override(
            activity_id, field, value, course_code=course_code, reason=reason
        )

    def _clear_override(target_type: str, target_id: str, field: str | None = None) -> dict:
        return study_manager.clear_override(target_type, target_id, field=field)

    def _get_overrides(course_code: str | None = None, target_type: str | None = None) -> list[dict]:
        return study_manager.get_overrides(course_code=course_code, target_type=target_type)

    def _get_effective_task(task_id: str) -> dict:
        result = study_manager.get_effective_task(task_id)
        if result is None:
            raise ToolNotFoundError(f"no such task: {task_id!r}")
        return result

    return [
        Tool(
            name="get_dashboard",
            description=(
                "Returns a combined study snapshot: today's/tomorrow's schedule, upcoming and "
                "overdue tasks, Checker findings, and source status. Reads only the local "
                "database - does not perform a live Teams/SSO synchronization."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_get_dashboard,
        ),
        Tool(
            name="get_upcoming_schedule",
            description=(
                "Returns upcoming SSO class occurrences (lectures/practices/labs), expanded "
                "from the weekly schedule template into real calendar dates. Reads only the "
                "local database - does not perform a live SSO synchronization."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": (
                            "How many days ahead to include. Omit to use Study Manager's own "
                            "configured default window (usually 7)."
                        ),
                    }
                },
                "required": [],
            },
            handler=_get_upcoming_schedule,
        ),
        Tool(
            name="get_upcoming_tasks",
            description=(
                "Returns upcoming (not yet due, not completed) Teams assignments from the "
                "normalized study database. Does not perform a live Teams synchronization, and "
                "never includes overdue tasks - use get_overdue_tasks for those."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": (
                            "How many days ahead to include. Omit to use Study Manager's own "
                            "configured default window (usually 7)."
                        ),
                    }
                },
                "required": [],
            },
            handler=_get_upcoming_tasks,
        ),
        Tool(
            name="get_overdue_tasks",
            description=(
                "Returns Teams assignments whose due date has already passed and that are not "
                "marked completed/submitted/returned. Always available regardless of any "
                "'upcoming' time window a caller might separately be using."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_get_overdue_tasks,
        ),
        Tool(
            name="get_course_tasks",
            description=(
                "Returns all Teams tasks for one course, resolved by course code or exact "
                "course title. Returns match='AMBIGUOUS' or match='NOT_FOUND' (with an empty "
                "task list) instead of guessing when the course can't be resolved unambiguously "
                "- never pick one course at random."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "course_code": {
                        "type": "string",
                        "description": "A course code (e.g. 'CSE4112') or an exact course title.",
                    }
                },
                "required": ["course_code"],
            },
            handler=_get_course_tasks,
        ),
        Tool(
            name="get_course_materials",
            description=(
                "Returns metadata only (title/category/file_id) for one course's SSO study "
                "materials (УМКД) - never downloads or returns actual file content."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "course_code": {
                        "type": "string",
                        "description": "A course code (e.g. 'CSE4112') or an exact course title.",
                    }
                },
                "required": ["course_code"],
            },
            handler=_get_course_materials,
        ),
        Tool(
            name="get_checker_findings",
            description=(
                "Returns Checker Agent's own MATCH/PARTIAL_MATCH/NO_MATCH/UNMATCHED_COURSE "
                "findings exactly as already computed and stored - never recomputes "
                "reconciliation logic here."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "course_code": {"type": "string", "description": "Optional course code to filter by."}
                },
                "required": [],
            },
            handler=_get_checker_findings,
        ),
        Tool(
            name="get_source_status",
            description=(
                "Returns Teams/SSO's current health status (OK/AUTH_REQUIRED/ERROR/UNAVAILABLE/"
                "UNKNOWN) plus last_successful_sync/last_error freshness info. Use this before "
                "presenting study data as fully current."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_get_source_status,
        ),
        Tool(
            name="refresh_study_data",
            description=(
                "Triggers a real synchronization of Teams/SSO/Checker/Auth Checker right now. "
                "May require authentication and takes noticeably longer than the read-only "
                "tools above. Only call this when the user explicitly asks to refresh/update/"
                "re-check their study data (e.g. 'обнови задания') - never for an ordinary read "
                "query like 'какие у меня задания'."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_refresh_study_data,
            read_only=False,
        ),
        Tool(
            name="set_task_override",
            description=(
                "Stores a user-owned LOCAL correction for one Teams task - academic_week, "
                "activity_number, is_current, local_due_date, local_completion_status, or "
                "user_note only; any other field name is rejected. Never modifies Microsoft "
                "Teams or any external source - only this app's own local study_overrides "
                "table. Marking local_completion_status=COMPLETED does NOT mark the assignment "
                "submitted in Teams."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The Teams task id to correct."},
                    "field": {"type": "string", "enum": _OVERRIDE_FIELD_ENUM},
                    "value": {"description": "The override's value - its expected type depends on `field`."},
                    "course_code": {
                        "type": "string",
                        "description": "Optional course code, stored alongside the override for grouping/lookup.",
                    },
                    "reason": {"type": "string", "description": "Optional short human-readable reason."},
                },
                "required": ["task_id", "field", "value"],
            },
            handler=_set_task_override,
            read_only=False,
        ),
        Tool(
            name="set_activity_override",
            description=(
                "Same as set_task_override, but for a scheduled class occurrence (e.g. a "
                "schedule entry id) rather than a specific Teams task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "activity_id": {"type": "string", "description": "The schedule entry id to correct."},
                    "field": {"type": "string", "enum": _OVERRIDE_FIELD_ENUM},
                    "value": {"description": "The override's value - its expected type depends on `field`."},
                    "course_code": {"type": "string", "description": "Optional course code for grouping/lookup."},
                    "reason": {"type": "string", "description": "Optional short human-readable reason."},
                },
                "required": ["activity_id", "field", "value"],
            },
            handler=_set_activity_override,
            read_only=False,
        ),
        Tool(
            name="clear_override",
            description=(
                "Removes a previously stored local correction - with no `field`, clears every "
                "active correction on that target at once. Reverts the effective value back to "
                "Teams/SSO's own source value. Never deletes Source Data itself."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_type": {"type": "string", "enum": _TARGET_TYPE_ENUM},
                    "target_id": {"type": "string"},
                    "field": {
                        "type": "string",
                        "enum": _OVERRIDE_FIELD_ENUM,
                        "description": "Optional - omit to clear every field on this target.",
                    },
                },
                "required": ["target_type", "target_id"],
            },
            handler=_clear_override,
            read_only=False,
        ),
        Tool(
            name="get_overrides",
            description=(
                "Lists the user's own active local corrections, optionally filtered by course "
                "or target type."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "course_code": {"type": "string"},
                    "target_type": {"type": "string", "enum": _TARGET_TYPE_ENUM},
                },
                "required": [],
            },
            handler=_get_overrides,
        ),
        Tool(
            name="get_effective_task",
            description=(
                "Returns one Teams task's source value, active override, and the resulting "
                "effective value side by side - use this to explain a task that has a local "
                "correction applied, without conflating 'user says done' with 'Teams says done'."
            ),
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
            handler=_get_effective_task,
        ),
    ]
