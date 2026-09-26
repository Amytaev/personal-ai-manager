"""Command handlers (TZ v4 §23-27).

None of these call an agent directly - Teams (Phase 5), Weather
(Phase 4) and VALORANT (Phase 6) agents all run on the scheduler in
run.py and write to SQLite; only the AI Manager's aggregation/priority/
LLM summary logic (Phase 7) doesn't exist yet. Commands that would show
agent data read straight from the SQLite tables Phase 2 already built,
and report honestly when there's nothing there yet instead of
pretending - "нет данных" is genuinely ambiguous between "agent hasn't
run yet" and "ran but found nothing", so the messages below say which
one it is per agent.
"""
from __future__ import annotations

import json
import logging

from telegram import InputMediaPhoto, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import AppConfig
from llm.provider import NullProvider, get_provider
from storage.database import Database
from storage.wishlist import WishlistStore

logger = logging.getLogger(__name__)

# The three agents named in the TZ - teams (Phase 5), weather (Phase 4)
# and valorant (Phase 6) are all implemented now and run on the
# scheduler. /status reports on all three by name.
KNOWN_AGENTS = ("teams", "weather", "valorant")

STATUS_MARKERS = {"working": "✅", "degraded": "\U0001f7e1", "failing": "\U0001f534"}

# Telegram's sendMediaGroup accepts 2-10 items - a store with more items
# than this (never seen in real data, VALORANT's daily store is always
# 4) would still need a cap so a single /store call can't blow past
# Telegram's own limit.
_MAX_MEDIA_GROUP_ITEMS = 10


def _parse_store_payload(skins_json: str) -> tuple[list[dict], str | None] | None:
    """Parses the payload storage.database.Database.save_valorant_store()
    writes ({"items": [...], "reset_in": ...}). Returns (items,
    reset_in), or None if the JSON is missing/malformed in a way that
    makes it unusable (an older row saved before this shape existed, or
    a genuinely corrupt one) - callers fall back to a raw-text rendering
    in that case rather than crashing /store."""
    try:
        payload = json.loads(skins_json)
        items = payload["items"]
        if not isinstance(items, list):
            return None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    reset_in = payload.get("reset_in")
    return items, (reset_in if isinstance(reset_in, str) else None)


def _price_text(item: dict) -> str:
    price = item.get("price_vp")
    return f"{price} VP" if price is not None else "цена неизвестна"


def _item_caption(item: dict) -> str:
    return f"{item.get('name', '?')} — {_price_text(item)}"


