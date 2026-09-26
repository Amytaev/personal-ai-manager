from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import AgentStatus
from agents.teams import agent as agent_module
from agents.teams.agent import TeamsAgent


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    # The real agent polls every _RESPONSE_POLL_INTERVAL_MS up to
    # _RESPONSE_WAIT_TIMEOUT_MS waiting for a network response - shrink
    # both so tests that exercise the "nothing ever arrived" path don't
    # actually burn several real seconds asleep.
    monkeypatch.setattr(agent_module, "_RESPONSE_POLL_INTERVAL_MS", 1)
    monkeypatch.setattr(agent_module, "_RESPONSE_WAIT_TIMEOUT_MS", 20)


def _assignment(id_: str, class_id: str, **overrides) -> dict:
    base = {
        "id": id_,
        "classId": class_id,
        "displayName": f"Задание {id_}",
        "dueDateTime": "2026-10-03T14:30:00Z",
        "createdDateTime": "2026-09-14T04:45:48.682Z",
        "isCompleted": False,
        "instructions": None,
        "submissions": [{"submittedDateTime": None, "returnedDateTime": None}],
    }
    base.update(overrides)
    return base


def _fake_response(body: dict):
    response = MagicMock()
    response.url = "https://assignments.edu.cloud.microsoft/api/v1.0/edu/me/work?%24filter=..."
    response.json = AsyncMock(return_value=body)
    return response


class _Env:
    """Fakes just enough of Playwright's Page/Frame/Locator API to drive
    TeamsAgent's real click -> listen-for-response -> click-tabs flow.

    ``tab_bodies`` maps a tab index (0 = the default tab that fires its
    own work-API response automatically once the dashboard opens, 1/2/3
    = the tabs the agent clicks through, matching
    agents/teams/agent.py's _TAB_INDEXES_TO_CLICK) to the response body
    that "clicking" (or, for index 0, just opening the dashboard)
    should deliver through the captured page.on("response", ...)
    handler - mirroring how Teams' real UI fires a network response
    after each interaction. A tab index with no entry delivers nothing,
    simulating an empty category or an unclickable/missing tab.
    """

    def __init__(
        self,
        tab_bodies: dict[int, dict] | None = None,
        has_assignments_frame: bool = True,
        failing_tab_indexes: tuple[int, ...] = (),
    ):
        self.tab_bodies = tab_bodies or {}
        self.failing_tab_indexes = failing_tab_indexes
        self._response_handler = None

        self.page = MagicMock()
        self.page.goto = AsyncMock()
        self.page.close = AsyncMock()
        self.page.on = MagicMock(side_effect=self._capture_handler)

        button_locator = MagicMock()
        button_locator.wait_for = AsyncMock()
        button_locator.click = AsyncMock(side_effect=self._make_click_side_effect(0))
        role_locator = MagicMock()
        role_locator.first = button_locator
        self.page.get_by_role = MagicMock(return_value=role_locator)

        if has_assignments_frame:
            top_frame = MagicMock()
            top_frame.url = "https://teams.cloud.microsoft/"
            assignments_frame = MagicMock()
            assignments_frame.url = "https://assignments.edu.cloud.microsoft/classes/all/list"

            tab_locators = {}
            for i in (1, 2, 3):
                loc = MagicMock()
                loc.click = AsyncMock(side_effect=self._make_click_side_effect(i))
                tab_locators[i] = loc
            tabs_locator = MagicMock()
            tabs_locator.nth = MagicMock(side_effect=lambda i: tab_locators[i])
            assignments_frame.get_by_role = MagicMock(return_value=tabs_locator)

            self.page.frames = [top_frame, assignments_frame]
        else:
            top_frame = MagicMock()
            top_frame.url = "https://teams.cloud.microsoft/"
            self.page.frames = [top_frame]

        self.context = MagicMock()
        self.context.new_page = AsyncMock(return_value=self.page)

        self.session = MagicMock()
        self.session._ensure_context = AsyncMock(return_value=self.context)
        self.session.close = AsyncMock()

    def _capture_handler(self, event_name, handler):
        if event_name == "response":
            self._response_handler = handler

    def _make_click_side_effect(self, tab_index: int):
        async def _side_effect(*args, **kwargs):
            if tab_index in self.failing_tab_indexes:
                raise RuntimeError(f"simulated click failure on tab {tab_index}")
            body = self.tab_bodies.get(tab_index)
            if body is not None and self._response_handler is not None:
                self._response_handler(_fake_response(body))

        return _side_effect


