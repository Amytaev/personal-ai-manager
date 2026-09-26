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

**Честное ограничение**: `LOGGED_IN_URL_HINT` (эвристика "залогинен
или нет" по URL) — предположение, не проверено против реального
Teams (в песочнице Claude нет сети до `teams.microsoft.com` и не
установлен сам браузер Chromium). `scripts/teams_login_setup.py`
печатает реальный URL после успешного входа — это нужно прислать
обратно, чтобы поправить эвристику по факту, а не по догадке.

## Что дальше — нужны две вещи от тебя

1. **Запусти `python -m scripts.teams_login_setup` локально** (после
   `pip install -r requirements.txt` и **`playwright install chromium`**
   — это отдельный шаг, pip браузер не скачивает). Один раз войди в
   Teams в открывшемся окне. Пришли, что напечатает скрипт (особенно
   финальный URL).
2. **Запусти `python -m scripts.teams_capture_assignments`**, зайди в
   любую команду → вкладку Assignments, полистай Upcoming/Overdue/
   Returned. Скрипт сам сохранит подходящие сетевые ответы в
   `teams_capture/responses.jsonl`, а по Enter в терминале — снимок
   HTML текущей страницы. **Проверь файлы сам перед тем, как прислать**
   — это твои настоящие названия курсов/заданий.

По этим двум наборам данных я напишу `agents/teams/parser.py` и
`agents/teams/agent.py` — с реальной структурой ответа, а не
предположением о том, как это должно выглядеть.

## Тесты

**60 тестов, все проходят** (53 из Phase 2-4 + 7 новых для
`TeamsSession`, все на замоканных объектах Playwright — реальный
браузер в тестах не запускается и не нужен: логика повторного
использования контекста, переключение headless-режима, обработка
таймаута логина).

```powershell
pip install -r requirements.txt
playwright install chromium
pytest -q
```
