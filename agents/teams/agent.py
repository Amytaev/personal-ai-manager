"""Teams Agent (TZ v4 §10, Phase 5).

Fetches assignments from the real internal Teams Education API
(assignments.edu.cloud.microsoft/api/v1.0/edu/me/work) using the
persistent Playwright profile's own session (agents/teams/auth.py) - no
Graph API (blocked by admin consent, see README), no DOM scraping, no
stored password.

HONEST, UNVERIFIED ASSUMPTION - read before trusting this in production:
this agent calls the work API directly via `context.request`, reusing
whatever cookies the persistent browser profile already holds for
assignments.edu.cloud.microsoft. Every real capture so far only ever
saw that API get called from *inside* the Teams UI, after clicking into
a class - never as a standalone call right after loading teams.microsoft.com
alone. It is unverified whether the SSO cookie for that specific origin is
already present after just an is_logged_in()-style Teams load, or whether
it only gets set once the user (or this agent) has actually opened the
Assignments app inside Teams at least once. scripts/teams_agent_smoke_test.py
exists specifically to test this for real on the user's machine before
this is treated as "done" - same pattern as LOGGED_IN_URL_HINT in Phase 5's
first cut, which turned out to need one real-world correction too.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from agents.base import AgentResult, AgentStatus, BaseAgent
from agents.teams.auth import TEAMS_URL, TeamsSession
from agents.teams.parser import parse_work_response
from storage.database import Database

logger = logging.getLogger(__name__)

WORK_API_URL = "https://assignments.edu.cloud.microsoft/api/v1.0/edu/me/work"

# $expand pulls in submission status/dates in the same call - matches
# what the real web client requested in every capture, not a
# simplification, since this is an undocumented API and a "cleaner"
# query we invented could behave differently.
_EXPAND = "submissions($expand=outcomes),categories,submissionAggregates"

# TODO(known limitation): no $skiptoken pagination - every real capture
# saw well under 50 items in even the largest bucket (completed, several
# semesters deep), so $top=50 covers what's been observed, but a student
# with a long enough history could have more. Fine for a personal daily
# briefing tool; would need pagination if that assumption ever breaks.
_TOP = 50


def _filters(now_iso: str) -> dict[str, str]:
    """The three $filter expressions scripts/teams_capture_assignments.py
    captured verbatim from the real web client for Upcoming/Overdue/
    Completed - see README "Реальный API заданий"."""
    edu_status = "microsoft.education.assignments.api.educationAssignmentStatus"
    return {
        "upcoming": (
            f"( ( status eq {edu_status}'assigned' and isCompleted eq false ) or "
            f"( status ne {edu_status}'assigned' and status ne {edu_status}'draft' and "
            f"status ne {edu_status}'pending' and status ne {edu_status}'inactive' ) ) "
            f"and dueDateTime ge {now_iso}"
        ),
        "overdue": (
            f"status eq {edu_status}'assigned' and isCompleted eq false "
            f"and dueDateTime le {now_iso} and allTurnedIn eq false"
        ),
        "completed": (
            f"( ( status eq {edu_status}'assigned' and isCompleted eq true ) or "
            f"( status eq {edu_status}'inactive' ) )"
        ),
    }


class TeamsAgent(BaseAgent):
    def __init__(self, profile_dir: str, db: Database, session: TeamsSession | None = None):
        super().__init__(name="teams")
        # A pre-built TeamsSession can be injected (tests, or a caller
        # that wants to share one session across agents) - otherwise
        # this agent owns and closes its own, like WeatherAgent owns its
        # own httpx.AsyncClient unless one is passed in.
        self.session = session or TeamsSession(profile_dir)
        self._owns_session = session is None
        self.db = db

    async def aclose(self) -> None:
        if self._owns_session:
            await self.session.close()

    async def _fetch_all_assignments(self) -> list[dict]:
        """Hits the three real $filter queries and returns the merged,
        de-duplicated raw educationAssignment dicts (the same assignment
        can legitimately appear in more than one bucket's response - a
        just-submitted item that's simultaneously "not yet due" is a real
        case seen in the actual captured data, not a hypothetical)."""
        context = await self.session._ensure_context(headless=True)  # noqa: SLF001 - see auth.py

        page = await context.new_page()
        try:
            # Loads the Teams shell first, same as is_logged_in() - the
            # assignments API call right after this is the part flagged as
            # unverified in this module's docstring.
            await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=30_000)
        finally:
            await page.close()

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        by_id: dict[str, dict] = {}
        for label, filter_expr in _filters(now_iso).items():
            response = await context.request.get(
                WORK_API_URL,
                params={
                    "$filter": filter_expr,
                    "$top": str(_TOP),
                    "$orderby": "dueDateTime desc",
                    "$expand": _EXPAND,
                },
            )
            if not response.ok:
                raise RuntimeError(
                    f"Teams work API returned HTTP {response.status} for the {label!r} query "
                    f"(work API auth cookie may be missing - see this module's docstring)"
                )
            body = await response.json()
            for raw in body.get("value", []):
                raw_id = raw.get("id")
                if raw_id:
                    by_id[raw_id] = raw  # last-write-wins de-dupe across the 3 queries

        return list(by_id.values())

    async def run(self) -> AgentResult:
        try:
            raw_assignments = await self._fetch_all_assignments()
        except Exception as exc:  # noqa: BLE001 - expected failure mode (auth expired, API down/reshaped)
            return AgentResult(agent=self.name, status=AgentStatus.FAILING, error=f"{type(exc).__name__}: {exc}")

        now = datetime.now(timezone.utc)
        tasks = [parse_work_response({"value": [raw]}, now=now)[0] for raw in raw_assignments]

        counts = {"new": 0, "changed": 0, "unchanged": 0}
        for task in tasks:
            existing = self.db.get_task_fingerprint(task["id"])
            if existing is None:
                state = "new"
            elif (
                existing["status"] != task["status"]
                or existing["due_at"] != task["due_at"]
                or existing["submitted_at"] != task["submitted_at"]
                or existing["title"] != task["title"]
            ):
                state = "changed"
            else:
                state = "unchanged"
            counts[state] += 1
            self.db.upsert_task(task, state=state)

        logger.info(
            "Teams agent: %s task(s) total (%s new, %s changed, %s unchanged)",
            len(tasks), counts["new"], counts["changed"], counts["unchanged"],
        )
        return AgentResult(
            agent=self.name,
            status=AgentStatus.WORKING,
            data={"total": len(tasks), **counts},
        )
