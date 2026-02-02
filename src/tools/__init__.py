"""
MCP Tools для Claude.

Каждый tool — отдельный модуль. Для добавления нового tool:
1. Создай файл в src/tools/
2. Определи tools через @tool декоратор
3. Добавь в TOOL_MODULES список
"""

from claude_agent_sdk import create_sdk_mcp_server

from src.tools.scheduler import SCHEDULER_TOOLS


# Список всех tools из всех модулей
ALL_TOOLS = [
    *SCHEDULER_TOOLS,
]

# Названия tools для allowed_tools
TOOL_NAMES = [
    "mcp__jobs__schedule_task",
    "mcp__jobs__list_scheduled_tasks",
    "mcp__jobs__cancel_scheduled_task",
]


def create_tools_server():
    """Создаёт MCP сервер со всеми tools."""
    return create_sdk_mcp_server(
        name="jobs",
        version="1.0.0",
        tools=ALL_TOOLS,
    )
