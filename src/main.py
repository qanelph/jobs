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
from src.heartbeat import HeartbeatRunner
from src.triggers import TriggerExecutor, TriggerManager, set_trigger_manager
from src.triggers.sources.tg_channel import TelegramChannelTrigger
from src.updater import Updater, AUTO_CHECK_INTERVAL


def setup_logging() -> None:
    """Настраивает логирование."""
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level="DEBUG",
    )


async def main() -> None:
    """Точка входа."""
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

    # Unified Trigger System
    session_manager = get_session_manager()
    executor = TriggerExecutor(client, session_manager)
    trigger_manager = TriggerManager(executor, client, str(settings.db_path))

    # Регистрируем типы динамических триггеров
    trigger_manager.register_type("tg_channel", TelegramChannelTrigger)

    # Регистрируем встроенные
    scheduler = SchedulerRunner(executor=executor)
    trigger_manager.register_builtin("scheduler", scheduler)

    if settings.heartbeat_interval_minutes > 0:
        heartbeat = HeartbeatRunner(
            executor=executor,
            client=client,
            session_manager=session_manager,
            interval_minutes=settings.heartbeat_interval_minutes,
        )
        trigger_manager.register_builtin("heartbeat", heartbeat)
    else:
        logger.info("Heartbeat disabled (interval=0)")

    # Устанавливаем singleton для trigger tools
    set_trigger_manager(trigger_manager)

    # Запуск (builtins + загрузка подписок из DB)
    await trigger_manager.start_all()

    # Регистрируем handlers (интерактивный Telegram — отдельно)
    handlers = TelegramHandlers(client, executor)
    handlers.register()
    await handlers.on_startup()

    # Автопроверка обновлений
    async def _auto_check_updates() -> None:
        updater = Updater()
        while True:
            await asyncio.sleep(AUTO_CHECK_INTERVAL)
            try:
                text = await updater.check_for_notification()
                if text:
                    await client.send_message(settings.tg_user_id, text)
            except Exception as e:
                logger.debug(f"Auto update check failed: {e}")

    asyncio.create_task(_auto_check_updates())

    logger.info("Bot is running. Send me a message!")

    try:
        await client.run_until_disconnected()
    finally:
        await trigger_manager.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
