"""Study Manager Agent (bro's ТЗ) - the orchestration/read layer on top
of TeamsAgent/SsoAgent/CheckerAgent/AuthCheckerAgent's PUBLIC interfaces
only (spec §4/§31 - never their internal modules, never Playwright,
never Microsoft/SSO auth, never Teams/SSO HTML/API directly).

WHAT THIS AGENT IS (spec §2/§31): a service/orchestration layer that
combines already-collected data. TeamsAgent knows Teams, SsoAgent knows
SSO, CheckerAgent knows reconciliation, AuthCheckerAgent knows
health/auth status - this agent knows how to COMBINE study data for a
future AI Manager to read. It is explicitly NOT a "superagent" that
reimplements any of that.

WHAT THIS AGENT DELIBERATELY DOES NOT DO (spec §33, verbatim scope
cut): no AI Manager, no Claude/OpenAI/any LLM SDK, no Telegram (import
or call), no Telegram Mini App, no WhatsApp, no Agent Recruiter/
Factory, no automatic notification sending, no automatic УМКД file
download, no writes back to Teams/SSO, no VALORANT changes, no MFA
relay. Ordinary reads (get_dashboard/get_upcoming_schedule/etc.) NEVER
open a browser or hit a network - they only ever read SQLite (spec
§27); a browser/network only happens inside refresh(), and even then
only via the source agents' own run() methods, never directly.

SECURITY (spec §29, verbatim-critical): this module and everything it
calls must never log a password, cookie, session/access token, MFA
code, IIN, DOB, Telegram bot token, or any other credential. Only
source name, status, duration/record counts, last successful sync, and
an error TYPE/message with no secret data are ever logged - and this
agent never invents a new log line containing raw exception text from
the source agents it calls; it only records/re-reads what those
agents' own run_and_record()/AuthCheckerAgent already sanitized before
writing to source_status/agent_runs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from agents.base import AgentResult, AgentStatus, BaseAgent
from agents.study_manager import logic
from storage.database import Database

logger = logging.getLogger(__name__)

DEFAULT_UPCOMING_DAYS_AHEAD = 7


class StudyManager(BaseAgent):
    """Dependency-injected on purpose (spec §36 step 1-2: check the
    existing public interfaces first, don't modify them) - accepts
    already-constructed TeamsAgent/SsoAgent/CheckerAgent/AuthCheckerAgent
    instances (any may be None if that source isn't wired up for this
    run, mirroring AuthCheckerAgent's own optional-session pattern) so
    run.py can hand it the SAME agent instances it already registers on
    the scheduler, rather than this class constructing its own
    (duplicate) copies."""

    def __init__(
        self,
        db: Database,
        teams_agent: BaseAgent | None = None,
        sso_agent: BaseAgent | None = None,
        checker_agent: BaseAgent | None = None,
        auth_checker_agent: BaseAgent | None = None,
        upcoming_days_ahead: int = DEFAULT_UPCOMING_DAYS_AHEAD,
    ):
        super().__init__(name="study_manager")
        self.db = db
        self.teams_agent = teams_agent
        self.sso_agent = sso_agent
        self.checker_agent = checker_agent
        self.auth_checker_agent = auth_checker_agent
        self.upcoming_days_ahead = upcoming_days_ahead

    # -- BaseAgent contract / scheduled sync mode (spec §22) -------------

    async def run(self) -> AgentResult:
        """The scheduled sync mode's entrypoint - just refresh(), reported
        as this agent's own AgentResult so run_and_record()'s agent_runs
        bookkeeping works the same as every other agent in this project."""
        result = await self.refresh()
        status_value = result["status"]
        if status_value == logic.StudyManagerStatus.ERROR.value:
            agent_status = AgentStatus.FAILING
        elif status_value == logic.StudyManagerStatus.OK.value:
            agent_status = AgentStatus.WORKING
        else:
            # PARTIAL/AUTH_REQUIRED/SOURCE_UNAVAILABLE - a normal,
            # expected degraded state (some source down), not a crash.
            agent_status = AgentStatus.DEGRADED
        return AgentResult(agent=self.name, status=agent_status, data=result)

    # -- manual + scheduled sync mode (spec §7) ---------------------------

    async def refresh(self) -> dict[str, Any]:
        """Both the manual refresh entrypoint AND what run() calls for
        the scheduled cycle (spec §7: "два режима синхронизации" share
        the same underlying logic, just a different trigger). Sequence:

        1. Auth Checker (if wired up) FIRST, so the "should I even try
           this source" decision below reflects the state from BEFORE
           this cycle's own sync attempt.
        2. Run Teams/SSO's own agents - skipped ONLY when that source's
           last known status is UNAVAILABLE (no session was ever wired
           up for it in this run at all - nothing to retry). AUTH_REQUIRED
           and ERROR are NOT skipped: SsoAgent already retries login via
           autofill on its own, and every source agent already degrades
           gracefully instead of crashing (agents/teams/agent.py,
           agents/sso/agent.py) - so it's always worth a real attempt
           rather than Study Manager pre-emptively giving up on its
           behalf.
        3. Auth Checker AGAIN - only AuthCheckerAgent writes to
           source_status (Teams/SSO agents never touch that table
           themselves, see agents/auth_checker.py's docstring), so this
           second call is what makes source_status reflect THIS cycle's
           sync result, in time for step 4 below to read it.
        4. Run Checker - but only when BOTH sources it depends on are
           OK; CheckerAgent already self-gates the same way internally
           (agents/checker/agent.py's own _blocking_status()), so this
           check is redundant with Checker's own safety net by design -
           it's what makes "Checker isn't called when a needed source
           is unavailable" an observable fact about THIS agent's own
           behavior (spec §32's Checker test group), not just Checker's.
        5. Compute and return the final status (spec §19, via
           logic.combine_source_status()) plus whatever internal errors
           happened along the way. A single source's own exception is
           always caught right where it happens (spec §28: one source's
           failure must never take down the rest of this method) -
           still recorded in ``errors`` and reflected in the final
           status, but the OTHER source's own sync always still runs.
        """
        errors: list[str] = []

        if self.auth_checker_agent is not None:
            await self._run_step("auth_checker", self.auth_checker_agent, errors)

        for name, agent in (("teams", self.teams_agent), ("sso", self.sso_agent)):
            if agent is None:
                continue
            status_row = self.db.get_source_status(name)
            if status_row is not None and status_row["status"] == "UNAVAILABLE":
                logger.info(
                    "Study Manager: skipping %s sync this cycle - status_status is UNAVAILABLE "
                    "(no session wired up for it right now).", name,
                )
                continue
            await self._run_step(name, agent, errors)

        if self.auth_checker_agent is not None:
            await self._run_step("auth_checker", self.auth_checker_agent, errors)

        teams_row = self.db.get_source_status("teams")
        sso_row = self.db.get_source_status("sso")
        teams_status = teams_row["status"] if teams_row else None
        sso_status = sso_row["status"] if sso_row else None

        if self.checker_agent is not None and teams_status == "OK" and sso_status == "OK":
            await self._run_step("checker", self.checker_agent, errors)
        else:
            logger.info(
                "Study Manager: skipping Checker this cycle - teams=%s sso=%s (both must be OK).",
                teams_status, sso_status,
            )

        overall_status = logic.combine_source_status(teams_status, sso_status)
        if errors:
            # spec §19: ERROR is reserved for a genuine internal
            # processing failure, not merely "a source is down" (that's
            # already PARTIAL/AUTH_REQUIRED/SOURCE_UNAVAILABLE above) -
            # an exception caught in _run_step() below is exactly that
            # kind of internal failure.
            overall_status = logic.StudyManagerStatus.ERROR.value

        return {
            "status": overall_status,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "teams_status": teams_status,
            "sso_status": sso_status,
            "errors": errors,
        }

    async def _run_step(self, name: str, agent: BaseAgent, errors: list[str]) -> None:
        try:
            await agent.run()
        except Exception as exc:  # noqa: BLE001 - per-source isolation boundary, spec §28
            # Exception TYPE + message only (no secrets ever end up in an
            # exception message this project's own agents raise, but
            # this is still the same discipline as run_isolated() /
            # agents/auth_checker.py's own error formatting) - never a
            # traceback, never raw request/response data.
            logger.warning("Study Manager: %s raised %s: %s", name, type(exc).__name__, exc)
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    # -- reads (spec §25/§27 - SQLite only, never a browser/network) ------

    def _semester_id(self) -> int | None:
        return self.db.get_latest_sso_semester_id()

    def _sso_courses(self) -> list[dict]:
        semester_id = self._semester_id()
        if semester_id is None:
            return []
        return [dict(row) for row in self.db.get_sso_courses(semester_id)]

    def _teams_task_views(self) -> list[dict]:
        rows = [dict(row) for row in self.db.get_tasks(source="teams")]
        return [logic.task_view(row) for row in rows]

    def get_source_status(self) -> list[dict]:
        """spec §20/§25: every source's status/last_successful_sync/
        last_error, straight from storage/models.py's source_status
        table - the single freshness report a caller needs, without
        knowing anything about Teams/SSO/Auth Checker internals."""
        return [dict(row) for row in self.db.get_all_source_status()]

    def get_upcoming_schedule(self, days_ahead: int | None = None) -> list[dict]:
        """spec §13. Empty (not an error) when SSO has never saved a
        snapshot at all yet - spec §21: no data is never turned into a
        false claim, it's just an empty, honest result."""
        semester_id = self._semester_id()
        if semester_id is None:
            return []
        schedule_rows = [dict(row) for row in self.db.get_sso_schedule(semester_id)]
        return logic.expand_weekly_schedule(
            schedule_rows, days_ahead=days_ahead if days_ahead is not None else self.upcoming_days_ahead
        )

    def get_upcoming_tasks(self, days_ahead: int | None = None) -> list[dict]:
        """spec §14. Reads storage.tasks directly - never re-parses a raw
        Teams response (spec §9), never opens a browser (spec §27)."""
        return logic.filter_upcoming_tasks(
            self._teams_task_views(),
            days_ahead=days_ahead if days_ahead is not None else self.upcoming_days_ahead,
        )

    def get_overdue_tasks(self) -> list[dict]:
        """spec §15 - always available regardless of any time window a
        caller might separately be using for "upcoming"."""
        return logic.filter_overdue_tasks(self._teams_task_views())

    def get_course_tasks(self, course_code: str) -> dict[str, Any]:
        """spec §16. ``match`` is one of "MATCHED"/"NOT_FOUND"/
        "AMBIGUOUS" - a caller must check it before trusting ``tasks``;
        an AMBIGUOUS/NOT_FOUND result always comes with an empty task
        list, never a guessed one."""
        course, tasks = logic.course_tasks_for(course_code, self._sso_courses(), self._teams_task_views())
        if isinstance(course, logic.CourseMatchResult):
            return {"match": course.value, "course": None, "tasks": []}
        return {
            "match": "MATCHED",
            "course": {"code": course.get("code"), "title": course.get("title")},
            "tasks": tasks,
        }

    def get_course_materials(self, course_code: str) -> dict[str, Any]:
        """spec §18 - metadata only (file_id/title/category), never the
        binary file itself (spec §2's explicit "no auto-download УМКД")."""
        courses = self._sso_courses()
        course = logic.resolve_course(courses, course_code)
        if isinstance(course, logic.CourseMatchResult):
            return {"match": course.value, "course": None, "materials": []}

        course_title_norm = logic.normalize_title(course.get("title"))
        materials_rows = [dict(row) for row in self.db.get_sso_materials()]
        matched = [
            m for m in materials_rows
            if logic.normalize_title(m.get("course_title")) == course_title_norm
        ]
        return {
            "match": "MATCHED",
            "course": {"code": course.get("code"), "title": course.get("title")},
            "materials": [
                {
                    "file_id": m.get("file_id"),
                    "title": m.get("file_name"),
                    "category": m.get("file_category_title"),
                    "instructor": m.get("instructor_name"),
                    "source": "sso",
                }
                for m in matched
            ],
        }

    def get_checker_findings(self, course_code: str | None = None) -> list[dict]:
        """spec §11: Checker's own ready-made findings, read as-is - this
        module never repeats Checker's own reconciliation logic."""
        result = []
        for row in self.db.get_checker_findings(course_code):
            finding = dict(row)
            details = finding.get("details")
            if isinstance(details, str):
                try:
                    finding["details"] = json.loads(details)
                except ValueError:
                    pass  # leave the raw string rather than crash a dashboard read
            result.append(finding)
        return result

    def get_dashboard(self) -> dict[str, Any]:
        """spec §26/§34 - one aggregating snapshot, deliberately NOT
        including the (potentially large) materials arrays (§26's "must
        NOT include huge material arrays" - use get_course_materials()
        for that on demand instead)."""
        now = datetime.now(timezone.utc)
        source_status = self.get_source_status()
        status_by_source = {row["source"]: row["status"] for row in source_status}
        overall_status = logic.combine_source_status(
            status_by_source.get("teams"), status_by_source.get("sso")
        )

        schedule = self.get_upcoming_schedule(days_ahead=max(self.upcoming_days_ahead, 1))
        today_str = now.date().isoformat()
        tomorrow_str = (now.date() + timedelta(days=1)).isoformat()

        return {
            "status": overall_status,
            "generated_at": now.isoformat(),
            "today": [s for s in schedule if s["date"] == today_str],
            "tomorrow": [s for s in schedule if s["date"] == tomorrow_str],
            "upcoming_tasks": self.get_upcoming_tasks(),
            "overdue_tasks": self.get_overdue_tasks(),
            "checker_findings": self.get_checker_findings(),
            "source_status": source_status,
        }
