"""
MCP Tools — инструменты для работы с пользователями.

Два набора:
- OWNER_TOOLS — для owner'а (управление пользователями)
- EXTERNAL_USER_TOOLS — для внешних пользователей (ограниченный доступ)
"""

import asyncio
import json
import re
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable, Awaitable

from claude_agent_sdk import tool
from loguru import logger

from .repository import get_users_repository


# Текущий инициатор разговора — set'ится в handlers перед session.query*.
# Унаследуется в task'и asyncio через Task.copy_context(), так что ContextVar
# виден внутри Claude SDK и tools. Используется для адресации tg_send_message,
# нотификаций create_task/ban_user/update_task — чтобы при нескольких owner'ах
# писать тому, кто реально инициировал разговор.
_current_user_id_var: ContextVar[int | None] = ContextVar("current_user_id", default=None)


def set_current_user(telegram_id: int) -> None:
    """Устанавливает ID инициатора текущего query (owner или external)."""
    _current_user_id_var.set(telegram_id)


def get_current_user_id() -> int | None:
    """ID инициатора текущего query. None если зов из скрипта/миграции."""
    return _current_user_id_var.get()


def get_current_owner_id() -> int | None:
    """current_user_id если это owner, иначе None.

    Для адресации нотификаций, которые должны идти owner'у. Если current —
    external user (например, в external-сессии), возвращаем None и caller
    делает fallback на primary_owner_id.
    """
    from src.config import settings
    uid = _current_user_id_var.get()
    if uid is not None and settings.is_owner(uid):
        return uid
    return None


# Telegram sender (устанавливается один раз при старте)
_telegram_sender: Callable[[int, str], Awaitable[None]] | None = None

# Context sender — инжектит в контекст сессии БЕЗ отправки в Telegram + триггерит autonomous query
_context_sender: Callable[[int, str], Awaitable[None]] | None = None

# Buffer sender — тихая буферизация в контекст БЕЗ autonomous query trigger
_buffer_sender: Callable[[int, str], Awaitable[None]] | None = None

# Task executor — запуск background task через TriggerExecutor
_task_executor: Callable[..., Awaitable[str | None]] | None = None


def set_telegram_sender(sender: Callable[[int, str], Awaitable[None]]) -> None:
    """Устанавливает функцию отправки сообщений в Telegram."""
    global _telegram_sender
    _telegram_sender = sender


def set_context_sender(sender: Callable[[int, str], Awaitable[None]]) -> None:
    """Устанавливает функцию инжекта в контекст + autonomous trigger."""
    global _context_sender
    _context_sender = sender


def set_buffer_sender(sender: Callable[[int, str], Awaitable[None]]) -> None:
    """Устанавливает функцию тихой буферизации (без autonomous trigger)."""
    global _buffer_sender
    _buffer_sender = sender


def set_task_executor(executor: Callable[..., Awaitable[str | None]]) -> None:
    """Устанавливает TriggerExecutor.execute для запуска background tasks."""
    global _task_executor
    _task_executor = executor


_SYSTEM_TAGS_RE = re.compile(r'<\s*/?(?:message-body|sender-meta)\s*/?\s*>', re.IGNORECASE)


def _sanitize_tags(text: str) -> str:
    """Удаляет системные теги из пользовательского ввода."""
    return _SYSTEM_TAGS_RE.sub('', text)


# =============================================================================
# Owner Tools (7)
# =============================================================================


