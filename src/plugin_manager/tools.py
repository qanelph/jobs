"""
Plugin Manager Tools — инструменты для управления плагинами через чат.
"""

from typing import Any

from claude_agent_sdk import tool
from loguru import logger

from src.plugin_manager.registry import PluginRegistry
from src.plugin_manager.config import get_plugin_config, save_plugin_config


@tool(
    "plugin_search",
    "Search for available plugins. Plugins extend Claude with skills, commands, hooks, agents, and MCP servers.",
    {
        "query": str,
    },
)
async def plugin_search(args: dict[str, Any]) -> dict[str, Any]:
    """Ищет плагины по запросу."""
    query = args.get("query")

    if not query:
        return _error("query обязателен")

    registry = PluginRegistry()
    plugins = registry.search(query, limit=10)

    if not plugins:
        # Показываем все доступные
        all_plugins = registry.scan_all()
        if all_plugins:
            names = [p.name for p in all_plugins[:10]]
            return _text(
                f"Ничего не найдено по '{query}'.\n\n"
                f"Доступные плагины: {', '.join(names)}"
            )
        return _text(f"Ничего не найдено по запросу '{query}'")

    lines = [f"Найдено {len(plugins)} плагинов:\n"]

    for p in plugins:
        features = []
        if p.has_skills:
            features.append("skills")
        if p.has_commands:
            features.append("commands")
        if p.has_hooks:
            features.append("hooks")
        if p.has_agents:
            features.append("agents")
        if p.has_mcp:
            features.append("mcp")

        lines.append(f"**{p.name}**")
        if p.description:
            lines.append(f"  {p.description[:150]}")
        if features:
            lines.append(f"  Содержит: {', '.join(features)}")
        if p.author_name:
            lines.append(f"  Автор: {p.author_name}")
        lines.append("")

    lines.append("Используй `plugin_install name=<name>` чтобы установить.")

    return _text("\n".join(lines))


@tool(
    "plugin_install",
    "Install a plugin by name. After installation, restart the session to activate.",
    {
        "name": str,
    },
)
async def plugin_install(args: dict[str, Any]) -> dict[str, Any]:
    """Устанавливает плагин."""
    name = args.get("name")

    if not name:
        return _error("name обязателен")

    # Находим плагин в реестре
    registry = PluginRegistry()
    plugin_info = registry.get_plugin(name)

    if not plugin_info:
        # Показываем похожие
        all_plugins = registry.scan_all()
        similar = [p.name for p in all_plugins if name.lower() in p.name.lower()][:5]
        msg = f"Плагин '{name}' не найден."
        if similar:
            msg += f"\n\nПохожие: {', '.join(similar)}"
        return _error(msg)

    config = get_plugin_config()

    # Проверяем не установлен ли уже
    if name in config.plugins:
        return _text(f"Плагин {name} уже установлен. Используй `plugin_enable` чтобы включить.")

    config.add_plugin(
        name=plugin_info.name,
        path=plugin_info.path,
        description=plugin_info.description,
        author_name=plugin_info.author_name,
        author_email=plugin_info.author_email,
    )
    save_plugin_config()

    # Сбрасываем сессию
    from src.users import get_session_manager
    await get_session_manager().reset_all()

    features = []
    if plugin_info.has_skills:
        features.append("skills")
    if plugin_info.has_commands:
        features.append("commands")
    if plugin_info.has_hooks:
        features.append("hooks")
    if plugin_info.has_agents:
        features.append("agents")
    if plugin_info.has_mcp:
        features.append("MCP серверы")

    lines = [
        f"Плагин **{name}** установлен.",
        "",
    ]

    if plugin_info.description:
        lines.append(plugin_info.description[:200])
        lines.append("")

    if features:
        lines.append(f"Добавлено: {', '.join(features)}")
        lines.append("")

    lines.append("Сессия будет перезапущена при следующем сообщении.")

    return _text("\n".join(lines))


@tool(
    "plugin_list",
    "List all installed plugins and their status.",
    {},
)
async def plugin_list(args: dict[str, Any]) -> dict[str, Any]:
    """Список установленных плагинов."""
    config = get_plugin_config()
    plugins = config.list_plugins()

    if not plugins:
        return _text(
            "Нет установленных плагинов.\n\n"
            "Используй `plugin_search query=<тема>` чтобы найти."
        )

    lines = ["Установленные плагины:\n"]

    for p in plugins:
        status = "[on]" if p["enabled"] else "[off]"
        lines.append(f"{status} **{p['name']}**")
        if p["description"]:
            lines.append(f"   {p['description']}")
        if p["author"]:
            lines.append(f"   Автор: {p['author']}")
        lines.append("")

    return _text("\n".join(lines))


