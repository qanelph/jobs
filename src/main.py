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
from src.telegram.tools import set_transports
from src.telegram.transport import Transport
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


def _has_telethon_config() -> bool:
    """Проверяет наличие Telethon конфигурации."""
    return bool(settings.tg_api_id and settings.tg_api_hash)


def _has_telethon_session() -> bool:
    """Проверяет наличие Telethon сессии."""
    session = load_session_string()
    return session is not None and len(session) > 0


async def main() -> None:
    """Точка входа."""
    setup_logging()

    force_setup = "--setup" in sys.argv
    if force_setup:
        logger.info("Принудительный setup (--setup)")
        if not await run_setup(force=True):
            logger.error("Setup не завершён")
            sys.exit(1)

    # SDK patches (до любого использования claude_agent_sdk)
    from src.users.sdk_compat import apply_sdk_patches
    apply_sdk_patches()

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

    transports: list[Transport] = []
    telethon_transport = None
    telethon_client = None

    # Telethon (если настроен и есть сессия)
    if _has_telethon_config() and _has_telethon_session():
        from src.telegram.telethon_transport import TelethonTransport

        session_string = load_session_string()
        client = create_client(session_string)
        telethon_transport = TelethonTransport(client)
        telethon_client = client

        try:
            await client.connect()

            if not await client.is_user_authorized():
                logger.error("Telegram Telethon сессия невалидна. Удалите data/telethon.session")
                sys.exit(1)

            me = await client.get_me()
            logger.info(f"Telethon: logged in as {me.first_name} (ID: {me.id})")

            # Загружаем диалоги в кэш и получаем инфо о owner'е
            await client.get_dialogs()
            try:
                owner = await client.get_entity(settings.primary_owner_id)
                set_owner_info(
                    telegram_id=settings.primary_owner_id,
                    first_name=owner.first_name,
                    username=owner.username,
                )
                logger.info(f"Owner: {owner.first_name} @{owner.username} (owners: {settings.tg_owner_ids})")
            except Exception as e:
                logger.warning(f"Could not get owner info: {e}. Write to bot first.")
                set_owner_info(settings.primary_owner_id, None, None)

            if me.id not in settings.tg_owner_ids:
                logger.warning(f"Logged user {me.id} not in TG_OWNER_IDS {settings.tg_owner_ids}")

            await telethon_transport.start()
            transports.append(telethon_transport)

        except Exception as e:
            logger.error(f"Telethon connection error: {e}")
            raise

    # Bot (если настроен)
    if settings.tg_bot_token:
        from src.telegram.bot_transport import BotTransport

        bot_transport = BotTransport(settings.tg_bot_token)
        await bot_transport.start()
        transports.append(bot_transport)

        me = await bot_transport.get_me()
        logger.info(f"Bot: @{me['username']} (ID: {me['id']})")

    if not transports:
        logger.error("Ни Telethon, ни Bot не настроены")
        sys.exit(1)

    # Primary transport: Telethon preferred, Bot fallback
    primary = transports[0]

    # Tools
    set_transports(primary, telethon_client)

    # Unified Trigger System
    session_manager = get_session_manager()
    executor = TriggerExecutor(primary, session_manager)
    trigger_manager = TriggerManager(executor, primary, str(settings.db_path))

    # Регистрируем типы динамических триггеров (только если Telethon)
    if telethon_transport:
        trigger_manager.register_type("tg_channel", TelegramChannelTrigger)

    # Регистрируем встроенные
    scheduler = SchedulerRunner(executor=executor)
    trigger_manager.register_builtin("scheduler", scheduler)

    if settings.heartbeat_interval_minutes > 0:
        heartbeat = HeartbeatRunner(
            executor=executor,
            transport=primary,
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

    # Регистрируем handlers — один экземпляр, регистрируем на все транспорты
    handlers = TelegramHandlers(primary, executor)
    for t in transports:
        handlers.register(t)
    await handlers.on_startup()

    # Автопроверка обновлений
    async def _auto_check_updates() -> None:
        updater = Updater()
        while True:
            await asyncio.sleep(AUTO_CHECK_INTERVAL)
            try:
                text = await updater.check_for_notification()
                if text:
                    await primary.send_message(settings.primary_owner_id, text)
            except Exception as e:
                logger.debug(f"Auto update check failed: {e}")

    asyncio.create_task(_auto_check_updates())

    logger.info(f"Bot is running ({len(transports)} transport(s)). Send me a message!")

    # Run transport loops параллельно
    loop_tasks = [asyncio.create_task(t.run_forever()) for t in transports]
    try:
        await asyncio.gather(*loop_tasks)
    finally:
        # Останавливаем транспорты
        for t in transports:
            try:
                await t.stop()
            except Exception as e:
                logger.error(f"Transport stop error: {e}")
        await trigger_manager.stop_all()


if __name__ == "__main__":
    asyncio.run(main())
