# Personal AI Manager

Локальная система персональной автоматизации (Teams / Weather / VALORANT →
AI Manager → Telegram). Полное ТЗ см. `TZ_v4.md` (хранится отдельно).

**Текущее состояние: Phase 6 — VALORANT Agent (Stack B) реализован.**
Teams Agent (Phase 5) и VALORANT Agent (Phase 6) оба закончены и
подключены в `run.py`; следующий шаг — Phase 7, AI Manager
(агрегация/приоритизация/LLM-сводка поверх всех трёх источников).

## Что уже есть

| Компонент | Файл | Статус |
| --- | --- | --- |
| Core infra, Telegram bot, Weather Agent | — | ✅ Phase 2-4 |
| **Microsoft Graph education API — проверено на реальном аккаунте** | — | ✅ VERIFIED: требует admin consent, недоступно студенту |
| **Persistent Teams browser session** (Interactive Authentication) | `agents/teams/auth.py` | ✅ Phase 5, протестировано на моках Playwright |
| Парсер заданий Teams → `Task` model + Teams Agent (полный цикл run()) | `agents/teams/parser.py`, `agents/teams/agent.py` | ✅ Phase 5, реальные данные подтверждены |
| **Riot RSO (OAuth) — исследовано, недоступно для этого проекта** | — | ✅ VERIFIED: только approved production apps, и даже так не отдаёт Personal Store |
| **Persistent Stack B browser session** | `agents/valorant/auth.py` | ✅ Phase 6, протестировано на моках Playwright |
| Парсер магазина Stack B → skin rows + VALORANT Agent (полный цикл run()) | `agents/valorant/parser.py`, `agents/valorant/agent.py` | ✅ Phase 6, реальные данные подтверждены |
| Диагностические скрипты (вход/захват трафика) | `scripts/teams_login_setup.py`, `scripts/teams_capture_assignments.py`, `scripts/valorant_login_setup.py`, `scripts/valorant_capture_store.py` | ✅ готовы к запуску пользователем |

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
выдуманного имени. Один из них (`46191dc6`), как выяснилось на живых
данных, использует "Задания" как канал для объявлений
преподавателя ("Конференция сегодня в 19:00!"), а не для настоящих
дедлайнов — все датированы апрелем 2026, отсекаются фильтром по дате
ниже, а не намеренно исключаются: `resolve_course_name()`'s fallback
корректно обработал этот edge case без падений.

### Фильтр по дате — старые семестры не попадают в БД

`TeamsAgent` отбрасывает задания с `dueDateTime` раньше
`TEAMS_MIN_DUE_DATE` (`.env`, по умолчанию `2026-09-01T00:00:00Z`) —
**до** записи в БД, а не только при отображении в `/tasks`. Задание
без даты вообще (`dueDateTime` отсутствует) не отбрасывается — судить
о его "возрасте" нечем, а выбросить потенциально актуальное задание
хуже, чем оставить одно старое. Результат прогона теперь возвращает
`dropped_stale` — сколько заданий отсеяно за этот прогон.

Если в БД уже накопился старый мусор с прогона до появления этого
фильтра — `python -m scripts.teams_cleanup_old_tasks` покажет список
и удалит только то, что реально старше порога (с подтверждением).

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

## Phase 6: VALORANT Agent — Stack B вместо RSO

### Почему не официальный Riot API / RSO

Прежде чем писать хоть строчку кода, исследовали два возможных
источника личного ежедневного магазина VALORANT — **официальный RSO
(Riot Sign-On)** и **официальный VALORANT API** — и оба оказались
закрыты:

