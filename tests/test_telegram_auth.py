from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram_bot.auth import require_authorized


def _fake_update(chat_id: int | str | None):
    update = MagicMock()
    if chat_id is None:
        update.effective_chat = None
    else:
        update.effective_chat = MagicMock(id=chat_id)
    return update


@pytest.mark.asyncio
async def test_authorized_chat_id_calls_the_handler():
    inner = AsyncMock()
    wrapped = require_authorized(expected_chat_id="12345")(inner)

    update = _fake_update(12345)  # int, as python-telegram-bot actually gives it
    context = MagicMock()

    await wrapped(update, context)

    inner.assert_awaited_once_with(update, context)


@pytest.mark.asyncio
async def test_unauthorized_chat_id_does_not_call_the_handler():
    inner = AsyncMock()
    wrapped = require_authorized(expected_chat_id="12345")(inner)

    update = _fake_update(999999)  # a stranger's chat
    context = MagicMock()

    await wrapped(update, context)

    inner.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_effective_chat_does_not_call_the_handler():
    inner = AsyncMock()
    wrapped = require_authorized(expected_chat_id="12345")(inner)

    update = _fake_update(None)
    context = MagicMock()

    await wrapped(update, context)

    inner.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorized_check_compares_as_strings():
    # TELEGRAM_CHAT_ID from .env arrives as a string; Telegram's own
    # chat.id is an int. The comparison must not silently fail because
    # of the type mismatch.
    inner = AsyncMock()
    wrapped = require_authorized(expected_chat_id="555")(inner)

    update = _fake_update(555)
    context = MagicMock()

    await wrapped(update, context)

    inner.assert_awaited_once()
