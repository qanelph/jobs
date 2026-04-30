"""
Telegram Tools — инструменты для работы с Telegram API.

Dual-mode:
- tg_send_message — работает через primary transport (Telethon или Bot)
- Telethon-only tools — требуют Telethon клиент (get_entity, dialogs, etc.)
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING
from pathlib import Path

from claude_agent_sdk import tool
from loguru import logger

from src.config import settings
from src.telegram.gate import use_client

if TYPE_CHECKING:
    from telethon import TelegramClient
    from src.telegram.transport import Transport


# Глобальные объекты (устанавливаются при старте)
_primary_transport: Transport | None = None
_telethon_client: TelegramClient | None = None


def set_transports(primary: Transport, telethon_client: TelegramClient | None) -> None:
    """Устанавливает транспорты для tools."""
    global _primary_transport, _telethon_client
    _primary_transport = primary
    _telethon_client = telethon_client


# Legacy alias
def set_telegram_client(client: TelegramClient) -> None:
    """Legacy: устанавливает Telethon клиент."""
    global _telethon_client
    _telethon_client = client


def _get_transport() -> Transport:
    """Получает primary transport."""
    if _primary_transport is None:
        raise RuntimeError("Transport not set")
    return _primary_transport


def has_telethon() -> bool:
    """Проверяет доступность Telethon клиента (runtime, не конфиг)."""
    return _telethon_client is not None


def _get_client() -> TelegramClient:
    """Получает Telethon клиент (для Telethon-only tools)."""
    if _telethon_client is None:
        raise RuntimeError("Требуется подключение Telethon (userbot)")
    return _telethon_client


# =============================================================================
# Send Tools
# =============================================================================


@tool(
    "tg_send_message",
    "Send a text message to any chat, channel, or user. Chat can be @username, phone, or ID. "
    "If chat is omitted, the message is sent to the user who initiated the current conversation "
    "(or primary owner as fallback).",
    {"chat": str, "message": str, "reply_to": int},
)
async def tg_send_message(args: dict[str, Any]) -> dict[str, Any]:
    """Отправляет текстовое сообщение через primary transport."""
    from src.users.tools import get_current_user_id
    chat = args.get("chat") or get_current_user_id() or settings.primary_owner_id
    message = args.get("message")
    reply_to = args.get("reply_to")

    if not message:
        return _error("message обязателен")

    transport = _get_transport()

    # Резолвим chat_id: если строка (@username / phone) — нужен Telethon
    chat_id: int
    if isinstance(chat, str) and not chat.lstrip("-").isdigit():
        if _telethon_client is None:
            return _error(f"Для отправки по @username/{chat} требуется Telethon")
        try:
            async with use_client() as client:
                entity = await client.get_entity(chat)
            chat_id = entity.id
        except Exception as e:
            return _error(f"Не удалось найти {chat}: {e}")
    else:
        chat_id = int(chat)

    try:
        # Если Telethon доступен и нужен reply_to — используем его напрямую
        if _telethon_client and reply_to:
            async with use_client() as client:
                entity = await client.get_entity(chat_id)
                result = await client.send_message(entity, message, reply_to=reply_to)
            msg_id = result.id
        else:
            msg_id = await transport.send_message(chat_id, message)
        return _text(f"Сообщение отправлено в {chat} (ID: {msg_id}):\n{message}")
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

    try:
        async with use_client() as client:
            entity = await client.get_entity(chat)
            result = await client.send_file(
                entity,
                path,
                caption=caption,
            )
        return _text(f"Медиа отправлено в {chat} (ID: {result.id})" + (f":\n{caption}" if caption else ""))
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

    try:
        async with use_client() as client:
            from_entity = await client.get_entity(from_chat)
            to_entity = await client.get_entity(to_chat)
            result = await client.forward_messages(
                to_entity,
                message_id,
                from_entity,
            )
        return _text(f"Сообщение {message_id} переслано из {from_chat} в {to_chat}")
    except Exception as e:
        return _error(f"Ошибка пересылки: {e}")


@tool(
    "tg_send_comment",
    "Post a comment on a channel post. Channel is @username or ID, post_id is the message ID.",
    {"channel": str, "post_id": int, "message": str},
)
async def tg_send_comment(args: dict[str, Any]) -> dict[str, Any]:
    """Отправляет комментарий к посту канала."""
    channel = args.get("channel")
    post_id = args.get("post_id")
    message = args.get("message")

    if not channel or not post_id or not message:
        return _error("channel, post_id и message обязательны")

    try:
        async with use_client() as client:
            entity = await client.get_entity(channel)
            result = await client.send_message(
                entity,
                message,
                comment_to=post_id,
            )
        return _text(f"Комментарий в {channel} к посту {post_id} (ID: {result.id}):\n{message}")
    except Exception as e:
        return _error(f"Ошибка отправки комментария: {e}")


@tool(
    "tg_get_participants",
    "Get members of a group or channel. Returns ID, name, username, status.",
    {"chat": str, "limit": int, "search": str},
)
async def tg_get_participants(args: dict[str, Any]) -> dict[str, Any]:
    """Получает список участников группы/канала."""
    chat = args.get("chat")
    limit = args.get("limit", 50)
    search = args.get("search", "")

    if not chat:
        return _error("chat обязателен")

    limit = min(limit, 200)

    try:
        async with use_client() as client:
            entity = await client.get_entity(chat)
            participants = await client.get_participants(entity, limit=limit, search=search)

        if not participants:
            return _text("Нет участников" + (f" по запросу '{search}'" if search else ""))

        lines = [f"Участники ({len(participants)}" + (f", поиск: '{search}'" if search else "") + "):\n"]

        for user in participants:
            name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "—"
            username = f"@{user.username}" if user.username else "—"
            status = _format_status(user.status)
            bot_tag = " [бот]" if user.bot else ""
            lines.append(f"[{user.id}] {name} ({username}) — {status}{bot_tag}")

        return _text("\n".join(lines))
    except Exception as e:
        return _error(f"Ошибка получения участников: {e}")


# =============================================================================
# Read Tools
# =============================================================================


@tool(
    "tg_read_channel",
    "Read recent posts from a channel or public group with reactions.",
    {"channel": str, "limit": int},
)
async def tg_read_channel(args: dict[str, Any]) -> dict[str, Any]:
    """Читает посты канала с реакциями."""
    channel = args.get("channel")
    limit = args.get("limit", 10)

    if not channel:
        return _error("channel обязателен")

    limit = min(limit, 50)  # Ограничение

    try:
        async with use_client() as client:
            entity = await client.get_entity(channel)
            messages = await client.get_messages(entity, limit=limit)

        if not messages:
            return _text("Нет сообщений")

        lines = [f"Последние {len(messages)} постов из {channel}:\n"]

        for msg in messages:
            date = msg.date.strftime("%d.%m %H:%M")
            text = msg.text[:200] + "..." if msg.text and len(msg.text) > 200 else (msg.text or "[медиа]")
            views = f" | {msg.views} views" if msg.views else ""

            # Реакции
            reactions_str = ""
            if msg.reactions and msg.reactions.results:
                reactions = []
                for r in msg.reactions.results:
                    emoji = r.reaction.emoticon if hasattr(r.reaction, 'emoticon') else "👍"
                    reactions.append(f"{emoji}{r.count}")
                reactions_str = f"\n   Реакции: {' '.join(reactions)}"

            # Комменты
            comments_str = ""
            if msg.replies and msg.replies.comments:
                comments_str = f" | {msg.replies.replies} comments"

            lines.append(f"[{msg.id}] {date}{views}{comments_str}\n{text}{reactions_str}\n")

        return _text("\n".join(lines))
    except Exception as e:
        return _error(f"Ошибка чтения: {e}")


@tool(
    "tg_read_comments",
    "Read comments on a channel post with user details (ID, username).",
    {"channel": str, "post_id": int, "limit": int},
)
async def tg_read_comments(args: dict[str, Any]) -> dict[str, Any]:
    """Читает комментарии к посту с деталями об авторах."""
    channel = args.get("channel")
    post_id = args.get("post_id")
    limit = args.get("limit", 20)

    if not channel or not post_id:
        return _error("channel и post_id обязательны")

    limit = min(limit, 50)

    try:
        async with use_client() as client:
            entity = await client.get_entity(channel)
            comments = await client.get_messages(
                entity,
                reply_to=post_id,
                limit=limit,
            )

            if not comments:
                return _text("Нет комментариев")

            lines = [f"Комментарии к посту {post_id}:\n"]

            for msg in comments:
                sender = await msg.get_sender()
                sender_info = _format_sender_detailed(sender)
                date = msg.date.strftime("%d.%m %H:%M")
                text = msg.text[:150] + "..." if msg.text and len(msg.text) > 150 else (msg.text or "[медиа]")
                lines.append(f"{sender_info} ({date}):\n{text}\n")

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

    try:
        async with use_client() as client:
            entity = await client.get_entity(chat)
            messages = await client.get_messages(entity, limit=limit)

            if not messages:
                return _text("Нет сообщений")

            lines = [f"История чата ({len(messages)} сообщений):\n"]

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

    try:
        async with use_client() as client:
            entity = await client.get_entity(chat)
            messages = await client.get_messages(
                entity,
                search=query,
                limit=limit,
            )

            if not messages:
                return _text(f"Ничего не найдено по запросу '{query}'")

            lines = [f"Найдено {len(messages)} сообщений по '{query}':\n"]

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
    from telethon.tl.types import User, Channel, Chat

    user = args.get("user")

    if not user:
        return _error("user обязателен")

    try:
        async with use_client() as client:
            entity = await client.get_entity(user)

        if isinstance(entity, User):
            username = f"@{entity.username}" if entity.username else "нет"
            phone = entity.phone or "скрыт"
            name = f"{entity.first_name or ''} {entity.last_name or ''}".strip() or "нет"
            status = _format_status(entity.status)
            bot = "да" if entity.bot else "нет"
            verified = "да" if entity.verified else "нет"

            return _text(
                f"Пользователь:\n"
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
                f"Канал/Группа:\n"
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
    from telethon.tl.types import User, Channel, Chat

    limit = args.get("limit", 30)
    limit = min(limit, 100)

    try:
        async with use_client() as client:
            dialogs = await client.get_dialogs(limit=limit)

        if not dialogs:
            return _text("Нет диалогов")

        lines = [f"Диалоги ({len(dialogs)}):\n"]

        for dialog in dialogs:
            entity = dialog.entity
            unread = f" [{dialog.unread_count} unread]" if dialog.unread_count else ""

            if isinstance(entity, User):
                name = f"{entity.first_name or ''} {entity.last_name or ''}".strip()
                username = f" @{entity.username}" if entity.username else ""
                lines.append(f"[user] {name}{username}{unread}")
            elif isinstance(entity, Channel):
                username = f" @{entity.username}" if entity.username else ""
                lines.append(f"[channel] {entity.title}{username}{unread}")
            elif isinstance(entity, Chat):
                lines.append(f"[group] {entity.title}{unread}")
            else:
                lines.append(f"[?] {dialog.name}{unread}")

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

    try:
        async with use_client() as client:
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

        return _text(f"Скачано: {downloaded}")
    except Exception as e:
        return _error(f"Ошибка скачивания: {e}")


# =============================================================================
# Browser Control
# =============================================================================

BROWSER_CONTROL_FILE = Path("/browser-control/proxy_enabled")
SUPERVISORD_URL = f"http://browser:9001/RPC2"


@tool(
    "browser_proxy",
    "Toggle browser proxy. enabled=true routes traffic through proxy, enabled=false connects directly.",
    {"enabled": bool},
)
async def browser_proxy(args: dict[str, Any]) -> dict[str, Any]:
    """Переключает прокси в браузере."""
    import aiohttp
    import xmlrpc.client

    enabled = args.get("enabled")
    if enabled is None:
        return _error("enabled обязателен (true/false)")

    # Пишем флаг
    BROWSER_CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    BROWSER_CONTROL_FILE.write_text("1" if enabled else "0")
    logger.info(f"Browser proxy set to {'ON' if enabled else 'OFF'}")

    # Рестартим tinyproxy (не chromium!) — CDP-соединение сохраняется
    headers = {"Content-Type": "text/xml"}

    async with aiohttp.ClientSession(trust_env=False) as session:
        payload = xmlrpc.client.dumps(("tinyproxy",), "supervisor.stopProcess")
        async with session.post(SUPERVISORD_URL, data=payload, headers=headers) as resp:
            await resp.text()

        payload = xmlrpc.client.dumps(("tinyproxy",), "supervisor.startProcess")
        async with session.post(SUPERVISORD_URL, data=payload, headers=headers) as resp:
            await resp.text()

    import asyncio
    await asyncio.sleep(1)

    mode = "через прокси" if enabled else "напрямую"
    return _text(f"Браузер перезапущен — трафик идёт {mode}")


# =============================================================================
# Tool Collections
# =============================================================================

TELEGRAM_TOOLS = [
    tg_send_message,
    tg_send_media,
    tg_forward_message,
    tg_send_comment,
    tg_read_channel,
    tg_read_comments,
    tg_read_chat,
    tg_search_messages,
    tg_get_user_info,
    tg_get_participants,
    tg_get_dialogs,
    tg_download_media,
    browser_proxy,
]

# Tools доступные всегда (через любой transport)
_COMMON_TOOL_NAMES = [
    "mcp__jobs__tg_send_message",
    "mcp__jobs__browser_proxy",
]

# Telethon-only tools (требуют Telethon клиент)
TELETHON_ONLY_TOOL_NAMES = {
    "mcp__jobs__tg_send_media",
    "mcp__jobs__tg_forward_message",
    "mcp__jobs__tg_send_comment",
    "mcp__jobs__tg_read_channel",
    "mcp__jobs__tg_read_comments",
    "mcp__jobs__tg_read_chat",
    "mcp__jobs__tg_search_messages",
    "mcp__jobs__tg_get_user_info",
    "mcp__jobs__tg_get_participants",
    "mcp__jobs__tg_get_dialogs",
    "mcp__jobs__tg_download_media",
}

# Legacy: полный список (для обратной совместимости)
TELEGRAM_TOOL_NAMES = [
    *_COMMON_TOOL_NAMES,
    *sorted(TELETHON_ONLY_TOOL_NAMES),
]


def get_available_telegram_tool_names() -> list[str]:
    """Возвращает список доступных Telegram tool names на основе текущих транспортов."""
    names = list(_COMMON_TOOL_NAMES)
    if _telethon_client is not None:
        names.extend(sorted(TELETHON_ONLY_TOOL_NAMES))
    return names


# =============================================================================
# Helpers
# =============================================================================


def _format_sender(sender) -> str:
    """Форматирует отправителя (краткий вариант)."""
    from telethon.tl.types import User, Channel

    if sender is None:
        return "Unknown"
    if isinstance(sender, User):
        name = f"{sender.first_name or ''} {sender.last_name or ''}".strip()
        return name or f"@{sender.username}" if sender.username else str(sender.id)
    if isinstance(sender, Channel):
        return sender.title
    return str(sender.id)


def _format_sender_detailed(sender) -> str:
    """Форматирует отправителя с ID и username."""
    from telethon.tl.types import User, Channel

    if sender is None:
        return "Unknown"

    if isinstance(sender, User):
        name = f"{sender.first_name or ''} {sender.last_name or ''}".strip() or "NoName"
        username = f"@{sender.username}" if sender.username else "no_username"
        return f"{name} [{username}, ID:{sender.id}]"

    if isinstance(sender, Channel):
        username = f"@{sender.username}" if sender.username else "no_username"
        return f"{sender.title} [{username}, ID:{sender.id}]"

    return f"Unknown [ID:{sender.id}]"


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
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}
