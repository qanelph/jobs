"""
Skill Manager Tools — создание, просмотр и удаление локальных skills.
"""

import shutil
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool
from loguru import logger

from src.config import settings


def _get_skills_dir() -> Path:
    """Возвращает директорию skills."""
    return Path(settings.workspace_dir) / ".claude" / "skills"


@tool(
    "skill_create",
    "Create a new skill. Skills are SKILL.md files with YAML frontmatter that define custom behaviors.",
    {
        "name": str,
        "description": str,
        "algorithm": str,
        "tools": str,
    },
)
async def skill_create(args: dict[str, Any]) -> dict[str, Any]:
    """Создаёт новый skill."""
    name = args.get("name")
    description = args.get("description")
    algorithm = args.get("algorithm")
    tools_str = args.get("tools", "Read, Bash")

    if not name or not description or not algorithm:
        return _error("name, description и algorithm обязательны")

    # Валидация имени
    if not name.replace("-", "").replace("_", "").isalnum():
        return _error("name должен содержать только буквы, цифры, - и _")

    skills_dir = _get_skills_dir()
    skill_dir = skills_dir / name

    if skill_dir.exists():
        return _error(f"Skill '{name}' уже существует. Используй skill_delete для удаления.")

    # Создаём директорию и SKILL.md
    skill_dir.mkdir(parents=True, exist_ok=True)

    skill_content = f"""---
name: {name}
description: {description}
tools: {tools_str}
---

# {name.replace("-", " ").title()}

## Algorithm

{algorithm}
"""

    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(skill_content)

    logger.info(f"Created skill: {name}")

    return _text(
        f"Skill **{name}** создан.\n\n"
        f"Путь: `{skill_file}`\n\n"
        f"Triggers: {description[:100]}...\n\n"
        "SDK подхватит при следующем сообщении."
    )


@tool(
    "skill_list",
    "List all local skills with their descriptions.",
    {},
)
async def skill_list(args: dict[str, Any]) -> dict[str, Any]:
    """Список локальных skills."""
    skills_dir = _get_skills_dir()

    if not skills_dir.exists():
        return _text("Нет локальных skills.\n\nИспользуй `skill_create` для создания.")

    skills: list[dict[str, str]] = []

    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue

        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        # Парсим frontmatter
        content = skill_file.read_text()
        name = skill_dir.name
        description = ""

        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1]
                for line in frontmatter.strip().split("\n"):
                    if line.startswith("description:"):
                        description = line.replace("description:", "").strip()
                        break

        skills.append({
            "name": name,
            "description": description[:100] if description else "(нет описания)",
        })

    if not skills:
        return _text("Нет локальных skills.\n\nИспользуй `skill_create` для создания.")

    lines = [f"Локальные skills ({len(skills)}):\n"]

    for s in skills:
        lines.append(f"• **{s['name']}**")
        lines.append(f"  {s['description']}")
        lines.append("")

    return _text("\n".join(lines))


@tool(
    "skill_delete",
    "Delete a local skill by name.",
    {
        "name": str,
    },
)
async def skill_delete(args: dict[str, Any]) -> dict[str, Any]:
    """Удаляет skill."""
    name = args.get("name")

    if not name:
        return _error("name обязателен")

    skills_dir = _get_skills_dir()
    skill_dir = skills_dir / name

    if not skill_dir.exists():
        return _error(f"Skill '{name}' не найден")

    # Удаляем директорию
    shutil.rmtree(skill_dir)
    logger.info(f"Deleted skill: {name}")

    return _text(f"Skill **{name}** удалён. SDK подхватит изменения при следующем сообщении.")


@tool(
    "skill_show",
    "Show the content of a skill's SKILL.md file.",
    {
        "name": str,
    },
)
async def skill_show(args: dict[str, Any]) -> dict[str, Any]:
    """Показывает содержимое skill."""
    name = args.get("name")

    if not name:
        return _error("name обязателен")

    skills_dir = _get_skills_dir()
    skill_file = skills_dir / name / "SKILL.md"

    if not skill_file.exists():
        return _error(f"Skill '{name}' не найден")

    content = skill_file.read_text()

    return _text(f"**{name}/SKILL.md:**\n\n```markdown\n{content}\n```")


@tool(
    "skill_edit",
    "Edit an existing skill's algorithm or description.",
    {
        "name": str,
        "description": str,
        "algorithm": str,
        "tools": str,
    },
)
async def skill_edit(args: dict[str, Any]) -> dict[str, Any]:
    """Редактирует skill."""
    name = args.get("name")
    description = args.get("description")
    algorithm = args.get("algorithm")
    tools_str = args.get("tools")

    if not name:
        return _error("name обязателен")

    skills_dir = _get_skills_dir()
    skill_file = skills_dir / name / "SKILL.md"

    if not skill_file.exists():
        return _error(f"Skill '{name}' не найден")

    # Читаем текущий контент для defaults
    current_content = skill_file.read_text()
    current_description = ""
    current_tools = "Read, Bash"
    current_algorithm = ""

    if current_content.startswith("---"):
        parts = current_content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            for line in frontmatter.strip().split("\n"):
                if line.startswith("description:"):
                    current_description = line.replace("description:", "").strip()
                elif line.startswith("tools:"):
                    current_tools = line.replace("tools:", "").strip()

            # Algorithm — всё после frontmatter, ищем ## Algorithm
            body = parts[2]
            if "## Algorithm" in body:
                current_algorithm = body.split("## Algorithm", 1)[1].strip()

    # Используем новые значения или текущие
    final_description = description if description else current_description
    final_tools = tools_str if tools_str else current_tools
    final_algorithm = algorithm if algorithm else current_algorithm

    skill_content = f"""---
name: {name}
description: {final_description}
tools: {final_tools}
---

# {name.replace("-", " ").title()}

## Algorithm

{final_algorithm}
"""

    skill_file.write_text(skill_content)
    logger.info(f"Updated skill: {name}")

    return _text(f"Skill **{name}** обновлён. SDK подхватит изменения при следующем сообщении.")


# Экспорт
SKILL_MANAGER_TOOLS = [
    skill_create,
    skill_list,
    skill_delete,
    skill_show,
    skill_edit,
]

SKILL_MANAGER_TOOL_NAMES = [
    "mcp__jobs__skill_create",
    "mcp__jobs__skill_list",
    "mcp__jobs__skill_delete",
    "mcp__jobs__skill_show",
    "mcp__jobs__skill_edit",
]


# Helpers
def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}
