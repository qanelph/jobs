"""
Triggers — unified trigger system.

Встроенные (scheduler, heartbeat) + динамические (tg_channel, ...).
"""

from src.triggers.models import TriggerEvent, TriggerSubscription
from src.triggers.executor import TriggerExecutor
from src.triggers.manager import TriggerManager, TriggerSource
from src.triggers.tools import TRIGGER_TOOLS, TRIGGER_TOOL_NAMES, set_trigger_manager

__all__ = [
    "TriggerEvent",
    "TriggerSubscription",
    "TriggerExecutor",
    "TriggerManager",
    "TriggerSource",
    "TRIGGER_TOOLS",
    "TRIGGER_TOOL_NAMES",
    "set_trigger_manager",
]
