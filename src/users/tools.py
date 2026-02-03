"""
MCP Tools — инструменты для работы с пользователями.

Два набора:
- OWNER_TOOLS — для owner'а (управление пользователями)
- EXTERNAL_USER_TOOLS — для внешних пользователей (ограниченный доступ)
"""

from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable, Awaitable

from claude_agent_sdk import tool
from loguru import logger

from .repository import get_users_repository


# =============================================================================
# Context — thread-safe контекст через contextvars
# =============================================================================

# ContextVar для текущего пользователя (thread-safe, async-safe)
_current_user_id_var: ContextVar[int | None] = ContextVar("current_user_id", default=None)

# Telegram sender (устанавливается один раз при старте)
_telegram_sender: Callable[[int, str], Awaitable[None]] | None = None


def set_current_user(telegram_id: int) -> None:
    """Устанавливает текущего пользователя для tools (async-safe)."""
    _current_user_id_var.set(telegram_id)


def set_telegram_sender(sender: Callable[[int, str], Awaitable[None]]) -> None:
    """Устанавливает функцию отправки сообщений в Telegram."""
    global _telegram_sender
    _telegram_sender = sender


def _get_current_user_id() -> int:
    """Получает ID текущего пользователя (async-safe)."""
    user_id = _current_user_id_var.get()
    if user_id is None:
        raise RuntimeError("Current user not set")
    return user_id


# =============================================================================
# Owner Tools
# =============================================================================


@tool(
    "send_to_user",
    "Send a message to a user via Telegram. User can be @username or name.",
    {"user": str, "message": str},
)
async def send_to_user(args: dict[str, Any]) -> dict[str, Any]:
    """Отправляет сообщение пользователю."""
    user_query = args.get("user")
    message = args.get("message")

    if not user_query or not message:
        return _error("user и message обязательны")

    repo = get_users_repository()
    user = await repo.find_user(user_query)

    if not user:
        return _error(f"Пользователь '{user_query}' не найден")

    # Отправляем через Telegram
    if _telegram_sender:
        try:
            await _telegram_sender(user.telegram_id, message)
            logger.info(f"Sent to {user.display_name}: {message[:50]}...")
            return _text(f"Отправлено {user.display_name}")
        except Exception as e:
            return _error(f"Ошибка отправки: {e}")
    else:
        return _error("Telegram sender не настроен")


@tool(
    "create_user_task",
    "Create a task for a user. Deadline format: YYYY-MM-DD or YYYY-MM-DD HH:MM",
    {"user": str, "description": str, "deadline": str},
)
async def create_user_task(args: dict[str, Any]) -> dict[str, Any]:
    """Создаёт задачу для пользователя."""
    user_query = args.get("user")
    description = args.get("description")
    deadline_str = args.get("deadline")

    if not user_query or not description:
        return _error("user и description обязательны")

    repo = get_users_repository()
    user = await repo.find_user(user_query)

    if not user:
        return _error(f"Пользователь '{user_query}' не найден")

    # Парсим дедлайн
    deadline = None
    if deadline_str:
        try:
            if " " in deadline_str:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
            else:
                deadline = datetime.strptime(deadline_str, "%Y-%m-%d")
                deadline = deadline.replace(hour=23, minute=59)
        except ValueError:
            return _error(f"Неверный формат даты: {deadline_str}")

    from src.config import settings
    task = await repo.create_task(
        assignee_id=user.telegram_id,
        description=description,
        deadline=deadline,
        created_by=settings.tg_user_id,
    )

    # Уведомляем пользователя
    deadline_str = f"\nДедлайн: {deadline.strftime('%d.%m.%Y %H:%M')}" if deadline else ""
    notification = f"Новая задача:\n{description}{deadline_str}\n\nПодтверди получение."

    if _telegram_sender:
        try:
            await _telegram_sender(user.telegram_id, notification)
        except Exception as e:
            logger.error(f"Failed to notify {user.telegram_id}: {e}")

    return _text(f"Задача [{task.id}] создана для {user.display_name}{deadline_str}")


@tool(
    "get_user_tasks",
    "Get tasks assigned to a user",
    {"user": str},
)
async def get_user_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """Получает задачи пользователя."""
    user_query = args.get("user")

    if not user_query:
        return _error("user обязателен")

    repo = get_users_repository()
    user = await repo.find_user(user_query)

    if not user:
        return _error(f"Пользователь '{user_query}' не найден")

    tasks = await repo.get_user_tasks(user.telegram_id)

    if not tasks:
        return _text(f"У {user.display_name} нет открытых задач")

    lines = [f"Задачи {user.display_name}:"]
    for task in tasks:
        deadline = f" (до {task.deadline.strftime('%d.%m')})" if task.deadline else ""
        lines.append(f"[{task.status}] [{task.id}] {task.description[:40]}{deadline}")

    return _text("\n".join(lines))


