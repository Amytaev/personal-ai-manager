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

from agents.ai_manager.agent import AIManager
from config import AppConfig
from llm.provider import NullProvider, get_provider
from storage.database import Database
from storage.wishlist import WishlistStore
from utils.academic_calendar import (
    FIRST_ATTESTATION_WEEK,
    TOTAL_TEACHING_WEEKS,
    current_teaching_week,
    teaching_week_bounds,
    today_in_kz,
)

logger = logging.getLogger(__name__)

# teams (Phase 5), weather (Phase 4), valorant (Phase 6) and sso (Этап B)
# are all implemented now and run on the scheduler. /status reports on
# all four by name.
KNOWN_AGENTS = ("teams", "weather", "valorant", "sso")

STATUS_MARKERS = {"working": "✅", "degraded": "\U0001f7e1", "failing": "\U0001f534"}

# Telegram's sendMediaGroup accepts 2-10 items - a store with more items
# than this (never seen in real data, VALORANT's daily store is always
# 4) would still need a cap so a single /store call can't blow past
# Telegram's own limit.
_MAX_MEDIA_GROUP_ITEMS = 10

# AI Manager ТЗ v3 §34 - "minimal short-term dialogue context", explicitly
# NOT full long-term memory/RAG (spec §66). A plain capped list of
# {"role", "content"} messages kept on the Handlers instance (in-process
# only - never persisted to SQLite or disk, so it resets on restart,
# which is fine for "minimal" context) is threaded into every
# AIManager.handle_message() call as `history=`. Counts individual
# messages (user + assistant), not exchanges, so this caps at 10 back-
# and-forth turns - generous for "а третья?"-style follow-ups without
# growing unboundedly across a long-running bot process.
_MAX_HISTORY_MESSAGES = 20

# GetTable's real column titles (agents/sso/parser.py's docstring/tests -
# e.g. "MONDAY_SHORT") are English weekday codes, not Russian day names
# and not in weekday order - ORDER BY day_title (storage/database.py's
# get_sso_schedule) is alphabetical, so /schedule below re-sorts by real
# weekday order and shows a Russian label instead, purely for display.
_SSO_WEEKDAY_LABELS = {
    "MONDAY_SHORT": "Понедельник",
    "TUESDAY_SHORT": "Вторник",
    "WEDNESDAY_SHORT": "Среда",
    "THURSDAY_SHORT": "Четверг",
    "FRIDAY_SHORT": "Пятница",
    "SATURDAY_SHORT": "Суббота",
    "SUNDAY_SHORT": "Воскресенье",
}
_SSO_WEEKDAY_ORDER = list(_SSO_WEEKDAY_LABELS)

# Real /umkd data can be large (226 materials seen in a live run across
# ~7 courses) - a filtered /umkd <course> listing is capped so a single
# unusually large course folder can't blow past Telegram's ~4096 char
# message limit the way an uncapped /store-style dump could.
_MAX_UMKD_LINES = 60


def _sso_day_label(day_title: str | None) -> str:
    if day_title is None:
        return "?"
    return _SSO_WEEKDAY_LABELS.get(day_title, day_title)


def _sso_day_sort_key(day_title: str | None) -> int:
    try:
        return _SSO_WEEKDAY_ORDER.index(day_title)
    except ValueError:
        # An unrecognized day_title (future API change, bad data) still
        # gets shown - just sorted after every known weekday rather than
        # dropped.
        return len(_SSO_WEEKDAY_ORDER)


def _sso_schedule_line(entry) -> str:
    start = entry["start_time"] or "?"
    end = entry["end_time"] or "?"
    code = entry["course_code"]
    title = entry["course_title"] or "?"
    course = f"[{code}] {title}" if code else title
    class_type = entry["class_type"]
    type_part = f" ({class_type})" if class_type else ""
    where = entry["room_title"] or "?"
    who = entry["instructor_name"] or "?"
    return f"  {start}–{end} {course}{type_part} — {where}, {who}"


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


