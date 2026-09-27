from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import AgentStatus
from agents.sso.agent import SsoAgent
from agents.sso.auth import API_BASE, STUD_BASE, SsoAutofillLoginFailed

_SEMESTERS_URL = f"{STUD_BASE}/api/ScheduleTable/GetCurrentAndAvailableSemesters"
_DISCIPLINES_URL = f"{STUD_BASE}/api/ScheduleTable/GetDesciplines"
_TABLE_URL = f"{STUD_BASE}/api/ScheduleTable/GetTable"
_FOLDERS_URL = f"{API_BASE}/api/Umkd/GetFoldersForStudent"
_FOLDER_CONTENT_URL = f"{API_BASE}/api/Umkd/GetFolderContent"


def _api_response(ok: bool, body):
    response = MagicMock()
    response.ok = ok
    response.status = 200 if ok else 500
    response.json = AsyncMock(return_value=body)
    return response


class _Env:
    """Fakes just enough of Playwright's BrowserContext/APIRequestContext
    API to drive SsoAgent's real light-goto -> is_logged_in ->
    context.request.get(...) flow (agents/sso/agent.py).

    ``responses`` maps an exact URL to the JSON body context.request.get
    should return for it; a URL not present in ``responses`` gets a
    404-shaped failure. ``logged_in`` is what session.is_logged_in()
    reports.
    """

    def __init__(
        self,
        responses: dict[str, object] | None = None,
        logged_in: bool = True,
        autofill_succeeds: bool = False,
        autofill_error: str = "saved credentials are stale",
    ):
        self.responses = responses or {}

        self.page = MagicMock()
        self.page.goto = AsyncMock()
        self.page.close = AsyncMock()
        self.page.wait_for_load_state = AsyncMock()

        self.request = MagicMock()
        self.request.get = AsyncMock(side_effect=self._get)

        self.context = MagicMock()
        self.context.new_page = AsyncMock(return_value=self.page)
        self.context.request = self.request

        self.session = MagicMock()
        self.session._ensure_context = AsyncMock(return_value=self.context)
        self.session.is_logged_in = AsyncMock(return_value=logged_in)
        self.session.close = AsyncMock()
        # agents/sso/agent.py tries login_via_autofill() automatically
        # before giving up with NeedsReauth - autofill_succeeds controls
        # which of those two paths a "not logged in" test exercises.
        self.session.login_via_autofill = AsyncMock()
        if autofill_succeeds:
            self.session.login_via_autofill.return_value = True
        else:
            self.session.login_via_autofill.side_effect = SsoAutofillLoginFailed(autofill_error)

    async def _get(self, url, timeout=None):
        if url in self.responses:
            return _api_response(True, self.responses[url])
        return _api_response(False, None)


def _semesters() -> list[dict]:
    return [{"id": 85, "title": "Осень 2026-27", "isCurrentSemester": 1}]


def _disciplines() -> list[dict]:
    return [{"code": "CSE5472", "title": "НИРС", "totalCredits": 3}]


