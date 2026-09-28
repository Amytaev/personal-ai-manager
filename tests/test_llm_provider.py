from __future__ import annotations

from datetime import datetime, timezone

import pytest

from unittest.mock import AsyncMock, MagicMock, patch

from config import AppConfig
from llm.provider import AnthropicProvider, LLMToolResult, NullProvider, ToolCall, get_provider


def _config(**overrides) -> AppConfig:
    base = dict(
        telegram_bot_token="",
        telegram_chat_id="",
        weather_location="",
        weather_api_key="",
        llm_provider="anthropic",
        llm_model="",
        llm_api_key="",
        teams_interval_minutes=60,
        weather_interval_minutes=60,
        valorant_interval_minutes=120,
        sso_interval_minutes=120,
        auth_checker_interval_minutes=30,
        checker_interval_minutes=120,
        checker_window_start_days=7,
        checker_window_end_days=30,
        study_manager_interval_minutes=120,
        briefing_hour=8,
        included_courses=(),
        excluded_courses=(),
        data_retention_days=30,
        database_path="data/agent.db",
        log_dir="logs",
        lock_file_path="data/.instance.lock",
        wishlist_path="config/wishlist.json",
        teams_profile_path="data/teams_browser_profile",
        teams_min_due_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        valorant_profile_path="data/valorant_browser_profile",
        sso_profile_path="data/sso_browser_profile",
        semester1_week1_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return AppConfig(**base)


def test_get_provider_falls_back_to_null_when_no_api_key():
    provider = get_provider(_config(llm_api_key=""))
    assert isinstance(provider, NullProvider)


def test_get_provider_returns_anthropic_when_configured():
    provider = get_provider(_config(llm_api_key="sk-fake", llm_model="claude-sonnet-5"))
    assert isinstance(provider, AnthropicProvider)


def test_get_provider_rejects_unsupported_provider():
    with pytest.raises(ValueError):
        get_provider(_config(llm_api_key="sk-fake", llm_provider="not-a-real-provider"))


@pytest.mark.asyncio
async def test_null_provider_does_not_raise():
    provider = NullProvider()
    result = await provider.generate("hello")
    assert "not configured" in result.lower()


# -- generate_with_tools() (AI Manager ТЗ v3 §6/§10/§86) --------------------


@pytest.mark.asyncio
async def test_default_generate_with_tools_falls_back_to_plain_text_with_no_tool_calls():
    # NullProvider never overrides generate_with_tools() - it inherits
    # LLMProvider's default, which must degrade gracefully (never
    # invent a fake tool call) when there's no real tool-use support.
    provider = NullProvider()
    result = await provider.generate_with_tools(
        messages=[{"role": "user", "content": "Что у меня завтра?"}],
        tools=[{"name": "get_upcoming_schedule", "description": "...", "input_schema": {}}],
    )
    assert isinstance(result, LLMToolResult)
    assert result.tool_calls == []
    assert "not configured" in (result.text or "").lower()


@pytest.mark.asyncio
async def test_anthropic_provider_generate_with_tools_parses_a_real_tool_use_response():
    provider = AnthropicProvider(api_key="sk-fake", model="claude-sonnet-5")

    # MagicMock(name=...) sets the mock's own debug name, not a
    # "name" attribute - the tool's real name has to be assigned after
    # construction instead.
    tool_use_block = MagicMock(type="tool_use", id="call_1", input={"days_ahead": 1})
    tool_use_block.name = "get_upcoming_schedule"
    fake_response = MagicMock(content=[tool_use_block])

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        result = await provider.generate_with_tools(
            messages=[{"role": "user", "content": "Что у меня завтра?"}],
            tools=[{"name": "get_upcoming_schedule", "description": "...", "input_schema": {}}],
            system="You are AI Manager.",
        )

    assert result.text is None
    assert result.tool_calls == [ToolCall(id="call_1", name="get_upcoming_schedule", arguments={"days_ahead": 1})]
    _, kwargs = fake_client.messages.create.call_args
    assert kwargs["system"] == "You are AI Manager."
    assert kwargs["tools"][0]["name"] == "get_upcoming_schedule"


@pytest.mark.asyncio
async def test_anthropic_provider_generate_with_tools_returns_final_text_with_no_tool_calls():
    provider = AnthropicProvider(api_key="sk-fake", model="claude-sonnet-5")

    text_block = MagicMock(type="text", text="Завтра у тебя две пары.")
    fake_response = MagicMock(content=[text_block])

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)

    with patch("anthropic.AsyncAnthropic", return_value=fake_client):
        result = await provider.generate_with_tools(
            messages=[{"role": "user", "content": "Что у меня завтра?"}], tools=[],
        )

    assert result.text == "Завтра у тебя две пары."
    assert result.tool_calls == []
