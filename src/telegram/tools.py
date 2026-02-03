"""
Telegram Tools — инструменты для работы с Telegram API.

Только для owner'а — полный доступ к Telegram через Telethon.
"""

from typing import Any, Callable, Awaitable
from pathlib import Path

from claude_agent_sdk import tool
from loguru import logger
from telethon import TelegramClient
from telethon.tl.types import User, Channel, Chat

from src.config import settings


# Глобальный клиент (устанавливается при старте)
_telegram_client: TelegramClient | None = None


def set_telegram_client(client: TelegramClient) -> None:
    """Устанавливает Telegram клиент для tools."""
    global _telegram_client
    _telegram_client = client


def _get_client() -> TelegramClient:
    """Получает Telegram клиент."""
    if _telegram_client is None:
        raise RuntimeError("Telegram client not set")
    return _telegram_client


# =============================================================================
# Send Tools
# =============================================================================


@tool(
    "tg_send_message",
    "Send a text message to any chat, channel, or user. Chat can be @username, phone, or ID.",
    {"chat": str, "message": str, "reply_to": int},
)
async def tg_send_message(args: dict[str, Any]) -> dict[str, Any]:
    """Отправляет текстовое сообщение."""
    chat = args.get("chat")
    message = args.get("message")
    reply_to = args.get("reply_to")

    if not chat or not message:
        return _error("chat и message обязательны")

    client = _get_client()

    try:
        entity = await client.get_entity(chat)
        result = await client.send_message(
            entity,
            message,
            reply_to=reply_to if reply_to else None,
        )
        return _text(f"✅ Сообщение отправлено (ID: {result.id})")
    except Exception as e:
        return _error(f"Ошибка отправки: {e}")


@tool(
    "tg_send_media",
    "Send photo or video to any chat. Media_path is local file path.",
    {"chat": str, "media_path": str, "caption": str},
)
async def tg_send_media(args: dict[str, Any]) -> dict[str, Any]:
    """Отправляет фото или видео."""
    chat = args.get("chat")
    media_path = args.get("media_path")
    caption = args.get("caption", "")

    if not chat or not media_path:
        return _error("chat и media_path обязательны")

    path = Path(media_path)
    if not path.exists():
        return _error(f"Файл не найден: {media_path}")

    client = _get_client()

    try:
        entity = await client.get_entity(chat)
        result = await client.send_file(
            entity,
            path,
            caption=caption,
        )
        return _text(f"✅ Медиа отправлено (ID: {result.id})")
    except Exception as e:
        return _error(f"Ошибка отправки: {e}")


@tool(
    "tg_forward_message",
    "Forward a message from one chat to another.",
    {"from_chat": str, "to_chat": str, "message_id": int},
)
async def tg_forward_message(args: dict[str, Any]) -> dict[str, Any]:
    """Пересылает сообщение."""
    from_chat = args.get("from_chat")
    to_chat = args.get("to_chat")
    message_id = args.get("message_id")

    if not from_chat or not to_chat or not message_id:
        return _error("from_chat, to_chat и message_id обязательны")

    client = _get_client()

    try:
        from_entity = await client.get_entity(from_chat)
        to_entity = await client.get_entity(to_chat)

        result = await client.forward_messages(
            to_entity,
            message_id,
            from_entity,
        )
        return _text(f"✅ Сообщение переслано")
    except Exception as e:
        return _error(f"Ошибка пересылки: {e}")


# =============================================================================
# Read Tools
# =============================================================================


@tool(
    "tg_read_channel",
    "Read recent posts from a channel or public group.",
    {"channel": str, "limit": int},
)
async def tg_read_channel(args: dict[str, Any]) -> dict[str, Any]:
    """Читает посты канала."""
    channel = args.get("channel")
    limit = args.get("limit", 10)

    if not channel:
        return _error("channel обязателен")

    limit = min(limit, 50)  # Ограничение
    client = _get_client()

    try:
        entity = await client.get_entity(channel)
        messages = await client.get_messages(entity, limit=limit)

        if not messages:
            return _text("Нет сообщений")

        lines = [f"📢 Последние {len(messages)} постов из {channel}:\n"]

        for msg in messages:
            date = msg.date.strftime("%d.%m %H:%M")
            text = msg.text[:200] + "..." if msg.text and len(msg.text) > 200 else (msg.text or "[медиа]")
            views = f" 👁 {msg.views}" if msg.views else ""
            lines.append(f"[{msg.id}] {date}{views}\n{text}\n")

        return _text("\n".join(lines))
    except Exception as e:
        return _error(f"Ошибка чтения: {e}")


