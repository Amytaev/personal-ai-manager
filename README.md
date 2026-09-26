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

## Что дальше — нужны две вещи от тебя

1. ~~Запусти `python -m scripts.teams_login_setup` локально~~ —
   **готово**, вход подтверждён реальным тестом (см. выше).
2. **Запусти `python -m scripts.teams_capture_assignments` (обновлённая
   версия)**, зайди в любую команду → вкладку Assignments, полистай
   Upcoming/Overdue/Returned. Скрипт сам сохранит подходящие сетевые
   ответы в `teams_capture/responses.jsonl`, а по Enter в терминале —
   снимок HTML текущей страницы. **Проверь файлы сам перед тем, как
   прислать** — это твои настоящие названия курсов/заданий.

   *Найденный и исправленный баг*: в предыдущей версии скрипт открывал
   новую страницу браузера, но никогда не переходил на сам Teams
   (`page.goto(...)` отсутствовал) — поэтому окно открывалось на пустой
   `about:blank` и не давало никуда кликнуть. Теперь страница сразу
   переходит на `TEAMS_URL`, используя уже сохранённую сессию логина.
   Скачай обновлённый код и запусти скрипт заново.

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
