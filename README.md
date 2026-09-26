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

У объекта задания есть только `classId` (GUID), не название курса — в
самом `/edu/me/work` названия курса просто нет. Резолвинг
`classId → название` пришлось собирать по кускам из отдельного
эндпоинта `teams.cloud.microsoft/api/csa/apac/api/v3/teams/users/me/updates`
(`teams[].displayName` + `teams[].channels[]/availableChannels[].groupId`,
где `groupId` — это тот же GUID, что и `classId`), плюс один курс
("ВиАУ Вт 7.50") резолвнут вручную по Teams deep-link'у, который
содержит и `groupId`, и человекочитаемое имя в тексте ссылки.

Итог: **5 из 8** встреченных в реальных захватах `classId` резолвятся
в настоящие названия курсов (`agents/teams/parser.py::COURSE_NAMES`).
Оставшиеся 3 — судя по всему, курсы из старых/архивных семестров — не
резолвятся; вместо того чтобы гадать название, `resolve_course_name()`
честно показывает укороченный id (`Курс e3f75118`) вместо
выдуманного имени.

## Teams Agent — готово (Phase 5)

`agents/teams/parser.py` и `agents/teams/agent.py` реализованы и
покрыты тестами (`tests/test_teams_parser.py`,
`tests/test_teams_agent.py`):

- каждое задание нормализуется `parser.py` в строку таблицы `tasks`
  (`storage/models.py`) и апсертится через
  `storage/database.py::upsert_task()`;
- при апсерте вычисляется `state` (`new`/`changed`/`unchanged`) —
  сравнением с уже сохранённым `status`/`due_at`/`submitted_at`/`title`
  по `get_task_fingerprint()`. **Известное ограничение**: задание,
  пропавшее из всех откликов (удалено преподавателем, закрылся
  семестр), никогда не помечается `resolved` — оно просто остаётся в
  БД со старым состоянием. Это не забыто, а сознательно отложено: для
  личного ежедневного брифинга это не критично, но если когда-нибудь
  понадобится — потребует сравнения полного snapshot'а с предыдущим
  прогоном, а не только апсерта по `id`;
- `run.py` подключает `TeamsAgent` в общий scheduler (по аналогии с
  `WeatherAgent`) с интервалом `TEAMS_INTERVAL_MINUTES` (по умолчанию
  60 минут) плюс один прогон сразу при старте.

### Как реально устроен сбор данных — почему не прямой вызов API

Первая версия `agent.py` делала прямой вызов `context.request.get()` к
`/edu/me/work`, предполагая, что кук persistent-профиля хватит для
авторизации. Реальное тестирование на живом тенанте (серия
диагностических скриптов — `scripts/teams_agent_smoke_test.py` →
`teams_agent_navigation_test.py` → `teams_agent_token_test.py` →
`teams_agent_click_test.py`, все сохранены в репозитории как честная
история расследования) последовательно опровергло эту гипотезу:

1. Просто загрузить `teams.microsoft.com` (с ожиданием `networkidle`
   или без) — не авторизует API, 401 в любом случае.
2. Прямой заход на URL страницы класса
   (`assignments.edu.cloud.microsoft/classes/<id>/list`) как
   самостоятельную страницу — грузится, но вообще не вызывает API:
   приложение ждёт postMessage-рукопожатие через Teams SDK с настоящим
   родительским окном Teams, которого при прямой навигации просто нет.
3. API авторизуется Bearer-токеном, который живёт в памяти JS iframe'а
   после настоящего SDK-рукопожатия — не кукой, которую можно было бы
   переиспользовать через `context.request`.
4. Настоящий клик по кнопке "Задания" в левой панели Teams (сделанный
   автоматически через Playwright, headless) **реально проходит** SDK-
   рукопожатие и даёт настоящий `200` с данными — но даже после такого
   клика отдельный `context.request`-вызов всё равно получает `401`:
   токен не покидает iframe.

