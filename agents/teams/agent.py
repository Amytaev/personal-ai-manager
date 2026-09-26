"""Teams Agent (TZ v4 §10, Phase 5).

Fetches assignments from the real internal Teams Education API
(assignments.edu.cloud.microsoft/api/v1.0/edu/me/work) - no Graph API
(blocked by admin consent, see README), no stored password.

WHY THIS ISN'T A DIRECT API CALL - a real, verified finding, not a
guess: the first cut of this agent called the work API straight via
`context.request`, assuming the persistent browser profile's own
cookies would authenticate it. Real testing against the live Satbayev
tenant (scripts/teams_agent_smoke_test.py, then
scripts/teams_agent_navigation_test.py, then
scripts/teams_agent_token_test.py, then scripts/teams_agent_click_test.py
- all four kept, all four disprove a different guess) showed:

1. A bare load of teams.microsoft.com/teams.cloud.microsoft, with or
   without waiting for networkidle, never authenticates the work API -
   401 every time (smoke test).
2. Navigating directly to the Assignments class page's own URL
   (assignments.edu.cloud.microsoft/classes/<id>/list) as a standalone
   top-level page loads fine but never even calls the work API at all -
   the page's own JS sits waiting for a Teams-SDK postMessage handshake
   with a real parent Teams shell that a bare page.goto() never
   provides (navigation test).
3. The work API is Bearer-token authenticated, not cookie-authenticated
   - the token lives in the Assignments iframe's own JS memory after a
   genuine SDK handshake, never in an HTTP-only cookie `context.request`
   can reuse. Confirmed by capturing the real Authorization header off
   the page's own request and finding no call ever fires without a real
   click happening first (token test).
4. A REAL click on the "Задания" button in Teams' left rail - done
   headless via Playwright, not by hand - DOES complete that handshake
   and DOES get a genuine 200 from the work API. But priming with just
   that one click does NOT unlock a separate context.request call
   afterwards (still 401) - the token stays scoped to the iframe, so
   the response BODIES from the page's own traffic are the only way to
   get the data (click test).

So this agent drives the real UI exactly like a student would: open
Teams, click "Задания", click through the Assignments dashboard's tabs,
and capture the JSON response bodies the page's own code already
fetches - the same technique scripts/teams_capture_assignments.py uses
for a manual capture, just automated.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from agents.base import AgentResult, AgentStatus, BaseAgent
from agents.teams.auth import LOGGED_IN_URL_HINT, TEAMS_URL, TeamsSession
from agents.teams.parser import parse_due, parse_work_response
from storage.database import Database

logger = logging.getLogger(__name__)


class NeedsReauth(RuntimeError):
    """Raised when the "Задания" button never became visible AND the
    page's URL no longer matches LOGGED_IN_URL_HINT - i.e. Teams itself
    navigated away from the app (real behavior confirmed live, see
    _fetch_all_assignments' comment at the wait_for() call below), the
    same signal TeamsSession.is_logged_in() already uses. A human needs
    to log back in by hand (scripts/teams_login_setup.py). Deliberately
    a distinct exception from a generic RuntimeError so run() can tell a
    stale session apart from an actual UI/markup change - mirrors
    agents/valorant/agent.py's NeedsReauth, added for the same reason
    when that agent hit the identical class of problem in Phase 6."""

# Substring match on every response's URL - matches both the global
# .../edu/me/work dashboard endpoint and any per-class
# .../edu/classes/<id>/assignments endpoint the UI might use instead,
# without hard-coding the exact query string (see this module's
# docstring point 3 for why a broader net than one exact URL matters
# for an undocumented API).
_WORK_API_PATH = "/api/v1.0/edu/me/work"

_ASSIGNMENTS_BUTTON_NAME = "Задания"
_TAB_ROLE = "tab"
# The Assignments dashboard's tabs, in DOM order - confirmed via a real
# accessibility-tree dump against the live tenant (see this module's
# docstring). Index 0 ("Предстоящие"/Upcoming) fires its own work-API
# call automatically as soon as the dashboard opens, so it's not
# re-clicked. Index 4 ("Черновики"/Drafts) is teacher-only assignment
# authoring, never relevant to a student, and is skipped on purpose.
_TAB_INDEXES_TO_CLICK = (1, 2, 3)  # Готово к оценке, Просрочено, Возвращено

# --- Historical reference only, kept for scripts/teams_agent_*_test.py ---
# These are the three $filter expressions + request shape the FIRST cut
# of this agent used with a direct context.request.get() call - disproven
# by real testing (see this module's docstring, point 3/4): the work API
# needs a real UI-driven Teams-SDK handshake, not a standalone
# cookie-authenticated call. Not used by _fetch_all_assignments() below
# any more; kept only so the diagnostic scripts that found this out
# (teams_agent_smoke_test.py, _navigation_test.py, _token_test.py,
# _click_test.py) still import successfully if ever run again.
WORK_API_URL = "https://assignments.edu.cloud.microsoft/api/v1.0/edu/me/work"
_EXPAND = "submissions($expand=outcomes),categories,submissionAggregates"
_TOP = 50


def _filters(now_iso: str) -> dict[str, str]:
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
# --- end historical reference ---


# How long to wait for a new work-API response after each click before
# giving up on that particular tab and moving on - a tab that's
# genuinely empty (e.g. no overdue work right now) may still fire a
# request that just returns an empty "value" list, but a tab that never
# fires anything at all (auth hiccup, layout change) shouldn't hang the
# whole agent run.
_RESPONSE_WAIT_TIMEOUT_MS = 8_000
_RESPONSE_POLL_INTERVAL_MS = 250


class TeamsAgent(BaseAgent):
    def __init__(
        self,
        profile_dir: str,
        db: Database,
        session: TeamsSession | None = None,
        min_due_date: datetime | None = None,
    ):
        super().__init__(name="teams")
        # A pre-built TeamsSession can be injected (tests, or a caller
        # that wants to share one session across agents) - otherwise
        # this agent owns and closes its own, like WeatherAgent owns its
        # own httpx.AsyncClient unless one is passed in.
        self.session = session or TeamsSession(profile_dir)
        self._owns_session = session is None
        self.db = db
        # Assignments due before this are dropped before anything is
        # saved to the DB (config.py's TEAMS_MIN_DUE_DATE) - real data
        # showed old/stale-semester classes (an unresolved classId whose
        # "assignments" turned out to be April 2026 announcement spam)
        # would otherwise pollute /tasks and /briefing forever, since
        # nothing currently ever marks a task as gone (see parser.py's
        # docstring). None (the default) means no filtering - used by
        # tests that don't care about date cutoffs.
        self.min_due_date = min_due_date

    async def aclose(self) -> None:
        if self._owns_session:
            await self.session.close()

    @staticmethod
    async def _wait_for_new_body(captured_bodies: list, previous_count: int) -> bool:
        """Polls until captured_bodies grows past previous_count or the
        timeout elapses. Returns whether a new body actually arrived -
        callers decide whether "no new body" is fatal (the very first
        dashboard load) or just means an empty tab (later tab clicks)."""
        elapsed_ms = 0
        while elapsed_ms < _RESPONSE_WAIT_TIMEOUT_MS:
            if len(captured_bodies) > previous_count:
                return True
            await asyncio.sleep(_RESPONSE_POLL_INTERVAL_MS / 1000)
            elapsed_ms += _RESPONSE_POLL_INTERVAL_MS
        return False

    async def _fetch_all_assignments(self) -> list[dict]:
        """Drives the real Assignments dashboard UI and returns the
        merged, de-duplicated raw educationAssignment dicts (the same
        assignment can legitimately appear in more than one tab's
        response - e.g. something just submitted right before its due
        date can show up under both Upcoming and Returned in the same
        polling cycle, a real case seen in earlier captures, not a
        hypothetical)."""
        context = await self.session._ensure_context(headless=True)  # noqa: SLF001 - see auth.py
        page = await context.new_page()

        captured_bodies: list[dict] = []

        async def _on_response(response) -> None:
            if _WORK_API_PATH not in response.url:
                return
            try:
                body = await response.json()
            except Exception:  # noqa: BLE001 - not every response is JSON, and a bad
                # body here shouldn't crash the listener and silently stop
                # capturing every response after it
                return
            if isinstance(body, dict) and isinstance(body.get("value"), list):
                captured_bodies.append(body)

        page.on("response", lambda r: asyncio.create_task(_on_response(r)))

        try:
            await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=30_000)

            # Real failure seen live (2026-09-26, twice, immediately after a
            # confirmed-successful scripts/teams_login_setup.py run whose own
            # headless is_logged_in() re-check passed): a fresh, still-valid
            # session can still bounce through a full top-level redirect to
            # login.microsoftonline.com/.../authorize (Teams' MSAL client
            # silently refreshing its access token) before landing back on
            # teams.microsoft.com/v2/. TeamsSession.is_logged_in() already
            # tolerates this round trip with its own page.wait_for_url() call
            # - this method didn't: it used to go straight from goto() (which
            # can resolve mid-redirect, while page.url is still the
            # login.microsoftonline.com address) into the button check below,
            # so it was reading page.url before the redirect had a chance to
            # finish and mis-diagnosing an in-flight refresh as a dead
            # session. Give it the same chance to settle here, using the
            # exact tested pattern from is_logged_in() - if the session
            # really is dead this will simply time out and page.url will
            # still show the login domain, which the check below already
            # handles correctly.
            try:
                await page.wait_for_url(f"**{LOGGED_IN_URL_HINT}**", timeout=20_000)
            except Exception:  # noqa: BLE001 - Playwright's own TimeoutError; handled below
                pass

            assignments_button = page.get_by_role("button", name=_ASSIGNMENTS_BUTTON_NAME).first
            try:
                await assignments_button.wait_for(state="visible", timeout=15_000)
            except Exception as exc:  # noqa: BLE001 - Playwright's own TimeoutError
                # Two different real causes produce the identical
                # TimeoutError here, so page.url - the same signal
                # TeamsSession.is_logged_in() already uses - decides
                # which one actually happened, instead of guessing:
                # either Teams itself navigated away from
                # LOGGED_IN_URL_HINT and STAYED away even after the
                # wait_for_url() above gave it 20s to bounce back (a
                # genuinely stale session), or the URL matches and the
                # button itself just isn't there (a real UI change,
                # unrelated to auth).
                if LOGGED_IN_URL_HINT not in page.url:
                    raise NeedsReauth(
                        f"Teams session looks expired - after opening {TEAMS_URL} the page "
                        f"ended up at {page.url!r} (expected a URL containing "
                        f"{LOGGED_IN_URL_HINT!r}) and the \"{_ASSIGNMENTS_BUTTON_NAME}\" button "
                        "never appeared. Run scripts/teams_login_setup.py to log back in."
                    ) from exc
                raise RuntimeError(
                    f"Session looks logged in (URL {page.url!r} matched "
                    f"{LOGGED_IN_URL_HINT!r}) but the \"{_ASSIGNMENTS_BUTTON_NAME}\" button "
                    "never became visible within 15000ms - Teams' UI may have changed."
                ) from exc
            await assignments_button.click(timeout=5_000)

            # The default ("Предстоящие"/Upcoming) tab fires its own
            # work-API call as soon as the dashboard opens - if THIS
            # never arrives, something more fundamental is wrong (auth
            # expired, UI changed) and the whole run should fail rather
            # than silently return zero tasks.
            got_first_body = await self._wait_for_new_body(captured_bodies, previous_count=0)
            if not got_first_body:
                raise RuntimeError(
                    f"No response matching {_WORK_API_PATH!r} was seen within "
                    f"{_RESPONSE_WAIT_TIMEOUT_MS}ms of opening the Assignments dashboard - "
                    "session may not be logged in, or the UI has changed "
                    "(see this module's docstring for how this was verified)."
                )

            assignments_frame = next(
                (f for f in page.frames if "assignments.edu.cloud.microsoft" in f.url), None
            )
            if assignments_frame is None:
                raise RuntimeError(
                    "Assignments dashboard opened and returned data, but its "
                    "assignments.edu.cloud.microsoft iframe could not be found afterwards - "
                    "cannot click through the remaining tabs."
                )

            tabs = assignments_frame.get_by_role(_TAB_ROLE)
            for tab_index in _TAB_INDEXES_TO_CLICK:
                before_count = len(captured_bodies)
                try:
                    await tabs.nth(tab_index).click(timeout=5_000)
                except Exception as exc:  # noqa: BLE001 - one missing/unclickable tab
                    # shouldn't fail the whole run when the other tabs
                    # already got real data - log and move on.
                    logger.warning(
                        "Teams agent: could not click Assignments tab index %s (%s: %s) - "
                        "skipping, some categories may be missing from this run.",
                        tab_index, type(exc).__name__, exc,
                    )
                    continue
                got_new_body = await self._wait_for_new_body(captured_bodies, before_count)
                if not got_new_body:
                    logger.warning(
                        "Teams agent: clicked Assignments tab index %s but no new response "
                        "arrived within %sms - that category may be empty, or something's off.",
                        tab_index, _RESPONSE_WAIT_TIMEOUT_MS,
                    )
        finally:
            await page.close()

        by_id: dict[str, dict] = {}
        for body in captured_bodies:
            for raw in body.get("value", []):
                raw_id = raw.get("id")
                if raw_id:
                    by_id[raw_id] = raw  # last-write-wins de-dupe across tabs
        return list(by_id.values())

    async def run(self) -> AgentResult:
        try:
            raw_assignments = await self._fetch_all_assignments()
        except NeedsReauth as exc:
            return AgentResult(
                agent=self.name,
                status=AgentStatus.FAILING,
                error=str(exc),
                data={"needs_reauth": True},
            )
        except Exception as exc:  # noqa: BLE001 - expected failure mode (UI changed, network, etc.)
            return AgentResult(agent=self.name, status=AgentStatus.FAILING, error=f"{type(exc).__name__}: {exc}")

        now = datetime.now(timezone.utc)
        tasks = [parse_work_response({"value": [raw]}, now=now)[0] for raw in raw_assignments]

        dropped_stale = 0
        if self.min_due_date is not None:
            before_count = len(tasks)
            # A task with no due date at all is kept - there's no date to
            # judge its age by, and dropping undated items risks losing
            # something genuinely current (real captures show
            # dueDateTime is sometimes null). Only a task with a REAL,
            # parseable due date earlier than the cutoff is dropped.
            tasks = [
                t for t in tasks
                if (due := parse_due(t["due_at"])) is None or due >= self.min_due_date
            ]
            dropped_stale = before_count - len(tasks)
            if dropped_stale:
                logger.info(
                    "Teams agent: dropped %s stale task(s) due before %s",
                    dropped_stale, self.min_due_date.isoformat(),
                )

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
            data={"total": len(tasks), "dropped_stale": dropped_stale, "needs_reauth": False, **counts},
        )
