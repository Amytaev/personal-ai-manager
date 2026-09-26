"""Telegram bot wiring (TZ v4 §23-27).

Builds the python-telegram-bot Application, registers every command
handler behind the chat_id authorization check (telegram_bot/auth.py), and
exposes start()/stop() so run.py can lifecycle it in the same asyncio
loop as the scheduler - graceful shutdown covers both together.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import AppConfig
from storage.database import Database
from storage.wishlist import WishlistStore
from telegram_bot.auth import require_authorized
from telegram_bot.handlers import Handlers
from utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, config: AppConfig, db: Database, wishlist: WishlistStore) -> None:
        if not config.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set")
        if not config.telegram_chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is not set")

        self.config = config
        self.db = db
        self.handlers = Handlers(db=db, wishlist=wishlist, config=config)

        self.app: Application = Application.builder().token(config.telegram_bot_token).build()
        self._register_handlers()
        self.app.add_error_handler(self._on_error)

    def _register_handlers(self) -> None:
        auth = require_authorized(self.config.telegram_chat_id)
        bindings = {
            "start": self.handlers.start,
            "help": self.handlers.help,
            "status": self.handlers.status,
            "tasks": self.handlers.tasks,
            "weather": self.handlers.weather,
            "store": self.handlers.store,
            "briefing": self.handlers.briefing,
            "wishlist": self.handlers.wishlist_cmd,
            "addskin": self.handlers.addskin,
            "removeskin": self.handlers.removeskin,
        }
        for command, handler in bindings.items():
            self.app.add_handler(CommandHandler(command, auth(handler)))

        # Any plain-text message that ISN'T a slash command goes straight
        # to Claude (telegram_bot/handlers.py: Handlers.chat) - no /ask
        # prefix needed. Slash commands above stay reserved for technical/
        # debug actions. filters.COMMAND matches anything starting with
        # "/" (including an unrecognized one like a mistyped "/wether"),
        # so those are excluded here too and simply go unanswered rather
        # than being treated as a chat question - a command typo staying
        # silent is less surprising than it accidentally becoming a
        # prompt to the LLM.
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, auth(self.handlers.chat))
        )

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # TZ v4 §26: user-facing messages stay friendly/generic, the real
        # exception stays in logs only. Never echo context.error's full
        # text back to the chat - it can contain request internals we
        # don't want to expose even to the bot's own owner over Telegram.
        logger.error("Telegram handler error: %s", context.error)
        if isinstance(update, Update) and update.effective_chat is not None:
            try:
                await update.effective_chat.send_message(
                    "\u26a0\ufe0f Что-то пошло не так при обработке команды. Подробности в логах."
                )
            except TelegramError:
                logger.error("Failed to notify user about the error above")

    @retry_with_backoff(max_attempts=3, base_delay=2.0, exceptions=(TelegramError,))
    async def _send(self, text: str) -> None:
        await self.app.bot.send_message(chat_id=self.config.telegram_chat_id, text=text)

    async def notify(self, kind: str, dedupe_key: str, text: str) -> bool:
        """Send a proactive notification, deduplicated by (kind,
        dedupe_key) via the Phase 2 notifications table (TZ v4 §20/§22).

        Returns True if a message was actually sent, False if it was
        skipped (already sent before) or failed after retries. Phase 7
        (AI Manager) will be the main caller once agents can detect real
        state changes (new task, agent working->failing, wishlist skin
        in store); Phase 3 only wires the mechanism so it's provably
        correct now, via tests, without needing a real agent yet.
        """
        if self.db.was_notified(kind, dedupe_key):
            logger.debug("Skipping duplicate notification %s/%s", kind, dedupe_key)
            return False
        try:
            await self._send(text)
        except TelegramError as exc:
            logger.error("Failed to send Telegram notification after retries: %s", exc)
            return False
        self.db.mark_notified(kind, dedupe_key)
        return True

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        logger.info("Telegram bot started (polling)")

    async def stop(self) -> None:
        logger.info("Stopping Telegram bot")
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        logger.info("Telegram bot stopped")