**Итоговое рабочее решение**: `TeamsAgent` ведёт себя как настоящий
пользователь — открывает Teams, кликает по кнопке "Задания"
(`get_by_role("button", name="Задания")`), затем кликает по вкладкам
дашборда (Готово к оценке / Просрочено / Возвращено — вкладка
"Черновики" пропускается, она для преподавателя) и **перехватывает
JSON-ответы, которые сама страница уже получает** через
`page.on("response", ...)` — та же техника, что и в
`teams_capture_assignments.py`, только автоматически, без участия
человека. Реальные роли/структура вкладок (`role="tab"`, задвоенный
текст из-за скрытой метки для скринридера) подтверждены прямым дампом
accessibility-дерева внутри iframe на живом тенанте — не угаданы.

## Chat with Claude — без `/ask`, просто пиши

Указываешь свой Claude API-ключ, и с ботом можно просто разговаривать —
никакой команды-префикса не нужно:

- в `.env` заполни `LLM_API_KEY` (ключ с
  [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys)
  — это отдельный аккаунт/ключ от обычного claude.ai, платится по
  использованию) и, при желании, `LLM_MODEL` (по умолчанию
  `claude-sonnet-5`);
- **любое обычное сообщение** (без `/` в начале) уходит напрямую
  Claude и ответ приходит в чат — "Скинь мне общую сводку", "Что там
  на сегодня?", "Объясни SQL JOIN" — всё это просто работает;
- слэш-команды (`/status`, `/tasks`, `/weather`, `/wishlist` и т.д.)
  остаются как есть — технические/отладочные действия, они не
  проходят через LLM;
- если `LLM_API_KEY` не задан, бот вежливо скажет об этом вместо того,
  чтобы упасть (`NullProvider`, уже был в Phase 2).

**Важная честная оговорка**: это пока **обычный чат-проход**, не
tool-calling роутер. На "Что там на сегодня?" Claude отвечает как
обычный ассистент — он **не** лезет в БД за реальными заданиями/погодой
и не притворяется, что уже знает твой день, даже несмотря на то, что
`TeamsAgent`/`WeatherAgent` теперь реально кладут данные в `tasks`/
`weather_snapshots`. Настоящий intent-routing (Claude сам решает
вызвать `get_tasks()`/`get_weather()`/`get_valorant()` по смыслу
сообщения) — это Phase 7 (AI Manager), и делать его раньше, чем
VALORANT-агент тоже кладёт данные в БД, значило бы строить роутер
поверх ещё не полного набора источников — почти наверняка придётся
переписывать под финальный набор интерфейсов. Сначала: `TeamsAgent` —
готово; следующий источник (VALORANT, Phase 6) → и только потом
полноценный роутер поверх всего этого.

## Тесты

**88 тестов, все проходят**: 64 из Phase 2-4/раннего Phase 5 (включая
7 для `TeamsSession` на замоканных объектах Playwright и 4 для чата с
Claude, `Handlers.chat`) + 18 для парсера + 8 для реального,
click-driven `TeamsAgent`:

- `tests/test_teams_parser.py` (18 тестов) — чистые функции
  `resolve_course_name`/`compute_status`/`parse_assignment`/
  `parse_work_response` на реальной форме данных: приоритет
  returned > submitted > completed > overdue/upcoming, обработка
  отсутствующего/сломанного `dueDateTime`, `instructions` как строка/
  dict/`None`, несколько курсов в одном ответе.
- `tests/test_teams_agent.py` (8 тестов) — `TeamsAgent.run()` на
  замоканных Playwright Page/Frame/Locator, воспроизводящих реальный
  click-flow (клик по "Задания" → перехват ответа → клик по вкладкам):
  данные только с дефолтной вкладки, склейка нескольких вкладок в один
  результат, дедупликация задания, встреченного на двух вкладках
  сразу, переход состояния `new → unchanged → changed` через три
  последовательных прогона на одной БД, `FAILING` когда дефолтная
  вкладка вообще не отвечает, `FAILING` когда iframe с Assignments не
  находится после открытия дашборда, продолжение прогона (не `FAILING`)
  когда клик по ОДНОЙ вкладке не удался, а остальные данные пришли,
  `aclose()` закрывает только сессию, которой сам владеет.

Реальный браузер в тестах не запускается и не нужен нигде — вся
логика Teams Agent (парсинг, дедупликация, change-detection,
error-handling) протестирована на замоканных объектах.

```powershell
pip install -r requirements.txt
playwright install chromium
pytest -q
```
