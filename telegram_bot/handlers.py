"""Command handlers (TZ v4 §23-27).

None of these call a Teams/Weather/VALORANT agent - those don't exist
yet (Phase 4/5/6), and neither does the AI Manager's aggregation/
priority/LLM summary logic (Phase 7). Commands that would show agent
data read straight from the SQLite tables Phase 2 already built, and
report honestly when there's nothing there yet instead of pretending.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import AppConfig
from llm.provider import NullProvider, get_provider
from storage.database import Database
from storage.wishlist import WishlistStore

logger = logging.getLogger(__name__)

# The three agents named in the TZ - not yet implemented, but /status
# reports on them by name so the shape of the command is already right.
KNOWN_AGENTS = ("teams", "weather", "valorant")

STATUS_MARKERS = {"working": "\u2705", "degraded": "\U0001f7e1", "failing": "\U0001f534"}


class Handlers:
    def __init__(self, db: Database, wishlist: WishlistStore, config: AppConfig):
        self.db = db
        self.wishlist = wishlist
        self.config = config

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "\U0001f916 Personal AI Manager\n\n"
            "Это личный бот, отвечает только твоему chat_id.\n"
            "Команды: /help"
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "/status - состояние агентов\n"
            "/tasks - учебные задания (Teams)\n"
            "/weather - текущая погода\n"
            "/store - магазин VALORANT\n"
            "/briefing - общая сводка\n"
            "/wishlist - список желаемых скинов\n"
            "/addskin <название> - добавить в wishlist\n"
            "/removeskin <название> - убрать из wishlist\n"
            "/help - это сообщение\n\n"
            "Любое обычное сообщение (без /) отправляется напрямую Claude "
            "(нужен LLM_API_KEY в .env)."
        )

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lines = ["\U0001f916 Статус агентов:\n"]
        for agent in KNOWN_AGENTS:
            row = self.db.last_run(agent)
            if row is None:
                lines.append(f"\u26aa {agent}: ещё не запускался (агент не реализован)")
            else:
                marker = STATUS_MARKERS.get(row["status"], "\u2753")
                lines.append(f"{marker} {agent}: {row['status']} (проверка {row['finished_at']})")
        await update.message.reply_text("\n".join(lines))

    async def tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT course, title, status, due_at FROM tasks ORDER BY due_at"
            ).fetchall()
        if not rows:
            await update.message.reply_text(
                "Заданий пока нет — Teams Agent ещё не реализован (Phase 5)."
            )
            return
        lines = [
            f"\u2022 [{row['course']}] {row['title']} — {row['status']} (до {row['due_at']})"
            for row in rows
        ]
        await update.message.reply_text("\n".join(lines))

    async def weather(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT payload_json, created_at FROM weather_snapshots "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            await update.message.reply_text(
                "Данных о погоде пока нет — Weather Agent ещё не реализован (Phase 4)."
            )
            return
        await update.message.reply_text(
            f"\U0001f324 Погода (обновлено {row['created_at']}):\n{row['payload_json']}"
        )

    async def store(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT skins_json, checked_at FROM valorant_store "
                "ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            await update.message.reply_text(
                "\U0001f3ae VALORANT\n\u26a0\ufe0f Магазин недоступен — "
                "агент ещё не реализован (Phase 6)."
            )
            return
        await update.message.reply_text(
            f"\U0001f3ae Магазин ({row['checked_at']}):\n{row['skins_json']}"
        )

    async def briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        with self.db.connect() as conn:
            task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
            weather_row = conn.execute(
                "SELECT created_at FROM weather_snapshots ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            store_row = conn.execute(
                "SELECT checked_at FROM valorant_store ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()

        parts = ["\U0001f4cb DAILY BRIEFING\n"]
        parts.append(
            f"\U0001f4da Учёба: {task_count} заданий в базе"
            if task_count
            else "\U0001f4da Учёба: данных нет (Teams Agent не реализован)"
        )
        parts.append(
            f"\U0001f324 Погода: обновлено {weather_row['created_at']}"
            if weather_row
            else "\U0001f324 Погода: данных нет (Weather Agent не реализован)"
        )
        parts.append(
            f"\U0001f3ae VALORANT: обновлено {store_row['checked_at']}"
            if store_row
            else "\U0001f3ae VALORANT: данных нет (VALORANT Agent не реализован)"
        )
        parts.append("\n(Приоритизация и LLM-сводка появятся в Phase 7 — AI Manager.)")
        await update.message.reply_text("\n".join(parts))

    async def wishlist_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        skins = self.wishlist.list()
        if not skins:
            await update.message.reply_text("Wishlist пуст. Добавь скин: /addskin <название>")
            return
        await update.message.reply_text(
            "\U0001f525 Wishlist:\n" + "\n".join(f"\u2022 {skin}" for skin in skins)
        )

    async def addskin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("Использование: /addskin <название скина>")
            return
        name = " ".join(context.args)
        if self.wishlist.add(name):
            await update.message.reply_text(f"\u2705 {name} добавлен в wishlist.")
        else:
            await update.message.reply_text(f"{name} уже в wishlist.")

    async def removeskin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("Использование: /removeskin <название скина>")
            return
        name = " ".join(context.args)
        if self.wishlist.remove(name):
            await update.message.reply_text(f"\U0001f5d1 {name} удалён из wishlist.")
        else:
            await update.message.reply_text(f"{name} не найден в wishlist.")

    async def chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ad-hoc chat with the configured LLM (llm/provider.py).

        Handles any plain-text message that isn't a slash command - no
        /ask prefix needed, per bro's Phase 5-adjacent review: slash
        commands stay for technical/debug actions (/status, /tasks,
        /weather, ...), plain conversation goes straight to Claude.

        This is intentionally NOT yet the Phase 7 AI Manager: it does
        not look anything up in the database or decide which agent's
        data is relevant (there's barely any real agent data to look up
        yet - Teams' parser/agent aren't written, VALORANT hasn't
        started). It's a plain chat passthrough - whatever the user
        typed goes to the model as-is, and if the user asks "what's up
        today", the model answers as a generic assistant, honestly, not
        pretending it has looked anything up. Real tool-calling routing
        to get_tasks()/get_weather()/get_valorant() belongs in Phase 7,
        once those are real data sources instead of empty tables.
        """
        question = update.message.text
        if not question or not question.strip():
            return

        provider = get_provider(self.config)
        if isinstance(provider, NullProvider):
            await update.message.reply_text(
                "⚠️ LLM не настроен. Добавь LLM_API_KEY (и, при желании, "
                "LLM_MODEL) в .env — см. .env.example — и перезапусти приложение."
            )
            return

        try:
            answer = await provider.generate(question)
        except Exception:  # noqa: BLE001 - never let a provider/network error crash the bot
            logger.exception("LLM provider failed while answering a chat message")
            await update.message.reply_text(
                "⚠️ Не удалось получить ответ от LLM. Подробности в логах."
            )
            return

        await update.message.reply_text(answer)