def _table() -> dict:
    return {
        "columns": [
            {
                "title": "MONDAY_SHORT",
                "containers": [
                    {
                        "time": {"start": {"title": "8:55"}, "end": {"title": "9:45"}},
                        "lessons": [
                            {
                                "classId": 222374,
                                "courseCode": "CSE5472",
                                "courseTitle": "НИРС",
                                "instructorName": "Сербин В.В.",
                                "roomTitle": "on line2",
                                "classType": 3,
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _folders() -> list[dict]:
    return [
        {
            "id": -1,
            "title": "CSE5472 НИРС",
            "nodes": [
                {
                    "id": -1,
                    "title": "Сербин В.В.",
                    "nodes": [{"id": 490059, "title": "Практика", "nodes": []}],
                }
            ],
        }
    ]


def _folder_content() -> list[dict]:
    return [{"fileId": 1, "fileName": "Практика 1.docx", "fileCategoryTitle": "Практика"}]


def _full_responses() -> dict[str, object]:
    return {
        _SEMESTERS_URL: _semesters(),
        f"{_DISCIPLINES_URL}?semesterId=85": _disciplines(),
        f"{_TABLE_URL}?semesterId=85": _table(),
        _FOLDERS_URL: _folders(),
        f"{_FOLDER_CONTENT_URL}?folderId=490059": _folder_content(),
    }


@pytest.mark.asyncio
async def test_run_reports_working_and_saves_the_full_snapshot(tmp_db):
    env = _Env(responses=_full_responses())
    agent = SsoAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["needs_reauth"] is False
    assert result.data["semester_id"] == 85
    assert result.data["courses"] == 1
    assert result.data["schedule_entries"] == 1
    assert result.data["materials"] == 1

    with tmp_db.connect() as conn:
        course = conn.execute("SELECT * FROM sso_courses WHERE semester_id = 85").fetchone()
        entry = conn.execute("SELECT * FROM sso_schedule_entries WHERE semester_id = 85").fetchone()
        material = conn.execute("SELECT * FROM sso_study_materials").fetchone()
    assert course["code"] == "CSE5472"
    assert entry["class_id"] == 222374
    assert material["file_name"] == "Практика 1.docx"
    # Never stores anything IIN/DOB-shaped - this agent never even calls
    # user/getuserinfo (agents/sso/parser.py's docstring), so there's no
    # such column to check either way; this asserts the material row has
    # exactly the normalized shape, nothing extra smuggled in.
    assert set(material.keys()) == {
        "file_id", "folder_id", "file_name", "file_category_title",
        "course_title", "instructor_name", "updated_at",
    }


@pytest.mark.asyncio
async def test_run_reports_needs_reauth_when_not_logged_in_and_autofill_also_fails(tmp_db):
    env = _Env(responses=_full_responses(), logged_in=False, autofill_succeeds=False)
    agent = SsoAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert result.data["needs_reauth"] is True
    assert "sso_login_setup" in result.error
    # The automatic autofill re-auth is tried FIRST, before falling back
    # to telling the person to log in by hand.
    env.session.login_via_autofill.assert_awaited_once()

    with tmp_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM sso_courses").fetchone()["c"]
    assert count == 0


@pytest.mark.asyncio
async def test_run_recovers_automatically_via_autofill_when_session_expired(tmp_db, monkeypatch):
    # login_via_autofill() defaults to a headed context - switching back
    # to headless afterward has a short real asyncio.sleep(3) in
    # agents/sso/agent.py to avoid a cookie-flush race; mocked out here
    # so this test doesn't actually wait 3 real seconds.
    monkeypatch.setattr("agents.sso.agent.asyncio.sleep", AsyncMock())
    env = _Env(responses=_full_responses(), logged_in=False, autofill_succeeds=True)
    agent = SsoAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["needs_reauth"] is False
    assert result.data["semester_id"] == 85
    env.session.login_via_autofill.assert_awaited_once()

    with tmp_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM sso_courses").fetchone()["c"]
    assert count == 1


@pytest.mark.asyncio
async def test_run_does_not_attempt_autofill_when_already_logged_in(tmp_db):
    env = _Env(responses=_full_responses(), logged_in=True)
    agent = SsoAgent(profile_dir="unused", db=tmp_db, session=env.session)

    await agent.run()

    env.session.login_via_autofill.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_reports_failing_when_no_semesters_available(tmp_db):
    env = _Env(responses={_SEMESTERS_URL: []})
    agent = SsoAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert result.data.get("needs_reauth") is not True


@pytest.mark.asyncio
async def test_run_skips_a_folder_that_fails_to_fetch_but_keeps_the_rest(tmp_db):
    responses = _full_responses()
    # A second leaf folder that 404s - the run should still succeed with
    # the first folder's materials, just skipping the broken one.
    responses[_FOLDERS_URL] = [
        {
            "id": -1,
            "title": "Course",
            "nodes": [
                {
                    "id": -1,
                    "title": "Teacher",
                    "nodes": [
                        {"id": 490059, "title": "Практика", "nodes": []},
                        {"id": 999999, "title": "Broken", "nodes": []},
                    ],
                }
            ],
        }
    ]
    env = _Env(responses=responses)
    agent = SsoAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["materials"] == 1  # only the folder that resolved


@pytest.mark.asyncio
async def test_run_treats_unexpected_exception_as_plain_failing(tmp_db):
    env = _Env(responses=_full_responses())
    env.session._ensure_context = AsyncMock(side_effect=RuntimeError("boom"))
    agent = SsoAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert result.data.get("needs_reauth") is not True
    assert "boom" in result.error


@pytest.mark.asyncio
async def test_aclose_only_closes_a_session_it_owns(tmp_db):
    env = _Env(responses=_full_responses())
    injected_agent = SsoAgent(profile_dir="unused", db=tmp_db, session=env.session)
    await injected_agent.aclose()
    env.session.close.assert_not_awaited()
