"""
Memory Tools — MCP инструменты для работы с памятью.

Tools:
- memory_search: семантический поиск по памяти
- memory_read: чтение файла памяти
- memory_append: добавление в память
- memory_log: запись в дневной лог
"""

from typing import Any

from claude_agent_sdk import tool
from loguru import logger

from src.memory.storage import get_storage
from src.memory.index import get_index


@tool(
    "memory_search",
    "Search through memory using semantic + keyword matching. Returns relevant snippets with file paths and line numbers.",
    {
        "query": str,
        "limit": int,
    },
)
async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
    """Семантический поиск по памяти."""
    query = args.get("query")
    limit = args.get("limit", 5)

    if not query:
        return _error("query обязателен")

    try:
        index = get_index()
        results = await index.search(query, limit=limit)

        if not results:
            return _text("Ничего не найдено")

        lines = []
        for r in results:
            lines.append(f"**{r.file_path}:{r.line_start}-{r.line_end}** (score: {r.score:.2f})")
            lines.append(f"```\n{r.content[:500]}{'...' if len(r.content) > 500 else ''}\n```")
            lines.append("")

        return _text("\n".join(lines))

    except Exception as e:
        logger.error(f"Memory search error: {e}")
        return _error(f"Ошибка поиска: {e}")


@tool(
    "memory_read",
    "Read a memory file by path. Use for MEMORY.md, daily logs (memory/YYYY-MM-DD.md), or session transcripts.",
    {
        "path": str,
    },
)
async def memory_read(args: dict[str, Any]) -> dict[str, Any]:
    """Читает файл памяти."""
    path = args.get("path")

    if not path:
        return _error("path обязателен")

    storage = get_storage()
    content = storage.read_file(path)

    if content is None:
        return _error(f"Файл не найден: {path}")

    return _text(content)


@tool(
    "memory_append",
    "Append important information to long-term memory (MEMORY.md). Use for: preferences, decisions, important facts about user.",
    {
        "content": str,
    },
)
async def memory_append(args: dict[str, Any]) -> dict[str, Any]:
    """Добавляет в долгосрочную память."""
    content = args.get("content")

    if not content:
        return _error("content обязателен")

    storage = get_storage()
    storage.append_to_memory(content)

    return _text(f"✅ Записано в MEMORY.md")


@tool(
    "memory_log",
    "Add entry to today's daily log (memory/YYYY-MM-DD.md). Use for: session notes, task progress, daily context.",
    {
        "content": str,
    },
)
async def memory_log(args: dict[str, Any]) -> dict[str, Any]:
    """Добавляет в дневной лог."""
    content = args.get("content")

    if not content:
        return _error("content обязателен")

    storage = get_storage()
    storage.append_to_daily_log(content)

    return _text(f"✅ Записано в дневной лог")


@tool(
    "memory_context",
    "Get recent context (today + yesterday daily logs + long-term memory). Call this at the start of conversation to understand context.",
    {},
)
async def memory_context(args: dict[str, Any]) -> dict[str, Any]:
    """Возвращает актуальный контекст."""
    storage = get_storage()

    parts = []

    # Долгосрочная память
    memory = storage.read_memory()
    if memory:
        parts.append("## Long-term Memory (MEMORY.md)\n\n" + memory)

    # Недавний контекст
    recent = storage.get_recent_context(days=2)
    if recent:
        parts.append("## Recent Context (last 2 days)\n\n" + recent)

    if not parts:
        return _text("Память пуста. Начни записывать важное через memory_append и memory_log.")

    return _text("\n\n---\n\n".join(parts))


@tool(
    "memory_reindex",
    "Reindex all memory files. Call after significant changes to memory files.",
    {},
)
async def memory_reindex(args: dict[str, Any]) -> dict[str, Any]:
    """Переиндексирует память."""
    storage = get_storage()
    index = get_index()

    files = storage.list_memory_files()
    count = await index.index_all(files)

    return _text(f"✅ Проиндексировано {count} чанков из {len(files)} файлов")


# Экспорт
MEMORY_TOOLS = [
    memory_search,
    memory_read,
    memory_append,
    memory_log,
    memory_context,
    memory_reindex,
]

MEMORY_TOOL_NAMES = [
    "mcp__jobs__memory_search",
    "mcp__jobs__memory_read",
    "mcp__jobs__memory_append",
    "mcp__jobs__memory_log",
    "mcp__jobs__memory_context",
    "mcp__jobs__memory_reindex",
]


# Helpers
def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"❌ {text}"}], "is_error": True}
