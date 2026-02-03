"""
MCP Tools для Claude.

Разделены по ролям:
- OWNER_ALLOWED_TOOLS — полный доступ для владельца
- EXTERNAL_ALLOWED_TOOLS — ограниченный доступ для внешних пользователей
"""

from claude_agent_sdk import create_sdk_mcp_server

from src.tools.scheduler import SCHEDULER_TOOLS
from src.memory.tools import MEMORY_TOOLS, MEMORY_TOOL_NAMES
from src.mcp_manager.tools import MCP_MANAGER_TOOLS, MCP_MANAGER_TOOL_NAMES
from src.users.tools import (
    OWNER_TOOLS,
    EXTERNAL_USER_TOOLS,
    OWNER_TOOL_NAMES,
    EXTERNAL_USER_TOOL_NAMES,
)
from src.telegram.tools import TELEGRAM_TOOLS, TELEGRAM_TOOL_NAMES


# =============================================================================
# All tools (регистрируются в MCP сервере)
# =============================================================================

ALL_TOOLS = [
    *SCHEDULER_TOOLS,
    *MEMORY_TOOLS,
    *MCP_MANAGER_TOOLS,
    *OWNER_TOOLS,
    *EXTERNAL_USER_TOOLS,
    *TELEGRAM_TOOLS,
]


# =============================================================================
# Allowed tools по ролям
# =============================================================================

# Owner — полный доступ
OWNER_ALLOWED_TOOLS = [
    # Scheduler
    "mcp__jobs__schedule_task",
    "mcp__jobs__list_scheduled_tasks",
    "mcp__jobs__cancel_scheduled_task",
    # Memory
    *MEMORY_TOOL_NAMES,
    # MCP Manager
    *MCP_MANAGER_TOOL_NAMES,
    # User management
    *[f"mcp__jobs__{name}" for name in OWNER_TOOL_NAMES],
    # Telegram API
    *TELEGRAM_TOOL_NAMES,
]

# External users — только свои задачи и сводки
EXTERNAL_ALLOWED_TOOLS = [
    *[f"mcp__jobs__{name}" for name in EXTERNAL_USER_TOOL_NAMES],
]

# Legacy: общий список (для обратной совместимости)
TOOL_NAMES = OWNER_ALLOWED_TOOLS


def create_tools_server():
    """Создаёт MCP сервер со всеми tools."""
    return create_sdk_mcp_server(
        name="jobs",
        version="1.0.0",
        tools=ALL_TOOLS,
    )
