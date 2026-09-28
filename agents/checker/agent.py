"""Checker Agent (bro's ТЗ) - the thin BaseAgent wrapper around
agents/checker/logic.py's pure sweep. Reads only already-normalized rows
already sitting in storage/database.py (sso_courses/sso_schedule_entries
for the current semester, tasks for source="teams", source_status for
Teams/SSO's live health) and writes checker_findings - never a network
call, never a browser, never SSO Agent/TeamsAgent/AuthCheckerAgent
themselves (spec §2/§19).

WHY THIS GATES ON source_status BEFORE DOING ANY ANALYSIS AT ALL (spec
§12) - Checker "physically doesn't know what's in Teams" the moment
Teams' own session is AUTH_REQUIRED/ERROR/UNAVAILABLE/UNKNOWN (same for
SSO), so it must never turn "no usable data from that source right now"
into an analytical claim like NO_MATCH - that would misrepresent a
stale/broken session as a confirmed missing assignment. So this checks
agents/auth_checker.py's own source_status rows FIRST, for both
sources, and skips the whole sweep (writing nothing, touching no
existing checker_findings rows) the moment either one isn't OK.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from agents.base import AgentResult, AgentStatus, BaseAgent
from agents.checker.logic import CheckerStatus, course_matches_task, evaluate_course
from storage.database import Database

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_START_DAYS = 7
DEFAULT_WINDOW_END_DAYS = 30

# agents/auth_checker.py's SourceStatus values (as stored in
# source_status.status) -> the CheckerStatus value bro's spec §11 uses
# to describe a blocked run. Deliberately a plain dict, not a shared
# import of SourceStatus itself - Checker's spec spells this one
# differently (SOURCE_UNAVAILABLE vs Auth Checker's own UNAVAILABLE),
# so this is the one explicit place that translation happens.
_SOURCE_STATUS_TO_CHECKER_STATUS = {
    "AUTH_REQUIRED": CheckerStatus.AUTH_REQUIRED.value,
    "ERROR": CheckerStatus.ERROR.value,
    "UNAVAILABLE": CheckerStatus.SOURCE_UNAVAILABLE.value,
    "UNKNOWN": CheckerStatus.SOURCE_UNAVAILABLE.value,
}


def _blocking_status(row) -> str | None:
    """None means "this source is OK, proceed" - anything else is the
    CheckerStatus value the whole run should report instead of
    analyzing anything. A missing row (Auth Checker has never run yet)
    is treated the same as SOURCE_UNAVAILABLE - Checker has no more
    information than "I don't know" either way."""
    if row is None:
        return CheckerStatus.SOURCE_UNAVAILABLE.value
    status = row["status"]
    if status == "OK":
        return None
    return _SOURCE_STATUS_TO_CHECKER_STATUS.get(status, CheckerStatus.SOURCE_UNAVAILABLE.value)


class CheckerAgent(BaseAgent):
    def __init__(
        self,
        db: Database,
        window_start_days: int = DEFAULT_WINDOW_START_DAYS,
        window_end_days: int = DEFAULT_WINDOW_END_DAYS,
    ):
        super().__init__(name="checker")
        self.db = db
        self.window_start_days = window_start_days
        self.window_end_days = window_end_days

    async def run(self) -> AgentResult:
        teams_blocked = _blocking_status(self.db.get_source_status("teams"))
        if teams_blocked is not None:
            return self._blocked_result(source="teams", reason=teams_blocked)

        sso_blocked = _blocking_status(self.db.get_source_status("sso"))
        if sso_blocked is not None:
            return self._blocked_result(source="sso", reason=sso_blocked)

        semester_id = self.db.get_latest_sso_semester_id()
        if semester_id is None:
            logger.info("Checker: no SSO snapshot saved yet - nothing to compare against.")
            return AgentResult(
                agent=self.name,
                status=AgentStatus.WORKING,
                data={"blocked": True, "reason": "no_sso_snapshot"},
            )

        # dict(...) here (not sqlite3.Row as-is) because agents/checker/
        # logic.py's course_matches_task() calls course.get(...) - Row
        # only supports bracket access, not .get(), so a raw Row would
        # blow up the very first time a course's optional field is read.
        courses = [dict(row) for row in self.db.get_sso_courses(semester_id)]
        schedule_rows = self.db.get_sso_schedule(semester_id)
        teams_tasks = [dict(row) for row in self.db.get_tasks(source="teams")]

        schedule_by_code: dict[str, list] = {}
        for row in schedule_rows:
            schedule_by_code.setdefault(row["course_code"], []).append(row)

        now = datetime.now(timezone.utc).date()
        window_start = now - timedelta(days=self.window_start_days)
        window_end = now + timedelta(days=self.window_end_days)
        checked_at = datetime.now(timezone.utc).isoformat()

        findings_written = 0
        for course in courses:
            course_schedule = schedule_by_code.get(course["code"], [])
            if not course_schedule:
                # Nothing scheduled for this course in the current
                # semester's table at all - nothing to sweep, and
                # nothing to claim either way.
                continue

            course_tasks = [t for t in teams_tasks if course_matches_task(course, t)]
            if not course_tasks:
                # spec §6.4: no Teams evidence for this course's
                # identity at all (not even a wrong-type assignment) -
                # UNMATCHED_COURSE, no per-activity detail to show.
                self.db.upsert_checker_finding(
                    course_code=course["code"],
                    course_title=course["title"],
                    overall_status=CheckerStatus.UNMATCHED_COURSE.value,
                    checked_at=checked_at,
                    window_start=window_start.isoformat(),
                    window_end=window_end.isoformat(),
                    details={"activities": []},
                )
                findings_written += 1
                continue

            activities, overall_status = evaluate_course(
                course_schedule, course_tasks, window_start, window_end
            )
            if not activities:
                # No occurrence of this course's schedule fell inside
                # the window at all (a very narrow window, say) -
                # nothing to record.
                continue

            self.db.upsert_checker_finding(
                course_code=course["code"],
                course_title=course["title"],
                overall_status=overall_status,
                checked_at=checked_at,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
                details={"activities": activities},
            )
            findings_written += 1

        logger.info(
            "Checker: %s course(s) checked, %s finding(s) written (window %s..%s)",
            len(courses), findings_written, window_start.isoformat(), window_end.isoformat(),
        )
        return AgentResult(
            agent=self.name,
            status=AgentStatus.WORKING,
            data={
                "blocked": False,
                "semester_id": semester_id,
                "courses_checked": len(courses),
                "findings_written": findings_written,
            },
        )

    def _blocked_result(self, source: str, reason: str) -> AgentResult:
        logger.info("Checker: blocked by %s status (%s) - skipping this run.", source, reason)
        status = AgentStatus.DEGRADED if reason == CheckerStatus.ERROR.value else AgentStatus.WORKING
        return AgentResult(
            agent=self.name,
            status=status,
            data={"blocked": True, "source": source, "reason": reason},
        )
