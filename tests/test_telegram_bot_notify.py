from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from config import AppConfig
from storage.wishlist import WishlistStore
from telegram_bot.bot import TelegramBot


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        telegram_bot_token="123456:FAKE-TOKEN-NOT-REAL",
        telegram_chat_id="42",
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
        database_path=str(tmp_path / "agent.db"),
        log_dir=str(tmp_path / "logs"),
        lock_file_path=str(tmp_path / ".instance.lock"),
        wishlist_path=str(tmp_path / "wishlist.json"),
        teams_profile_path=str(tmp_path / "teams_browser_profile"),
        teams_min_due_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        valorant_profile_path=str(tmp_path / "valorant_browser_profile"),
    )


@pytest.fixture
def bot(tmp_db, tmp_path):
    wishlist = WishlistStore(tmp_path / "wishlist.json")
    # Building the Application does NOT make a network call - only
    # initialize()/start_polling() would, and this test never calls
    # those (no real bot token or network access is available/needed
    # here; see the Phase 3 report for why live polling can't be
    # exercised in this environment).
    return TelegramBot(config=_config(tmp_path), db=tmp_db, wishlist=wishlist)


def test_missing_token_raises_immediately(tmp_db, tmp_path):
    wishlist = WishlistStore(tmp_path / "wishlist.json")
    config = _config(tmp_path)
    config = config.__class__(**{**config.__dict__, "telegram_bot_token": ""})

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        TelegramBot(config=config, db=tmp_db, wishlist=wishlist)


@pytest.mark.asyncio
async def test_notify_sends_once_for_a_new_event(bot):
    with patch.object(bot, "_send", new=AsyncMock()) as mock_send:
        sent = await bot.notify("task_overdue", "task-123", "Lab 2 is overdue")

    assert sent is True
    mock_send.assert_awaited_once_with("Lab 2 is overdue")
    assert bot.db.was_notified("task_overdue", "task-123") is True


@pytest.mark.asyncio
async def test_notify_skips_duplicate_event_without_sending(bot):
    with patch.object(bot, "_send", new=AsyncMock()) as mock_send:
        await bot.notify("task_overdue", "task-123", "Lab 2 is overdue")
        sent_again = await bot.notify("task_overdue", "task-123", "Lab 2 is overdue (again)")

    assert sent_again is False
    mock_send.assert_awaited_once()  # only the first call actually sent anything


@pytest.mark.asyncio
async def test_notify_different_dedupe_key_sends_again(bot):
    with patch.object(bot, "_send", new=AsyncMock()) as mock_send:
        await bot.notify("task_overdue", "task-123", "Lab 2 is overdue")
        sent = await bot.notify("task_overdue", "task-456", "Lab 3 is overdue")

    assert sent is True
    assert mock_send.await_count == 2


@pytest.mark.asyncio
async def test_notify_does_not_mark_notified_when_send_fails(bot):
    from telegram.error import TelegramError

    with patch.object(bot, "_send", new=AsyncMock(side_effect=TelegramError("boom"))):
        sent = await bot.notify("agent_status_change", "teams:failing", "Teams is down")

    assert sent is False
    # Because the send failed, this must NOT be marked as notified - a
    # future successful attempt should still go through.
    assert bot.db.was_notified("agent_status_change", "teams:failing") is False
