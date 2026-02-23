"""
TriggerManager — центральный координатор триггеров.

Два уровня:
1. Встроенные (builtin) — scheduler, heartbeat. Всегда работают.
2. Динамические (dynamic) — tg_channel, email. Агент создаёт через tools.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable, TYPE_CHECKING

from loguru import logger

from src.triggers.executor import TriggerExecutor
from src.triggers.models import TriggerSubscription
from src.triggers.storage import TriggerStorage

if TYPE_CHECKING:
    from src.telegram.transport import Transport


@runtime_checkable
class TriggerSource(Protocol):
    """Протокол для любого источника триггеров."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# Factory: (executor, transport, config, prompt) → TriggerSource
TriggerFactory = Callable[
    [TriggerExecutor, "Transport", dict, str],
    TriggerSource,
]


MAX_DYNAMIC_SUBSCRIPTIONS = 20


class TriggerManager:
    """Управляет всеми источниками триггеров."""

    def __init__(
        self,
        executor: TriggerExecutor,
        transport: "Transport",
        db_path: str,
    ) -> None:
        self._executor = executor
        self._transport = transport
        self._storage = TriggerStorage(db_path)
        self._builtins: dict[str, TriggerSource] = {}
        self._dynamic: dict[str, TriggerSource] = {}  # sub_id → source
        self._type_registry: dict[str, TriggerFactory] = {}

    def register_builtin(self, name: str, source: TriggerSource) -> None:
        """Регистрирует встроенный источник (scheduler, heartbeat)."""
        self._builtins[name] = source
        logger.debug(f"Builtin trigger registered: {name}")

    def register_type(self, type_name: str, factory: TriggerFactory) -> None:
        """Регистрирует тип динамического триггера."""
        self._type_registry[type_name] = factory
        logger.debug(f"Trigger type registered: {type_name}")

    async def start_all(self) -> None:
        """Запускает builtins + загружает подписки из DB."""
        # 1. Builtins
        for name, source in self._builtins.items():
            await source.start()
            logger.info(f"Builtin trigger started: {name}")

        # 2. Dynamic subscriptions from DB
        subs = await self._storage.list_active()
        for sub in subs:
            await self._start_subscription(sub)

        logger.info(
            f"TriggerManager started: {len(self._builtins)} builtins, "
            f"{len(self._dynamic)} dynamic"
        )

    async def stop_all(self) -> None:
        """Останавливает всё (dynamic first, then builtins)."""
        # Dynamic
        for sub_id, source in list(self._dynamic.items()):
            await self._safe_stop(source, f"dynamic:{sub_id}")
        self._dynamic.clear()

        # Builtins (reversed)
        for name in reversed(list(self._builtins.keys())):
            await self._safe_stop(self._builtins[name], f"builtin:{name}")

        # Storage
        await self._storage.close()

        logger.info("TriggerManager stopped")

    async def subscribe(
        self, trigger_type: str, config: dict, prompt: str
    ) -> TriggerSubscription:
        """Создаёт подписку и запускает source. Откатывает при ошибке старта."""
        if trigger_type not in self._type_registry:
            available = ", ".join(self._type_registry.keys()) or "нет"
            raise ValueError(
                f"Неизвестный тип триггера: {trigger_type}. Доступные: {available}"
            )

        if len(self._dynamic) >= MAX_DYNAMIC_SUBSCRIPTIONS:
            raise ValueError(
                f"Лимит подписок ({MAX_DYNAMIC_SUBSCRIPTIONS}) исчерпан. "
                "Удали ненужные через unsubscribe_trigger."
            )

        # Проверка дубликатов по типу + config
        existing = await self._storage.list_active()
        for s in existing:
            if s.trigger_type == trigger_type and s.config == config:
                raise ValueError(
                    f"Подписка на {trigger_type} с такой конфигурацией уже существует [{s.id}]"
                )

        sub = await self._storage.create(trigger_type, config, prompt)

        try:
            await self._start_subscription_strict(sub)
        except Exception as e:
            await self._storage.delete(sub.id)
            raise ValueError(f"Не удалось запустить триггер: {e}") from e

        logger.info(f"Subscribed [{sub.id}]: {trigger_type} {config}")
        return sub

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Останавливает source и удаляет подписку."""
        source = self._dynamic.pop(subscription_id, None)
        if source:
            await self._safe_stop(source, f"dynamic:{subscription_id}")

        deleted = await self._storage.delete(subscription_id)
        if deleted:
            logger.info(f"Unsubscribed [{subscription_id}]")
        return deleted

    async def list_subscriptions(self) -> list[TriggerSubscription]:
        """Все активные подписки."""
        return await self._storage.list_active()

    async def _start_subscription_strict(self, sub: TriggerSubscription) -> None:
        """Создаёт и запускает source. Пробрасывает ошибки (для subscribe)."""
        factory = self._type_registry.get(sub.trigger_type)
        if not factory:
            raise ValueError(f"Нет factory для типа '{sub.trigger_type}'")

        source = factory(self._executor, self._transport, sub.config, sub.prompt)
        await source.start()
        self._dynamic[sub.id] = source
        logger.info(f"Dynamic trigger started [{sub.id}]: {sub.trigger_type}")

    async def _start_subscription(self, sub: TriggerSubscription) -> None:
        """Создаёт и запускает source. Soft-режим для загрузки из DB."""
        try:
            await self._start_subscription_strict(sub)
        except Exception as e:
            logger.error(f"Failed to start trigger [{sub.id}]: {e}")

    async def _safe_stop(self, source: TriggerSource, label: str) -> None:
        """Безопасно останавливает source."""
        try:
            await source.stop()
        except Exception as e:
            logger.error(f"Error stopping {label}: {e}")
