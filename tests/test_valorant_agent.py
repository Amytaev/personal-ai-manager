from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import AgentStatus
from agents.valorant import agent as agent_module
from agents.valorant.agent import ValorantAgent


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    # Mirrors tests/test_teams_agent.py's _fast_polling fixture - shrink
    # the real agent's poll timeout/interval so the "nothing ever
    # arrived" path doesn't burn real seconds asleep in tests.
    monkeypatch.setattr(agent_module, "_RESPONSE_POLL_INTERVAL_MS", 1)
    monkeypatch.setattr(agent_module, "_RESPONSE_WAIT_TIMEOUT_MS", 20)


def _storefront_component(html: str, time_left: str | None = "7 часов и 49 минут") -> dict:
    """One Livewire component dict shaped like the real captured
    "storefront" component response (Phase 6.2 investigation) - compact
    (no-space) JSON separators on purpose, matching real PHP/Laravel
    json_encode output and agents/valorant/agent.py's
    _STOREFRONT_COMPONENT_MARKER substring check."""
    snapshot = json.dumps(
        {
            "data": {"timeLeft": time_left},
            "memo": {"name": "storefront", "path": "riot/storefront"},
        },
        separators=(",", ":"),
    )
    return {"snapshot": snapshot, "effects": {"html": html}}


def _other_component(name: str) -> dict:
    """A Livewire component response for some OTHER component (e.g.
    "live-comments", seen constantly in real capture traffic on the same
    page) - the agent must ignore these."""
    snapshot = json.dumps({"data": {}, "memo": {"name": name}}, separators=(",", ":"))
    return {"snapshot": snapshot, "effects": {"html": "<div>irrelevant</div>"}}


def _fake_response(url: str, body: dict):
    response = MagicMock()
    response.url = url
    response.json = AsyncMock(return_value=body)
    return response


class _Env:
    """Fakes just enough of Playwright's Page API to drive
    ValorantAgent's real goto -> listen-for-response flow - mirrors
    tests/test_teams_agent.py's _Env.

    ``responses`` is a list of (url, body) pairs "fired" on the page
    right after goto() resolves (simulating the page's own JS firing its
    network requests once loaded). ``final_url`` is what page.url reads
    afterwards (simulating a same-navigation redirect, e.g. to /login).
    """

    def __init__(self, responses: list[tuple[str, dict]] | None = None, final_url: str = "https://stackb.net/riot/storefront"):
        self.responses = responses or []
        self._response_handler = None

        self.page = MagicMock()
        self.page.url = final_url
        self.page.goto = AsyncMock(side_effect=self._fire_responses)
        self.page.close = AsyncMock()
        self.page.on = MagicMock(side_effect=self._capture_handler)

        self.context = MagicMock()
        self.context.new_page = AsyncMock(return_value=self.page)

        self.session = MagicMock()
        self.session._ensure_context = AsyncMock(return_value=self.context)
        self.session.close = AsyncMock()

    def _capture_handler(self, event_name, handler):
        if event_name == "response":
            self._response_handler = handler

    async def _fire_responses(self, *args, **kwargs):
        for url, body in self.responses:
            if self._response_handler is not None:
                self._response_handler(_fake_response(url, body))


@pytest.mark.asyncio
async def test_run_reports_working_and_saves_items_on_success(tmp_db, monkeypatch):
    html = "<div>4 items here, real parsing is agents/valorant/parser.py's job - stubbed via monkeypatch below</div>"
    env = _Env(responses=[("https://stackb.net/livewire/update", {"components": [_storefront_component(html)]})])
    agent = ValorantAgent(profile_dir="unused", db=tmp_db, session=env.session)

    fake_items = [{"uuid": "u1", "name": "Апертура", "price_vp": 1275, "image_url": "x"}]
    monkeypatch.setattr(agent_module, "parse_storefront_items", lambda h: fake_items if h == html else [])

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["items"] == fake_items
    assert result.data["reset_in"] == "7 часов и 49 минут"
    assert result.data["needs_reauth"] is False

    with tmp_db.connect() as conn:
        row = conn.execute("SELECT skins_json FROM valorant_store ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert json.loads(row["skins_json"])["items"] == fake_items


@pytest.mark.asyncio
async def test_run_ignores_unrelated_livewire_components(tmp_db, monkeypatch):
    html = "<div>real content</div>"
    env = _Env(
        responses=[
            ("https://stackb.net/livewire/update", {"components": [_other_component("live-comments")]}),
            ("https://stackb.net/livewire/update", {"components": [_storefront_component(html)]}),
        ]
    )
    agent = ValorantAgent(profile_dir="unused", db=tmp_db, session=env.session)
    fake_items = [{"uuid": "u1", "name": "X", "price_vp": 1, "image_url": "x"}]
    monkeypatch.setattr(agent_module, "parse_storefront_items", lambda h: fake_items if h == html else [])

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["items"] == fake_items


@pytest.mark.asyncio
async def test_run_reports_needs_reauth_when_redirected_to_login(tmp_db):
    env = _Env(responses=[], final_url="https://stackb.net/login")
    agent = ValorantAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert result.data["needs_reauth"] is True
    assert "login" in result.error.lower() or "session" in result.error.lower()

    with tmp_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM valorant_store").fetchone()["c"]
    assert count == 0


@pytest.mark.asyncio
async def test_run_reports_failing_when_nothing_arrives_and_session_looks_valid(tmp_db):
    # Session is fine (no /login redirect) but no storefront component
    # response ever showed up - the page's own loading behavior changed.
    env = _Env(responses=[], final_url="https://stackb.net/riot/storefront")
    agent = ValorantAgent(profile_dir="unused", db=tmp_db, session=env.session)

    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert result.data.get("needs_reauth") is not True


@pytest.mark.asyncio
async def test_run_reports_parse_error_when_zero_items_found(tmp_db, monkeypatch):
    html = "<div>markup changed, nothing recognizable</div>"
    env = _Env(responses=[("https://stackb.net/livewire/update", {"components": [_storefront_component(html)]})])
    agent = ValorantAgent(profile_dir="unused", db=tmp_db, session=env.session)
    monkeypatch.setattr(agent_module, "parse_storefront_items", lambda h: [])

    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert "VALORANT_STORE_PARSE_ERROR" in result.error
    with tmp_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM valorant_store").fetchone()["c"]
    assert count == 0


@pytest.mark.asyncio
async def test_aclose_only_closes_a_session_it_owns(tmp_db):
    env = _Env(responses=[])
    injected_agent = ValorantAgent(profile_dir="unused", db=tmp_db, session=env.session)
    await injected_agent.aclose()
    env.session.close.assert_not_awaited()
