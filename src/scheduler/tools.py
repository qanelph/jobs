from datetime import datetime, timedelta
from typing import Any
import uuid

from claude_agent_sdk import tool, create_sdk_mcp_server
from loguru import logger

from src.scheduler.store import scheduler_store


@tool(
    "schedule_task",
    "Schedule a task to be executed later. Use this when user asks to remind, schedule, or do something at a specific time.",
    {
        "prompt": str,  # Что выполнить
        "delay_minutes": int,  # Через сколько минут (опционально)
        "at_time": str,  # Конкретное время в формате HH:MM (опционально)
        "at_date": str,  # Конкретная дата в формате YYYY-MM-DD (опционально)
    }
)
async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
    """Планирует задачу на выполнение позже."""
    prompt = args.get("prompt")
    delay_minutes = args.get("delay_minutes")
    at_time = args.get("at_time")
    at_date = args.get("at_date")

    if not prompt:
        return {
            "content": [{"type": "text", "text": "Ошибка: не указан prompt для задачи"}],
            "is_error": True,
        }

    # Вычисляем время выполнения
    now = datetime.now()

    if delay_minutes:
        scheduled_at = now + timedelta(minutes=delay_minutes)
    elif at_time:
        # Парсим время HH:MM
        try:
            hour, minute = map(int, at_time.split(":"))
            scheduled_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            # Если время уже прошло сегодня — на завтра
            if scheduled_at <= now:
                scheduled_at += timedelta(days=1)
        except ValueError:
            return {
                "content": [{"type": "text", "text": f"Ошибка: неверный формат времени '{at_time}', ожидается HH:MM"}],
                "is_error": True,
            }
    else:
        return {
            "content": [{"type": "text", "text": "Ошибка: укажи delay_minutes или at_time"}],
            "is_error": True,
        }

    # Если указана дата
    if at_date:
        try:
            year, month, day = map(int, at_date.split("-"))
            scheduled_at = scheduled_at.replace(year=year, month=month, day=day)
        except ValueError:
            return {
                "content": [{"type": "text", "text": f"Ошибка: неверный формат даты '{at_date}', ожидается YYYY-MM-DD"}],
                "is_error": True,
            }

    # Создаём задачу
    task_id = str(uuid.uuid4())[:8]

    await scheduler_store.add_task(
        task_id=task_id,
        prompt=prompt,
        scheduled_at=scheduled_at,
    )

    time_str = scheduled_at.strftime("%d.%m.%Y %H:%M")
    logger.info(f"Scheduled task {task_id}: '{prompt[:50]}...' at {time_str}")

    return {
        "content": [{
            "type": "text",
            "text": f"✅ Задача запланирована на {time_str}\nID: {task_id}\nЗадача: {prompt}"
        }]
    }


@tool(
    "list_scheduled_tasks",
    "List all pending scheduled tasks",
    {}
)
async def list_scheduled_tasks(args: dict[str, Any]) -> dict[str, Any]:
    """Показывает список запланированных задач."""
    tasks = await scheduler_store.get_pending_tasks()

    if not tasks:
        return {
            "content": [{"type": "text", "text": "Нет запланированных задач"}]
        }

    lines = ["📋 Запланированные задачи:\n"]
    for task in tasks:
        time_str = task["scheduled_at"].strftime("%d.%m.%Y %H:%M")
        lines.append(f"• [{task['id']}] {time_str}: {task['prompt'][:50]}...")

    return {
        "content": [{"type": "text", "text": "\n".join(lines)}]
    }


@tool(
    "cancel_scheduled_task",
    "Cancel a scheduled task by its ID",
    {"task_id": str}
)
async def cancel_scheduled_task(args: dict[str, Any]) -> dict[str, Any]:
    """Отменяет запланированную задачу."""
    task_id = args.get("task_id")

    if not task_id:
        return {
            "content": [{"type": "text", "text": "Ошибка: не указан task_id"}],
            "is_error": True,
        }

    success = await scheduler_store.cancel_task(task_id)

    if success:
        return {
            "content": [{"type": "text", "text": f"✅ Задача {task_id} отменена"}]
        }
    else:
        return {
            "content": [{"type": "text", "text": f"❌ Задача {task_id} не найдена"}],
            "is_error": True,
        }


def create_scheduler_server():
    """Создаёт MCP сервер с инструментами планировщика."""
    return create_sdk_mcp_server(
        name="scheduler",
        version="1.0.0",
        tools=[schedule_task, list_scheduled_tasks, cancel_scheduled_task],
    )
