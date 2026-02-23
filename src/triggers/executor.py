"""
TriggerExecutor — единая точка выполнения TriggerEvent.

Принимает событие, отправляет preview, запрашивает агента,
проверяет silent_marker, доставляет результат owner'у.

Каждое выполнение получает одноразовую сессию —
параллельные задачи не блокируют друг друга и не прерывают owner.

Transcript каждой задачи сохраняется для доступа из owner session.
"""

import json
from datetime import datetime
from pathlib import Path

from loguru import logger

from src.config import settings
from src.triggers.models import TriggerEvent
from src.users.session_manager import SessionManager

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.telegram.transport import Transport


MAX_MESSAGE_LENGTH = 4000
TRANSCRIPTS_DIR = Path(settings.data_dir) / "task_transcripts"


class TriggerExecutor:
    """Выполняет TriggerEvent: query → deliver."""

    def __init__(self, transport: "Transport", session_manager: SessionManager) -> None:
        self._transport = transport
        self._session_manager = session_manager

    async def execute(self, event: TriggerEvent) -> str | None:
        """
        Выполняет событие триггера в одноразовой сессии.

        1. Отправляет preview_message owner'у (если есть)
        2. Запрашивает агента через ephemeral background session
        3. Проверяет silent_marker — если есть, не доставляет
        4. Добавляет result_prefix, truncate, отправляет owner'у

        Returns:
            Ответ агента или None (если silent).
        """
        logger.debug(f"Executing trigger event: {event.source}")

        # Preview (без буферизации — это просто уведомление)
        if event.preview_message and event.notify_owner:
            await self.send_to_owner(event.preview_message, buffer=False)

        # Одноразовая сессия с owner tools
        session = self._session_manager.create_background_session()
        try:
            content = await session.query(event.prompt)
        finally:
            await session.destroy()

        content = content.strip()

        # Silent marker check
        if event.silent_marker and event.silent_marker in content:
            logger.debug(f"Trigger {event.source}: silent ({event.silent_marker})")
            return None

        # Prepare result
        if event.silent_marker:
            content = content.replace(event.silent_marker, "").strip()

        if not content:
            return None

        if event.result_prefix:
            content = f"{event.result_prefix}\n{content}"

        # Truncate
        if len(content) > MAX_MESSAGE_LENGTH:
            content = content[:MAX_MESSAGE_LENGTH] + "..."

        # Сохраняем transcript для доступа из owner session
        task_id = event.context.get("task_id") if event.context else None
        if task_id:
            self._save_transcript(
                task_id=task_id,
                source=event.source,
                prompt=event.prompt,
                result=content,
            )

        # Deliver
        if event.notify_owner:
            await self.send_to_owner(content)

        return content

    async def send_to_owner(self, text: str, buffer: bool = True) -> None:
        """
        Отправляет сообщение owner'у.

        Args:
            text: текст сообщения
            buffer: буферизовать в owner session (для сохранения контекста)
        """
        await self._transport.send_message(settings.primary_owner_id, text)

        # Буферизуем в owner session чтобы сохранить контекст
        if buffer:
            owner_session = self._session_manager.get_session(settings.primary_owner_id)
            owner_session.receive_incoming(f"[Background task output]\n{text}")

    def _save_transcript(
        self,
        task_id: str,
        source: str,
        prompt: str,
        result: str,
    ) -> None:
        """Сохраняет transcript задачи для доступа из owner session."""
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

        transcript = {
            "task_id": task_id,
            "source": source,
            "timestamp": datetime.now().isoformat(),
            "prompt": prompt,
            "result": result,
        }

        # Сохраняем по task_id
        transcript_file = TRANSCRIPTS_DIR / f"{task_id}.json"
        transcript_file.write_text(json.dumps(transcript, ensure_ascii=False, indent=2))

        # Также добавляем в общий лог последних задач (для поиска)
        recent_file = TRANSCRIPTS_DIR / "recent.jsonl"
        with open(recent_file, "a") as f:
            f.write(json.dumps(transcript, ensure_ascii=False) + "\n")

        # Держим только последние 100 записей в recent
        self._trim_recent_log(recent_file, max_lines=100)

        logger.debug(f"Saved transcript for task [{task_id}]")

    def _trim_recent_log(self, file: Path, max_lines: int) -> None:
        """Обрезает лог до последних N строк."""
        if not file.exists():
            return

        lines = file.read_text().strip().split("\n")
        if len(lines) > max_lines:
            file.write_text("\n".join(lines[-max_lines:]) + "\n")
