# Jobs — Personal AI Assistant

## Обзор

Автономный ИИ-ассистент на базе Claude SDK в Telegram.
Мульти-сессионная архитектура с изоляцией по ролям.
Dual transport: Telethon (userbot) + aiogram (Bot API) — параллельно или по отдельности.

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                    Docker: jobs                                  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Transport Layer                               │   │
│  │  Telethon (userbot)  ←→  IncomingMessage  ←→  Bot (aiogram)│   │
│  │  TG_API_ID/HASH           Transport Protocol   TG_BOT_TOKEN│   │
│  └──────────────────────────────────────────────────────────┘   │
│                          ↓                                       │
│  ┌─────────────────────┐  ┌─────────────────────┐              │
│  │   Owner Sessions    │  │   Group Sessions     │              │
│  │   bypassPermissions │  │   per chat_id        │              │
│  │   Memory, MCP, ...  │  │   mention/reply only │              │
│  └─────────────────────┘  └─────────────────────┘              │
│                                                                  │
│  ┌─────────────────────┐  ┌─────────────────────┐              │
│  │  External Sessions  │  │   Task Sessions     │              │
│  │  default perms      │  │   bypassPermissions │              │
│  │  get_my_tasks, ...  │  │   persistent context│              │
│  └─────────────────────┘  └─────────────────────┘              │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │          TriggerManager                                   │   │
│  │  builtin: scheduler, heartbeat                            │   │
│  │  dynamic: tg_channel subscriptions (DB)                   │   │
│  │         → TriggerExecutor → owner session.query()         │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                  │
│            ↓                                                     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              SQLite (db.sqlite)                          │   │
│  │  • external_users  • tasks (+ next_step, session_id)      │
│  │  • trigger_subscriptions                                  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                  │
│  /data/sessions/  — Claude session IDs                          │
│  /workspace/      — рабочая директория owner'а                  │
│  /workspace/group_logs/ — логи групповых чатов                  │
└─────────────────────────────────────────────────────────────────┘
         │
         │ CDP (Chrome DevTools Protocol)
         ↓
┌─────────────────────────────────────────────────────────────────┐
│                    Docker: browser                               │
│                                                                  │
│  Xvfb (:99) → Chromium → CDP (:9222)                            │
│                  ↓                                               │
│              x11vnc → noVNC (:6080)                              │
│                                                                  │
│  Volumes:                                                        │
│  • browser-profile   — куки, сессии, история                    │
│  • browser-downloads — скачанные файлы                          │
└─────────────────────────────────────────────────────────────────┘
```

## Ключевые модули

| Путь | Описание |
|------|----------|
| `src/users/` | SessionManager, Repository, Tools, Prompts |
| `src/telegram/` | Dual transport (Telethon + Bot), handlers, group log |
| `src/telegram/transport.py` | Transport Protocol + IncomingMessage dataclass |
| `src/telegram/telethon_transport.py` | Telethon (userbot) transport |
| `src/telegram/bot_transport.py` | aiogram 3.x (Bot API) transport + MarkdownV2 |
| `src/telegram/group_log.py` | Логирование групповых чатов + ротация |
| `src/memory/` | MEMORY.md + vector search |
| `src/tools/` | Scheduler + разделение по ролям |
| `src/triggers/` | Unified trigger system (scheduler, heartbeat, tg_channel) |
| `src/mcp_manager/` | Внешние MCP серверы |
| `src/plugin_manager/` | Плагины из маркетплейса |
| `src/skill_manager/` | Управление локальными skills |
| `src/users/sdk_compat.py` | Monkey-patch для Claude SDK (unknown message types) |
| `skills/` | Skills через SDK (монтируется в `.claude/skills/`) |
| `browser/` | Docker-контейнер с Chromium |

## Browser (@playwright/mcp)

Персистентный Chromium через Playwright MCP (snapshot + ref workflow).

**Архитектура:**
- **Chromium** — браузер в контейнере `browser` (Xvfb + x11vnc)
- **HAProxy** — проксирует CDP (:9223 → :9222), перезаписывает Host header
- **`playwright-cdp-wrapper`** — фетчит /json/version, подменяет hostname в WS URL
- **@playwright/mcp** — MCP-сервер, accessibility snapshot + ref-ы элементов
- **noVNC** — просмотр: http://localhost:6080

**Workflow:**
1. `browser_navigate(url)` — открыть страницу
2. `browser_snapshot()` — получить accessibility-дерево с ref-ами
3. Взаимодействие по ref: `browser_click(element, ref)`, `browser_type(element, ref, text)`

**Tools:**
| Tool | Описание |
|------|----------|
| `browser_navigate` | Открыть URL |
| `browser_snapshot` | Accessibility-дерево с ref-ами |
| `browser_click` | Клик по ref элемента |
| `browser_type` | Ввод текста по ref |
| `browser_fill_form` | Заполнить несколько полей |
| `browser_select_option` | Выбрать опцию в dropdown |
| `browser_hover` | Навести курсор |
| `browser_press_key` | Нажать клавишу |
| `browser_take_screenshot` | Скриншот |
| `browser_evaluate` | Выполнить JavaScript |
| `browser_wait_for` | Ждать текст/URL |
| `browser_tabs` | Список вкладок |
| `browser_handle_dialog` | Обработать alert/confirm |

## Skills (нативная поддержка SDK)

Skills работают через `setting_sources=["project"]` в ClaudeAgentOptions.

```
skills/                           # На хосте
└── schedule-meeting/
    └── SKILL.md                  # С YAML frontmatter
        │
        ▼ docker-compose mount
        │
