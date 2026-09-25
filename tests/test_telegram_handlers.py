from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from storage.wishlist import WishlistStore
from telegram_bot.handlers import Handlers


def _fake_update_and_context(args: list[str] | None = None):
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args or []
    return update, context


@pytest.fixture
def handlers(tmp_db, tmp_path):
    wishlist = WishlistStore(tmp_path / "wishlist.json")
    return Handlers(db=tmp_db, wishlist=wishlist, config=MagicMock())


@pytest.mark.asyncio
async def test_status_reports_not_yet_run_for_all_known_agents(handlers):
    update, context = _fake_update_and_context()
    await handlers.status(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "teams" in text and "weather" in text and "valorant" in text
    assert "не запускался" in text


@pytest.mark.asyncio
async def test_status_reflects_a_recorded_agent_run(handlers, tmp_db):
    now = datetime.now(timezone.utc).isoformat()
    tmp_db.record_agent_run("weather", "working", now, now, None)

    update, context = _fake_update_and_context()
    await handlers.status(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "weather: working" in text


@pytest.mark.asyncio
async def test_tasks_honest_when_empty(handlers):
    update, context = _fake_update_and_context()
    await handlers.tasks(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "не реализован" in text


@pytest.mark.asyncio
async def test_tasks_lists_rows_from_db(handlers, tmp_db):
    with tmp_db.connect() as conn:
        conn.execute(
            "INSERT INTO tasks (id, course, title, status, source, due_at, updated_at) "
            "VALUES ('t1', 'CS101', 'Lab 2', 'overdue', 'teams', '2026-09-22', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )

    update, context = _fake_update_and_context()
    await handlers.tasks(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "CS101" in text and "Lab 2" in text and "overdue" in text


@pytest.mark.asyncio
async def test_weather_honest_when_empty(handlers):
    update, context = _fake_update_and_context()
    await handlers.weather(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "не реализован" in text


@pytest.mark.asyncio
async def test_store_honest_when_empty(handlers):
    update, context = _fake_update_and_context()
    await handlers.store(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "не реализован" in text


@pytest.mark.asyncio
async def test_briefing_reports_all_three_sections_as_empty(handlers):
    update, context = _fake_update_and_context()
    await handlers.briefing(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Teams Agent не реализован" in text
    assert "Weather Agent не реализован" in text
    assert "VALORANT Agent не реализован" in text


@pytest.mark.asyncio
async def test_wishlist_cmd_empty(handlers):
    update, context = _fake_update_and_context()
    await handlers.wishlist_cmd(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "пуст" in text


@pytest.mark.asyncio
async def test_addskin_then_wishlist_then_removeskin_roundtrip(handlers):
    update, context = _fake_update_and_context(args=["Reaver", "Vandal"])
    await handlers.addskin(update, context)
    assert "добавлен" in update.message.reply_text.call_args.args[0]

    update2, context2 = _fake_update_and_context()
    await handlers.wishlist_cmd(update2, context2)
    assert "Reaver Vandal" in update2.message.reply_text.call_args.args[0]

    update3, context3 = _fake_update_and_context(args=["Reaver", "Vandal"])
    await handlers.removeskin(update3, context3)
    assert "удалён" in update3.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_addskin_without_args_shows_usage(handlers):
    update, context = _fake_update_and_context(args=[])
    await handlers.addskin(update, context)

    assert "Использование" in update.message.reply_text.call_args.args[0]
