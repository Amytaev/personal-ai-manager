# Personal AI Manager

Локальная система персональной автоматизации (Teams / Weather / VALORANT →
AI Manager → Telegram). Полное ТЗ см. `TZ_v4.md` (хранится отдельно).

**Текущее состояние: Phase 5 — Teams Agent, инфраструктура (в процессе).**
Полноценного парсера заданий ещё нет — см. "Что дальше" ниже, это не
недоделка, а осознанная остановка перед сбором реальных данных.

## Что уже есть

| Компонент | Файл | Статус |
| --- | --- | --- |
| Core infra, Telegram bot, Weather Agent | — | ✅ Phase 2-4 |
| **Microsoft Graph education API — проверено на реальном аккаунте** | — | ✅ VERIFIED: требует admin consent, недоступно студенту |
| **Persistent Teams browser session** (Interactive Authentication) | `agents/teams/auth.py` | ✅ Phase 5, протестировано на моках Playwright |
| Диагностический скрипт: первый интерактивный вход | `scripts/teams_login_setup.py` | ✅ готов к запуску пользователем |
| Диагностический скрипт: захват реальной структуры Assignments | `scripts/teams_capture_assignments.py` | ✅ готов к запуску пользователем |
| Парсер заданий Teams → `Task` model | `agents/teams/parser.py` | ⏳ ждёт реальных данных от диагностики |
| Teams Agent (полный цикл run()) | `agents/teams/agent.py` | ⏳ то же |

## Phase 1 → Phase 5: закрытый вопрос про Graph API

**Подтверждено на реальном аккаунте KazNITU/Satbayev University** (не
предположение из документации — реальный клик в Microsoft Graph
Explorer, `EduRoster.ReadBasic`, `Admin consent required: Yes`):

> «Требуется утверждение администратора. Для Graph Explorer требуется
> разрешение на доступ к глобальным ресурсам в вашей организации,
> которое может выдать только администратор.»

Т.е. Graph API как путь получения заданий закрыт для обычного
студенческого аккаунта — ровно то, что предсказывал Phase 1 Architecture
Review. Переходим на Playwright + Interactive Authentication, как и
планировалось изначально.

Также подтверждено (открытые источники Satbayev University): реальный
источник заданий — вкладка **Assignments внутри Microsoft Teams**
(не PolytechOnline — тот используется для другого).

## Interactive Authentication — как это работает

`agents/teams/auth.py` — `TeamsSession`:

- держит один persistent Playwright-профиль (`TEAMS_PROFILE_PATH` в
  `.env`, по умолчанию `data/teams_browser_profile/`) — обычная папка
  профиля Chromium на диске, с cookies и сессией, как у настоящего
  браузера;
- `login_interactively()` — открывает **видимое** окно браузера,
  пользователь сам логинится (включая MFA/Conditional Access), пароль
  приложение не видит и не трогает;
- `is_logged_in()` — headless-проверка валидности уже сохранённой
  сессии, для использования в реальном планировщике;
- при смене headless-режима старый контекст закрывается перед
  созданием нового (Chromium не даёт открыть один и тот же профиль
  дважды параллельно).

**`LOGGED_IN_URL_HINT` подтверждён реальным тестом**: на живой машине
`scripts/teams_login_setup.py` после успешного входа напечатал финальный
URL, и он совпал с `LOGGED_IN_URL_HINT` в точности — эвристика оказалась
верной с первого раза, догадку больше не нужно перепроверять.

## Реальный API заданий — и почему он не Graph

`python -m scripts.teams_capture_assignments` уже дал 239 реальных
сетевых ответов с живого тенанта. Источник данных оказался не
Graph API и не DOM-парсинг, а собственный backend веб-клиента Teams
Education:

```
https://assignments.edu.cloud.microsoft/api/v1.0/edu/me/work?$filter=...
```

Это тот же `educationAssignment`-объект, что и в Graph (`displayName`,
`dueDateTime`, `status`, `isCompleted`, `allTurnedIn`, вложенный
`submissions[0].status/submittedDateTime`), но три разных `$filter`
на `/edu/me/work` дают ровно три списка, которые и нужны:

- `dueDateTime ge <now>` → **Upcoming**
- `isCompleted eq false and dueDateTime le <now> and allTurnedIn eq false` → **Overdue**
- `isCompleted eq true` (с пагинацией через `$skiptoken`) → **Completed/Returned**

**Честная оговорка про стабильность**: это *не* официальный,
версионированный, документированный Graph API — это внутренний
backend, на который опирается сам JS-клиент Teams Education
(`eduassignmentsui`). У него нет публичного контракта обратной
совместимости: Microsoft может поменять форму ответа или путь без
предупреждения при обновлении клиента. Это вынужденный выбор — Graph
закрыт admin consent'ом (см. выше), а DOM-парсинг был бы ещё более
хрупким — но `agents/teams/parser.py` и `agents/teams/agent.py`
пишутся с расчётом на то, что эта форма ответа может однажды
измениться: парсинг оборачивается в защитный код (как уже сделано для
Weather в Phase 4 — `try/except` вместо жёсткого предположения о
форме), а `run_isolated()` переводит агента в `FAILING`, а не роняет
всё приложение, если API вдруг изменится.

Не хватает одного: у объекта задания есть только `classId` (GUID), не
название курса — резолвинг `classId → название` ещё предстоит найти
(обновлённый скрипт уже ловит более широкий набор URL с `class`/`team`
в пути для следующего захвата).

## Ask Claude — `/ask` в Telegram

Добавлена возможность указать свой Claude API-ключ и получить рабочий
чат прямо в Telegram, отдельно от Teams/Weather/VALORANT-агентов:

- в `.env` заполни `LLM_API_KEY` (ключ с
  [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)
  — это отдельный аккаунт/ключ от обычного claude.ai, платится по
  использованию) и, при желании, `LLM_MODEL` (по умолчанию
  `claude-sonnet-5`);
- команда `/ask <вопрос>` отправляет вопрос напрямую модели и
  возвращает ответ в чат;
- если `LLM_API_KEY` не задан, бот вежливо скажет об этом вместо того,
  чтобы упасть (`NullProvider`, уже был в Phase 2);
- это **не** автоматическая сводка по заданиям/погоде — просто ручной
  чат. Автоматическую LLM-сводку по данным агентов добавит Phase 7 (AI
  Manager), эта команда её не заменяет и не блокирует.

## Тесты

**64 теста, все проходят** (53 из Phase 2-4 + 7 для `TeamsSession` на
замоканных объектах Playwright — реальный браузер в тестах не
запускается и не нужен: логика повторного использования контекста,
переключение headless-режима, обработка таймаута логина + 4 новых для
`/ask`: нет ключа → просят настроить, есть ответ → возвращается как
есть, провайдер падает → пользователю дружелюбное сообщение без утечки
текста исключения).

```powershell
pip install -r requirements.txt
playwright install chromium
pytest -q
```