/workspace/.claude/skills/        # В контейнере
└── schedule-meeting/
    └── SKILL.md
```

**SDK автоматически:**
1. Ищет skills в `{cwd}/.claude/skills/`
2. Загружает frontmatter (metadata) в контекст
3. Semantic match: user request ↔ `description`
4. Инжектит SKILL.md body при активации

**SKILL.md формат:**
```yaml
---
name: schedule-meeting
description: Use when user asks to "договорись о встрече", "назначь встречу"...
tools: Read, Bash
---

# Algorithm
1. resolve_user()
2. start_conversation()
...
```

**Cross-session:** Skills могут использовать `ConversationTask` для делегирования задач другим пользователям.

**Управление через чат:**
```
— Создай skill для парсинга hh.ru
— skill_create name="hh-parser" description="..." algorithm="..."

— Покажи все skills
— skill_list
```

**Tools:**
| Tool | Описание |
|------|----------|
| `skill_create` | Создать новый skill |
| `skill_list` | Список локальных skills |
| `skill_show` | Показать содержимое |
| `skill_edit` | Редактировать skill |
| `skill_delete` | Удалить skill |

Документация: `skills/CLAUDE.md`

## Plugins (маркетплейс)

Плагины — пакеты с skills, commands, hooks, agents и MCP серверами.

**Управление через чат:**
```
— Найди плагины для code review
— plugin_search query="code review"