@tool(
    "resolve_user",
    "Find user by @username, name or phone",
    {"query": str},
)
async def resolve_user(args: dict[str, Any]) -> dict[str, Any]:
    """Ищет пользователя."""
    query = args.get("query")

    if not query:
        return _error("query обязателен")

    repo = get_users_repository()
    user = await repo.find_user(query)

    if not user:
        return _text(f"Пользователь '{query}' не найден")

    return _text(
        f"{user.display_name}\n"
        f"ID: {user.telegram_id}\n"
        f"Username: @{user.username or 'нет'}\n"
        f"Телефон: {user.phone or 'нет'}\n"
        f"Последний контакт: {user.last_contact.strftime('%d.%m.%Y')}"
    )


@tool(
    "list_users",
    "List all known users",
    {},
)
async def list_users(args: dict[str, Any]) -> dict[str, Any]:
    """Список всех пользователей."""
    repo = get_users_repository()
    users = await repo.list_users()

    if not users:
        return _text("Нет известных пользователей")

    lines = ["Пользователи:"]
    for user in users:
        username = f"@{user.username}" if user.username else ""
        lines.append(f"• {user.display_name} {username}")

    return _text("\n".join(lines))


@tool(
    "get_overdue_tasks",
    "Get all overdue tasks across all users",
    {},
)
async def get_overdue_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """Получает все просроченные задачи."""
    repo = get_users_repository()
    tasks = await repo.get_overdue_tasks()

    if not tasks:
        return _text("Нет просроченных задач")

    lines = ["Просроченные задачи:"]
    for task in tasks:
        user = await repo.get_user(task.assignee_id)
        user_name = user.display_name if user else str(task.assignee_id)
        overdue_days = (datetime.now() - task.deadline).days if task.deadline else 0
        lines.append(f"• [{task.id}] {user_name}: {task.description[:30]}... (просрочено {overdue_days} дн.)")

    return _text("\n".join(lines))


@tool(
    "ban_user",
    "Ban a user from using the bot",
    {"user": str},
)
async def ban_user(args: dict[str, Any]) -> dict[str, Any]:
    """Банит пользователя."""
    user_query = args.get("user")

    if not user_query:
        return _error("user обязателен")

    repo = get_users_repository()
    user = await repo.find_user(user_query)

    if not user:
        return _error(f"Пользователь '{user_query}' не найден")

    if user.is_banned:
        return _text(f"{user.display_name} уже забанен")

    await repo.ban_user(user.telegram_id)

    # Уведомляем owner'а
    from src.config import settings
    if _telegram_sender:
        username = f" (@{user.username})" if user.username else ""
        await _telegram_sender(
            settings.tg_user_id,
            f"Пользователь {user.display_name}{username} забанен"
        )

    return _text(f"{user.display_name} забанен")


@tool(
    "unban_user",
    "Unban a user and reset their warnings",
    {"user": str},
)
async def unban_user(args: dict[str, Any]) -> dict[str, Any]:
    """Разбанивает пользователя."""
    user_query = args.get("user")

    if not user_query:
        return _error("user обязателен")

    repo = get_users_repository()
    user = await repo.find_user(user_query)

    if not user:
        return _error(f"Пользователь '{user_query}' не найден")

    if not user.is_banned:
        return _text(f"{user.display_name} не забанен")

    await repo.unban_user(user.telegram_id)

    # Сбрасываем сессию — чистый лист
    from src.users import get_session_manager
    get_session_manager().reset_session(user.telegram_id)

    # Уведомляем owner'а
    from src.config import settings
    if _telegram_sender:
        username = f" (@{user.username})" if user.username else ""
        await _telegram_sender(
            settings.tg_user_id,
            f"Пользователь {user.display_name}{username} разбанен"
        )

    return _text(f"{user.display_name} разбанен, сессия сброшена")


@tool(
    "list_banned",
    "List all banned users",
    {},
)
async def list_banned(args: dict[str, Any]) -> dict[str, Any]:
    """Список забаненных пользователей."""
    repo = get_users_repository()
    users = await repo.list_banned_users()

    if not users:
        return _text("Нет забаненных пользователей")

    lines = ["Забаненные:"]
    for user in users:
        username = f" @{user.username}" if user.username else ""
        lines.append(f"• {user.display_name}{username} (ID: {user.telegram_id})")

    return _text("\n".join(lines))


# =============================================================================
# External User Tools
# =============================================================================


