"""
SDK compatibility patches.

Monkey-patch для claude_agent_sdk — пропускаем неизвестные типы сообщений
(e.g. rate_limit_event) вместо падения с MessageParseError.

TODO: удалить после обновления SDK с поддержкой rate_limit_event.
"""

from loguru import logger

_applied = False


def apply_sdk_patches() -> None:
    """Применяет патчи к SDK. Безопасно вызывать несколько раз."""
    global _applied
    if _applied:
        return

    import claude_agent_sdk._internal.message_parser as parser

    original = parser.parse_message

    def safe_parse_message(data: dict) -> object | None:
        try:
            return original(data)
        except Exception as exc:
            if "Unknown message type" in str(exc):
                logger.warning(f"SDK: skipping unknown message type: {exc}")
                return None
            raise

    parser.parse_message = safe_parse_message
    _applied = True
    logger.debug("SDK patches applied")
