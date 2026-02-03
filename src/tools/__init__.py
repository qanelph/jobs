"""
MCP Tools для Claude.

Каждый tool — отдельный модуль. Для добавления нового tool:
1. Создай файл в src/tools/
2. Определи tools через @tool декоратор
3. Добавь в ALL_TOOLS и TOOL_NAMES
"""

from claude_agent_sdk import create_sdk_mcp_server

from src.tools.scheduler import SCHEDULER_TOOLS
from src.memory.tools import MEMORY_TOOLS, MEMORY_TOOL_NAMES


# Список всех tools из всех модулей
ALL_TOOLS = [
    *SCHEDULER_TOOLS,
    *MEMORY_TOOLS,
]

# Названия tools для allowed_tools
TOOL_NAMES = [
    # Scheduler
    "mcp__jobs__schedule_task",
    "mcp__jobs__list_scheduled_tasks",
    "mcp__jobs__cancel_scheduled_task",
    # Memory
    *MEMORY_TOOL_NAMES,
]


def create_tools_server():
    """Создаёт MCP сервер со всеми tools."""
    return create_sdk_mcp_server(
        name="jobs",
        version="1.0.0",
        tools=ALL_TOOLS,
    )
