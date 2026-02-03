"""
Repository — хранение данных о пользователях и задачах в SQLite.
"""

import asyncio
import uuid
from datetime import datetime

import aiosqlite
from loguru import logger

from src.config import settings
from .models import ExternalUser, UserTask, ConversationTask


class UsersRepository:
    """Репозиторий для пользователей и задач."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()  # Защита от race condition

    async def _get_db(self) -> aiosqlite.Connection:
        if self._db is not None:
            return self._db

        async with self._db_lock:
            # Double-check после получения lock
            if self._db is None:
                self._db = await aiosqlite.connect(self._db_path)
                self._db.row_factory = aiosqlite.Row
                await self._init_schema()
        return self._db

    async def _init_schema(self) -> None:
        """Создаёт таблицы если не существуют."""
        await self._db.execute("PRAGMA journal_mode=WAL")

        # Внешние пользователи
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS external_users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                notes TEXT DEFAULT '',
                first_contact TEXT DEFAULT CURRENT_TIMESTAMP,
                last_contact TEXT DEFAULT CURRENT_TIMESTAMP,
                warnings_count INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0
            )
        """)

        # Миграция: добавляем колонки если их нет
        try:
            await self._db.execute("ALTER TABLE external_users ADD COLUMN warnings_count INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await self._db.execute("ALTER TABLE external_users ADD COLUMN is_banned INTEGER DEFAULT 0")
        except Exception:
            pass

        # Задачи пользователей
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS user_tasks (
                id TEXT PRIMARY KEY,
                assignee_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                deadline TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                created_by INTEGER,
                FOREIGN KEY (assignee_id) REFERENCES external_users(telegram_id)
            )
        """)

        # Conversation Tasks (cross-session communication)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_tasks (
                id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                task_type TEXT DEFAULT 'custom',
                title TEXT DEFAULT '',
                context TEXT DEFAULT '{}',
                status TEXT DEFAULT 'pending',
                result TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Индексы
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON user_tasks(assignee_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON user_tasks(status)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_username ON external_users(username)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_user ON conversation_tasks(user_id, status)"
        )

        await self._db.commit()
        logger.debug("Users DB schema initialized")

    # =========================================================================
    # Users
    # =========================================================================

    async def get_user(self, telegram_id: int) -> ExternalUser | None:
        """Получает пользователя по telegram_id."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM external_users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_user(row)
        return None

    async def upsert_user(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        phone: str | None = None,
    ) -> ExternalUser:
        """Создаёт или обновляет пользователя."""
        db = await self._get_db()

        existing = await self.get_user(telegram_id)
        now = datetime.now().isoformat()

        if existing:
            # Обновляем только если есть новые данные
            await db.execute(
                """
                UPDATE external_users SET
                    username = COALESCE(?, username),
                    first_name = COALESCE(?, first_name),
                    last_name = COALESCE(?, last_name),
                    phone = COALESCE(?, phone),
                    last_contact = ?
                WHERE telegram_id = ?
                """,
                (username, first_name, last_name, phone, now, telegram_id),
            )
        else:
            await db.execute(
                """
                INSERT INTO external_users (telegram_id, username, first_name, last_name, phone, first_contact, last_contact)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, username, first_name, last_name, phone, now, now),
            )
            logger.info(f"New user: {telegram_id} (@{username})")

        await db.commit()
        return await self.get_user(telegram_id)

    async def update_user_notes(self, telegram_id: int, notes: str) -> None:
        """Обновляет заметки о пользователе."""
        db = await self._get_db()
        await db.execute(
            "UPDATE external_users SET notes = ? WHERE telegram_id = ?",
            (notes, telegram_id),
        )
        await db.commit()

    async def find_user(self, query: str) -> ExternalUser | None:
        """
        Ищет пользователя по username, имени или телефону.
        Поддерживает fuzzy matching для имён.
        query может быть: @username, имя, телефон
        """
        db = await self._get_db()
        query_clean = query.strip().lstrip("@").lower()

        # 1. Точный поиск по username
        cursor = await db.execute(
            "SELECT * FROM external_users WHERE LOWER(username) = ?",
            (query_clean,),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_user(row)

        # 2. Точный поиск по telegram_id (если это число)
        if query_clean.isdigit():
            cursor = await db.execute(
                "SELECT * FROM external_users WHERE telegram_id = ?",
                (int(query_clean),),
            )
            row = await cursor.fetchone()
            if row:
                return self._row_to_user(row)

        # 3. Поиск по имени (частичное совпадение)
        cursor = await db.execute(
            """
            SELECT * FROM external_users
            WHERE LOWER(first_name) LIKE ? OR LOWER(last_name) LIKE ?
            """,
            (f"%{query_clean}%", f"%{query_clean}%"),
        )
        rows = await cursor.fetchall()
        if rows:
            # Если одно совпадение — возвращаем
            if len(rows) == 1:
                return self._row_to_user(rows[0])
            # Если несколько — ищем лучшее через fuzzy matching
            best = self._fuzzy_match(query_clean, rows)
            if best:
                return best

        # 4. Поиск по телефону
        cursor = await db.execute(
            "SELECT * FROM external_users WHERE phone LIKE ?",
            (f"%{query_clean}%",),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_user(row)

        # 5. Fuzzy search по всем пользователям
        all_users = await self.list_users()
        return self._fuzzy_match(query_clean, all_users)

    def _fuzzy_match(
        self,
        query: str,
        candidates: list,
    ) -> ExternalUser | None:
        """
        Fuzzy matching для поиска пользователя.
        Использует простой алгоритм на основе общих символов.
        """
        if not candidates:
            return None

        query_lower = query.lower()
        best_score = 0
        best_user = None

        for candidate in candidates:
            # Если это Row — конвертируем
            if hasattr(candidate, 'keys'):
                user = self._row_to_user(candidate)
            else:
                user = candidate

            # Собираем все варианты имени
            names = []
            if user.first_name:
                names.append(user.first_name.lower())
            if user.last_name:
                names.append(user.last_name.lower())
            if user.username:
                names.append(user.username.lower())

            # Считаем score для каждого варианта
            for name in names:
                score = self._similarity_score(query_lower, name)
                if score > best_score:
                    best_score = score
                    best_user = user

        # Порог совпадения — минимум 50%
        if best_score >= 0.5:
            return best_user
        return None

    def _similarity_score(self, s1: str, s2: str) -> float:
        """
        Вычисляет схожесть двух строк (0.0 - 1.0).
        Простой алгоритм на основе общих биграмм.
        """
        if not s1 or not s2:
            return 0.0

        # Точное совпадение
        if s1 == s2:
            return 1.0

        # Одна строка содержит другую
        if s1 in s2 or s2 in s1:
            return 0.9

        # Биграммы
        def bigrams(s: str) -> set:
            return {s[i:i+2] for i in range(len(s) - 1)} if len(s) > 1 else {s}

        bg1 = bigrams(s1)
        bg2 = bigrams(s2)

        if not bg1 or not bg2:
            return 0.0

        intersection = len(bg1 & bg2)
        union = len(bg1 | bg2)

        return intersection / union if union > 0 else 0.0

    async def list_users(self) -> list[ExternalUser]:
        """Список всех пользователей."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM external_users ORDER BY last_contact DESC"
        )
        return [self._row_to_user(row) for row in await cursor.fetchall()]

    def _row_to_user(self, row: aiosqlite.Row) -> ExternalUser:
        return ExternalUser(
            telegram_id=row["telegram_id"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            phone=row["phone"],
            notes=row["notes"] or "",
            first_contact=datetime.fromisoformat(row["first_contact"]) if row["first_contact"] else datetime.now(),
            last_contact=datetime.fromisoformat(row["last_contact"]) if row["last_contact"] else datetime.now(),
            warnings_count=row["warnings_count"] if "warnings_count" in row.keys() else 0,
            is_banned=bool(row["is_banned"]) if "is_banned" in row.keys() else False,
        )

    # =========================================================================
    # Bans & Warnings
    # =========================================================================

    async def is_user_banned(self, telegram_id: int) -> bool:
        """Проверяет забанен ли пользователь."""
        user = await self.get_user(telegram_id)
        return user.is_banned if user else False

    async def ban_user(self, telegram_id: int) -> bool:
        """Банит пользователя."""
        db = await self._get_db()
        cursor = await db.execute(
            "UPDATE external_users SET is_banned = 1 WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()
        if cursor.rowcount > 0:
            logger.info(f"User {telegram_id} banned")
            return True
        return False

    async def unban_user(self, telegram_id: int) -> bool:
        """Разбанивает пользователя и сбрасывает предупреждения."""
        db = await self._get_db()
        cursor = await db.execute(
            "UPDATE external_users SET is_banned = 0, warnings_count = 0 WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()
        if cursor.rowcount > 0:
            logger.info(f"User {telegram_id} unbanned")
            return True
        return False

    async def add_warning(self, telegram_id: int) -> int:
        """
        Добавляет предупреждение пользователю.
        Возвращает новое количество предупреждений.
        После 2 предупреждений автоматически банит.
        """
        db = await self._get_db()

        # Увеличиваем счётчик
        await db.execute(
            "UPDATE external_users SET warnings_count = warnings_count + 1 WHERE telegram_id = ?",
            (telegram_id,),
        )
        await db.commit()

        # Получаем текущее количество
        user = await self.get_user(telegram_id)
        if not user:
            return 0

        warnings = user.warnings_count

        # Авто-бан после 2 предупреждений
        if warnings >= 2:
            await self.ban_user(telegram_id)
            logger.warning(f"User {telegram_id} auto-banned after {warnings} warnings")

        return warnings

    async def get_warnings(self, telegram_id: int) -> int:
        """Получает количество предупреждений."""
        user = await self.get_user(telegram_id)
        return user.warnings_count if user else 0

    async def list_banned_users(self) -> list[ExternalUser]:
        """Список забаненных пользователей."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM external_users WHERE is_banned = 1"
        )
        return [self._row_to_user(row) for row in await cursor.fetchall()]

    # =========================================================================
    # Tasks
    # =========================================================================

    async def create_task(
        self,
        assignee_id: int,
        description: str,
        deadline: datetime | None = None,
        created_by: int | None = None,
    ) -> UserTask:
        """Создаёт задачу для пользователя."""
        db = await self._get_db()
        task_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()

        await db.execute(
            """
            INSERT INTO user_tasks (id, assignee_id, description, deadline, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, assignee_id, description, deadline.isoformat() if deadline else None, now, created_by),
        )
        await db.commit()
        logger.info(f"Task created: [{task_id}] for {assignee_id}")

        return await self.get_task(task_id)

    async def get_task(self, task_id: str) -> UserTask | None:
        """Получает задачу по ID."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM user_tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_task(row)
        return None

    async def get_user_tasks(
        self,
        assignee_id: int,
        include_completed: bool = False,
    ) -> list[UserTask]:
        """Получает задачи пользователя."""
        db = await self._get_db()

        if include_completed:
            cursor = await db.execute(
                "SELECT * FROM user_tasks WHERE assignee_id = ? ORDER BY created_at DESC",
                (assignee_id,),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM user_tasks WHERE assignee_id = ? AND status NOT IN ('completed') ORDER BY deadline ASC NULLS LAST",
                (assignee_id,),
            )

        return [self._row_to_task(row) for row in await cursor.fetchall()]

    async def get_overdue_tasks(self) -> list[UserTask]:
        """Получает все просроченные задачи."""
        db = await self._get_db()
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """
            SELECT * FROM user_tasks
            WHERE status NOT IN ('completed') AND deadline IS NOT NULL AND deadline < ?
            ORDER BY deadline ASC
            """,
            (now,),
        )
        return [self._row_to_task(row) for row in await cursor.fetchall()]

    async def get_upcoming_tasks(self, hours: int = 24) -> list[UserTask]:
        """Получает задачи с дедлайном в ближайшие N часов."""
        db = await self._get_db()
        now = datetime.now()
        future = datetime.now().replace(hour=now.hour + hours) if hours < 24 else datetime.now()

        # Простой вариант: дедлайн сегодня
        cursor = await db.execute(
            """
            SELECT * FROM user_tasks
            WHERE status NOT IN ('completed') AND deadline IS NOT NULL
            ORDER BY deadline ASC
            """,
        )
        tasks = [self._row_to_task(row) for row in await cursor.fetchall()]

        # Фильтруем в Python (проще чем datetime арифметика в SQLite)
        from datetime import timedelta
        cutoff = now + timedelta(hours=hours)
        return [t for t in tasks if t.deadline and t.deadline <= cutoff]

    async def update_task_status(self, task_id: str, status: str) -> bool:
        """Обновляет статус задачи."""
        db = await self._get_db()
        cursor = await db.execute(
            "UPDATE user_tasks SET status = ? WHERE id = ?",
            (status, task_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    def _row_to_task(self, row: aiosqlite.Row) -> UserTask:
        return UserTask(
            id=row["id"],
            assignee_id=row["assignee_id"],
            description=row["description"],
            deadline=datetime.fromisoformat(row["deadline"]) if row["deadline"] else None,
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else datetime.now(),
            created_by=row["created_by"],
        )

    # =========================================================================
    # Conversation Tasks (cross-session)
    # =========================================================================

    async def create_conversation_task(
        self,
        owner_id: int,
        user_id: int,
        task_type: str = "custom",
        title: str = "",
        context: dict | None = None,
    ) -> ConversationTask:
        """Создаёт задачу согласования между owner и user."""
        import json
        db = await self._get_db()
        task_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        context_json = json.dumps(context or {}, ensure_ascii=False)

        await db.execute(
            """
            INSERT INTO conversation_tasks (id, owner_id, user_id, task_type, title, context, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, owner_id, user_id, task_type, title, context_json, now, now),
        )
        await db.commit()
        logger.info(f"ConversationTask created: [{task_id}] owner={owner_id} user={user_id} type={task_type}")

        return await self.get_conversation_task(task_id)

    async def get_conversation_task(self, task_id: str) -> ConversationTask | None:
        """Получает задачу согласования по ID."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM conversation_tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        if row:
            return ConversationTask.from_row(dict(row))
        return None

    async def get_active_conversation_tasks(self, user_id: int) -> list[ConversationTask]:
        """Получает активные задачи согласования для user'а."""
        db = await self._get_db()
        cursor = await db.execute(
            """
            SELECT * FROM conversation_tasks
            WHERE user_id = ? AND status IN ('pending', 'in_progress')
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return [ConversationTask.from_row(dict(row)) for row in await cursor.fetchall()]

    async def update_conversation_task(
        self,
        task_id: str,
        status: str | None = None,
        result: dict | None = None,
    ) -> bool:
        """Обновляет задачу согласования."""
        import json
        db = await self._get_db()
        now = datetime.now().isoformat()

        updates = ["updated_at = ?"]
        params = [now]

        if status:
            updates.append("status = ?")
            params.append(status)

        if result is not None:
            updates.append("result = ?")
            params.append(json.dumps(result, ensure_ascii=False))

        params.append(task_id)

        cursor = await db.execute(
            f"UPDATE conversation_tasks SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        await db.commit()
        return cursor.rowcount > 0

    async def close(self) -> None:
        """Закрывает соединение."""
        if self._db:
            await self._db.close()
            self._db = None


# Singleton с защитой от race condition
_repository: UsersRepository | None = None
_repository_lock = asyncio.Lock()


def get_users_repository() -> UsersRepository:
    """
    Возвращает глобальный репозиторий.

    Note: Синхронная функция, но безопасна для async контекста,
    так как создание UsersRepository не требует await.
    """
    global _repository
    if _repository is None:
        _repository = UsersRepository(str(settings.db_path))
    return _repository
