"""
Repository — хранение данных о пользователях и задачах в SQLite.
"""

import asyncio
import sqlite3
import uuid
from datetime import datetime

import aiosqlite
from loguru import logger

from src.config import settings
from .models import ExternalUser, Task


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
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            await self._db.execute("ALTER TABLE external_users ADD COLUMN is_banned INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists

        # Unified tasks table
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_by INTEGER,
                assignee_id INTEGER,
                deadline TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                kind TEXT DEFAULT 'task',
                context TEXT DEFAULT '{}',
                result TEXT,
                schedule_at TEXT,
                schedule_repeat INTEGER
            )
        """)

        # Миграция: schedule поля для существующих таблиц
        try:
            await self._db.execute("ALTER TABLE tasks ADD COLUMN schedule_at TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            await self._db.execute("ALTER TABLE tasks ADD COLUMN schedule_repeat INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists

        # Миграция из старых таблиц (user_tasks → tasks)
        await self._migrate_old_tables()

        # Индексы
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(kind)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_schedule ON tasks(schedule_at)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_username ON external_users(username)"
        )

        await self._db.commit()
        logger.debug("Users DB schema initialized")

    async def _migrate_old_tables(self) -> None:
        """Мигрирует данные из user_tasks и conversation_tasks в tasks."""
        # Проверяем есть ли старая таблица user_tasks
        cursor = await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_tasks'"
        )
        if await cursor.fetchone():
            # Мигрируем user_tasks → tasks (пропускаем уже существующие id)
            await self._db.execute("""
                INSERT OR IGNORE INTO tasks (id, title, status, created_by, assignee_id, deadline, created_at, updated_at, kind, context, result)
                SELECT
                    id,
                    description,
                    CASE status
                        WHEN 'accepted' THEN 'in_progress'
                        WHEN 'completed' THEN 'done'
                        WHEN 'overdue' THEN 'pending'
                        ELSE status
                    END,
                    created_by,
                    assignee_id,
                    deadline,
                    created_at,
                    created_at,
                    'task',
                    '{}',
                    NULL
                FROM user_tasks
            """)
            logger.debug("Migrated user_tasks → tasks")

        # Проверяем есть ли старая таблица conversation_tasks
        cursor = await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_tasks'"
        )
        if await cursor.fetchone():
            # Мигрируем conversation_tasks → tasks
            await self._db.execute("""
                INSERT OR IGNORE INTO tasks (id, title, status, created_by, assignee_id, deadline, created_at, updated_at, kind, context, result)
                SELECT
                    id,
                    title,
                    status,
                    owner_id,
                    user_id,
                    NULL,
                    created_at,
                    updated_at,
                    task_type,
                    context,
                    result
                FROM conversation_tasks
            """)
            logger.debug("Migrated conversation_tasks → tasks")

        # Проверяем есть ли старая таблица scheduled_tasks
        cursor = await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'"
        )
        if await cursor.fetchone():
            import json as _json
            # Читаем все pending задачи
            cursor = await self._db.execute(
                "SELECT id, prompt, scheduled_at, repeat_seconds, status FROM scheduled_tasks WHERE status = 'pending'"
            )
            rows = await cursor.fetchall()
            now = datetime.now().isoformat()
            for row in rows:
                task_id = row["id"]
                prompt = row["prompt"]
                scheduled_at = row["scheduled_at"]
                repeat_seconds = row["repeat_seconds"]
                context_json = _json.dumps({"prompt": prompt}, ensure_ascii=False)

                await self._db.execute(
                    """
                    INSERT OR IGNORE INTO tasks
                        (id, title, kind, status, created_at, updated_at, context, schedule_at, schedule_repeat)
                    VALUES (?, ?, 'scheduled', 'pending', ?, ?, ?, ?, ?)
                    """,
                    (task_id, prompt[:80], now, now, context_json, scheduled_at, repeat_seconds),
                )
            logger.debug(f"Migrated {len(rows)} scheduled_tasks → tasks")

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
        title: str,
        kind: str = "task",
        assignee_id: int | None = None,
        created_by: int | None = None,
        deadline: datetime | None = None,
        context: dict | None = None,
        schedule_at: datetime | None = None,
        schedule_repeat: int | None = None,
    ) -> Task:
        """Создаёт задачу."""
        import json
        db = await self._get_db()
        task_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()
        context_json = json.dumps(context or {}, ensure_ascii=False)

        await db.execute(
            """
            INSERT INTO tasks (id, title, kind, assignee_id, created_by, deadline,
                               created_at, updated_at, context, schedule_at, schedule_repeat)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, title, kind, assignee_id, created_by,
             deadline.isoformat() if deadline else None, now, now, context_json,
             schedule_at.isoformat() if schedule_at else None, schedule_repeat),
        )
        await db.commit()
        logger.info(f"Task created: [{task_id}] kind={kind} assignee={assignee_id}")

        return await self.get_task(task_id)

    async def get_task(self, task_id: str) -> Task | None:
        """Получает задачу по ID."""
        db = await self._get_db()
        cursor = await db.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        if row:
            return Task.from_row(dict(row))
        return None

    async def list_tasks(
        self,
        assignee_id: int | None = None,
        status: str | None = None,
        kind: str | None = None,
        overdue_only: bool = False,
        include_done: bool = False,
    ) -> list[Task]:
        """Получает задачи с фильтрами."""
        db = await self._get_db()

        conditions: list[str] = []
        params: list = []

        if assignee_id is not None:
            conditions.append("assignee_id = ?")
            params.append(assignee_id)

        if status:
            conditions.append("status = ?")
            params.append(status)
        elif not include_done:
            conditions.append("status NOT IN ('done', 'cancelled')")

        if kind:
            conditions.append("kind = ?")
            params.append(kind)

        if overdue_only:
            now = datetime.now().isoformat()
            conditions.append("deadline IS NOT NULL AND deadline < ?")
            params.append(now)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        cursor = await db.execute(
            f"SELECT * FROM tasks {where} ORDER BY deadline ASC NULLS LAST, created_at DESC",
            params,
        )
        return [Task.from_row(dict(row)) for row in await cursor.fetchall()]

    async def get_scheduled_due(self) -> list[Task]:
        """Возвращает scheduled-задачи, у которых schedule_at <= now."""
        db = await self._get_db()
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """
            SELECT * FROM tasks
            WHERE kind = 'scheduled'
              AND schedule_at IS NOT NULL
              AND schedule_at <= ?
              AND status NOT IN ('done', 'cancelled')
            ORDER BY schedule_at ASC
            """,
            (now,),
        )
        return [Task.from_row(dict(row)) for row in await cursor.fetchall()]

    async def update_schedule(self, task_id: str, schedule_at: datetime | None) -> bool:
        """Обновляет schedule_at для задачи."""
        db = await self._get_db()
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "UPDATE tasks SET schedule_at = ?, updated_at = ? WHERE id = ?",
            (schedule_at.isoformat() if schedule_at else None, now, task_id),
        )
        await db.commit()
        return cursor.rowcount > 0

    async def update_task(
        self,
        task_id: str,
        status: str | None = None,
        result: dict | None = None,
    ) -> bool:
        """Обновляет задачу."""
        import json
        db = await self._get_db()
        now = datetime.now().isoformat()

        updates = ["updated_at = ?"]
        params: list = [now]

        if status:
            updates.append("status = ?")
            params.append(status)

        if result is not None:
            updates.append("result = ?")
            params.append(json.dumps(result, ensure_ascii=False))

        params.append(task_id)

        cursor = await db.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?",
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
