from __future__ import annotations

import pytest

from agents.base import AgentResult, AgentStatus, BaseAgent
from agents.runner import run_and_record


class _StubAgent(BaseAgent):
    def __init__(self, result: AgentResult):
        super().__init__(name=result.agent)
        self._result = result

    async def run(self) -> AgentResult:
        return self._result


@pytest.mark.asyncio
async def test_successful_run_is_recorded_as_working(tmp_db):
    agent = _StubAgent(AgentResult(agent="weather", status=AgentStatus.WORKING, data={"ok": True}))

    result = await run_and_record(agent, tmp_db)

    assert result.status == AgentStatus.WORKING
    last = tmp_db.last_run("weather")
    assert last["status"] == "working"
    assert last["error"] is None


@pytest.mark.asyncio
async def test_failing_run_is_recorded_with_error(tmp_db):
    agent = _StubAgent(AgentResult(agent="weather", status=AgentStatus.FAILING, error="API down"))

    result = await run_and_record(agent, tmp_db)

    assert result.status == AgentStatus.FAILING
    last = tmp_db.last_run("weather")
    assert last["status"] == "failing"
    assert last["error"] == "API down"


@pytest.mark.asyncio
async def test_crashing_agent_is_still_recorded_via_run_isolated(tmp_db):
    class _CrashingAgent(BaseAgent):
        async def run(self) -> AgentResult:
            raise RuntimeError("boom")

    agent = _CrashingAgent(name="weather")
    result = await run_and_record(agent, tmp_db)

    assert result.status == AgentStatus.FAILING
    assert "boom" in result.error
    last = tmp_db.last_run("weather")
    assert last["status"] == "failing"
