"""Single-user chat_id authorization (TZ v4 §24).

Every command handler is wrapped with require_authorized() BEFORE it
does any real work. An unauthorized chat gets nothing back - not the
command's data, and not even an error message that would confirm the
bot is alive and reachable at all.
"""
from __future__ import annotations

import functools
import logging
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

Handler = Callable[[Update, "ContextTypes.DEFAULT_TYPE"], Awaitable[None]]


def require_authorized(expected_chat_id: str) -> Callable[[Handler], Handler]:
    """Decorator factory: only calls the wrapped handler when
    ``incoming_chat_id == TELEGRAM_CHAT_ID`` (TZ v4 §24). Any other
    chat is silently ignored - no reply, no data, no acknowledgement.
    """

    def decorator(handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat = update.effective_chat
            incoming_chat_id = str(chat.id) if chat is not None else None

            if incoming_chat_id != str(expected_chat_id):
                # Logging the *incoming* id is fine (it's not a secret,
                # unlike a bot token) - it's useful evidence in the
                # owner's own local logs if someone finds the bot.
                logger.warning(
                    "Ignored command from unauthorized chat_id=%s", incoming_chat_id
                )
                return

            await handler(update, context)

        return wrapper

    return decorator
