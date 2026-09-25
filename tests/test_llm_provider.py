from __future__ import annotations

import pytest

from config import AppConfig
from llm.provider import AnthropicProvider, NullProvider, get_provider


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
        briefing_hour=8,
        included_courses=(),
        excluded_courses=(),
        data_retention_days=30,
        database_path="data/agent.db",
        log_dir="logs",
        lock_file_path="data/.instance.lock",
        wishlist_path="config/wishlist.json",
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