@tool(
    "tg_read_comments",
    "Read comments on a channel post.",
    {"channel": str, "post_id": int, "limit": int},
)
async def tg_read_comments(args: dict[str, Any]) -> dict[str, Any]:
    """Читает комментарии к посту."""
    channel = args.get("channel")
    post_id = args.get("post_id")
    limit = args.get("limit", 20)

    if not channel or not post_id:
        return _error("channel и post_id обязательны")

    limit = min(limit, 50)
    client = _get_client()

    try:
        entity = await client.get_entity(channel)
        comments = await client.get_messages(
            entity,
            reply_to=post_id,
            limit=limit,
        )

        if not comments:
            return _text("Нет комментариев")

        lines = [f"💬 Комментарии к посту {post_id}:\n"]

        for msg in comments:
            sender = await msg.get_sender()
            name = _format_sender(sender)
            date = msg.date.strftime("%d.%m %H:%M")
            text = msg.text[:150] + "..." if msg.text and len(msg.text) > 150 else (msg.text or "[медиа]")
            lines.append(f"{name} ({date}):\n{text}\n")

        return _text("\n".join(lines))
    except Exception as e:
        return _error(f"Ошибка чтения комментариев: {e}")


@tool(
    "tg_read_chat",
    "Read message history from a chat.",
    {"chat": str, "limit": int},
)
async def tg_read_chat(args: dict[str, Any]) -> dict[str, Any]:
    """Читает историю чата."""
    chat = args.get("chat")
    limit = args.get("limit", 20)

    if not chat:
        return _error("chat обязателен")

    limit = min(limit, 50)
    client = _get_client()

    try:
        entity = await client.get_entity(chat)
        messages = await client.get_messages(entity, limit=limit)

        if not messages:
            return _text("Нет сообщений")

        lines = [f"💬 История чата ({len(messages)} сообщений):\n"]

        for msg in reversed(messages):  # Хронологический порядок
            sender = await msg.get_sender()
            name = _format_sender(sender)
            date = msg.date.strftime("%d.%m %H:%M")
            text = msg.text[:200] + "..." if msg.text and len(msg.text) > 200 else (msg.text or "[медиа]")
            lines.append(f"[{msg.id}] {name} ({date}):\n{text}\n")

        return _text("\n".join(lines))
    except Exception as e:
        return _error(f"Ошибка чтения: {e}")


@tool(
    "tg_search_messages",
    "Search messages in a chat by query.",
    {"chat": str, "query": str, "limit": int},
)
async def tg_search_messages(args: dict[str, Any]) -> dict[str, Any]:
    """Ищет сообщения в чате."""
    chat = args.get("chat")
    query = args.get("query")
    limit = args.get("limit", 20)

    if not chat or not query:
        return _error("chat и query обязательны")

    limit = min(limit, 50)
    client = _get_client()

    try:
        entity = await client.get_entity(chat)
        messages = await client.get_messages(
            entity,
            search=query,
            limit=limit,
        )

        if not messages:
            return _text(f"Ничего не найдено по запросу '{query}'")

        lines = [f"🔍 Найдено {len(messages)} сообщений по '{query}':\n"]

        for msg in messages:
            sender = await msg.get_sender()
            name = _format_sender(sender)
            date = msg.date.strftime("%d.%m %H:%M")
            text = msg.text[:150] + "..." if msg.text and len(msg.text) > 150 else (msg.text or "[медиа]")
            lines.append(f"[{msg.id}] {name} ({date}):\n{text}\n")

        return _text("\n".join(lines))
    except Exception as e:
        return _error(f"Ошибка поиска: {e}")


# =============================================================================
# Info Tools
# =============================================================================


@tool(
    "tg_get_user_info",
    "Get information about a user by @username, phone, or ID.",
    {"user": str},
)
async def tg_get_user_info(args: dict[str, Any]) -> dict[str, Any]:
    """Получает информацию о пользователе."""
    user = args.get("user")

    if not user:
        return _error("user обязателен")

    client = _get_client()

    try:
        entity = await client.get_entity(user)

        if isinstance(entity, User):
            username = f"@{entity.username}" if entity.username else "нет"
            phone = entity.phone or "скрыт"
            name = f"{entity.first_name or ''} {entity.last_name or ''}".strip() or "нет"
            status = _format_status(entity.status)
            bot = "да" if entity.bot else "нет"
            verified = "да" if entity.verified else "нет"

            return _text(
                f"👤 Пользователь:\n"
                f"ID: {entity.id}\n"
                f"Имя: {name}\n"
                f"Username: {username}\n"
                f"Телефон: {phone}\n"
                f"Статус: {status}\n"
                f"Бот: {bot}\n"
                f"Верифицирован: {verified}"
            )

        elif isinstance(entity, (Channel, Chat)):
            title = entity.title
            username = f"@{entity.username}" if hasattr(entity, 'username') and entity.username else "нет"
            members = getattr(entity, 'participants_count', 'неизвестно')

            return _text(
                f"📢 Канал/Группа:\n"
                f"ID: {entity.id}\n"
                f"Название: {title}\n"
                f"Username: {username}\n"
                f"Участников: {members}"
            )

        else:
            return _text(f"Entity type: {type(entity).__name__}, ID: {entity.id}")

    except Exception as e:
        return _error(f"Ошибка получения инфо: {e}")