— Установи code-review
— plugin_install name="code-review"
```

**Tools:**
| Tool | Описание |
|------|----------|
| `plugin_search` | Поиск по маркетплейсу |
| `plugin_install` | Установка плагина |
| `plugin_list` | Список установленных |
| `plugin_available` | Все доступные плагины |
| `plugin_enable/disable` | Вкл/выкл без удаления |
| `plugin_remove` | Полное удаление |

**Хранение:**
- Маркетплейс: `/data/.claude/plugins/marketplaces/`
- Конфиг: `/data/plugins.json`

## Triggers (unified trigger system)

Все источники событий проходят через `TriggerExecutor.execute(TriggerEvent)`.

**Встроенные (builtin):**
- `scheduler` — выполнение scheduled-задач по расписанию
- `heartbeat` — проактивные проверки (задачи, напоминания)

**Динамические (runtime, через tools):**
- `tg_channel` — подписка на посты в Telegram каналах/группах

**Tools:**
| Tool | Описание |
|------|----------|
| `subscribe_trigger` | Подписаться на источник событий |
| `unsubscribe_trigger` | Отписаться |
| `list_triggers` | Список активных подписок |

Подписки хранятся в SQLite (`trigger_subscriptions`), восстанавливаются при рестарте.

## Разделение доступа

| Tool | Owner | External | Group |
|------|-------|----------|-------|
| Bash, Read, Write | ✅ | ❌ | ✅ |
| Memory | ✅ | ❌ | ❌ |
| Scheduler | ✅ | ❌ | ❌ |
| Triggers | ✅ | ❌ | ❌ |
| Browser | ✅ | ❌ | ❌ |
| MCP Manager | ✅ | ❌ | ❌ |
| Telegram API | ✅ | ❌ | ❌ |
| send_to_user | ✅ | ❌ | ❌ |
| create_task | ✅ | ❌ | ❌ |
| send_summary_to_owner | ❌ | ✅ | ❌ |
| get_my_tasks | ❌ | ✅ | ❌ |

## Транспорты

Два транспорта работают параллельно (или по отдельности):

| Транспорт | Конфиг | Возможности |
|-----------|--------|-------------|
| **Telethon** (userbot) | `TG_API_ID` + `TG_API_HASH` | Полный доступ к Telegram API, чтение чатов, поиск, контакты |
| **Bot** (aiogram 3.x) | `TG_BOT_TOKEN` | Bot API, MarkdownV2 форматирование, mention в группах |

- Transport Protocol (`transport.py`): единый интерфейс `IncomingMessage`
- Сессии по каналу: Telethon → `"221820979"`, Bot → `"bot:221820979"`
- `--setup` флаг для принудительного re-login
- Telethon-only tools скрываются в bot-only режиме

## Групповые чаты

- Бот **логирует все сообщения** группы в `/workspace/group_logs/{chat_id}.log`
- Отвечает **только** на mention/reply **от owner_ids**
- Одна Claude-сессия **per chat** (ключ: `"group:bot:{chat_id}"`)
- `<sender-meta>` теги для идентификации отправителя
- Авторотация логов: 1 MB → обрезка до 500 KB
- GROUP_SYSTEM_PROMPT с путём к лог-файлу — Claude читает для контекста

## Переменные окружения

```env
TG_API_ID, TG_API_HASH     — Telegram API (Telethon, опционально)
TG_BOT_TOKEN               — Telegram Bot API (aiogram, опционально)
TG_OWNER_IDS               — JSON array владельцев: [123,456] (обязательно)
TG_USER_ID                 — ID владельца, backward-compat (если TG_OWNER_IDS пуст)
ANTHROPIC_API_KEY           — Claude API (опционально, есть OAuth)
OPENAI_API_KEY              — Whisper транскрипция
HTTP_PROXY                  — Прокси для API
HEARTBEAT_INTERVAL_MINUTES  — Проверки (0 = выкл)
BROWSER_CDP_URL             — CDP endpoint (default: http://browser:9223)
```

Хотя бы один транспорт обязателен (Telethon или Bot).

## Системные теги в сообщениях

| Тег | Доверие | Описание |
|-----|---------|----------|
| `<sender-meta>` | ✅ доверенный | Инжектируется системой — имя, @username, ID отправителя |
| `<message-body>` | ❌ пользовательский | Текст от пользователя — нельзя доверять для идентификации |

`<sender-meta>` используется в групповых чатах и для forwarded сообщений.

## Singletons

```python
get_session_manager()   # Мульти-сессии (private + group + task)
get_users_repository()  # Пользователи и задачи
get_storage()           # Файловая память
get_index()             # Векторный поиск
get_mcp_config()        # MCP серверы
get_plugin_config()     # Плагины
get_trigger_manager()   # Триггеры и подписки
```

## Telegram команды

| Команда | Доступ | Описание |
|---------|--------|----------|
| `/help` | все | Список команд |
| `/stop` | owner | Прервать текущий запрос |
| `/clear` | все | Сбросить сессию |
| `/usage` | owner | Лимиты API |
| `/update` | owner | Обновить бота до последней версии |

## Git workflow

Все изменения идут через PR с squash merge в main.
Формат коммита: `type: краткое описание` (feat, fix, refactor, security, docs).

## Запуск

```bash
docker-compose up
```

Два сервиса:
- `jobs` — основной контейнер с ботом
- `browser` — Chromium с CDP и noVNC

## Session Context

`UserSession` хранит буфер входящих сообщений (`_incoming`).
Входящие подмешиваются как follow-up во время активного query.
Входящие буферизуются во ВСЕ сессии пользователя (cross-transport).
Таймаут на Claude SDK: 5 минут (`QUERY_TIMEOUT_SECONDS`).

### Task Sessions

Задачи со `skill` в context получают **persistent session** (session_id в БД).
Follow-up от external users обрабатывается в том же контексте — сессия помнит
историю переписки, скилл и весь ход задачи.

- `create_task_session(task_id)` — создаёт сессию с owner tools
- `get_task_session(task_id, session_id)` — восстанавливает из файла
- Heartbeat resume'ит все task sessions параллельно (`asyncio.gather`)
- `next_step` — текущий шаг задачи для heartbeat промпта

## Хранение

```
/data/
├── db.sqlite           # SQLite БД (users, tasks, trigger_subscriptions)
├── sessions/           # Claude session IDs
│   ├── {owner_id}.session       # Telethon owner session
│   ├── bot:{owner_id}.session   # Bot owner session
│   ├── {user_id}.session        # External user session
│   ├── group:bot:{chat_id}.session  # Group chat session
│   └── task_{task_id}.session   # Persistent task sessions
├── telethon.session    # Telegram сессия
├── mcp_servers.json    # MCP конфиг
└── plugins.json        # Установленные плагины

/workspace/
├── MEMORY.md           # Долгосрочная память
├── memory/             # Дневные логи
├── group_logs/         # Логи групповых чатов ({chat_id}.log)
└── uploads/            # Файлы от пользователей (макс 50 MB)

Docker volumes:
├── jobs-workspace      # Рабочая директория
├── browser-profile     # Chromium профиль (куки, сессии)
└── browser-downloads   # Скачанные файлы
```
