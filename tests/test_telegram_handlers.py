from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm.provider import NullProvider
from storage.wishlist import WishlistStore
from telegram_bot.handlers import Handlers


def _fake_update_and_context(args: list[str] | None = None, text: str | None = None):
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    update.message.reply_media_group = AsyncMock()
    update.message.text = text
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
    # Teams Agent is implemented (Phase 5) - an empty tasks table now
    # honestly means "hasn't run yet / couldn't authenticate", not "not
    # implemented", so this checks for that distinction instead.
    update, context = _fake_update_and_context()
    await handlers.tasks(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "не запускался" in text


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
    assert "не запускался" in text


@pytest.mark.asyncio
async def test_store_sends_media_group_with_captions_and_reset_time(handlers, tmp_db):
    # Real shape (Phase 6.3): 2+ items with image_url -> one media group
    # message (Telegram requires >=2 items for sendMediaGroup), each
    # photo captioned with name + price, followed by a text message with
    # the reset countdown.
    tmp_db.save_valorant_store(
        [
            {"uuid": "88f1bcbd-4dfd-f2ef-8a2c-44b3baa26b3c", "name": "Апертура", "price_vp": 1275, "image_url": "https://example.com/a.png"},
            {"uuid": "72b3bacc-48ac-85f7-ec38-5ab629654486", "name": "Куронами", "price_vp": 2375, "image_url": "https://example.com/b.png"},
        ],
        reset_in="7 часов и 49 минут",
    )

    update, context = _fake_update_and_context()
    await handlers.store(update, context)

    update.message.reply_media_group.assert_awaited_once()
    media = update.message.reply_media_group.call_args.kwargs["media"]
    assert len(media) == 2
    assert media[0].caption == "Апертура — 1275 VP"
    assert media[0].media == "https://example.com/a.png"
    assert media[1].caption == "Куронами — 2375 VP"

    update.message.reply_photo.assert_not_awaited()
    tail_text = update.message.reply_text.call_args.args[0]
    # Real live /store showed Telegram's media-group view rendering NO
    # per-photo captions at all (a real Telegram client limitation, not a
    # bug here) - so the name/price text must always be repeated in this
    # follow-up message too, not just set on the photos themselves.
    assert "Апертура — 1275 VP" in tail_text
    assert "Куронами — 2375 VP" in tail_text
    assert "7 часов и 49 минут" in tail_text


@pytest.mark.asyncio
async def test_store_sends_single_photo_when_only_one_item_has_an_image(handlers, tmp_db):
    tmp_db.save_valorant_store(
        [{"uuid": "u1", "name": "Пустошь", "price_vp": 1275, "image_url": "https://example.com/x.png"}],
        reset_in="1 час",
    )

    update, context = _fake_update_and_context()
    await handlers.store(update, context)

    update.message.reply_photo.assert_awaited_once()
    kwargs = update.message.reply_photo.call_args.kwargs
    assert kwargs["photo"] == "https://example.com/x.png"
    assert kwargs["caption"] == "Пустошь — 1275 VP"
    update.message.reply_media_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_lists_items_missing_an_image_as_text(handlers, tmp_db):
    tmp_db.save_valorant_store(
        [
            {"uuid": "u1", "name": "Апертура", "price_vp": 1275, "image_url": "https://example.com/a.png"},
            {"uuid": "u2", "name": "Куронами", "price_vp": 2375, "image_url": "https://example.com/b.png"},
            {"uuid": "u3", "name": "БезКартинки", "price_vp": 500, "image_url": None},
        ],
        reset_in=None,
    )

    update, context = _fake_update_and_context()
    await handlers.store(update, context)

    update.message.reply_media_group.assert_awaited_once()
    tail_text = update.message.reply_text.call_args.args[0]
    # The full list is always sent, not just the items missing a photo -
    # and the one missing an image is flagged so it's clear why it has no
    # picture above it.
    assert "Апертура — 1275 VP" in tail_text
    assert "Куронами — 2375 VP" in tail_text
    assert "БезКартинки — 500 VP (без фото)" in tail_text


@pytest.mark.asyncio
async def test_store_falls_back_to_text_when_sending_photos_fails(handlers, tmp_db):
    from telegram.error import TelegramError

    tmp_db.save_valorant_store(
        [
            {"uuid": "u1", "name": "Апертура", "price_vp": 1275, "image_url": "https://example.com/a.png"},
            {"uuid": "u2", "name": "Куронами", "price_vp": 2375, "image_url": "https://example.com/b.png"},
        ],
        reset_in="1 час",
    )
    update, context = _fake_update_and_context()
    update.message.reply_media_group = AsyncMock(side_effect=TelegramError("bad photo url"))

    await handlers.store(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Апертура" in text and "1275 VP" in text
    assert "Куронами" in text and "2375 VP" in text


@pytest.mark.asyncio
async def test_store_warns_when_last_agent_run_failed_but_shows_stale_data(handlers, tmp_db):
    tmp_db.save_valorant_store(
        [{"uuid": "u1", "name": "Апертура", "price_vp": 1275, "image_url": "https://example.com/a.png"}],
        reset_in="1 час",
    )
    now = datetime.now(timezone.utc).isoformat()
    tmp_db.record_agent_run("valorant", "failing", now, now, "VALORANT_STORE_PARSE_ERROR: ...")

    update, context = _fake_update_and_context()
    await handlers.store(update, context)

    # First call is the warning, then the photo, then the reset-time tail.
    warning_text = update.message.reply_text.call_args_list[0].args[0]
    assert "устаревш" in warning_text.lower() or "ошибк" in warning_text.lower()
    update.message.reply_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_schedule_honest_when_no_semester_saved_yet(handlers):
    update, context = _fake_update_and_context()
    await handlers.schedule(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "не запускался" in text


@pytest.mark.asyncio
async def test_schedule_honest_when_semester_known_but_empty(handlers, tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85, courses=[{"code": "C1", "title": "Course"}], schedule_entries=[], materials=[]
    )

    update, context = _fake_update_and_context()
    await handlers.schedule(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "85" in text and "записей пока нет" in text


@pytest.mark.asyncio
async def test_schedule_groups_by_day_in_real_weekday_order_not_alphabetical(handlers, tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[],
        schedule_entries=[
            {
                "class_id": 1,
                "course_code": "CSE4112",
                "course_title": "Администрирование систем и сетей",
                "instructor_name": "Иванов И.И.",
                "room_title": "301",
                "class_type": "Лекция",
                "day_title": "TUESDAY_SHORT",
                "start_time": "10:00",
                "end_time": "10:50",
            },
            {
                "class_id": 2,
                "course_code": "CSE5472",
                "course_title": "НИРС",
                "instructor_name": "Петров П.П.",
                "room_title": "212",
                "class_type": "Практика",
                "day_title": "MONDAY_SHORT",
                "start_time": "8:55",
                "end_time": "9:45",
            },
        ],
        materials=[],
    )

    update, context = _fake_update_and_context()
    await handlers.schedule(update, context)

    text = update.message.reply_text.call_args.args[0]
    # MONDAY_SHORT ("Понедельник") must appear before TUESDAY_SHORT
    # ("Вторник") even though alphabetically Tuesday's Russian label
    # sorts first - real weekday order, not a raw ORDER BY day_title.
    assert text.index("Понедельник") < text.index("Вторник")
    assert "CSE4112" in text and "301" in text and "Иванов И.И." in text


@pytest.mark.asyncio
async def test_schedule_warns_when_last_sso_run_failed_but_shows_stale_data(handlers, tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[],
        schedule_entries=[{"class_id": 1, "day_title": "MONDAY_SHORT", "start_time": "8:55", "end_time": "9:45"}],
        materials=[],
    )
    now = datetime.now(timezone.utc).isoformat()
    tmp_db.record_agent_run("sso", "failing", now, now, "SSO_NEEDS_REAUTH")

    update, context = _fake_update_and_context()
    await handlers.schedule(update, context)

    warning_text = update.message.reply_text.call_args_list[0].args[0]
    assert "устаревш" in warning_text.lower() or "ошибк" in warning_text.lower()


@pytest.mark.asyncio
async def test_umkd_honest_when_empty(handlers):
    update, context = _fake_update_and_context()
    await handlers.umkd(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "не запускался" in text


@pytest.mark.asyncio
async def test_umkd_without_args_shows_per_course_counts(handlers, tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[],
        schedule_entries=[],
        materials=[
            {"file_id": 1, "folder_id": 1, "file_name": "a.docx", "course_title": "CSE4112 Сети"},
            {"file_id": 2, "folder_id": 1, "file_name": "b.docx", "course_title": "CSE4112 Сети"},
            {"file_id": 3, "folder_id": 2, "file_name": "c.docx", "course_title": "CSE5472 НИРС"},
        ],
    )

    update, context = _fake_update_and_context()
    await handlers.umkd(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "всего материалов: 3" in text
    assert "CSE4112 Сети: 2" in text
    assert "CSE5472 НИРС: 1" in text


@pytest.mark.asyncio
async def test_umkd_with_query_filters_by_course_and_lists_files(handlers, tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[],
        schedule_entries=[],
        materials=[
            {
                "file_id": 1,
                "folder_id": 1,
                "file_name": "Лекция 1.pdf",
                "file_category_title": "Лекции",
                "course_title": "CSE4112 Администрирование систем и сетей",
            },
            {
                "file_id": 2,
                "folder_id": 2,
                "file_name": "Практика 1.docx",
                "file_category_title": "Практика",
                "course_title": "CSE5472 НИРС",
            },
        ],
    )

    update, context = _fake_update_and_context(args=["4112"])
    await handlers.umkd(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Лекция 1.pdf" in text
    assert "Практика 1.docx" not in text


@pytest.mark.asyncio
async def test_umkd_with_query_no_match_says_so(handlers, tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[],
        schedule_entries=[],
        materials=[{"file_id": 1, "folder_id": 1, "file_name": "a.docx", "course_title": "CSE4112 Сети"}],
    )

    update, context = _fake_update_and_context(args=["nonexistent"])
    await handlers.umkd(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "не найдено" in text


@pytest.fixture
def handlers_with_semester_start(tmp_db, tmp_path):
    wishlist = WishlistStore(tmp_path / "wishlist.json")
    config = MagicMock()
    config.semester1_week1_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return Handlers(db=tmp_db, wishlist=wishlist, config=config)


@pytest.mark.asyncio
async def test_week_before_semester_start(handlers_with_semester_start):
    update, context = _fake_update_and_context()
    with patch("telegram_bot.handlers.today_in_kz", return_value=date(2026, 8, 15)):
        await handlers_with_semester_start.week(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "не начался" in text


@pytest.mark.asyncio
async def test_week_reports_week_number_and_bounds_without_sso_data(handlers_with_semester_start):
    update, context = _fake_update_and_context()
    with patch("telegram_bot.handlers.today_in_kz", return_value=date(2026, 9, 10)):
        await handlers_with_semester_start.week(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "2 из 15" in text
    assert "лаба №2" in text
    assert "ещё не запускался" in text


@pytest.mark.asyncio
async def test_week_lists_real_courses_when_sso_data_exists(handlers_with_semester_start, tmp_db):
    tmp_db.save_sso_snapshot(
        semester_id=85,
        courses=[{"code": "CSE4112", "title": "Администрирование систем и сетей"}],
        schedule_entries=[],
        materials=[],
    )

    update, context = _fake_update_and_context()
    with patch("telegram_bot.handlers.today_in_kz", return_value=date(2026, 9, 10)):
        await handlers_with_semester_start.week(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "CSE4112" in text and "Администрирование систем и сетей" in text


@pytest.mark.asyncio
async def test_week_flags_first_attestation_week(handlers_with_semester_start):
    update, context = _fake_update_and_context()
    with patch("telegram_bot.handlers.today_in_kz", return_value=date(2026, 10, 20)):
        await handlers_with_semester_start.week(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "8 из 15" in text
    assert "1-я аттестация" in text


@pytest.mark.asyncio
async def test_week_after_teaching_ends(handlers_with_semester_start):
    update, context = _fake_update_and_context()
    with patch("telegram_bot.handlers.today_in_kz", return_value=date(2026, 12, 20)):
        await handlers_with_semester_start.week(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "закончились" in text


@pytest.mark.asyncio
async def test_briefing_reports_all_three_sections_as_empty(handlers):
    update, context = _fake_update_and_context()
    await handlers.briefing(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "Teams Agent ещё не запускался" in text
    assert "Weather Agent не реализован" in text
    assert "VALORANT Agent ещё не запускался" in text


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


@pytest.mark.asyncio
async def test_chat_ignores_empty_or_whitespace_only_text(handlers):
    update, context = _fake_update_and_context(text="   ")
    await handlers.chat(update, context)

    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_chat_without_llm_configured_tells_user_to_set_key(handlers):
    update, context = _fake_update_and_context(text="What is 2+2?")
    with patch("telegram_bot.handlers.get_provider", return_value=NullProvider()):
        await handlers.chat(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "LLM_API_KEY" in text


@pytest.mark.asyncio
async def test_chat_returns_the_provider_answer(handlers):
    fake_provider = MagicMock()
    fake_provider.generate = AsyncMock(return_value="4")
    update, context = _fake_update_and_context(text="What is 2+2?")

    with patch("telegram_bot.handlers.get_provider", return_value=fake_provider):
        await handlers.chat(update, context)

    fake_provider.generate.assert_awaited_once_with("What is 2+2?")
    assert update.message.reply_text.call_args.args[0] == "4"


@pytest.mark.asyncio
async def test_chat_reports_a_friendly_error_when_the_provider_fails(handlers):
    fake_provider = MagicMock()
    fake_provider.generate = AsyncMock(side_effect=RuntimeError("boom"))
    update, context = _fake_update_and_context(text="hi")

    with patch("telegram_bot.handlers.get_provider", return_value=fake_provider):
        await handlers.chat(update, context)

    text = update.message.reply_text.call_args.args[0]
    assert "не удалось" in text.lower()
    assert "boom" not in text
