from __future__ import annotations

import pytest

from agents.base import AgentResult, AgentStatus, BaseAgent, run_isolated


class WorkingAgent(BaseAgent):
    async def run(self) -> AgentResult:
        return AgentResult(agent=self.name, status=AgentStatus.WORKING, data={"ok": True})


class CrashingAgent(BaseAgent):
    async def run(self) -> AgentResult:
        raise ValueError("simulated crash")


class SelfReportingFailingAgent(BaseAgent):
    async def run(self) -> AgentResult:
        # An agent that catches its OWN expected failure (e.g. API down)
        # and reports it cleanly, per the spec, rather than raising.
        return AgentResult(agent=self.name, status=AgentStatus.FAILING, error="API unavailable")


@pytest.mark.asyncio
async def test_working_agent_returns_structured_result():
    agent = WorkingAgent("weather")
    result = await run_isolated(agent)

    assert result.agent == "weather"
    assert result.status == AgentStatus.WORKING
    assert result.data == {"ok": True}
    assert result.error is None


@pytest.mark.asyncio
async def test_crashing_agent_does_not_raise_out_of_run_isolated():
    agent = CrashingAgent("teams")
    result = await run_isolated(agent)

    assert result.status == AgentStatus.FAILING
    assert "simulated crash" in result.error


@pytest.mark.asyncio
async def test_self_reported_failure_is_passed_through_unchanged():
    agent = SelfReportingFailingAgent("valorant")
    result = await run_isolated(agent)

    assert result.status == AgentStatus.FAILING
    assert result.error == "API unavailable"


def test_agent_result_to_dict_matches_spec_shape():
    result = AgentResult(agent="weather", status=AgentStatus.WORKING, data={"temp": 14})
    payload = result.to_dict()

    assert set(payload.keys()) == {"agent", "status", "timestamp", "data", "error"}
    assert payload["status"] == "working"  # enum serialized as its string value
