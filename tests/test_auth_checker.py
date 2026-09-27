from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agents.auth_checker import AuthCheckerAgent, SourceStatus
from agents.base import AgentStatus


def _fake_session(*, logged_in: bool | None = None, raises: Exception | None = None):
    session = AsyncMock()
    if raises is not None:
        session.is_logged_in = AsyncMock(side_effect=raises)
    else:
        session.is_logged_in = AsyncMock(return_value=logged_in)
    return session


@pytest.mark.asyncio
async def test_run_reports_ok_for_both_sources_when_logged_in(tmp_db):
    teams = _fake_session(logged_in=True)
    sso = _fake_session(logged_in=True)
    agent = AuthCheckerAgent(db=tmp_db, teams_session=teams, sso_session=sso)

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["teams"] == {"status": "OK", "error": None}
    assert result.data["sso"] == {"status": "OK", "error": None}

    assert tmp_db.get_source_status("teams")["status"] == "OK"
    assert tmp_db.get_source_status("sso")["status"] == "OK"


@pytest.mark.asyncio
async def test_run_reports_auth_required_when_is_logged_in_returns_false(tmp_db):
    teams = _fake_session(logged_in=False)
    sso = _fake_session(logged_in=True)
    agent = AuthCheckerAgent(db=tmp_db, teams_session=teams, sso_session=sso)

    result = await agent.run()

    assert result.status == AgentStatus.WORKING  # AUTH_REQUIRED is an expected outcome, not a bug
    assert result.data["teams"] == {"status": "AUTH_REQUIRED", "error": None}
    assert tmp_db.get_source_status("teams")["status"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_run_reports_error_and_degraded_when_the_check_itself_raises(tmp_db):
    teams = _fake_session(raises=RuntimeError("browser crashed"))
    sso = _fake_session(logged_in=True)
    agent = AuthCheckerAgent(db=tmp_db, teams_session=teams, sso_session=sso)

    result = await agent.run()

    assert result.status == AgentStatus.DEGRADED
    assert result.data["teams"]["status"] == "ERROR"
    assert "browser crashed" in result.data["teams"]["error"]
    row = tmp_db.get_source_status("teams")
    assert row["status"] == "ERROR"
    assert "browser crashed" in row["last_error"]
    # The other source's own status must be unaffected by teams' failure.
    assert result.data["sso"] == {"status": "OK", "error": None}


@pytest.mark.asyncio
async def test_run_reports_unavailable_when_a_session_was_never_wired_up(tmp_db):
    sso = _fake_session(logged_in=True)
    agent = AuthCheckerAgent(db=tmp_db, teams_session=None, sso_session=sso)

    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["teams"]["status"] == "UNAVAILABLE"
    assert tmp_db.get_source_status("teams")["status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_run_never_calls_login_methods_on_either_session(tmp_db):
    # Auth Checker must only ever observe - recovering a session is the
    # owning source agent's job (bro's ТЗ, Auth Checker step, point 10).
    teams = _fake_session(logged_in=False)
    sso = _fake_session(logged_in=False)
    agent = AuthCheckerAgent(db=tmp_db, teams_session=teams, sso_session=sso)

    await agent.run()

    teams.login_interactively.assert_not_called()
    sso.login_interactively.assert_not_called()
    sso.login_via_autofill.assert_not_called()


def test_source_status_enum_has_all_five_required_values():
    assert {s.value for s in SourceStatus} == {
        "OK", "AUTH_REQUIRED", "ERROR", "UNAVAILABLE", "UNKNOWN",
    }