@tool(
    "plugin_enable",
    "Enable a disabled plugin.",
    {
        "name": str,
    },
)
async def plugin_enable(args: dict[str, Any]) -> dict[str, Any]:
    """Включает плагин."""
    name = args.get("name")

    if not name:
        return _error("name обязателен")

    config = get_plugin_config()

    if config.enable_plugin(name):
        save_plugin_config()
        from src.users import get_session_manager
        await get_session_manager().reset_all()
        return _text(f"Плагин {name} включён. Сессия перезапустится при следующем сообщении.")

    return _error(f"Плагин '{name}' не установлен")


@tool(
    "plugin_disable",
    "Disable a plugin without removing it.",
    {
        "name": str,
    },
)
async def plugin_disable(args: dict[str, Any]) -> dict[str, Any]:
    """Отключает плагин."""
    name = args.get("name")

    if not name:
        return _error("name обязателен")

    config = get_plugin_config()

    if config.disable_plugin(name):
        save_plugin_config()
        from src.users import get_session_manager
        await get_session_manager().reset_all()
        return _text(f"Плагин {name} отключён. Сессия перезапустится при следующем сообщении.")

    return _error(f"Плагин '{name}' не установлен")


@tool(
    "plugin_remove",
    "Completely remove a plugin.",
    {
        "name": str,
    },
)
async def plugin_remove(args: dict[str, Any]) -> dict[str, Any]:
    """Удаляет плагин."""
    name = args.get("name")

    if not name:
        return _error("name обязателен")

    config = get_plugin_config()

    if config.remove_plugin(name):
        save_plugin_config()
        from src.users import get_session_manager
        await get_session_manager().reset_all()
        return _text(f"Плагин {name} удалён. Сессия перезапустится при следующем сообщении.")

    return _error(f"Плагин '{name}' не установлен")


@tool(
    "plugin_available",
    "List all available plugins from marketplace.",
    {},
)
async def plugin_available(args: dict[str, Any]) -> dict[str, Any]:
    """Показывает все доступные плагины."""
    registry = PluginRegistry()
    all_plugins = registry.scan_all()

    if not all_plugins:
        return _text("Маркетплейс плагинов не найден или пуст.")

    # Группируем по категориям (LSP, стили, инструменты)
    lsp_plugins = [p for p in all_plugins if "lsp" in p.name.lower()]
    style_plugins = [p for p in all_plugins if "style" in p.name.lower()]
    other_plugins = [p for p in all_plugins if p not in lsp_plugins and p not in style_plugins]

    lines = [f"Доступно {len(all_plugins)} плагинов:\n"]

    if other_plugins:
        lines.append("**Инструменты:**")
        for p in other_plugins:
            desc = f" — {p.description[:60]}..." if p.description else ""
            lines.append(f"  • {p.name}{desc}")
        lines.append("")

    if style_plugins:
        lines.append("**Стили вывода:**")
        for p in style_plugins:
            desc = f" — {p.description[:60]}..." if p.description else ""
            lines.append(f"  • {p.name}{desc}")
        lines.append("")

    if lsp_plugins:
        lines.append("**LSP интеграции:**")
        for p in lsp_plugins:
            lines.append(f"  • {p.name}")
        lines.append("")

    lines.append("Используй `plugin_install name=<name>` для установки.")

    return _text("\n".join(lines))


# Экспорт
PLUGIN_MANAGER_TOOLS = [
    plugin_search,
    plugin_install,
    plugin_list,
    plugin_enable,
    plugin_disable,
    plugin_remove,
    plugin_available,
]

PLUGIN_MANAGER_TOOL_NAMES = [
    "mcp__jobs__plugin_search",
    "mcp__jobs__plugin_install",
    "mcp__jobs__plugin_list",
    "mcp__jobs__plugin_enable",
    "mcp__jobs__plugin_disable",
    "mcp__jobs__plugin_remove",
    "mcp__jobs__plugin_available",
]


# Helpers
def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}