def _format_store_text(skins_json: str, checked_at: str) -> str:
    """Text-only rendering of the store - used when the payload can't be
    parsed at all, or as a fallback when sending photos itself fails
    (bad/expired image URL, Telegram API hiccup)."""
    parsed = _parse_store_payload(skins_json)
    if parsed is None:
        return f"\U0001f3ae Магазин ({checked_at}):\n{skins_json}"

    items, reset_in = parsed
    lines = [f"\U0001f3ae Магазин VALORANT (обновлено {checked_at}):"]
    lines.extend(f"• {_item_caption(item)}" for item in items)
    if reset_in:
        lines.append(f"\n⏳ Сброс через: {reset_in}")
    return "\n".join(lines)


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
            "/store - магазин VALORANT (с картинками скинов)\n"
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
                lines.append(f"⚪ {agent}: ещё не запускался")
            else:
                marker = STATUS_MARKERS.get(row["status"], "❓")
                lines.append(f"{marker} {agent}: {row['status']} (проверка {row['finished_at']})")
        await update.message.reply_text("\n".join(lines))

    async def tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT course, title, status, due_at FROM tasks ORDER BY due_at"
            ).fetchall()
        if not rows:
            await update.message.reply_text(
                "Заданий пока нет — Teams Agent либо ещё не запускался, либо запускался "
                "неудачно (нет доступа к сессии). Проверь /status: teams."
            )
            return
        lines = [
            f"• [{row['course']}] {row['title']} — {row['status']} (до {row['due_at']})"
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
        """Shows today's VALORANT store WITH each skin's image (Phase 6.3):
        Stack B already hands the agent a real image_url per item
        (agents/valorant/parser.py), stored straight through in
        valorant_store.skins_json - this just sends it on as an actual
        Telegram photo/media group instead of a bare URL string.

        Several honest-degradation paths, in order, since a stored row
        can be in worse shape than "everything worked":
        1. no row at all -> agent never ran/never got real data yet.
        2. row exists but the JSON doesn't parse into the expected
           {"items": [...]} shape -> raw text fallback
           (_format_store_text), same as before this feature existed.
        3. items parsed but the list is empty -> same text fallback
           (a real VALORANT store is never actually empty, so this
           would only happen for a row saved before parser.py existed).
        4. items exist, but the LAST agent run since then failed
           (needs_reauth or a parse error) -> the stored data is shown
           anyway (it's still real, just possibly stale) with an
           explicit warning up front, rather than either hiding it or
           silently pretending it's fresh.
        5. sending the photos itself fails (bad/expired image URL,
           Telegram API hiccup) -> falls back to the plain-text
           rendering rather than leaving the person with half a reply.
        """
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT skins_json, checked_at FROM valorant_store "
                "ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            await update.message.reply_text(
                "\U0001f3ae VALORANT\n⚠️ Магазин недоступен — VALORANT Agent ещё "
                "не запускался или не смог авторизоваться в Stack B. Проверь /status: valorant."
            )
            return

        parsed = _parse_store_payload(row["skins_json"])
        if parsed is None or not parsed[0]:
            await update.message.reply_text(_format_store_text(row["skins_json"], row["checked_at"]))
            return
        items, reset_in = parsed

        last_run = self.db.last_run("valorant")
        if last_run is not None and last_run["status"] != "working":
            await update.message.reply_text(
                f"⚠️ Последний прогон VALORANT Agent завершился с ошибкой "
                f"({last_run['status']}) — ниже последний успешно сохранённый магазин "
                f"({row['checked_at']}), он может быть устаревшим."
            )

        with_images = [item for item in items if item.get("image_url")]
        without_images = [item for item in items if not item.get("image_url")]

        try:
            if len(with_images) >= 2:
                media = [
                    InputMediaPhoto(media=item["image_url"], caption=_item_caption(item))
                    for item in with_images[:_MAX_MEDIA_GROUP_ITEMS]
                ]
                await update.message.reply_media_group(media=media)
            elif len(with_images) == 1:
                item = with_images[0]
                await update.message.reply_photo(photo=item["image_url"], caption=_item_caption(item))
        except TelegramError:
            logger.exception("Failed to send VALORANT store photos, falling back to text")
            await update.message.reply_text(_format_store_text(row["skins_json"], row["checked_at"]))
            return

        tail_lines = []
        if without_images:
            tail_lines.append("Без картинки:")
            tail_lines.extend(f"• {_item_caption(item)}" for item in without_images)
        if reset_in:
            tail_lines.append(f"\n⏳ Сброс через: {reset_in}")
        if tail_lines:
            await update.message.reply_text("\n".join(tail_lines))

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
            else "\U0001f4da Учёба: данных нет (Teams Agent ещё не запускался/не смог авторизоваться)"
        )
        parts.append(
            f"\U0001f324 Погода: обновлено {weather_row['created_at']}"
            if weather_row
            else "\U0001f324 Погода: данных нет (Weather Agent не реализован)"
        )
        parts.append(
            f"\U0001f3ae VALORANT: обновлено {store_row['checked_at']}"
            if store_row
            else "\U0001f3ae VALORANT: данных нет (VALORANT Agent ещё не запускался/не смог авторизоваться)"
        )
        parts.append("\n(Приоритизация и LLM-сводка появятся в Phase 7 — AI Manager.)")
        await update.message.reply_text("\n".join(parts))

    async def wishlist_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        skins = self.wishlist.list()
        if not skins:
            await update.message.reply_text("Wishlist пуст. Добавь скин: /addskin <название>")
            return
        await update.message.reply_text(
            "\U0001f525 Wishlist:\n" + "\n".join(f"• {skin}" for skin in skins)
        )

    async def addskin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.args:
            await update.message.reply_text("Использование: /addskin <название скина>")
            return
        name = " ".join(context.args)
        if self.wishlist.add(name):
            await update.message.reply_text(f"✅ {name} добавлен в wishlist.")
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
        data is relevant. It's a plain chat passthrough - whatever the
        user typed goes to the model as-is, and if the user asks "what's
        up today", the model answers as a generic assistant, honestly,
        not pretending it has looked anything up. Real tool-calling
        routing to get_tasks()/get_weather()/get_valorant() belongs in
        Phase 7.
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
