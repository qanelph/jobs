"""
Jobs — Personal AI Assistant.

Точка входа приложения.
"""

import asyncio
import sys

from loguru import logger

from src.config import settings, set_owner_info
from src.telegram.client import create_client, load_session_string
from src.telegram.handlers import TelegramHandlers
from src.telegram.tools import set_telegram_client
from src.setup import run_setup, is_telegram_configured, is_claude_configured
from src.tools.scheduler import SchedulerRunner
from src.users import get_session_manager
from src.memory import get_storage
from src.heartbeat import HeartbeatRunner, set_heartbeat_client


def setup_logging() -> None:
    """Настраивает логирование."""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level="DEBUG",
    )


async def on_heartbeat_alert(message: str) -> None:
    """Callback для heartbeat уведомлений."""
    logger.info(f"Heartbeat alert: {message[:50]}...")

    client = _telegram_client
    await client.send_message(settings.tg_user_id, message)


async def on_scheduled_task(task_id: str, prompt: str) -> None:
    """Callback для выполнения запланированной задачи."""
    logger.info(f"Executing task {task_id}")

    # Получаем клиент из глобального контекста
    client = _telegram_client

    await client.send_message(
        settings.tg_user_id,
        f"Выполняю задачу:\n{prompt}",
    )

    # Используем сессию owner'а для scheduled tasks
    session_manager = get_session_manager()
    session = session_manager.get_owner_session()
    content = await session.query(prompt)

    if len(content) > 4000:
        content = content[:4000] + "..."

    await client.send_message(
        settings.tg_user_id,
        f"Результат [{task_id}]:\n{content}",
    )


# Глобальная ссылка на клиент для scheduler callback
_telegram_client = None


async def main() -> None:
    """Точка входа."""
    global _telegram_client

    setup_logging()
    logger.info("Starting Jobs - Personal AI Assistant")

    # Инициализируем память (создаёт структуру файлов)
    memory_storage = get_storage()
    logger.info(f"Memory initialized at {settings.workspace_dir}")

    # Setup при первом запуске
    if not is_telegram_configured() or not is_claude_configured():
        logger.info("Требуется первоначальная настройка")
        if not await run_setup():
            logger.error("Setup не завершён")
            sys.exit(1)

    # Создаём клиент
    session_string = load_session_string()
    client = create_client(session_string)
    _telegram_client = client
    set_heartbeat_client(client)  # Для отправки напоминаний
    set_telegram_client(client)  # Для Telegram tools

    try:
        await client.connect()

        if not await client.is_user_authorized():
            logger.error("Telegram сессия невалидна. Удалите data/telethon.session")
            sys.exit(1)

        me = await client.get_me()
        logger.info(f"Logged in as {me.first_name} (ID: {me.id})")

        # Загружаем диалоги в кэш и получаем инфо о owner'е
        await client.get_dialogs()
        try:
            owner = await client.get_entity(settings.tg_user_id)
            set_owner_info(
                telegram_id=settings.tg_user_id,
                first_name=owner.first_name,
                username=owner.username,
            )
            logger.info(f"Owner: {owner.first_name} @{owner.username} (ID: {settings.tg_user_id})")
        except Exception as e:
            logger.warning(f"Could not get owner info: {e}. Write to bot first.")
            set_owner_info(settings.tg_user_id, None, None)

        if me.id != settings.tg_user_id:
            logger.warning(f"Logged user {me.id} != TG_USER_ID {settings.tg_user_id}")

    except Exception as e:
        logger.error(f"Connection error: {e}")
        raise

    # Запускаем scheduler
    scheduler = SchedulerRunner(on_task_due=on_scheduled_task)
    await scheduler.start()

    # Запускаем heartbeat (если включён)
    heartbeat = None
    if settings.heartbeat_interval_minutes > 0:
        heartbeat = HeartbeatRunner(
            on_alert=on_heartbeat_alert,
            interval_minutes=settings.heartbeat_interval_minutes,
        )
        await heartbeat.start()
    else:
        logger.info("Heartbeat disabled (interval=0)")

    # Регистрируем handlers
    handlers = TelegramHandlers(client)
    handlers.register()

    logger.info("Bot is running. Send me a message!")

    try:
        await client.run_until_disconnected()
    finally:
        if heartbeat:
            await heartbeat.stop()
        await scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
