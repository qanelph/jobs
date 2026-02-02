import asyncio
import sys

from loguru import logger

from src.config import settings
from src.telegram.client import create_client, load_session_string
from src.telegram.handlers import TelegramHandlers
from src.setup import run_setup, is_telegram_configured, is_claude_configured
from src.scheduler.runner import SchedulerRunner
from src.claude.runner import get_session


def setup_logging() -> None:
    """Настройка логирования."""
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

    # Проверяем нужен ли setup
    needs_setup = not is_telegram_configured() or not is_claude_configured()

    if needs_setup:
        logger.info("Требуется первоначальная настройка")
        success = await run_setup()
        if not success:
            logger.error("Setup не завершён, выход")
            sys.exit(1)

    # Загружаем сессию и запускаем бота
    session_string = load_session_string()
    client = create_client(session_string)

    try:
        await client.connect()

        if not await client.is_user_authorized():
            logger.error("Telegram сессия невалидна. Удалите data/telethon.session и перезапустите")
            sys.exit(1)

        me = await client.get_me()
        logger.info(f"Logged in as {me.first_name} (ID: {me.id})")

        if me.id != settings.tg_user_id:
            logger.warning(
                f"Logged in user ID ({me.id}) != configured TG_USER_ID ({settings.tg_user_id})"
            )
            logger.warning("Бот будет отвечать только на сообщения от TG_USER_ID")

    except Exception as e:
        logger.error(f"Ошибка подключения: {e}")
        raise

    # Callback для выполнения запланированных задач
    async def on_scheduled_task(task_id: str, prompt: str) -> None:
        """Выполняет запланированную задачу и отправляет результат."""
        logger.info(f"Executing scheduled task {task_id}")

        # Уведомляем пользователя о начале
        await client.send_message(
            settings.tg_user_id,
            f"⏰ Выполняю запланированную задачу:\n{prompt}"
        )

        # Выполняем через Claude
        session = get_session()
        response = await session.query(prompt)

        # Отправляем результат
        if len(response.content) > 4000:
            # TODO: Telegraph для длинных ответов
            await client.send_message(
                settings.tg_user_id,
                f"📋 Результат задачи {task_id}:\n{response.content[:4000]}..."
            )
        else:
            await client.send_message(
                settings.tg_user_id,
                f"📋 Результат задачи {task_id}:\n{response.content}"
            )

    # Запускаем scheduler
    scheduler = SchedulerRunner(on_task_due=on_scheduled_task)
    await scheduler.start()

    # Регистрируем обработчики
    handlers = TelegramHandlers(client)
    handlers.register()

    logger.info("Bot is running. Send me a message!")

    try:
        await client.run_until_disconnected()
    finally:
        await scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
