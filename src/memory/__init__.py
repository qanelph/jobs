"""
Memory System — долгосрочная память агента.

Структура:
- MEMORY.md — долгосрочная память (предпочтения, факты)
- memory/YYYY-MM-DD.md — дневные логи
- sessions/YYYY-MM-DD-slug.md — транскрипты диалогов
"""

from src.memory.storage import MemoryStorage, get_storage
from src.memory.tools import MEMORY_TOOLS, MEMORY_TOOL_NAMES

__all__ = [
    "MemoryStorage",
    "get_storage",
    "MEMORY_TOOLS",
    "MEMORY_TOOL_NAMES",
]