@tool(
    "send_summary_to_owner",
    "Send a summary to the bot owner about current conversation",
    {"summary": str},
)
async def send_summary_to_owner(args: dict[str, Any]) -> dict[str, Any]:
    """Отправляет сводку owner'у."""
    summary = args.get("summary")

    if not summary:
        return _error("summary обязателен")

    from src.config import settings

    user_id = _get_current_user_id()
    repo = get_users_repository()
    user = await repo.get_user(user_id)
    user_name = user.display_name if user else str(user_id)

    message = f"Сводка от {user_name}:\n\n{summary}"

    if _telegram_sender:
        try:
            await _telegram_sender(settings.tg_user_id, message)
            logger.info(f"Summary sent to owner from {user_name}")
            return _text("Сводка отправлена владельцу")
        except Exception as e:
            return _error(f"Ошибка отправки: {e}")
    else:
        return _error("Telegram sender не настроен")


@tool(
    "get_my_tasks",
    "Get tasks assigned to the current user",
    {},
)
async def get_my_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """Получает задачи текущего пользователя."""
    user_id = _get_current_user_id()
    repo = get_users_repository()
    tasks = await repo.get_user_tasks(user_id)

    if not tasks:
        return _text("У вас нет открытых задач")

    lines = ["Ваши задачи:"]
    for task in tasks:
        deadline = f" (до {task.deadline.strftime('%d.%m')})" if task.deadline else ""
        overdue = " [ПРОСРОЧЕНО]" if task.is_overdue else ""
        lines.append(f"[{task.status}] [{task.id}] {task.description}{deadline}{overdue}")

    return _text("\n".join(lines))


@tool(
    "ban_current_user",
    "Ban the current user for rule violations. Use after warnings.",
    {"reason": str},
)
async def ban_current_user(args: dict[str, Any]) -> dict[str, Any]:
    """Банит текущего пользователя."""
    reason = args.get("reason", "нарушение правил")

    user_id = _get_current_user_id()
    repo = get_users_repository()

    user = await repo.get_user(user_id)
    if not user:
        return _error("Пользователь не найден")

    if user.is_banned:
        return _text("Пользователь уже забанен")

    await repo.ban_user(user_id)

    # Уведомляем owner'а
    from src.config import settings
    if _telegram_sender:
        username = f" (@{user.username})" if user.username else ""
        await _telegram_sender(
            settings.tg_user_id,
            f"{user.display_name}{username} забанен.\nПричина: {reason}"
        )

    return _text(f"Вы забанены: {reason}")


@tool(
    "update_task_status",
    "Update task status. Status: pending, accepted, completed",
    {"task_id": str, "status": str},
)
async def update_task_status(args: dict[str, Any]) -> dict[str, Any]:
    """Обновляет статус задачи."""
    task_id = args.get("task_id")
    status = args.get("status")

    if not task_id or not status:
        return _error("task_id и status обязательны")

    valid_statuses = ["pending", "accepted", "completed"]
    if status not in valid_statuses:
        return _error(f"Неверный статус. Допустимые: {', '.join(valid_statuses)}")

    repo = get_users_repository()
    task = await repo.get_task(task_id)

    if not task:
        return _error(f"Задача [{task_id}] не найдена")

    # Проверяем что это задача текущего пользователя
    user_id = _get_current_user_id()
    if task.assignee_id != user_id:
        return _error("Вы можете обновлять только свои задачи")

    await repo.update_task_status(task_id, status)

    # Уведомляем owner'а о смене статуса
    from src.config import settings
    user = await repo.get_user(user_id)
    user_name = user.display_name if user else str(user_id)

    notification = f"{user_name} изменил статус задачи [{task_id}]: {status}"

    if _telegram_sender:
        try:
            await _telegram_sender(settings.tg_user_id, notification)
        except Exception as e:
            logger.error(f"Failed to notify owner: {e}")

    return _text(f"Статус [{task_id}] обновлён: {status}")


# =============================================================================
# Tool Collections
# =============================================================================

OWNER_TOOLS = [
    send_to_user,
    create_user_task,
    get_user_tasks,
    resolve_user,
    list_users,
    get_overdue_tasks,
    ban_user,
    unban_user,
    list_banned,
]

EXTERNAL_USER_TOOLS = [
    send_summary_to_owner,
    get_my_tasks,
    update_task_status,
    ban_current_user,
]

OWNER_TOOL_NAMES = [t.name for t in OWNER_TOOLS]
EXTERNAL_USER_TOOL_NAMES = [t.name for t in EXTERNAL_USER_TOOLS]


# =============================================================================
# Helpers
# =============================================================================


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}
