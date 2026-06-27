"""Lightweight skill summaries for the WebUI."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from hczkbot.agent.skills import SkillsLoader


class SkillDeletionError(Exception):
    """Raised when a skill cannot be deleted."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


def webui_skills_payload(
    workspace_path: Path,
    *,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any]:
    """Return agent skills without leaking local filesystem paths."""
    loader = SkillsLoader(workspace_path, disabled_skills=disabled_skills)
    entries = sorted(
        loader.list_skills(filter_unavailable=False),
        key=lambda entry: (entry.get("source") != "workspace", entry["name"]),
    )
    return {"skills": [_skill_payload(loader, entry) for entry in entries]}


def webui_skill_detail_payload(
    workspace_path: Path,
    name: str,
    *,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any] | None:
    """Return a single skill's safe detail payload."""
    loader = SkillsLoader(workspace_path, disabled_skills=disabled_skills)
    entries = loader.list_skills(filter_unavailable=False)
    entry = next((item for item in entries if item["name"] == name), None)
    if entry is None:
        return None
    return {
        **_skill_payload(loader, entry),
        "requirements": loader.get_skill_requirements(name),
        "raw_markdown": loader.load_skill(name) or "",
    }


def _skill_payload(loader: SkillsLoader, entry: dict[str, str]) -> dict[str, Any]:
    name = entry["name"]
    metadata = loader.get_skill_metadata(name)
    available, unavailable_reason = loader.get_skill_availability(name)
    return {
        "name": name,
        "description": _description(metadata, name),
        "source": entry.get("source", "unknown"),
        "available": available,
        "unavailable_reason": unavailable_reason,
    }


def _description(metadata: dict[str, Any] | None, fallback: str) -> str:
    if metadata is None:
        return fallback
    value = metadata.get("description")
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def delete_workspace_skill(workspace_path: Path, name: str) -> dict[str, Any]:
    """Delete a workspace skill directory.

    Only skills whose source is ``workspace`` (i.e. living under
    ``workspace/skills/<name>/``) can be deleted.  Built-in skills are
    read-only.

    Raises:
        SkillDeletionError: On validation failure, missing skill, or
            attempts to delete a built-in skill.
    """
    if not name or "/" in name or "\\" in name or ".." in name or "\x00" in name:
        raise SkillDeletionError(400, "invalid skill name")
    if not name.replace("-", "").replace("_", "").isalnum():
        raise SkillDeletionError(400, "invalid skill name")

    loader = SkillsLoader(workspace_path)
    entries = loader.list_skills(filter_unavailable=False)
    entry = next((item for item in entries if item["name"] == name), None)
    if entry is None:
        raise SkillDeletionError(404, "skill not found")
    if entry.get("source") != "workspace":
        raise SkillDeletionError(403, "built-in skills cannot be deleted")

    skill_dir = workspace_path / "skills" / name
    # Resolve to avoid symlink/traversal tricks.
    try:
        resolved = skill_dir.resolve(strict=True)
        workspace_resolved = workspace_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SkillDeletionError(404, "skill not found")
    if not str(resolved).startswith(str(workspace_resolved)):
        raise SkillDeletionError(403, "skill directory is outside workspace")

    shutil.rmtree(resolved)
    return {"deleted": True, "name": name}
