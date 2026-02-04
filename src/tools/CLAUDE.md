# tools/ — MCP инструменты

## Структура

```
tools/
├── __init__.py      # Сборка tools + разделение по ролям
└── scheduler.py     # Планировщик задач (SQLite)
```

## Разделение по ролям

```python
# __init__.py

OWNER_ALLOWED_TOOLS = [
    Scheduler, Memory, MCP Manager, User Management
]

EXTERNAL_ALLOWED_TOOLS = [
    send_summary_to_owner, get_my_tasks, update_task_status
]
```

## Все инструменты

### Scheduler (2) — только Owner
| Tool | Описание |
|------|----------|
| `schedule_task(title, prompt?, time, repeat?)` | Запланировать задачу (создаёт Task с kind="scheduled") |
| `cancel_task(task_id)` | Отменить любую задачу |

### Memory (6) — только Owner
| Tool | Описание |
|------|----------|
| `memory_search(query)` | Поиск в памяти |
| `memory_read(path)` | Прочитать файл |
| `memory_append(content)` | → MEMORY.md |
| `memory_log(content)` | → дневной лог |
| `memory_context()` | Полный контекст |
| `memory_reindex()` | Переиндексация |

### MCP Manager (7) — только Owner
| Tool | Описание |
|------|----------|
| `mcp_search(query)` | Поиск в реестре |
| `mcp_install(name, command, args)` | Установить сервер |
| `mcp_set_env(name, key, value)` | Установить credentials |
| `mcp_list()` | Список серверов |
| `mcp_enable(name)` | Включить |
| `mcp_disable(name)` | Отключить |
| `mcp_remove(name)` | Удалить |

### User Tools — см. `users/CLAUDE.md`

## Scheduler (scheduler.py)

Расписание — свойство Task (kind="scheduled", schedule_at, schedule_repeat).
Scheduler читает из таблицы `tasks` через `repo.get_scheduled_due()`.
Для списка: `list_tasks(kind="scheduled")`.

### SchedulerRunner
```python
class SchedulerRunner:
    def __init__(self, on_task_due: Callable[[str, str], Awaitable]):
        # callback вызывается когда задача готова к выполнению

    async def start(self):
        # Каждые 30 секунд проверяет due tasks через repo.get_scheduled_due()
```

## create_tools_server()

```python
def create_tools_server():
    """
    Создаёт MCP сервер 'jobs' со всеми tools.
    Используется в users/session_manager.py при создании ClaudeAgentOptions.
    """
    return create_sdk_mcp_server(
        name="jobs",
        version="1.0.0",
        tools=ALL_TOOLS,
    )
```

## Использование в users/session_manager.py

```python
# UserSession._build_options()

mcp_servers = {"jobs": self._tools_server}

# Owner получает внешние MCP серверы
if self.is_owner:
    mcp_servers.update(external_servers)

# Разные allowed_tools по ролям
allowed_tools = OWNER_ALLOWED_TOOLS if self.is_owner else EXTERNAL_ALLOWED_TOOLS
```
