"""Файловые операции над skills для HTTP API и tools."""

from __future__ import annotations

import io
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
    return Path(settings.workspace_dir) / ".claude" / "skills"


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
        content = skill_file.read_text(errors="replace")
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
    return skill_file.read_text(errors="replace")


def write_skill(name: str, content: str, *, overwrite: bool) -> str:
    """Создать/заменить SKILL.md.

    Возвращает "created" | "replaced" | "skipped".
    Бросает ValueError при невалидном имени или превышении лимита.
    """
    if not is_valid_name(name):
        raise ValueError("invalid skill name")
    if len(content.encode("utf-8")) > MAX_SKILL_SIZE:
        raise ValueError("skill too large")

    skill_dir = get_skills_dir() / name
    skill_file = skill_dir / SKILL_FILENAME
    existed = skill_file.is_file()

    if existed and not overwrite:
        return "skipped"

    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file.write_text(content)
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


def import_zip(data: bytes, *, overwrite: bool) -> list[ImportResult]:
    """Распаковать ZIP и записать каждый SKILL.md.

    Возвращает per-файл результат. Невалидные пути / лимиты → status=error.
    """
    results: list[ImportResult] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("not a zip archive")

    total = sum(zi.file_size for zi in archive.infolist())
    if total > MAX_ARCHIVE_SIZE:
        raise ValueError("archive too large")

    seen: set[str] = set()
    for info in archive.infolist():
        if info.is_dir():
            continue
        path = info.filename
        # Ожидаем строго "{name}/SKILL.md", без подпапок и без traversal.
        parts = path.split("/")
        if len(parts) != 2 or parts[1] != SKILL_FILENAME:
            results.append(ImportResult(name=path, status="error", error="invalid path"))
            continue
        name = parts[0]
        if name in seen:
            continue
        seen.add(name)
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
