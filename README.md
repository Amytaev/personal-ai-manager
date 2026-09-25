# Personal AI Manager

Локальная система персональной автоматизации (Teams / Weather / VALORANT →
AI Manager → Telegram). Полное ТЗ см. `TZ_v4.md` (не входит в этот архив —
хранится отдельно).

**Текущее состояние: Phase 2 — Core Infrastructure.** Ни один из трёх
агентов (Teams/Weather/VALORANT), Telegram-бот и сам AI Manager ещё **не
реализованы** — это Phase 3–7. Phase 2 строит только фундамент, на
котором они будут собраны.

## Что уже есть

| Компонент | Файл | Статус |
| --- | --- | --- |
| BaseAgent + структурированный результат + error isolation | `agents/base.py` | ✅ реализовано, протестировано |
| SQLite схема (tasks/weather_snapshots/valorant_store/notifications/agent_runs) | `storage/models.py` | ✅ |
| Storage layer (connection, agent_runs, dedup, retention cleanup) | `storage/database.py` | ✅ реализовано, протестировано |
| Scheduler foundation + single-instance guard + graceful shutdown | `scheduler/scheduler.py` | ✅ реализовано, протестировано |
| Retry/backoff декоратор | `utils/retry.py` | ✅ реализовано, протестировано |
| LLM provider abstraction (Anthropic + Null fallback) | `llm/provider.py` | ✅ реализовано, протестировано |
| Конфигурация из `.env` | `config.py` | ✅ |
| Логирование (rotating file + console, без credentials) | `logging_config.py` | ✅ |
| Entrypoint, связывающий всё вместе | `run.py` | ✅ (без агентов — просто держит lock, крутит scheduler с одной задачей retention cleanup, корректно завершается) |
| **Teams / Weather / VALORANT агенты** | `agents/` | ⏳ Phase 4/5/6 |
| **AI Manager** (aggregation/priority/LLM summary) | `manager/` | ⏳ Phase 7 (сейчас только заглушка с комментарием) |
| **Telegram bot** | `telegram/` | ⏳ Phase 3 (сейчас только заглушка с комментарием) |

## Установка

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Заполни `.env` — на этом этапе реально нужен только `LLM_API_KEY`, если
хочешь проверить `AnthropicProvider` (без него используется `NullProvider`,
тесты и `run.py` работают и так).

## Запуск

```powershell
python run.py
```

Поднимет логирование, займёт single-instance lock, запустит scheduler с
единственной задачей — ежедневной retention-очисткой БД (`DATA_RETENTION_DAYS`,
по умолчанию 30 дней) — и будет висеть, ожидая Ctrl+C. Повторный запуск
второй копии откажет с понятным сообщением (`Application already running`).

## Тесты

```powershell
pytest -q
```

17 тестов, все проходят. Покрытие: BaseAgent/error isolation, storage
(schema/dedup/retention), single-instance guard, retry/backoff, LLM
provider selection.

## Известный нюанс: graceful shutdown на Windows

`asyncio.loop.add_signal_handler()` **не реализован на Windows вообще**
(`NotImplementedError` на дефолтном event loop) — при этом Windows 11
целевая платформа проекта. Поэтому:

- **SIGTERM** обрабатывается через `add_signal_handler` — работает на
  Linux/Mac (для разработки), молча пропускается на Windows.
- **Ctrl+C (SIGINT)** работает на всех платформах, включая Windows, через
  штатный механизм `asyncio.run()`: он сам превращает `KeyboardInterrupt`
  в отмену (`CancelledError`) текущей задачи, поэтому cleanup-код в
  `run.py` находится в голом `finally` (а не в `except KeyboardInterrupt`
  внутри корутины — это была реальная ошибка в первой версии, поймана и
  исправлена при тестировании: `finally` отрабатывает при любом типе
  исключения, `except KeyboardInterrupt` внутри `await` — нет).

Проверено вручную: сигнал доставляется, весь cleanup (лог "Shutdown
complete", закрытие scheduler, снятие lock-файла) отрабатывает до выхода
процесса, без зависаний и без "грязного" traceback.

Для Phase 8 (запуск как служба через NSSM/pywin32/Task Scheduler — см.
Phase 1 Architecture Review) может понадобиться отдельно проверить, как
именно каждый из трёх вариантов доставляет stop-сигнал процессу на
Windows — это не то же самое, что Ctrl+C в интерактивной консоли.

## Технический долг / что сознательно не сделано в Phase 2

- `manager/` и `telegram/` — только заглушки с докстрингом, отсылающим к
  своей фазе. Пустых классов-болванок специально не создавал, чтобы не
  плодить код, который придётся переписывать в Phase 3/7.
- `requirements.txt` содержит закомментированный список зависимостей
  будущих фаз (`python-telegram-bot`, `playwright`, `httpx`, `keyring`) —
  не установлены и не импортируются нигде в Phase 2, добавлены только как
  справка.
- `DATA_RETENTION_DAYS` очищает `weather_snapshots` и `agent_runs` по
  времени; `tasks`/`notifications` сознательно не трогает (см. коммент в
  `storage/database.py`) — они по ТЗ живут по другой логике (учебный
  период / дедупликация), которая появится вместе с Teams-агентом.