@tool(
    "create_task",
    "Create a task for a user. kind: task, meeting, question, reminder, check, etc. "
    "Deadline format: YYYY-MM-DD or YYYY-MM-DD HH:MM. "
    "context: additional data (e.g. meeting slots). message: optional initial message to send.",
    {"user": str, "title": str, "kind": str, "deadline": str, "context": dict, "message": str},
)
async def create_task(args: dict[str, Any]) -> dict[str, Any]:
    """Создаёт задачу для пользователя."""
    user_query = args.get("user")
    title = args.get("title")
    kind = args.get("kind", "task")
    deadline_str = args.get("deadline")
    context = args.get("context")
    message = args.get("message")

    if not user_query or not title:
        return _error("user и title обязательны")

    repo = get_users_repository()
    user = await repo.find_user(user_query)

    if not user:
        return _error(f"Пользователь '{user_query}' не найден")

    # Парсим дедлайн
    deadline = None
    if deadline_str:
        if " " in deadline_str:
            deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
        else:
            deadline = datetime.strptime(deadline_str, "%Y-%m-%d")
            deadline = deadline.replace(hour=23, minute=59)

    from src.config import settings
    task = await repo.create_task(
        title=title,
        kind=kind,
        assignee_id=user.telegram_id,
        created_by=get_current_owner_id() or settings.primary_owner_id,
        deadline=deadline,
        context=context,
    )

    # Отправляем сообщение пользователю
    notification = message
    if not notification:
        deadline_str_fmt = f"\nДедлайн: {deadline.strftime('%d.%m.%Y %H:%M')}" if deadline else ""
        notification = f"Новая задача:\n{title}{deadline_str_fmt}\n\nПодтверди получение."

    if _telegram_sender:
        await _telegram_sender(user.telegram_id, notification)

    deadline_info = f" (до {deadline.strftime('%d.%m.%Y %H:%M')})" if deadline else ""
    return _text(f"💎 Создана [{task.id}] для {user.display_name}{deadline_info}")


