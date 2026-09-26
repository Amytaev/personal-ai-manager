from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import AgentStatus
from agents.teams.agent import TeamsAgent


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


def _mock_response(status: int = 200, body: dict | None = None):
    response = MagicMock()
    response.ok = 200 <= status < 300
    response.status = status
    response.json = AsyncMock(return_value=body or {"value": []})
    return response


def _mock_session(responses_by_call):
    """A fake TeamsSession whose context.request.get() returns
    ``responses_by_call`` in order, one per call (one per $filter query -
    upcoming/overdue/completed, in that order per agents/teams/agent.py's
    _filters())."""
    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_page.close = AsyncMock()

    mock_context = MagicMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.request = MagicMock()
    mock_context.request.get = AsyncMock(side_effect=responses_by_call)

    mock_session = MagicMock()
    mock_session._ensure_context = AsyncMock(return_value=mock_context)
    mock_session.close = AsyncMock()
    return mock_session, mock_context


@pytest.mark.asyncio
async def test_run_reports_multiple_courses(tmp_db):
    upcoming = _mock_response(200, {"value": [_assignment("a1", "e3f75118-bae6-4636-87c7-b2e91b63f913")]})
    overdue = _mock_response(200, {"value": [_assignment("a2", "162b0fa2-6461-4eb0-aef3-da996e67e7e7")]})
    completed = _mock_response(200, {"value": []})
    session, _ = _mock_session([upcoming, overdue, completed])

    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=session)
    result = await agent.run()

    assert result.status == AgentStatus.WORKING
    assert result.data["total"] == 2
    assert result.data["new"] == 2

    with tmp_db.connect() as conn:
        rows = conn.execute("SELECT course FROM tasks ORDER BY id").fetchall()
    courses = {r["course"] for r in rows}
    assert len(courses) == 2


@pytest.mark.asyncio
async def test_run_deduplicates_an_assignment_seen_in_two_filters(tmp_db):
    # The real API can (and does) return the same assignment from more
    # than one $filter query - e.g. something just submitted right
    # before its due date can satisfy both "upcoming" and part of the
    # "completed" OR-clause in the same polling cycle.
    shared = _assignment("dup-1", "e3f75118-bae6-4636-87c7-b2e91b63f913")
    upcoming = _mock_response(200, {"value": [shared]})
    overdue = _mock_response(200, {"value": []})
    completed = _mock_response(200, {"value": [shared]})
    session, _ = _mock_session([upcoming, overdue, completed])

    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=session)
    result = await agent.run()

    assert result.data["total"] == 1
    with tmp_db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert count == 1


@pytest.mark.asyncio
async def test_run_marks_new_then_unchanged_then_changed_across_runs(tmp_db):
    assignment = _assignment("a1", "e3f75118-bae6-4636-87c7-b2e91b63f913")
    empty = _mock_response(200, {"value": []})

    # First run: brand new task.
    session1, _ = _mock_session(
        [_mock_response(200, {"value": [assignment]}), empty, empty]
    )
    agent1 = TeamsAgent(profile_dir="unused", db=tmp_db, session=session1)
    result1 = await agent1.run()
    assert result1.data["new"] == 1
    assert result1.data["changed"] == 0

    with tmp_db.connect() as conn:
        state = conn.execute("SELECT state FROM tasks WHERE id = 'a1'").fetchone()["state"]
    assert state == "new"

    # Second run: identical data -> unchanged.
    session2, _ = _mock_session(
        [_mock_response(200, {"value": [assignment]}), empty, empty]
    )
    agent2 = TeamsAgent(profile_dir="unused", db=tmp_db, session=session2)
    result2 = await agent2.run()
    assert result2.data["unchanged"] == 1
    assert result2.data["new"] == 0

    # Third run: due date changed -> changed.
    moved = _assignment("a1", "e3f75118-bae6-4636-87c7-b2e91b63f913", dueDateTime="2026-11-01T14:30:00Z")
    session3, _ = _mock_session(
        [_mock_response(200, {"value": [moved]}), empty, empty]
    )
    agent3 = TeamsAgent(profile_dir="unused", db=tmp_db, session=session3)
    result3 = await agent3.run()
    assert result3.data["changed"] == 1

    with tmp_db.connect() as conn:
        row = conn.execute("SELECT state, due_at FROM tasks WHERE id = 'a1'").fetchone()
    assert row["state"] == "changed"
    assert row["due_at"] == "2026-11-01T14:30:00Z"


@pytest.mark.asyncio
async def test_run_reports_failing_on_non_ok_response(tmp_db):
    session, _ = _mock_session([_mock_response(401, {}), _mock_response(200, {"value": []}), _mock_response(200, {"value": []})])

    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=session)
    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert "401" in result.error


@pytest.mark.asyncio
async def test_run_reports_failing_when_request_raises(tmp_db):
    session, context = _mock_session([])
    context.request.get = AsyncMock(side_effect=RuntimeError("network gone"))

    agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=session)
    result = await agent.run()

    assert result.status == AgentStatus.FAILING
    assert "network gone" in result.error


@pytest.mark.asyncio
async def test_aclose_only_closes_a_session_it_owns(tmp_db):
    session, _ = _mock_session([])

    injected_agent = TeamsAgent(profile_dir="unused", db=tmp_db, session=session)
    await injected_agent.aclose()
    session.close.assert_not_awaited()
