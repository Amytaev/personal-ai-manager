"""SSO Agent (Этап B) - Schedule + UMKD -> normalized data -> DB.

Unlike TeamsAgent/ValorantAgent (agents/teams/agent.py, agents/valorant/
agent.py), which have to drive real UI clicks and capture the page's own
network responses because their target APIs turned out to be
UI-handshake-gated (Teams) or HTML-only (VALORANT/Stack B), the real
research capture for this system (scripts/sso_capture_data.py's Этап A
run, 2026-09-27 - see agents/sso/parser.py's docstring for the full
endpoint list) found a clean, cookie-authenticated JSON API for
everything needed here. So this agent makes direct
``context.request.get()`` calls against that API - the persistent
profile's own cookies authenticate them, no UI clicking required.

A single light ``page.goto()`` to the schedule page still happens first
(SCHEDULE_PAGE_URL) before any API call - not because it's technically
required (the API calls authenticate via cookies regardless), but to
keep this agent's traffic shaped like a normal signed-in visit rather
than a cookie-jar-only script hitting internal endpoints cold, matching
this project's standing "look like a normal browser session, not an
evasion technique" discipline (TZ v4 §3.4).

Read-only by design (TZ v4 requirement for this stage): only GET calls
are made, and Umkd/Download (the actual file bytes) is never called at
all - see agents/sso/parser.py's docstring for why.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from agents.base import AgentResult, AgentStatus, BaseAgent
from agents.sso.auth import API_BASE, SCHEDULE_PAGE_URL, STUD_BASE, SsoSession
from agents.sso.parser import (
    build_umkd_path_by_folder_id,
    iter_umkd_leaf_folders,
    parse_courses,
    parse_materials,
    parse_schedule,
    pick_current_semester_id,
)
from storage.database import Database

logger = logging.getLogger(__name__)

_SEMESTERS_URL = f"{STUD_BASE}/api/ScheduleTable/GetCurrentAndAvailableSemesters"
_DISCIPLINES_URL = f"{STUD_BASE}/api/ScheduleTable/GetDesciplines"
_TABLE_URL = f"{STUD_BASE}/api/ScheduleTable/GetTable"
_FOLDERS_URL = f"{API_BASE}/api/Umkd/GetFoldersForStudent"
_FOLDER_CONTENT_URL = f"{API_BASE}/api/Umkd/GetFolderContent"

_REQUEST_TIMEOUT_MS = 15_000


class NeedsReauth(RuntimeError):
    """Raised when SsoSession.is_logged_in() reports the saved session
    is no longer valid. A human needs to log back in by hand
    (scripts/sso_login_setup.py). Deliberately a distinct exception from
    a generic RuntimeError, mirroring agents/teams/agent.py's and
    agents/valorant/agent.py's NeedsReauth, for the same reason: run()
    needs to tell a stale session apart from an actual API/shape
    problem."""


class SsoAgent(BaseAgent):
    def __init__(
        self,
        profile_dir: str,
        db: Database,
        session: SsoSession | None = None,
    ):
        super().__init__(name="sso")
        # A pre-built SsoSession can be injected (tests, or a caller that
        # wants to share one session) - otherwise this agent owns and
        # closes its own, same pattern as TeamsAgent/ValorantAgent.
        self.session = session or SsoSession(profile_dir)
        self._owns_session = session is None
        self.db = db

    async def aclose(self) -> None:
        if self._owns_session:
            await self.session.close()

    @staticmethod
    async def _get_json(context, url: str):
        response = await context.request.get(url, timeout=_REQUEST_TIMEOUT_MS)
        if not response.ok:
            raise RuntimeError(f"GET {url} returned HTTP {response.status}")
        return await response.json()

    async def _fetch_normalized_data(self) -> tuple[int, list[dict], list[dict], list[dict]]:
        """Returns (semester_id, courses, schedule_entries, materials).
        Raises NeedsReauth if the saved session has expired, or a plain
        RuntimeError if the session looks valid but the API shape wasn't
        what was expected (endpoint changed)."""
        context = await self.session._ensure_context(headless=True)  # noqa: SLF001 - see auth.py
        page = await context.new_page()
        try:
            await page.goto(SCHEDULE_PAGE_URL, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001 - proceed either way; the API check below is authoritative
                pass
        finally:
            await page.close()

        if not await self.session.is_logged_in():
            raise NeedsReauth(
                "SSO session looks expired - Auth/IsAuthenticated did not report a logged-in "
                "session. Run scripts/sso_login_setup.py to log back in."
            )

        raw_semesters = await self._get_json(context, _SEMESTERS_URL)
        semester_id = pick_current_semester_id(raw_semesters)
        if semester_id is None:
            raise RuntimeError(
                "GetCurrentAndAvailableSemesters returned no semesters - cannot fetch a "
                "schedule/discipline list without a semesterId."
            )

        raw_disciplines = await self._get_json(
            context, f"{_DISCIPLINES_URL}?semesterId={semester_id}"
        )
        raw_table = await self._get_json(context, f"{_TABLE_URL}?semesterId={semester_id}")
        raw_folders = await self._get_json(context, _FOLDERS_URL)

        courses = parse_courses(raw_disciplines)
        schedule_entries = parse_schedule(raw_table)

        leaf_folders = iter_umkd_leaf_folders(raw_folders)
        path_by_folder_id = build_umkd_path_by_folder_id(leaf_folders)

        materials: list[dict] = []
        for leaf in leaf_folders:
            folder_id = leaf["folder_id"]
            try:
                raw_files = await self._get_json(
                    context, f"{_FOLDER_CONTENT_URL}?folderId={quote(str(folder_id))}"
                )
            except Exception as exc:  # noqa: BLE001 - one bad folder shouldn't fail the whole run
                logger.warning(
                    "SSO agent: could not fetch UMKD folder %s (%s: %s) - skipping.",
                    folder_id, type(exc).__name__, exc,
                )
                continue
            materials.extend(parse_materials(folder_id, raw_files, path_by_folder_id.get(folder_id)))

        return semester_id, courses, schedule_entries, materials

    async def run(self) -> AgentResult:
        try:
            semester_id, courses, schedule_entries, materials = await self._fetch_normalized_data()
        except NeedsReauth as exc:
            return AgentResult(
                agent=self.name,
                status=AgentStatus.FAILING,
                error=str(exc),
                data={"needs_reauth": True},
            )
        except Exception as exc:  # noqa: BLE001 - expected failure mode (network, timeout, API shape changed)
            return AgentResult(agent=self.name, status=AgentStatus.FAILING, error=f"{type(exc).__name__}: {exc}")

        self.db.save_sso_snapshot(
            semester_id=semester_id,
            courses=courses,
            schedule_entries=schedule_entries,
            materials=materials,
        )

        logger.info(
            "SSO agent: semester %s - %s course(s), %s schedule entr(y/ies), %s material(s)",
            semester_id, len(courses), len(schedule_entries), len(materials),
        )
        return AgentResult(
            agent=self.name,
            status=AgentStatus.WORKING,
            data={
                "semester_id": semester_id,
                "courses": len(courses),
                "schedule_entries": len(schedule_entries),
                "materials": len(materials),
                "needs_reauth": False,
            },
        )
