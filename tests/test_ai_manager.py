from __future__ import annotations

import asyncio
import json

import pytest

from agents.ai_manager.agent import AIManager
from agents.ai_manager.prompt import build_system_prompt
from llm.provider import LLMProvider, LLMToolResult, ToolCall
from tools.base import Tool, ToolRegistry


class FakeProvider(LLMProvider):
    """A scripted mock LLM (spec §79's "mock LLM") - each
    generate_with_tools() call pops the next scripted item, which is
    either an LLMToolResult to return or an Exception to raise. Records
    every call's (messages, tools, system) for assertions."""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []

    async def generate(self, prompt: str) -> str:
        return "n/a"

    async def generate_with_tools(self, messages, tools, system=None):
        self.calls.append({"messages": messages, "tools": tools, "system": system})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _registry_with(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _tool(name: str, handler) -> Tool:
    return Tool(name=name, description="d", parameters={"type": "object", "properties": {}, "required": []}, handler=handler)


# ===========================================================================
# No tool needed - plain text
# ===========================================================================


@pytest.mark.asyncio
async def test_handle_message_returns_plain_text_when_no_tool_is_needed():
    provider = FakeProvider([LLMToolResult(text="Привет! Чем могу помочь?", tool_calls=[])])
    manager = AIManager(provider, ToolRegistry())
    result = await manager.handle_message("Привет")
    assert result.status == "OK"
    assert result.text == "Привет! Чем могу помочь?"
    assert result.tool_calls == []


# ===========================================================================
# Single and multi-tool routing (spec §29/§31)
# ===========================================================================


@pytest.mark.asyncio
async def test_handle_message_executes_a_tool_call_and_feeds_the_exact_result_back():
    registry = _registry_with(_tool("get_dashboard", lambda: {"status": "OK", "today": []}))
    provider = FakeProvider([
        LLMToolResult(
            tool_calls=[ToolCall(id="call_1", name="get_dashboard", arguments={})],
            raw_content=[{"type": "tool_use", "id": "call_1", "name": "get_dashboard", "input": {}}],
        ),
        LLMToolResult(text="Сегодня пар нет.", tool_calls=[]),
    ])
    manager = AIManager(provider, registry)
    result = await manager.handle_message("Что там по учебе?")

    assert result.status == "OK"
    assert result.text == "Сегодня пар нет."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_dashboard"
    assert result.tool_calls[0].status == "OK"

    # The second provider call must have received the tool's EXACT
    # result, not a paraphrase or a subset (spec §37's anti-hallucination
    # guard starts here - what the model sees must match what the tool
    # actually returned).
    second_call_messages = provider.calls[1]["messages"]
    tool_result_message = second_call_messages[-1]
    block = tool_result_message["content"][0]
    assert block["tool_use_id"] == "call_1"
    payload = json.loads(block["content"])
    assert payload == {"status": "OK", "data": {"status": "OK", "today": []}, "error": None}


@pytest.mark.asyncio
async def test_handle_message_supports_multiple_tool_calls_in_one_turn():
    registry = _registry_with(
        _tool("get_upcoming_schedule", lambda: [{"course": "CSE4112"}]),
        _tool("get_weather", lambda: {"temp": 20}),
    )
    provider = FakeProvider([
        LLMToolResult(
            tool_calls=[
                ToolCall(id="c1", name="get_upcoming_schedule", arguments={}),
                ToolCall(id="c2", name="get_weather", arguments={}),
            ],
            raw_content=[],
        ),
        LLMToolResult(text="Завтра одна пара, погода тёплая.", tool_calls=[]),
    ])
    manager = AIManager(provider, registry)
    result = await manager.handle_message("Что у меня завтра и какая погода?")

    assert result.status == "OK"
    assert {c.name for c in result.tool_calls} == {"get_upcoming_schedule", "get_weather"}
    tool_result_blocks = provider.calls[1]["messages"][-1]["content"]
    assert len(tool_result_blocks) == 2
    assert {b["tool_use_id"] for b in tool_result_blocks} == {"c1", "c2"}


# ===========================================================================
# Tool failure is relayed to the model, never raised (spec §42/§85)
# ===========================================================================


@pytest.mark.asyncio
async def test_a_failing_tool_is_reported_as_an_error_result_not_a_crash():
    def _boom():
        raise RuntimeError("Teams is unavailable")

    registry = _registry_with(_tool("get_upcoming_tasks", _boom))
    provider = FakeProvider([
        LLMToolResult(tool_calls=[ToolCall(id="c1", name="get_upcoming_tasks", arguments={})], raw_content=[]),
        LLMToolResult(text="По учебе сейчас не получилось получить данные.", tool_calls=[]),
    ])
    manager = AIManager(provider, registry)
    result = await manager.handle_message("Что сдавать?")

    assert result.status == "OK"  # the OVERALL turn still completes
    assert result.tool_calls[0].status == "ERROR"
    payload = json.loads(provider.calls[1]["messages"][-1]["content"][0]["content"])
    assert payload["status"] == "ERROR"
    assert payload["error"]["message"] == "Teams is unavailable"


# ===========================================================================
# Provider errors + retry (spec §64/§89)
# ===========================================================================


@pytest.mark.asyncio
async def test_provider_error_is_retried_and_can_still_succeed():
    provider = FakeProvider([RuntimeError("temporary network blip"), LLMToolResult(text="ok", tool_calls=[])])
    manager = AIManager(provider, ToolRegistry(), provider_retries=1)
    result = await manager.handle_message("hi")
    assert result.status == "OK"
    assert result.text == "ok"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_provider_error_exhausting_retries_returns_llm_provider_error_status():
    provider = FakeProvider([RuntimeError("down"), RuntimeError("still down")])
    manager = AIManager(provider, ToolRegistry(), provider_retries=1)
    result = await manager.handle_message("hi")
    assert result.status == "LLM_PROVIDER_ERROR"
    assert "не получилось" in result.text.lower() or "попробуй" in result.text.lower()


# ===========================================================================
# Timeouts + max iterations (spec §63)
# ===========================================================================


@pytest.mark.asyncio
async def test_a_hung_tool_call_times_out_without_crashing_the_turn():
    async def _slow():
        await asyncio.sleep(1)
        return {"never": "reached"}

    registry = _registry_with(_tool("slow_tool", _slow))
    provider = FakeProvider([
        LLMToolResult(tool_calls=[ToolCall(id="c1", name="slow_tool", arguments={})], raw_content=[]),
        LLMToolResult(text="Не удалось получить данные вовремя.", tool_calls=[]),
    ])
    manager = AIManager(provider, registry, tool_timeout_seconds=0.05)
    result = await manager.handle_message("hi")
    assert result.status == "OK"
    assert result.tool_calls[0].status == "ERROR"
    payload = json.loads(provider.calls[1]["messages"][-1]["content"][0]["content"])
    assert payload["error"]["code"] == "TOOL_TIMEOUT"


@pytest.mark.asyncio
async def test_exceeding_max_tool_iterations_returns_a_max_iterations_status():
    registry = _registry_with(_tool("get_dashboard", lambda: {}))
    call = ToolCall(id="c1", name="get_dashboard", arguments={})
    provider = FakeProvider([
        LLMToolResult(tool_calls=[call], raw_content=[]),
        LLMToolResult(tool_calls=[call], raw_content=[]),
    ])
    manager = AIManager(provider, registry, max_tool_iterations=2)
    result = await manager.handle_message("hi")
    assert result.status == "MAX_ITERATIONS"
    assert len(result.tool_calls) == 2


# ===========================================================================
# Observability (spec §61-62)
# ===========================================================================


@pytest.mark.asyncio
async def test_each_call_gets_its_own_unique_request_id():
    provider = FakeProvider([
        LLMToolResult(text="a", tool_calls=[]),
        LLMToolResult(text="b", tool_calls=[]),
    ])
    manager = AIManager(provider, ToolRegistry())
    first = await manager.handle_message("first")
    second = await manager.handle_message("second")
    assert first.request_id != second.request_id


@pytest.mark.asyncio
async def test_system_prompt_builder_is_used_for_every_provider_call():
    provider = FakeProvider([LLMToolResult(text="ok", tool_calls=[])])
    manager = AIManager(provider, ToolRegistry(), system_prompt_builder=lambda: "CUSTOM SYSTEM PROMPT")
    await manager.handle_message("hi")
    assert provider.calls[0]["system"] == "CUSTOM SYSTEM PROMPT"


# ===========================================================================
# The real system prompt (spec §56) - smoke test, not a full text match
# ===========================================================================


def test_build_system_prompt_covers_the_required_topics():
    prompt = build_system_prompt()
    for required_phrase in (
        "get_dashboard",
        "AUTH_REQUIRED",
        "study_overrides" if "study_overrides" in prompt else "Study Overrides",
        "hallucin" if "hallucin" in prompt.lower() else "выдум",
    ):
        assert required_phrase in prompt or required_phrase.lower() in prompt.lower()
    # Never embeds a real secret placeholder-looking value.
    assert "sk-" not in prompt
    assert "TELEGRAM_BOT_TOKEN=" not in prompt


def test_build_system_prompt_distinguishes_session_validity_from_real_data_sync():
    """Live bug, 2026-09-28: the model said 'Teams synced just now' from
    source_status's session-validity check alone, while the real Teams
    data fetch had actually just failed. The prompt must steer the
    model toward last_data_sync/last_data_status for that question."""
    prompt = build_system_prompt()
    assert "last_data_sync" in prompt
    assert "last_data_status" in prompt
