"""
Memory Index — векторный + полнотекстовый поиск.

Использует:
- sqlite-vec для векторного поиска
- FTS5 для BM25 (полнотекстовый)
- Гибридный поиск (70% vector + 30% BM25)
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import openai
import httpx
from loguru import logger

from src.config import settings


# Размер чанка и overlap (как в OpenClaw)
CHUNK_SIZE = 400  # токенов (~1600 символов)
CHUNK_OVERLAP = 80  # токенов
CHARS_PER_TOKEN = 4  # приблизительно

CHUNK_CHARS = CHUNK_SIZE * CHARS_PER_TOKEN
OVERLAP_CHARS = CHUNK_OVERLAP * CHARS_PER_TOKEN

# Веса для гибридного поиска
VECTOR_WEIGHT = 0.7
BM25_WEIGHT = 0.3


@dataclass
class SearchResult:
    """Результат поиска."""
    content: str
    file_path: str
    line_start: int
    line_end: int
    score: float


class MemoryIndex:
    """
    Индекс памяти с гибридным поиском.

    Структура БД:
    - chunks: текстовые чанки с метаданными
    - chunks_fts: FTS5 таблица для BM25
    - chunks_vec: sqlite-vec таблица для векторов
    - embeddings_cache: кеш embeddings по hash
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._vec_available = False
        self._embedding_dim = 1536  # text-embedding-3-small

    def _get_conn(self) -> sqlite3.Connection:
        """Получает соединение с БД."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
            self._try_load_vec()
        return self._conn

    def _try_load_vec(self) -> None:
        """Пытается загрузить sqlite-vec расширение."""
        try:
            import sqlite_vec
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._vec_available = True
            logger.info("sqlite-vec loaded successfully")
        except Exception as e:
            logger.warning(f"sqlite-vec not available, using fallback: {e}")
            self._vec_available = False

    def _init_schema(self) -> None:
        """Инициализирует схему БД."""
        conn = self._conn

        # Основная таблица чанков
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                file_path TEXT NOT NULL,
                line_start INTEGER NOT NULL,
                line_end INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                embedding BLOB,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # FTS5 для полнотекстового поиска
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                content,
                content='chunks',
                content_rowid='id'
            )
        """)

        # Триггеры для синхронизации FTS
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content)
                VALUES('delete', old.id, old.content);
            END
        """)
        conn.execute("""
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content)
                VALUES('delete', old.id, old.content);
                INSERT INTO chunks_fts(rowid, content) VALUES (new.id, new.content);
            END
        """)

        # Кеш embeddings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embeddings_cache (
                content_hash TEXT PRIMARY KEY,
                embedding BLOB NOT NULL
            )
        """)

        # Индексы
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(content_hash)")

        conn.commit()

    def _init_vec_table(self) -> None:
        """Инициализирует sqlite-vec таблицу."""
        if not self._vec_available:
            return

        conn = self._get_conn()
        try:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
                    chunk_id INTEGER PRIMARY KEY,
                    embedding FLOAT[{self._embedding_dim}]
                )
            """)
            conn.commit()
        except Exception as e:
            logger.error(f"Failed to create vec table: {e}")

    # =========================================================================
    # Chunking
    # =========================================================================

    def _chunk_text(self, text: str, file_path: str) -> list[dict]:
        """
        Разбивает текст на чанки с overlap.

        Returns:
            Список словарей с content, line_start, line_end
        """
        lines = text.split('\n')
        chunks = []

        current_chunk = []
        current_chars = 0
        chunk_start_line = 1

        for i, line in enumerate(lines, 1):
            line_chars = len(line) + 1  # +1 для \n

            if current_chars + line_chars > CHUNK_CHARS and current_chunk:
                # Сохраняем чанк
                content = '\n'.join(current_chunk)
                chunks.append({
                    'content': content,
                    'line_start': chunk_start_line,
                    'line_end': i - 1,
                    'file_path': file_path,
                })

                # Overlap: берём последние N символов
                overlap_lines = []
                overlap_chars = 0
                for line in reversed(current_chunk):
                    if overlap_chars + len(line) > OVERLAP_CHARS:
                        break
                    overlap_lines.insert(0, line)
                    overlap_chars += len(line) + 1

                current_chunk = overlap_lines
                current_chars = overlap_chars
                chunk_start_line = i - len(overlap_lines)

            current_chunk.append(line)
            current_chars += line_chars

        # Последний чанк
        if current_chunk:
            content = '\n'.join(current_chunk)
            chunks.append({
                'content': content,
                'line_start': chunk_start_line,
                'line_end': len(lines),
                'file_path': file_path,
            })

        return chunks

    # =========================================================================
    # Embeddings
    # =========================================================================

    async def _get_embedding(self, text: str) -> list[float] | None:
        """Получает embedding для текста (с кешированием)."""
        if not settings.openai_api_key:
            return None

        # Проверяем кеш
        content_hash = hashlib.sha256(text.encode()).hexdigest()
        conn = self._get_conn()

        cached = conn.execute(
            "SELECT embedding FROM embeddings_cache WHERE content_hash = ?",
            (content_hash,)
        ).fetchone()

        if cached:
            import struct
            embedding = list(struct.unpack(f'{self._embedding_dim}f', cached['embedding']))
            return embedding

        # Запрашиваем у OpenAI
        try:
            http_client = None
            if settings.http_proxy:
                http_client = httpx.AsyncClient(proxy=settings.http_proxy)

            client = openai.AsyncOpenAI(
                api_key=settings.openai_api_key,
                http_client=http_client,
            )

            response = await client.embeddings.create(
                model="text-embedding-3-small",
                input=text,
            )

            embedding = response.data[0].embedding

            # Сохраняем в кеш
            import struct
            embedding_bytes = struct.pack(f'{len(embedding)}f', *embedding)
            conn.execute(
                "INSERT OR REPLACE INTO embeddings_cache (content_hash, embedding) VALUES (?, ?)",
                (content_hash, embedding_bytes)
            )
            conn.commit()

            return embedding

        except Exception as e:
            logger.error(f"Embedding error: {e}")
            return None

    # =========================================================================
    # Indexing
    # =========================================================================

    async def index_file(self, file_path: Path) -> int:
        """
        Индексирует файл.

        Returns:
            Количество проиндексированных чанков.
        """
        if not file_path.exists():
            return 0

        content = file_path.read_text()
        relative_path = str(file_path.relative_to(settings.workspace_dir))

        # Удаляем старые чанки этого файла
        conn = self._get_conn()
        conn.execute("DELETE FROM chunks WHERE file_path = ?", (relative_path,))

        # Разбиваем на чанки
        chunks = self._chunk_text(content, relative_path)

        for chunk in chunks:
            content_hash = hashlib.sha256(chunk['content'].encode()).hexdigest()

            # Получаем embedding
            embedding = await self._get_embedding(chunk['content'])
            embedding_bytes = None
            if embedding:
                import struct
                embedding_bytes = struct.pack(f'{len(embedding)}f', *embedding)

            # Сохраняем чанк
            conn.execute("""
                INSERT INTO chunks (file_path, line_start, line_end, content, content_hash, embedding)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                chunk['file_path'],
                chunk['line_start'],
                chunk['line_end'],
                chunk['content'],
                content_hash,
                embedding_bytes,
            ))

        conn.commit()
        logger.info(f"Indexed {len(chunks)} chunks from {relative_path}")
        return len(chunks)

    async def index_all(self, files: list[Path]) -> int:
        """Индексирует все файлы."""
        total = 0
        for file_path in files:
            total += await self.index_file(file_path)
        return total

    # =========================================================================
    # Search
    # =========================================================================

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """
        Гибридный поиск (vector + BM25).

        Args:
            query: Поисковый запрос
            limit: Максимум результатов

        Returns:
            Список SearchResult отсортированный по релевантности
        """
        conn = self._get_conn()
        results = {}

        # BM25 поиск
        bm25_results = conn.execute("""
            SELECT c.id, c.file_path, c.line_start, c.line_end, c.content,
                   bm25(chunks_fts) as score
            FROM chunks_fts f
            JOIN chunks c ON c.id = f.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
        """, (query, limit * 2)).fetchall()

        for row in bm25_results:
            results[row['id']] = {
                'file_path': row['file_path'],
                'line_start': row['line_start'],
                'line_end': row['line_end'],
                'content': row['content'],
                'bm25_score': -row['score'],  # BM25 возвращает отрицательные значения
                'vec_score': 0,
            }

        # Vector поиск
        query_embedding = await self._get_embedding(query)
        if query_embedding:
            # Fallback: косинусное сходство в Python
            all_chunks = conn.execute("""
                SELECT id, file_path, line_start, line_end, content, embedding
                FROM chunks WHERE embedding IS NOT NULL
            """).fetchall()

            import struct
            for row in all_chunks:
                if row['embedding']:
                    chunk_embedding = list(struct.unpack(
                        f'{self._embedding_dim}f', row['embedding']
                    ))
                    # Косинусное сходство
                    dot = sum(a * b for a, b in zip(query_embedding, chunk_embedding))
                    norm_q = sum(a * a for a in query_embedding) ** 0.5
                    norm_c = sum(a * a for a in chunk_embedding) ** 0.5
                    similarity = dot / (norm_q * norm_c) if norm_q and norm_c else 0

                    if row['id'] in results:
                        results[row['id']]['vec_score'] = similarity
                    else:
                        results[row['id']] = {
                            'file_path': row['file_path'],
                            'line_start': row['line_start'],
                            'line_end': row['line_end'],
                            'content': row['content'],
                            'bm25_score': 0,
                            'vec_score': similarity,
                        }

        # Нормализация и комбинирование
        if results:
            max_bm25 = max(r['bm25_score'] for r in results.values()) or 1
            max_vec = max(r['vec_score'] for r in results.values()) or 1

            for r in results.values():
                r['bm25_norm'] = r['bm25_score'] / max_bm25
                r['vec_norm'] = r['vec_score'] / max_vec
                r['final_score'] = (
                    VECTOR_WEIGHT * r['vec_norm'] +
                    BM25_WEIGHT * r['bm25_norm']
                )

        # Сортируем и возвращаем
        sorted_results = sorted(
            results.values(),
            key=lambda x: x['final_score'],
            reverse=True
        )[:limit]

        return [
            SearchResult(
                content=r['content'],
                file_path=r['file_path'],
                line_start=r['line_start'],
                line_end=r['line_end'],
                score=r['final_score'],
            )
            for r in sorted_results
        ]

    def close(self) -> None:
        """Закрывает соединение."""
        if self._conn:
            self._conn.close()
            self._conn = None


# Singleton
_index: MemoryIndex | None = None


def get_index() -> MemoryIndex:
    """Возвращает глобальный индекс."""
    global _index
    if _index is None:
        _index = MemoryIndex(settings.data_dir / "memory_index.sqlite")
    return _index