@tool(
    "tg_get_dialogs",
    "Get list of all chats/dialogs.",
    {"limit": int},
)
async def tg_get_dialogs(args: dict[str, Any]) -> dict[str, Any]:
    """Получает список диалогов."""
    limit = args.get("limit", 30)
    limit = min(limit, 100)

    client = _get_client()

    try:
        dialogs = await client.get_dialogs(limit=limit)

        if not dialogs:
            return _text("Нет диалогов")

        lines = [f"💬 Диалоги ({len(dialogs)}):\n"]

        for dialog in dialogs:
            entity = dialog.entity
            unread = f" 🔴 {dialog.unread_count}" if dialog.unread_count else ""

            if isinstance(entity, User):
                name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
                username = f" @{entity.username}" if entity.username else ""
                lines.append(f"👤 {name}{username}{unread}")
            elif isinstance(entity, Channel):
                username = f" @{entity.username}" if entity.username else ""
                lines.append(f"📢 {entity.title}{username}{unread}")
            elif isinstance(entity, Chat):
                lines.append(f"👥 {entity.title}{unread}")
            else:
                lines.append(f"❓ {dialog.name}{unread}")

        return _text("\n".join(lines))
    except Exception as e:
        return _error(f"Ошибка получения диалогов: {e}")


@tool(
    "tg_download_media",
    "Download media from a message to workspace.",
    {"chat": str, "message_id": int, "filename": str},
)
async def tg_download_media(args: dict[str, Any]) -> dict[str, Any]:
    """Скачивает медиа из сообщения."""
    chat = args.get("chat")
    message_id = args.get("message_id")
    filename = args.get("filename")

    if not chat or not message_id:
        return _error("chat и message_id обязательны")

    client = _get_client()

    try:
        entity = await client.get_entity(chat)
        messages = await client.get_messages(entity, ids=message_id)

        if not messages:
            return _error(f"Сообщение {message_id} не найдено")

        msg = messages[0] if isinstance(messages, list) else messages

        if not msg.media:
            return _error("В сообщении нет медиа")

        # Путь для сохранения
        downloads_dir = settings.workspace_dir / "downloads"
        downloads_dir.mkdir(exist_ok=True)

        if filename:
            path = downloads_dir / filename
        else:
            path = downloads_dir

        downloaded = await client.download_media(msg, path)

        return _text(f"✅ Скачано: {downloaded}")
    except Exception as e:
        return _error(f"Ошибка скачивания: {e}")


# =============================================================================
# Tool Collections
# =============================================================================

TELEGRAM_TOOLS = [
    tg_send_message,
    tg_send_media,
    tg_forward_message,
    tg_read_channel,
    tg_read_comments,
    tg_read_chat,
    tg_search_messages,
    tg_get_user_info,
    tg_get_dialogs,
    tg_download_media,
]

TELEGRAM_TOOL_NAMES = [
    "mcp__jobs__tg_send_message",
    "mcp__jobs__tg_send_media",
    "mcp__jobs__tg_forward_message",
    "mcp__jobs__tg_read_channel",
    "mcp__jobs__tg_read_comments",
    "mcp__jobs__tg_read_chat",
    "mcp__jobs__tg_search_messages",
    "mcp__jobs__tg_get_user_info",
    "mcp__jobs__tg_get_dialogs",
    "mcp__jobs__tg_download_media",
]


# =============================================================================
# Helpers
# =============================================================================


def _format_sender(sender) -> str:
    """Форматирует отправителя."""
    if sender is None:
        return "Unknown"
    if isinstance(sender, User):
        name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
        return name or f"@{sender.username}" if sender.username else str(sender.id)
    if isinstance(sender, Channel):
        return sender.title
    return str(sender.id)


def _format_status(status) -> str:
    """Форматирует статус пользователя."""
    if status is None:
        return "неизвестно"

    status_type = type(status).__name__

    if "Online" in status_type:
        return "онлайн"
    elif "Offline" in status_type:
        if hasattr(status, 'was_online'):
            return f"был {status.was_online.strftime('%d.%m %H:%M')}"
        return "оффлайн"
    elif "Recently" in status_type:
        return "недавно"
    elif "LastWeek" in status_type:
        return "на этой неделе"
    elif "LastMonth" in status_type:
        return "в этом месяце"
    else:
        return "давно"


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"❌ {text}"}], "is_error": True}