def _item_line(item: dict) -> str:
    """Same as _item_caption, but flags an item that has no image_url -
    used only in the always-sent text listing (see store() below), so a
    reader can tell why that one skin has no photo above it without
    losing the name/price line entirely."""
    caption = _item_caption(item)
    return caption if item.get("image_url") else f"{caption} (без фото)"


def _format_store_text(skins_json: str, checked_at: str) -> str:
    """Text-only rendering of the store - used when the payload can't be
    parsed at all, or as a fallback when sending photos itself fails
    (bad/expired image URL, Telegram API hiccup)."""
    parsed = _parse_store_payload(skins_json)
    if parsed is None:
        return f"\U0001f3ae Магазин ({checked_at}):\n{skins_json}"

    items, reset_in = parsed
    lines = [f"\U0001f3ae Магазин VALORANT (обновлено {checked_at}):"]
    lines.extend(f"• {_item_line(item)}" for item in items)
    if reset_in:
        lines.append(f"\n⏳ Сброс через: {reset_in}")
    return "\n".join(lines)


class Handlers:
    def __init__(
        self,
        db: Database,
        wishlist: WishlistStore,
        config: AppConfig,
        ai_manager: AIManager | None = None,
    ):
        self.db = db
        self.wishlist = wishlist
        self.config = config
        # Optional - spec §95 Этап O wires this in from run.py once an
        # LLM_API_KEY is configured; None keeps the old plain-passthrough
        # chat() behavior (and every existing test for it) working
        # unchanged, rather than forcing every caller/test to construct a
        # full AIManager just to exercise Handlers.
        self.ai_manager = ai_manager
        # spec §34's short-term dialogue context - see _MAX_HISTORY_MESSAGES.
        self._chat_history: list[dict] = []

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
            "/schedule - расписание занятий (SSO)\n"
            "/umkd [курс] - материалы УМКД (SSO); без аргумента - список курсов со счётчиками\n"
            "/week - текущая учебная неделя (1-15) + номер лабы/практики\n"
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

        # Telegram's own clients only ever surface ONE caption (or none at
        # all) in a media group's compact feed view - a real album sent
        # live showed every per-photo caption above simply invisible, even
        # though they were set correctly (Telegram's real, documented album
        # UI limitation, not a bug on our side). So the name/price text is
        # NEVER left to those per-photo captions alone: this always-sent
        # follow-up message repeats every item's name and price as plain
        # text, guaranteed visible regardless of how any given Telegram
        # client chooses to render the album above it.
        tail_lines = [f"\U0001f3ae Магазин VALORANT (обновлено {row['checked_at']}):"]
        tail_lines.extend(f"• {_item_line(item)}" for item in items)
        if reset_in:
            tail_lines.append(f"\n⏳ Сброс через: {reset_in}")
        await update.message.reply_text("\n".join(tail_lines))

    async def schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Shows the SSO Agent's collected weekly schedule (Этап B data -
        stud.satbayev.university's real GetTable API, agents/sso/parser.py).

        Same honest-degradation shape as /tasks: "no semester saved yet"
        (agent never ran/never got past IsAuthenticated) is a different
        message from "semester known but empty" (get_sso_schedule
        returned nothing for it), and a failed LAST run still shows
        whatever was last saved successfully, with a stale-data warning -
        exactly like /store does for VALORANT via last_run().
        """
        semester_id = self.db.get_latest_sso_semester_id()
        if semester_id is None:
            await update.message.reply_text(
                "Расписание пока недоступно — SSO Agent либо ещё не запускался, либо не "
                "смог авторизоваться в sso.satbayev.university. Проверь /status: sso."
            )
            return

        last_run = self.db.last_run("sso")
        if last_run is not None and last_run["status"] != "working":
            await update.message.reply_text(
                f"⚠️ Последний прогон SSO Agent завершился с ошибкой ({last_run['status']}) — "
                f"ниже последнее успешно сохранённое расписание, оно может быть устаревшим."
            )

        rows = self.db.get_sso_schedule(semester_id)
        if not rows:
            await update.message.reply_text(
                f"\U0001f4c5 Расписание (семестр {semester_id}): записей пока нет."
            )
            return

        by_day: dict[str | None, list] = {}
        for row in rows:
            by_day.setdefault(row["day_title"], []).append(row)

        lines = [f"\U0001f4c5 Расписание (семестр {semester_id}):"]
        for day_title in sorted(by_day, key=_sso_day_sort_key):
            lines.append(f"\n{_sso_day_label(day_title)}:")
            day_rows = sorted(by_day[day_title], key=lambda r: r["start_time"] or "")
            lines.extend(_sso_schedule_line(row) for row in day_rows)

        await update.message.reply_text("\n".join(lines))

    async def umkd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Shows the SSO Agent's collected УМКД (study materials) metadata
        only - names/categories/course, never the files themselves (see
        agents/sso/parser.py's docstring: Umkd/Download is deliberately
        never called, so there's nothing to download here either).

        No argument: a per-course summary (real data is ~7 courses, so
        this always fits one message) plus how to drill into one.
        With an argument: filters to courses whose title contains it
        (case-insensitive substring) and lists every matching file,
        capped at _MAX_UMKD_LINES so one unusually large course folder
        can't blow past Telegram's message-length limit.
        """
        materials = self.db.get_sso_materials()
        if not materials:
            await update.message.reply_text(
                "УМКД данных пока нет — SSO Agent либо ещё не запускался, либо не нашёл ни "
                "одной папки с материалами. Проверь /status: sso."
            )
            return

        last_run = self.db.last_run("sso")
        if last_run is not None and last_run["status"] != "working":
            await update.message.reply_text(
                f"⚠️ Последний прогон SSO Agent завершился с ошибкой ({last_run['status']}) — "
                f"ниже последние успешно сохранённые материалы, они могут быть устаревшими."
            )

        query = " ".join(context.args).strip().lower() if context.args else None

        if not query:
            counts: dict[str, int] = {}
            for material in materials:
                course = material["course_title"] or "Без курса"
                counts[course] = counts.get(course, 0) + 1
            lines = [f"\U0001f4da УМКД — всего материалов: {len(materials)}\n"]
            lines.extend(f"• {course}: {count}" for course, count in sorted(counts.items()))
            lines.append("\nПодробнее: /umkd <название курса>")
            await update.message.reply_text("\n".join(lines))
            return

        matched = [m for m in materials if query in (m["course_title"] or "").lower()]
        if not matched:
            await update.message.reply_text(f"По запросу «{query}» материалов не найдено.")
            return

        lines = [f"\U0001f4da УМКД по запросу «{query}» ({len(matched)}):"]
        shown = matched[:_MAX_UMKD_LINES]
        for material in shown:
            category = material["file_category_title"] or "?"
            lines.append(f"• [{category}] {material['file_name']}")
        if len(matched) > len(shown):
            lines.append(f"\n… показаны первые {len(shown)} из {len(matched)}.")
        await update.message.reply_text("\n".join(lines))

    async def week(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Current teaching week (utils/academic_calendar.py - Desae's own
        request, not part of bro's ТЗ): one lab + one practice per course
        per teaching week, so the week number IS the lab/practice number.
        Lists it per real course when SSO course data is available
        (self.db.get_latest_sso_semester_id()/get_sso_courses()), falls
        back to a bare week number when it isn't (SSO Agent hasn't run
        yet - this command still works either way, since the week
        calculation itself doesn't depend on SSO data at all).
        """
        today = today_in_kz()
        week1_start = self.config.semester1_week1_start.date()

        if today < week1_start:
            await update.message.reply_text(
                f"Семестр ещё не начался — старт {week1_start.isoformat()}."
            )
            return

        current = current_teaching_week(today, week1_start)
        if current is None:
            await update.message.reply_text(
                f"Учебные недели закончились ({TOTAL_TEACHING_WEEKS} из {TOTAL_TEACHING_WEEKS}) "
                "— похоже, сейчас сессия или каникулы."
            )
            return

        start, end = teaching_week_bounds(current, week1_start)
        lines = [
            f"\U0001f4c6 Учебная неделя: {current} из {TOTAL_TEACHING_WEEKS} "
            f"({start.isoformat()} – {end.isoformat()})"
        ]
        if current == FIRST_ATTESTATION_WEEK:
            lines.append(
                f"\U0001f4dd 1-я аттестация: к этой неделе — {current} практик и "
                f"{current} лабораторных по каждому курсу."
            )

        semester_id = self.db.get_latest_sso_semester_id()
        courses = self.db.get_sso_courses(semester_id) if semester_id is not None else []
        if courses:
            lines.append(f"\nНа этой неделе (по каждому курсу — лаба №{current}, практика №{current}):")
            lines.extend(f"• [{c['code']}] {c['title']}" for c in courses)
        else:
            lines.append(
                f"\nНа этой неделе — лаба №{current}, практика №{current} по каждому курсу "
                "(список курсов пока недоступен — SSO Agent ещё не запускался)."
            )

        await update.message.reply_text("\n".join(lines))

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
        """Ad-hoc chat, routed through the AI Manager (AI Manager ТЗ v3
        §43's Telegram Adapter layer - Telegram Adapter → AI Manager →
        Tools, never Telegram → Tools directly and never AI Manager
        constructing its own Bot instance).

        Handles any plain-text message that isn't a slash command - no
        /ask prefix needed, per bro's Phase 5-adjacent review: slash
        commands stay for technical/debug actions (/status, /tasks,
        /weather, ...), plain conversation goes to the AI Manager.

        When ``self.ai_manager`` is None (no LLM_API_KEY configured, so
        run.py never built one - see run.py), this falls back to the
        original plain passthrough (get_provider()/provider.generate()
        directly) so the "no key" experience is unchanged and this
        method never requires an AIManager to be constructed just to be
        called (kept exactly as-is for backward compatibility - spec
        §80 - rather than swapping in a fake/minimal AIManager here).
        """
        question = update.message.text
        if not question or not question.strip():
            return

        if self.ai_manager is None:
            await self._chat_without_ai_manager(question, update)
            return

        try:
            # A copy, not a reference - self._chat_history is mutated
            # right below on success, and AIManager itself also mutates
            # its own local `messages` list built from `history` (see
            # agents/ai_manager/agent.py's handle_message()); neither
            # mutation should ever alias this handler's own list.
            result = await self.ai_manager.handle_message(
                question, history=list(self._chat_history)
            )
        except Exception:  # noqa: BLE001 - never let AI Manager/provider/tool failures crash the bot
            logger.exception("AI Manager failed while answering a chat message")
            await update.message.reply_text(
                "⚠️ Не удалось получить ответ. Подробности в логах."
            )
            return

        # spec §61-62 - correlate this turn's outcome without logging its
        # actual (potentially sensitive - user's own study data) content.
        logger.info(
            "AI Manager [%s]: status=%s tool_calls=%d",
            result.request_id, result.status, len(result.tool_calls),
        )

        if result.status == "OK":
            # Only a successful turn extends the remembered dialogue -
            # a provider/timeout/max-iterations failure's half-formed
            # exchange shouldn't poison the next turn's context.
            self._chat_history.append({"role": "user", "content": question})
            self._chat_history.append({"role": "assistant", "content": result.text})
            if len(self._chat_history) > _MAX_HISTORY_MESSAGES:
                self._chat_history = self._chat_history[-_MAX_HISTORY_MESSAGES:]

        await update.message.reply_text(result.text)

    async def _chat_without_ai_manager(self, question: str, update: Update) -> None:
        """Original Phase-5 plain LLM passthrough - preserved as the
        fallback for when no AIManager was configured (see chat())."""
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
