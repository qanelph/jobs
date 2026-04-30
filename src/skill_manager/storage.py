"""Файловые операции над skills для HTTP API и tools."""

from __future__ import annotations

import io
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import TypedDict

from src.config import settings

MAX_SKILL_SIZE = 256 * 1024  # 256 KB на один SKILL.md
MAX_ARCHIVE_SIZE = 5 * 1024 * 1024  # 5 MB на ZIP-импорт (распакованный размер)
SKILL_FILENAME = "SKILL.md"

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SkillSummary(TypedDict):
    name: str
    description: str
    size: int


class ImportResult(TypedDict):
    name: str
    status: str  # created | replaced | skipped | error
    error: str | None


def get_skills_dir() -> Path:
    return settings.skills_dir


def is_valid_name(name: str) -> bool:
    return bool(name) and bool(_NAME_RE.fullmatch(name))


def parse_description(content: str) -> str:
    """Достаёт description из YAML frontmatter SKILL.md. Пусто если нет."""
    if not content.startswith("---"):
        return ""
    parts = content.split("---", 2)
    if len(parts) < 3:
        return ""
    for line in parts[1].strip().splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def list_skills() -> list[SkillSummary]:
    skills_dir = get_skills_dir()
    if not skills_dir.exists():
        return []

    items: list[SkillSummary] = []
    for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        skill_file = entry / SKILL_FILENAME
        if not skill_file.is_file():
            continue
        content = skill_file.read_text(encoding="utf-8", errors="replace")
        items.append(
            SkillSummary(
                name=entry.name,
                description=parse_description(content),
                size=skill_file.stat().st_size,
            )
        )
    return items


def read_skill(name: str) -> str | None:
    if not is_valid_name(name):
        return None
    skill_file = get_skills_dir() / name / SKILL_FILENAME
    if not skill_file.is_file():
        return None
    return skill_file.read_text(encoding="utf-8", errors="replace")


def write_skill(name: str, content: str, *, overwrite: bool) -> str:
    """Создать/заменить SKILL.md.

    Возвращает "created" | "replaced" | "skipped".
    Бросает ValueError при невалидном имени или превышении лимита.

    Атомарность: при overwrite=False создание идёт через O_CREAT|O_EXCL —
    параллельные PUT без overwrite не могут оба «создать» один и тот же
    SKILL.md (TOCTOU защита). При overwrite=True гонка возможна, но
    семантически дозволена (последний выигрывает).
    """
    if not is_valid_name(name):
        raise ValueError("invalid skill name")
    if len(content.encode("utf-8")) > MAX_SKILL_SIZE:
        raise ValueError("skill too large")

    skill_dir = get_skills_dir() / name
    skill_file = skill_dir / SKILL_FILENAME
    skill_dir.mkdir(parents=True, exist_ok=True)

    if not overwrite:
        try:
            fd = os.open(skill_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return "skipped"
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return "created"

    existed = skill_file.is_file()
    skill_file.write_text(content, encoding="utf-8")
    return "replaced" if existed else "created"


def delete_skill(name: str) -> bool:
    if not is_valid_name(name):
        return False
    skill_dir = get_skills_dir() / name
    if not skill_dir.is_dir():
        return False
    shutil.rmtree(skill_dir)
    return True


def export_zip(names: list[str] | None) -> bytes:
    """Запаковать выбранные скиллы в ZIP. Если names пуст/None — все."""
    available = {s["name"] for s in list_skills()}
    selected = [n for n in (names or sorted(available)) if n in available]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in selected:
            content = read_skill(name)
            if content is None:
                continue
            zf.writestr(f"{name}/{SKILL_FILENAME}", content)
    return buf.getvalue()


def import_zip(
    data: bytes,
    *,
    overwrite: bool,
    only_names: set[str] | None = None,
) -> list[ImportResult]:
    """Распаковать ZIP и записать каждый SKILL.md.

    only_names — если задано, обрабатываются только эти имена; остальные
    в архиве пропускаются полностью (даже не возвращаются в results).
    Используется для retry'я по «пропущенным» — чтобы не перетереть скиллы,
    которые при первом проходе создались успешно.

    Невалидные пути / лимиты → status=error в результате (не валит весь импорт).
    """
    results: list[ImportResult] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("not a zip archive")

    with archive:
        total = sum(zi.file_size for zi in archive.infolist())
        if total > MAX_ARCHIVE_SIZE:
            raise ValueError("archive too large")

        seen: set[str] = set()
        for info in archive.infolist():
            if info.is_dir():
                continue
            path = info.filename
            # macOS Finder при упаковке кладёт мусорные resource-форки в __MACOSX/
            # и отдельные .DS_Store. Тихо пропускаем — это не ошибка пользователя.
            if path.startswith("__MACOSX/") or path.endswith("/.DS_Store") or path == ".DS_Store":
                continue
            # Снимаем верхний префикс "skills/", если он есть — частый случай,
            # когда юзер заархивировал собственную папку skills/ через Finder.
            if path.startswith("skills/"):
                path = path[len("skills/") :]
            # Ожидаем строго "{name}/SKILL.md", без подпапок и без traversal.
            parts = path.split("/")
            if len(parts) != 2 or parts[1] != SKILL_FILENAME:
                results.append(ImportResult(name=path, status="error", error="invalid path"))
                continue
            name = parts[0]
            if name in seen:
                continue
            seen.add(name)
            if only_names is not None and name not in only_names:
                continue
            if not is_valid_name(name):
                results.append(ImportResult(name=name, status="error", error="invalid name"))
                continue
            if info.file_size > MAX_SKILL_SIZE:
                results.append(ImportResult(name=name, status="error", error="file too large"))
                continue

            content = archive.read(info).decode("utf-8", errors="replace")
            try:
                status = write_skill(name, content, overwrite=overwrite)
            except ValueError as exc:
                results.append(ImportResult(name=name, status="error", error=str(exc)))
                continue
            results.append(ImportResult(name=name, status=status, error=None))

    return results