@pytest.mark.asyncio
async def test_run_reports_default_tab_only(tmp_db):
    env = _Env(tab_bodies={0: {"value": [_assignment("a1", "e3f75118-bae6-4636-87c7-b2e91b63f913")]}})
    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["total"] == 1
    assert result.data["new"] == 1


@pytest.mark.asyncio
async def test_run_merges_assignments_from_multiple_tabs(tmp_db):
    env = _Env(
        tab_bodies={
            0: {"value": [_assignment("a1", "e3f75118-bae6-4636-87c7-b2e91b63f913")]},
            2: {"value": [_assignment("a2", "162b0fa2-6461-4eb0-aef3-da996e67e7e7")]},  # Просрочено
        }
    )
    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.data["total"] == 2
    with tmp_db.connect() as conn:
        courses = {r["course"] for r in conn.execute("SELECT course FROM tasks").fetchall()}
    assert len(courses) == 2


@pytest.mark.asyncio
async def test_run_deduplicates_an_assignment_seen_in_two_tabs(tmp_db):
    # A just-submitted item due soon can legitimately show up under both
    # Upcoming and Returned in the same run (seen in real captures).
    shared = _assignment("dup-1", "e3f75118-bae6-4636-87c7-b2e91b63f913")
    env = _Env(tab_bodies={0: {"value": [shared]}, 3: {"value": [shared]}})  # Возвращено
    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.data["total"] == 1
    with tmp_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert count == 1


@pytest.mark.asyncio
async def test_run_marks_new_then_unchanged_then_changed_across_runs(tmp_db):
    assignment = _assignment("a1", "e3f75118-bae6-4636-87c7-b2e91b63f913")

    env1 = _Env(tab_bodies={0: {"value": [assignment]}})
    agent1 = TeamsAgent(profile_dir="unused", db=tmp_db, session=env1.session)
    result1 = await agent1.run()
    assert result1.data["new"] == 1

    env2 = _Env(tab_bodies={0: {"value": [assignment]}})
    agent2 = TeamsAgent(profile_dir="unused", db=tmp_db, session=env2.session)
    result2 = await agent2.run()
    assert result2.data["unchanged"] == 1
    assert result2.data["new"] == 0

    moved = _assignment("a1", "e3f75118-bae6-4636-87c7-b2e91b63f913", dueDateTime="2026-11-01T14:30:00Z")
    env3 = _Env(tab_bodies={0: {"value": [moved]}})
    agent3 = TeamsAgent(profile_dir="unused", db=tmp_db, session=env3.session)
    result3 = await agent3.run()
    assert result3.data["changed"] == 1

    with tmp_db.connect() as conn:
        row = conn.execute("SELECT state, due_at FROM tasks WHERE id = 'a1'").fetchone()
    assert row["state"] == "changed"
    assert row["due_at"] == "2026-11-01T14:30:00Z"


@pytest.mark.asyncio
async def test_run_reports_failing_when_no_default_response_arrives(tmp_db):
    # Nothing ever fires for tab index 0 - simulates an expired/broken
    # session where opening the dashboard never produces real data.
    env = _Env(tab_bodies={})
    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert "RuntimeError" in result.error


@pytest.mark.asyncio
async def test_run_reports_failing_when_assignments_frame_is_missing(tmp_db):
    # The default tab's response DOES arrive, but the assignments
    # iframe can't be found afterwards - can't click through the rest.
    env = _Env(tab_bodies={0: {"value": []}}, has_assignments_frame=False)
    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert "iframe" in result.error


@pytest.mark.asyncio
async def test_run_continues_when_one_tab_click_fails(tmp_db):
    # Tab index 2 ("Просрочено") is unclickable for some reason, but the
    # default tab and the other clickable tabs still have real data -
    # one bad tab shouldn't fail the whole run.
    env = _Env(
        tab_bodies={
            0: {"value": [_assignment("a1", "e3f75118-bae6-4636-87c7-b2e91b63f913")]},
            3: {"value": [_assignment("a2", "162b0fa2-6461-4eb0-aef3-da996e67e7e7")]},
        },
        failing_tab_indexes=(2,),
    )
    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["total"] == 2


@pytest.mark.asyncio
async def test_aclose_only_closes_a_session_it_owns(tmp_db):
    env = _Env(tab_bodies={})

    injected_agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=env.session)
    await injected_agent.aclose()
    env.session.close.assert_not_awaited()
