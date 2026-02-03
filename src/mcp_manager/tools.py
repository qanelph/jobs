"""
MCP Manager Tools — инструменты для управления MCP серверами через чат.
"""

from typing import Any

from claude_agent_sdk import tool
from loguru import logger

from src.mcp_manager.registry import MCPRegistry
from src.mcp_manager.config import get_mcp_config, save_mcp_config

# NOTE: get_session_manager импортируется внутри функций (lazy import)
# чтобы избежать циклического импорта:
# src.users → session_manager → src.tools → mcp_manager.tools → src.users


@tool(
    "mcp_search",
    "Search for MCP servers in the official registry. Use this to find integrations like postgres, github, slack, etc.",
    {
        "query": str,
    },
)
async def mcp_search(args: dict[str, Any]) -> dict[str, Any]:
    """Ищет MCP серверы в реестре."""
    query = args.get("query")

    if not query:
        return _error("query обязателен")

    registry = MCPRegistry()
    servers = await registry.search(query, limit=5)

    if not servers:
        return _text(f"Ничего не найдено по запросу '{query}'")

    lines = [f"Найдено {len(servers)} MCP серверов:\n"]

    for s in servers:
        install = s.install_command or "см. документацию"
        lines.append(f"**{s.name}** — {s.title}")
        lines.append(f"  {s.description[:100]}...")
        lines.append(f"  Установка: `{install}`")
        lines.append("")

    lines.append("Используй `mcp_install` чтобы подключить сервер.")

    return _text("\n".join(lines))


@tool(
    "mcp_install",
    "Install and connect an MCP server. After installation, set required env variables with mcp_set_env.",
    {
        "name": str,
        "command": str,
        "args": str,
    },
)
async def mcp_install(args: dict[str, Any]) -> dict[str, Any]:
    """Устанавливает MCP сервер."""
    name = args.get("name")
    command = args.get("command")
    args_str = args.get("args", "")

    if not name or not command:
        return _error("name и command обязательны")

    # Парсим args
    cmd_args = args_str.split() if args_str else []

    # Пробуем получить инфо из реестра
    registry = MCPRegistry()
    info = await registry.get_server(name)

    title = info.title if info else name
    description = info.description if info else ""

    config = get_mcp_config()
    config.add_server(
        name=name,
        command=command,
        args=cmd_args,
        title=title,
        description=description,
        source="registry" if info else "manual",
    )
    save_mcp_config()

    lines = [
        f"MCP сервер {name} добавлен.",
        "",
        f"Команда: `{command} {args_str}`".strip(),
        "",
        "Если серверу нужны credentials (API ключи, connection strings),",
        "используй `mcp_set_env` чтобы их задать.",
        "",
        "Пример: `mcp_set_env name=postgres key=DATABASE_URL value=postgresql://...`",
        "",
        "Сессия будет перезапущена при следующем сообщении.",
    ]

    # Сбрасываем сессию чтобы новые MCP подхватились
    from src.users import get_session_manager
    get_session_manager().reset_all()

    return _text("\n".join(lines))


@tool(
    "mcp_set_env",
    "Set environment variable for an MCP server (for credentials, API keys, etc.)",
    {
        "name": str,
        "key": str,
        "value": str,
    },
)
async def mcp_set_env(args: dict[str, Any]) -> dict[str, Any]:
    """Устанавливает env переменную для сервера."""
    name = args.get("name")
    key = args.get("key")
    value = args.get("value")

    if not name or not key or not value:
        return _error("name, key и value обязательны")

    config = get_mcp_config()

    if name not in config.servers:
        return _error(f"Сервер '{name}' не найден. Сначала установи через mcp_install.")

    config.set_env(name, key, value)
    save_mcp_config()

    # Сбрасываем сессию
    from src.users import get_session_manager
    get_session_manager().reset_all()

    return _text(f"Установлено {name}.env.{key}. Сессия перезапустится при следующем сообщении.")


@tool(
    "mcp_list",
    "List all configured MCP servers and their status.",
    {},
)
async def mcp_list(args: dict[str, Any]) -> dict[str, Any]:
    """Список подключённых серверов."""
    config = get_mcp_config()
    servers = config.list_servers()

    if not servers:
        return _text("Нет подключённых MCP серверов.\n\nИспользуй `mcp_search` чтобы найти и подключить.")

    lines = ["MCP серверы:\n"]

    for s in servers:
        status = "[on]" if s["enabled"] else "[off]"
        lines.append(f"{status} {s['name']} — {s['title']}")
        if s["description"]:
            lines.append(f"   {s['description']}")
        lines.append(f"   `{s['command']}`")
        lines.append("")

    return _text("\n".join(lines))


@tool(
    "mcp_enable",
    "Enable a disabled MCP server.",
    {
        "name": str,
    },
)
async def mcp_enable(args: dict[str, Any]) -> dict[str, Any]:
    """Включает сервер."""
    name = args.get("name")

    if not name:
        return _error("name обязателен")

    config = get_mcp_config()

    if config.enable_server(name):
        save_mcp_config()
        from src.users import get_session_manager
        get_session_manager().reset_all()
        return _text(f"MCP сервер {name} включён. Сессия перезапустится при следующем сообщении.")

    return _error(f"Сервер '{name}' не найден")


@tool(
    "mcp_disable",
    "Disable an MCP server without removing it.",
    {
        "name": str,
    },
)
async def mcp_disable(args: dict[str, Any]) -> dict[str, Any]:
    """Отключает сервер."""
    name = args.get("name")

    if not name:
        return _error("name обязателен")

    config = get_mcp_config()

    if config.disable_server(name):
        save_mcp_config()
        from src.users import get_session_manager
        get_session_manager().reset_all()
        return _text(f"MCP сервер {name} отключён. Сессия перезапустится при следующем сообщении.")

    return _error(f"Сервер '{name}' не найден")


@tool(
    "mcp_remove",
    "Completely remove an MCP server configuration.",
    {
        "name": str,
    },
)
async def mcp_remove(args: dict[str, Any]) -> dict[str, Any]:
    """Удаляет сервер."""
    name = args.get("name")

    if not name:
        return _error("name обязателен")

    config = get_mcp_config()

    if config.remove_server(name):
        save_mcp_config()
        from src.users import get_session_manager
        get_session_manager().reset_all()
        return _text(f"MCP сервер {name} удалён. Сессия перезапустится при следующем сообщении.")

    return _error(f"Сервер '{name}' не найден")


# Экспорт
MCP_MANAGER_TOOLS = [
    mcp_search,
    mcp_install,
    mcp_set_env,
    mcp_list,
    mcp_enable,
    mcp_disable,
    mcp_remove,
]

MCP_MANAGER_TOOL_NAMES = [
    "mcp__jobs__mcp_search",
    "mcp__jobs__mcp_install",
    "mcp__jobs__mcp_set_env",
    "mcp__jobs__mcp_list",
    "mcp__jobs__mcp_enable",
    "mcp__jobs__mcp_disable",
    "mcp__jobs__mcp_remove",
]


# Helpers
def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}
