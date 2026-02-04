"""
Trigger Tools — subscribe/unsubscribe/list для агента.
"""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from claude_agent_sdk import tool
from loguru import logger

if TYPE_CHECKING:
    from src.triggers.manager import TriggerManager


# Singleton — устанавливается из main.py
_trigger_manager: TriggerManager | None = None


def set_trigger_manager(manager: TriggerManager) -> None:
    """Устанавливает singleton TriggerManager."""
    global _trigger_manager
    _trigger_manager = manager


def get_trigger_manager() -> TriggerManager:
    """Возвращает singleton TriggerManager."""
    if _trigger_manager is None:
        raise RuntimeError("TriggerManager not initialized")
    return _trigger_manager


def _parse_config(raw: Any) -> dict:
    """Парсит config из любого формата (dict, str, None)."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        # Сначала пробуем как JSON
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Fallback: если строка похожа на @channel — трактуем как channel name
            if raw.startswith("@") or raw.startswith("-"):
                return {"channel": raw}
            raise
    logger.warning(f"Unexpected config type: {type(raw).__name__}, value: {raw!r}")
    return {}


@tool(
    "subscribe_trigger",
    "Subscribe to an event source. "
    "Types: tg_channel. "
    'Config for tg_channel: {"channel": "@channel_name"}. '
    "Prompt: instruction for agent when event fires.",
    {"type": str, "config": dict, "prompt": str},
)
async def subscribe_trigger(args: dict[str, Any]) -> dict[str, Any]:
    logger.info(f"subscribe_trigger called: {args!r}")

    trigger_type = args.get("type")
    prompt = args.get("prompt", "")

    # Парсим config — принимаем и dict, и JSON-строку
    try:
        config = _parse_config(args.get("config"))
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"subscribe_trigger config parse error: {e}, raw={args.get('config')!r}")
        return _error(f"Неверный формат config: {e}")

    logger.info(f"subscribe_trigger parsed: type={trigger_type}, config={config}, prompt={prompt[:50]}")

    if not trigger_type:
        return _error("type обязателен")
    if not prompt:
        return _error("prompt обязателен")

    # Валидация config по типу триггера
    if trigger_type == "tg_channel" and not config.get("channel"):
        return _error('config.channel обязателен для tg_channel (например: {"channel": "@channel_name"})')

    manager = get_trigger_manager()

    try:
        sub = await manager.subscribe(trigger_type, config, prompt)
    except ValueError as e:
        logger.warning(f"subscribe_trigger failed: {e}")
        return _error(str(e))

    logger.info(f"subscribe_trigger success: [{sub.id}] {trigger_type}")
    return _text(f"Подписка [{sub.id}] создана: {trigger_type} {config}")


@tool(
    "unsubscribe_trigger",
    "Unsubscribe from event source by subscription ID.",
    {"subscription_id": str},
)
async def unsubscribe_trigger(args: dict[str, Any]) -> dict[str, Any]:
    logger.info(f"unsubscribe_trigger called: {args!r}")

    sub_id = args.get("subscription_id")
    if not sub_id:
        return _error("subscription_id обязателен")

    manager = get_trigger_manager()
    deleted = await manager.unsubscribe(sub_id)

    if deleted:
        logger.info(f"unsubscribe_trigger success: [{sub_id}]")
        return _text(f"Подписка [{sub_id}] удалена")

    logger.warning(f"unsubscribe_trigger: [{sub_id}] not found")
    return _error(f"Подписка [{sub_id}] не найдена")


@tool(
    "list_triggers",
    "List active trigger subscriptions.",
    {},
)
async def list_triggers(args: dict[str, Any]) -> dict[str, Any]:
    logger.info("list_triggers called")

    manager = get_trigger_manager()
    subs = await manager.list_subscriptions()

    if not subs:
        return _text("Нет активных подписок")

    lines = []
    for sub in subs:
        lines.append(
            f"[{sub.id}] {sub.trigger_type} | {sub.config} | {sub.prompt[:50]}"
        )

    return _text("\n".join(lines))


TRIGGER_TOOLS = [subscribe_trigger, unsubscribe_trigger, list_triggers]

TRIGGER_TOOL_NAMES = [
    "mcp__jobs__subscribe_trigger",
    "mcp__jobs__unsubscribe_trigger",
    "mcp__jobs__list_triggers",
]


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}
