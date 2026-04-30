"""
Trigger Models — структуры данных для системы триггеров.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TriggerEvent:
    """Событие от любого источника (scheduler, heartbeat, tg_channel, ...)."""

    source: str                            # "scheduler", "heartbeat", "tg_channel:@news"
    prompt: str                            # Промпт для агента
    context: dict = field(default_factory=dict)
    notify_owner: bool = True
    preview_message: str | None = None     # "Выполняю задачу..." → отправить ДО query
    silent_marker: str | None = None       # "HEARTBEAT_OK" → не доставлять результат
    result_prefix: str | None = None       # "Результат [id]:" → добавить к ответу
    # Кому слать notify_owner / preview_message. None = primary owner (backward compat),
    # [] = не слать никому (выключено), [123, 456] = broadcast.
    recipient_ids: list[int] | None = None

    # Override модели Claude для выполнения. None = settings.claude_model.
    # Алиас ("haiku"/"sonnet"/"opus") или полное имя ("claude-opus-4-7").
    model: str | None = None


@dataclass
class TriggerSubscription:
    """Динамическая подписка на источник событий."""

    id: str
    trigger_type: str                      # "tg_channel", "email", ...
    config: dict = field(default_factory=dict)
    prompt: str = ""
    active: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    # Получатели срабатываний. None = backward compat (primary owner),
    # [] = не доставлять никому, [123, 456] = broadcast этим owner'ам.
    recipient_ids: list[int] | None = None
