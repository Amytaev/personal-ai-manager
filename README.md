# Personal AI Manager

Локальная система персональной автоматизации (Teams / Weather / VALORANT →
AI Manager → Telegram). Полное ТЗ см. `TZ_v4.md` (хранится отдельно).

**Текущее состояние: Phase 3 — Core Infrastructure + Telegram Bot.**
Teams / Weather / VALORANT агенты и сам AI Manager ещё **не реализованы**
(Phase 4-7) — команды бота, которые должны показывать их данные, честно
читают из (пока пустых) SQLite-таблиц и говорят "агент не реализован",
а не выдумывают ответ.

## Что уже есть

| Компонент | Файл | Статус |
| --- | --- | --- |
| BaseAgent + структурированный результат + error isolation | `agents/base.py` | ✅ Phase 2 |
| SQLite схема + storage layer (dedup, retention) | `storage/models.py`, `storage/database.py` | ✅ Phase 2 |
| Scheduler + single-instance guard + graceful shutdown | `scheduler/scheduler.py` | ✅ Phase 2 |
| Retry/backoff, LLM provider abstraction | `utils/retry.py`, `llm/provider.py` | ✅ Phase 2 |
| **Telegram bot**: все команды из ТЗ | `telegram_bot/handlers.py` | ✅ Phase 3 |
| **chat_id авторизация** (single-user) | `telegram_bot/auth.py` | ✅ Phase 3 |
| **Wishlist** (JSON, /addskin /removeskin /wishlist) | `storage/wishlist.py` | ✅ Phase 3 |
| **Дедуплицированные уведомления** (`notify()`) | `telegram_bot/bot.py` | ✅ Phase 3, механизм готов, реальных вызовов пока нет (это Phase 7) |
| Startup/shutdown бота вместе со scheduler | `run.py` | ✅ Phase 3 |
| Teams / Weather / VALORANT агенты | `agents/` | ⏳ Phase 4/5/6 |
| AI Manager (aggregation/priority/LLM summary) | `manager/` | ⏳ Phase 7 |

## Команды бота

`/start /help /status /tasks /weather /store /briefing /wishlist /addskin /removeskin`

Каждая команда сначала проверяет `incoming_chat_id == TELEGRAM_CHAT_ID`
(`telegram_bot/auth.py`) — чужой chat не получает вообще никакого ответа,
даже отказа.

## Установка

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Заполни `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в `.env`, чтобы бот
реально стартовал (без них `run.py` работает как в Phase 2, без бота,
с явным предупреждением в логе).

## Запуск

```powershell
python run.py
```

## Тесты

```powershell
pytest -q
```

**43 теста, все проходят** (17 из Phase 2 + 26 новых в Phase 3): auth-декоратор
(авторизован/не авторизован/сравнение int-vs-str chat_id), wishlist
(add/remove/dedup/повреждённый файл), все хендлеры команд на реальной
tmp-БД с замоканным Telegram `Update`, и дедупликация `notify()`
(включая кейс "отправка упала — не должно помечаться как отправленное").

### ⚠️ Что НЕ было проверено вживую и почему

В этой среде разработки исходящий сетевой доступ ограничен списком
доменов, `api.telegram.org` туда не входит (подтверждено: попытка
запроса возвращает `403 Forbidden` от egress-прокси мгновенно, не
зависает). Это значит: **реальный long-polling против настоящего
Telegram API здесь физически невозможно проверить** — ни моими
силами, ни любым другим инструментом в этом контейнере.

Всё, что можно было проверить без сети, проверено по-настоящему:
- сборка `Application` из токена (`Application.builder().token(...).build()`)
  не делает сетевых вызовов — протестировано;
- вся бизнес-логика хендлеров (что бот ОТВЕЧАЕТ на каждую команду)
  протестирована с замоканным `Update`/`Context`;
- **сценарий реального сбоя запуска бота я воспроизвёл по-настоящему**:
  запустил `run.py` с fake-токеном, получил настоящую сетевую ошибку от
  заблокированного `api.telegram.org` — и именно так нашёл и исправил
  реальный баг (см. ниже).

**Тебе нужно самому один раз прогнать `python run.py` с настоящим
`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` на своей машине** и вручную
проверить каждую команду в самом Telegram — это единственный способ
закрыть этот пробел, я не могу сделать это за тебя из песочницы.

## Два реальных бага, найденных и исправленных при тестировании

1. **`telegram/` как имя пакета конфликтует с самой библиотекой
   `python-telegram-bot`** (она импортируется как `import telegram`).
   Изначальная структура из ТЗ (`telegram/bot.py` и т.д.) ломает импорт
   стороннего пакета — Python видит наш локальный `telegram/` раньше
   установленного. Переименовал в **`telegram_bot/`** — это единственное
   отступление от структуры ТЗ, и оно вынужденное, не косметическое.
2. **Падение `bot.start()` (нет сети / неверный токен) оставляло
   lock-файл висеть навсегда.** Cleanup был только вокруг
   `stop_event.wait()`, а не вокруг всего старта — если бот падал ДО
   этой точки, `guard.release()` просто не вызывался. Следующий запуск
   получал ложное "Application already running" при отсутствии
   реального процесса. Исправлено: весь блок старта (scheduler + bot)
   теперь внутри одного `try`, `finally` очищает bot/scheduler/lock по
   отдельности (сбой одного шага очистки не блокирует остальные) —
   воспроизведено и перепроверено вручную до и после фикса.

## Технический долг / сознательно не сделано в Phase 3

- `notify()` в `telegram_bot/bot.py` реализован и протестирован
  (dedup через ту же таблицу `notifications`, что и в Phase 2), но
  реальных вызовов пока нет — их добавит AI Manager в Phase 7, когда
  появятся события, о которых нужно проактивно уведомлять.
- `/status`, `/tasks`, `/weather`, `/store`, `/briefing` читают сырые
  данные из SQLite напрямую, без приоритизации/LLM-форматирования —
  это Phase 7.
- Webhook-режим не рассматривался, используется long-polling
  (`updater.start_polling()`) — для личного бота с одним пользователем
  этого достаточно, вебхук потребовал бы публичного HTTPS-эндпоинта.