- **RSO существует и безопасен** (OAuth2, пароль третья сторона не
  получает), но **недоступен для хобби-проекта**: «RSO Clients are
  only available for applications that have an existing, approved
  production application ID» — сначала нужно одобренное
  production-приложение, а на новые приложения Riot присылает
  приглашение сама, self-serve регистрации нет
  ([support-developer.riotgames.com](https://support-developer.riotgames.com/hc/en-us/articles/22801670382739-RSO-Riot-Sign-On)).
- Даже если бы доступ был — **официальный VALORANT API магазин не
  отдаёт в принципе**, ни при каком уровне доступа: прямым текстом в
  документации, среди неодобряемых use-case'ов — «Online store
  tracking or updates. The technology for this does not currently
  exist in the API.»
  ([developer.riotgames.com](https://developer.riotgames.com/docs/valorant)).
- Третий возможный путь — локальный API самого запущенного клиента
  VALORANT (`127.0.0.1` + `lockfile`, на чём построены community-
  библиотеки вроде `python-valclient`) — отклонён по другой причине:
  требует запущенную и залогиненную игру на том же ПК, то есть не
  решает исходную задачу «проверить магазин с телефона».

**Итог**: [Stack B](https://stackb.net/valorant/store) — сторонний
сервис, который сам проходит настоящий Riot-логин и показывает
персональный магазин через свой веб-интерфейс — единственный
практически доступный источник. Подтверждено реальным перехватом
трафика (`scripts/valorant_capture_store.py`,
`valorant_capture/responses.jsonl`): настоящие данные магазина
действительно приходят именно так.

### Как Stack B на самом деле отдаёт магазин

У Stack B **нет документированного JSON API** для магазина. Реальный
перехваченный трафик показал:

- `GET /riot/storefront` возвращает только пустую оболочку страницы —
  Livewire-компонент `storefront` монтируется без товаров;
- сами товары приходят отдельным `POST /livewire/update`, который
  компонент `storefront` сам вызывает через `wire:init="getDailyItems()"`
  сразу после монтирования — и в ответе `effects.html` содержит уже
  полностью отрендеренный на сервере HTML-фрагмент (не структурированный
  JSON: поле `snapshot.data.dailyItems` — это внутренние ID строк БД, а
  не сами товары);
- каждая карточка скина — это `<div class="skin-card" onclick="window.openValorantItem('<uuid>')">`
  с `<img alt="Название">` и ценой прямо перед иконкой VP.

`agents/valorant/agent.py` открывает `/riot/storefront` headless-браузером
(сохранённая сессия Stack B, `agents/valorant/auth.py`) и перехватывает
именно этот `POST /livewire/update` — та же техника «слушать реальный
трафик страницы», что и `TeamsAgent` (см. Phase 5 выше). Затем
`agents/valorant/parser.py` регэкспом достаёт `{uuid, name, price_vp,
image_url}` из HTML-фрагмента — подтверждено на 4 реальных товарах из
настоящего захвата.

### Две разные, специально различаемые ошибки

- **`needs_reauth`** — Stack B редиректнул на `/login`: сессия
  протухла, HTML тут ни при чём. `run.py` шлёт отдельное Telegram-
  уведомление с инструкцией `python -m scripts.valorant_login_setup`
  (дедуплицируется через `notifications`, как у остальных уведомлений).
- **`VALORANT_STORE_PARSE_ERROR`** — сессия в порядке (редиректа на
  `/login` не было), но парсер не нашёл ни одной карточки: значит,
  Stack B поменял вёрстку. Намеренно **не** трактуется как «магазин
  пуст сегодня» — настоящий магазин VALORANT никогда не бывает пустым.

### Честная оговорка про стабильность

Как и Teams API (Phase 5), это внутренний недокументированный backend
чужого веб-клиента — контракта обратной совместимости нет, Stack B
может поменять вёрстку без предупреждения. `parser.py` поэтому
защитный по конструкции: одна нераспознанная карточка просто
пропускается, а не роняет весь прогон — фатальным считается только
случай «карточек не нашлось вообще» (`VALORANT_STORE_PARSE_ERROR`
выше).

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
обычный ассистент — он **не** лезет в БД за реальными заданиями/
погодой/магазином и не притворяется, что уже знает твой день, даже
несмотря на то, что `TeamsAgent`/`WeatherAgent`/`ValorantAgent` теперь
реально кладут данные в `tasks`/`weather_snapshots`/`valorant_store`.
Настоящий intent-routing (Claude сам решает вызвать
`get_tasks()`/`get_weather()`/`get_valorant()` по смыслу сообщения) —
это Phase 7 (AI Manager). Все три источника (Teams, Weather, VALORANT)
теперь готовы — Phase 7 может строить роутер поверх финального набора
интерфейсов, без риска переписывать его под источник, которого ещё не
было.

## Тесты

**116 тестов, все проходят**: 60 из Phase 2-4 (core infra, БД,
scheduler/retry, Weather Agent, Telegram bot/handlers/auth, wishlist,
LLM provider — включая 4 для чата с Claude, `Handlers.chat`) + 36 из
Phase 5 (18 для Teams-парсера, 7 для `TeamsSession`, 11 для реального,
click-driven `TeamsAgent`) + 20 новых для VALORANT (Phase 6, ниже).

- `tests/test_teams_parser.py` (18 тестов) — чистые функции
  `resolve_course_name`/`compute_status`/`parse_assignment`/
  `parse_work_response`/`parse_due` на реальной форме данных: приоритет
  returned > submitted > completed > overdue/upcoming, обработка
  отсутствующего/сломанного `dueDateTime`, `instructions` как строка/
  dict/`None`, несколько курсов в одном ответе.
- `tests/test_teams_agent.py` (11 тестов) — `TeamsAgent.run()` на
  замоканных Playwright Page/Frame/Locator, воспроизводящих реальный
  click-flow (клик по "Задания" → перехват ответа → клик по вкладкам):
  данные только с дефолтной вкладки, склейка нескольких вкладок в один
  результат, дедупликация задания, встреченного на двух вкладках
  сразу, переход состояния `new → unchanged → changed` через три
  последовательных прогона на одной БД, `FAILING` когда дефолтная
  вкладка вообще не отвечает, `FAILING` когда iframe с Assignments не
  находится после открытия дашборда, продолжение прогона (не `FAILING`)
  когда клик по ОДНОЙ вкладке не удался, а остальные данные пришли,
  `aclose()` закрывает только сессию, которой сам владеет, + 3 теста на
  фильтр `min_due_date`: старое задание отсекается и не попадает в БД,
  задание без даты вообще не отсекается, без `min_due_date` фильтрация
  не работает вообще (обратная совместимость).
- `tests/test_valorant_parser.py` (7 тестов) — `parse_storefront_items`
  на HTML-фрагменте, структурно повторяющем реальную карточку Stack B
  (Phase 6.2): извлечение всех карточек с правильными `uuid`/`name`/
  `price_vp`, пустой список для не-магазинного HTML и для пустой
  строки, дедупликация задвоенного `uuid` (карточка ссылается на себя
  дважды — `onclick`/`onkeydown`), fallback-имя при пустом `alt`, плюс
  `parse_reset_time_left` (есть значение / отсутствует / не строка).
- `tests/test_valorant_auth.py` (7 тестов) — `ValorantSession` на
  замоканных объектах Playwright, той же структуры, что и
  `test_teams_auth.py`: `is_logged_in()` true/false по наличию редиректа
  на `/login`, `login_interactively()` возвращает финальный URL (в том
  числе не совпадающий с целевой страницей — как в реальном захвате,
  где логин привёл на `/posts`, а не `/riot/storefront`), `TimeoutError`
  → `ValorantLoginTimeout`, пересоздание контекста при смене
  headless-режима, переиспользование при одинаковом режиме, `close()`.
- `tests/test_valorant_agent.py` (6 тестов) — `ValorantAgent.run()` на
  замоканных Playwright Page, перехватывающих `POST /livewire/update`:
  успешный прогон сохраняет товары в БД и `reset_in`, посторонние
  Livewire-компоненты (`live-comments` и т.п.) игнорируются, редирект на
  `/login` → `FAILING` с `needs_reauth=True` (и ничего не пишется в БД),
  ничего не пришло при валидной сессии → `FAILING` без `needs_reauth`,
  ноль карточек в HTML → `FAILING` с `VALORANT_STORE_PARSE_ERROR` (и
  ничего не пишется в БД), `aclose()` закрывает только свою сессию.

Реальный браузер в тестах не запускается и не нужен нигде — вся
логика Teams Agent и VALORANT Agent (парсинг, дедупликация,
change-detection, error-handling) протестирована на замоканных
объектах.

```powershell
pip install -r requirements.txt
playwright install chromium
pytest -q
```
