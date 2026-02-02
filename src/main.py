"""
Jobs — Personal AI Assistant.

Точка входа приложения.
"""

import asyncio
import sys

from loguru import logger

from src.config import settings
from src.telegram.client import create_client, load_session_string
from src.telegram.handlers import TelegramHandlers
from src.setup import run_setup, is_telegram_configured, is_claude_configured
from src.tools.scheduler import SchedulerRunner
from src.session import get_session


def setup_logging() -> None:
    """Настраивает логирование."""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level="DEBUG",
    )


async def on_scheduled_task(task_id: str, prompt: str) -> None:
    """Callback для выполнения запланированной задачи."""
    logger.info(f"Executing task {task_id}")

    # Получаем клиент из глобального контекста
    client = _telegram_client

    await client.send_message(
        settings.tg_user_id,
        f"⏰ Выполняю задачу:\n{prompt}",
    )

    session = get_session()
    response = await session.query(prompt)

    content = response.content
    if len(content) > 4000:
        content = content[:4000] + "..."

    await client.send_message(
        settings.tg_user_id,
        f"📋 Результат [{task_id}]:\n{content}",
    )


# Глобальная ссылка на клиент для scheduler callback
_telegram_client = None


async def main() -> None:
    """Точка входа."""
    global _telegram_client

    setup_logging()
    logger.info("Starting Jobs - Personal AI Assistant")

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

    try:
        await client.connect()

        if not await client.is_user_authorized():
            logger.error("Telegram сессия невалидна. Удалите data/telethon.session")
            sys.exit(1)

        me = await client.get_me()
        logger.info(f"Logged in as {me.first_name} (ID: {me.id})")

        if me.id != settings.tg_user_id:
            logger.warning(f"Logged user {me.id} != TG_USER_ID {settings.tg_user_id}")

    except Exception as e:
        logger.error(f"Connection error: {e}")
        raise

    # Запускаем scheduler
    scheduler = SchedulerRunner(on_task_due=on_scheduled_task)
    await scheduler.start()

    # Регистрируем handlers
    handlers = TelegramHandlers(client)
    handlers.register()

    logger.info("Bot is running. Send me a message!")

    try:
        await client.run_until_disconnected()
    finally:
        await scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