@tool(
    "list_tasks",
    "List tasks. Filter by user, status (pending/in_progress/done/cancelled), kind, overdue_only.",
    {"user": str, "status": str, "kind": str, "overdue_only": bool},
)
async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """Получает задачи с фильтрами."""
    user_query = args.get("user")
    status = args.get("status")
    kind = args.get("kind")
    overdue_only = args.get("overdue_only", False)

    repo = get_users_repository()

    assignee_id = None
    user_name = None
    if user_query:
        user = await repo.find_user(user_query)
        if not user:
            return _error(f"Пользователь '{user_query}' не найден")
        assignee_id = user.telegram_id
        user_name = user.display_name

    tasks = await repo.list_tasks(
        assignee_id=assignee_id,
        status=status,
        kind=kind,
        overdue_only=overdue_only,
    )

    if not tasks:
        scope = f" {user_name}" if user_name else ""
        return _text(f"Нет задач{scope}")

    header = f"Задачи {user_name}:" if user_name else "Все задачи:"
    lines = [header]
    for task in tasks:
        deadline = f" (до {task.deadline.strftime('%d.%m')})" if task.deadline else ""
        overdue_mark = " [ПРОСРОЧЕНО]" if task.is_overdue else ""
        kind_mark = f" [{task.kind}]" if task.kind != "task" else ""
        result_mark = ""
        if task.result:
            result_mark = f" → {json.dumps(task.result, ensure_ascii=False)[:50]}"

        # Schedule info для scheduled задач
        schedule_mark = ""
        if task.is_scheduled:
            time_str = task.schedule_at.strftime("%d.%m %H:%M")
            repeat = f", каждые {task.schedule_repeat}с" if task.schedule_repeat else ""
            schedule_mark = f" ⏰ {time_str}{repeat}"

        lines.append(f"[{task.status}] [{task.id}]{kind_mark} {task.title[:40]}{deadline}{overdue_mark}{schedule_mark}{result_mark}")

    return _text("\n".join(lines))


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

    if _telegram_sender:
        await _telegram_sender(user.telegram_id, message)
        logger.info(f"Sent to {user.display_name}: {message[:50]}...")
        return _text(f"Отправлено {user.display_name}")
    else:
        return _error("Telegram sender не настроен")


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
    "List known users. Set banned_only=true to show only banned.",
    {"banned_only": bool},
)
async def list_users(args: dict[str, Any]) -> dict[str, Any]:
    """Список пользователей."""
    banned_only = args.get("banned_only", False)

    repo = get_users_repository()

    if banned_only:
        users = await repo.list_banned_users()
        if not users:
            return _text("Нет забаненных пользователей")
        label = "Забаненные:"
    else:
        users = await repo.list_users()
        if not users:
            return _text("Нет известных пользователей")
        label = "Пользователи:"

    lines = [label]
    for user in users:
        username = f" @{user.username}" if user.username else ""
        banned = " [BAN]" if user.is_banned else ""
        lines.append(f"• {user.display_name}{username}{banned}")

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

    from src.config import settings
    if _telegram_sender:
        username = f" (@{user.username})" if user.username else ""
        await _telegram_sender(
            get_current_owner_id() or settings.primary_owner_id,
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

    from src.users import get_session_manager
    await get_session_manager().reset_session(user.telegram_id)

    from src.config import settings
    if _telegram_sender:
        username = f" (@{user.username})" if user.username else ""
        await _telegram_sender(
            get_current_owner_id() or settings.primary_owner_id,
            f"Пользователь {user.display_name}{username} разбанен"
        )

    return _text(f"{user.display_name} разбанен, сессия сброшена")


# =============================================================================
# External User Tools (4)
# =============================================================================


@tool(
    "get_my_tasks",
    "Get your tasks (regular and conversation). Use your Telegram ID from the system prompt.",
    {"user_id": int},
)
async def get_my_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """Получает задачи пользователя."""
    user_id = args.get("user_id")
    if not user_id:
        return _error("user_id обязателен")

    repo = get_users_repository()
    tasks = await repo.list_tasks(assignee_id=user_id)

    if not tasks:
        return _text("У вас нет открытых задач")

    lines = ["Ваши задачи:"]
    for task in tasks:
        deadline = f" (до {task.deadline.strftime('%d.%m')})" if task.deadline else ""
        overdue = " [ПРОСРОЧЕНО]" if task.is_overdue else ""
        kind_mark = f" [{task.kind}]" if task.kind != "task" else ""
        context_mark = ""
        if task.context:
            context_mark = f"\n  Контекст: {json.dumps(task.context, ensure_ascii=False)[:80]}"
        lines.append(f"[{task.status}] [{task.id}]{kind_mark} {task.title}{deadline}{overdue}{context_mark}")

    return _text("\n".join(lines))


@tool(
    "update_task",
    "Update task status or result. Use your Telegram ID from the system prompt. "
    "Status: pending, in_progress, done, cancelled. result: collected data (e.g. chosen time).",
    {"user_id": int, "task_id": str, "status": str, "result": dict},
)
async def update_task(args: dict[str, Any]) -> dict[str, Any]:
    """Обновляет задачу."""
    user_id = args.get("user_id")
    task_id = args.get("task_id")
    status = args.get("status")
    result = args.get("result")

    if not user_id or not task_id:
        return _error("user_id и task_id обязательны")

    if not status and result is None:
        return _error("Укажите status или result для обновления")

    valid_statuses = ["pending", "in_progress", "done", "cancelled"]
    if status and status not in valid_statuses:
        return _error(f"Неверный статус. Допустимые: {', '.join(valid_statuses)}")

    repo = get_users_repository()
    task = await repo.get_task(task_id)

    if not task:
        return _error(f"Задача [{task_id}] не найдена")

    if task.assignee_id != user_id:
        return _error("Вы можете обновлять только свои задачи")

    success = await repo.update_task(
        task_id=task_id,
        status=status,
        result=result,
    )

    if not success:
        return _error("Не удалось обновить задачу")

    # Уведомляем owner'а
    from src.config import settings
    user = await repo.get_user(user_id)
    user_name = user.display_name if user else str(user_id)

    skill = task.context.get("skill") if task.context else None

    if skill:
        async def _run_task_followup() -> None:
            try:
                from src.users.session_manager import get_session_manager
                sm = get_session_manager()

                # Получаем или создаём persistent task session
                session = sm.get_task_session(task_id, task.session_id)
                if session is None:
                    session = sm.create_task_session(task_id)

                prompt = _build_task_update_prompt(task, user_name, status, result)
                content = await session.query(prompt)

                # Сохраняем session_id в БД (если новый)
                if session._session_id and session._session_id != task.session_id:
                    await repo.update_task_session(task_id, session._session_id)

                # Уведомляем создателя задачи (или primary как fallback)
                if content and _telegram_sender:
                    from src.config import settings as _s
                    recipient = task.created_by or _s.primary_owner_id
                    await _telegram_sender(recipient, f"💎 Обновлена [{task_id}]:\n{content[:500]}")
            except Exception as e:
                logger.error(f"Task followup [{task_id}] failed: {e}")

        asyncio.create_task(_run_task_followup())
        logger.info(f"Launched persistent task session for [{task_id}] with skill={skill}")
    elif _context_sender:
        # Inject в контекст создателя задачи + autonomous query
        detail_parts = []
        if status:
            detail_parts.append(f"Статус: {status}")
        if result:
            detail_parts.append(f"Результат: {json.dumps(result, ensure_ascii=False)}")
        details = "\n".join(detail_parts)
        message = f"<sender-meta>{user_name} (ID: {user_id}) обновил задачу [{task_id}]</sender-meta>"
        if details:
            details = _sanitize_tags(details)
            message += f"\n<message-body>\n{details}\n</message-body>"
        recipient = task.created_by or settings.primary_owner_id
        await _context_sender(recipient, message)

    return _text(f"💎 Обновлена [{task_id}], владелец уведомлён")


@tool(
    "send_summary_to_owner",
    "Send a summary to the bot owner. Use your Telegram ID from the system prompt.",
    {"user_id": int, "summary": str},
)
async def send_summary_to_owner(args: dict[str, Any]) -> dict[str, Any]:
    """Отправляет сводку owner'у."""
    user_id = args.get("user_id")
    summary = args.get("summary")

    if not user_id or not summary:
        return _error("user_id и summary обязательны")

    from src.config import settings

    repo = get_users_repository()
    user = await repo.get_user(user_id)
    user_name = user.display_name if user else str(user_id)

    summary = _sanitize_tags(summary)
    message = f"<sender-meta>Сводка от {user_name} (ID: {user_id})</sender-meta>\n<message-body>\n{summary}\n</message-body>"

    if _context_sender:
        # Если у external'а есть открытая задача — шлём её создателю.
        # Иначе fallback на primary (нет привязки к конкретному owner'у).
        recipient = settings.primary_owner_id
        tasks = await repo.list_tasks(assignee_id=user_id, include_done=False)
        if tasks:
            # Берём самую свежую — она актуальна для контекста сводки.
            tasks_sorted = sorted(tasks, key=lambda t: t.created_at, reverse=True)
            if tasks_sorted[0].created_by:
                recipient = tasks_sorted[0].created_by
        await _context_sender(recipient, message)
        logger.info(f"Summary sent to owner context from {user_name} (recipient={recipient})")
        return _text("Сводка отправлена владельцу")
    else:
        return _error("Context sender не настроен")


@tool(
    "ban_violator",
    "Ban a user for rule violations. Use after warnings. Use your Telegram ID from the system prompt.",
    {"user_id": int, "reason": str},
)
async def ban_violator(args: dict[str, Any]) -> dict[str, Any]:
    """Банит нарушителя."""
    user_id = args.get("user_id")
    reason = args.get("reason", "нарушение правил")

    if not user_id:
        return _error("user_id обязателен (твой Telegram ID из промпта)")

    from src.config import settings as _settings
    if _settings.is_owner(user_id):
        return _error("Невозможно забанить владельца")

    repo = get_users_repository()

    user = await repo.get_user(user_id)
    if not user:
        return _error("Пользователь не найден")

    if user.is_banned:
        return _text("Пользователь уже забанен")

    await repo.ban_user(user_id)

    from src.config import settings
    if _telegram_sender:
        username = f" (@{user.username})" if user.username else ""
        await _telegram_sender(
            settings.primary_owner_id,
            f"{user.display_name}{username} забанен.\nПричина: {reason}"
        )

    return _text(f"Вы забанены: {reason}")


# =============================================================================
# Helpers — task follow-up
# =============================================================================


def _build_task_update_prompt(task: "Task", user_name: str, status: str | None, result: dict | None) -> str:
    """Формирует промпт для persistent task session при обновлении задачи."""
    parts = [f"<sender-meta>{user_name} обновил задачу [{task.id}] ({task.kind})</sender-meta>"]
    parts.append(f"Тема: {task.title}")
    if status:
        parts.append(f"Новый статус: {status}")
    if result:
        result_str = _sanitize_tags(json.dumps(result, ensure_ascii=False))
        parts.append(f"Результат: {result_str}")
    if task.context:
        parts.append(f"Контекст задачи: {json.dumps(task.context, ensure_ascii=False)}")
    parts.append("")
    parts.append(
        f"Используй скилл `{task.context.get('skill')}` для обработки этого результата. "
        f"Выполни все необходимые follow-up действия автоматически."
    )
    return "\n".join(parts)


# =============================================================================
# Task Context Tools
# =============================================================================


@tool(
    "read_task_context",
    "Read the full context (prompt + result) of a background task execution. "
    "Use task_id from list_tasks or 'recent' to see last 10 tasks.",
    {"task_id": str},
)
async def read_task_context(args: dict[str, Any]) -> dict[str, Any]:
    """Читает контекст выполненной background задачи."""
    import json
    from pathlib import Path
    from src.config import settings

    task_id = args.get("task_id", "").strip()

    if not task_id:
        return _error("task_id обязателен. Используй 'recent' для последних задач.")

    transcripts_dir = Path(settings.data_dir) / "task_transcripts"

    # Показать последние задачи
    if task_id.lower() == "recent":
        recent_file = transcripts_dir / "recent.jsonl"
        if not recent_file.exists():
            return _text("Нет сохранённых задач")

        lines = recent_file.read_text().strip().split("\n")[-10:]
        tasks = []
        for line in lines:
            try:
                t = json.loads(line)
                tasks.append(f"[{t['task_id']}] {t['timestamp'][:16]} — {t['prompt'][:50]}...")
            except Exception:
                continue

        if not tasks:
            return _text("Нет сохранённых задач")

        return _text("Последние задачи:\n" + "\n".join(tasks))

    # Читаем конкретную задачу
    transcript_file = transcripts_dir / f"{task_id}.json"

    if not transcript_file.exists():
        return _error(f"Transcript для задачи [{task_id}] не найден")

    try:
        transcript = json.loads(transcript_file.read_text())
    except Exception as e:
        return _error(f"Ошибка чтения: {e}")

    output = [
        f"**Задача [{transcript['task_id']}]**",
        f"Время: {transcript['timestamp']}",
        f"Источник: {transcript['source']}",
        "",
        "**Prompt:**",
        transcript["prompt"],
        "",
        "**Result:**",
        transcript["result"],
    ]

    return _text("\n".join(output))


# =============================================================================
# Tool Collections
# =============================================================================

OWNER_TOOLS = [
    create_task,
    list_tasks,
    send_to_user,
    resolve_user,
    list_users,
    ban_user,
    unban_user,
    read_task_context,
]

EXTERNAL_USER_TOOLS = [
    get_my_tasks,
    update_task,
    send_summary_to_owner,
    ban_violator,
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
