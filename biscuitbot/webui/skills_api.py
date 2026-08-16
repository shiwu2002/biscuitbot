"""Lightweight skill summaries for the WebUI."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from biscuitbot.agent.skills import SkillsLoader


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
        "tier": _tier(metadata),
        "available": available,
        "unavailable_reason": unavailable_reason,
    }


# Recognised skill tiers. ``system`` operates on the host OS/installation,
# ``agent`` governs the agent's own state/memory/scheduling, ``user`` covers
# user-domain skills. Missing/invalid values fall back to ``user``.
_VALID_TIERS = {"system", "agent", "user"}


def _tier(metadata: dict[str, Any] | None) -> str:
    if metadata is None:
        return "user"
    value = metadata.get("tier")
    if isinstance(value, str) and value.strip().lower() in _VALID_TIERS:
        return value.strip().lower()
    return "user"


def _description(metadata: dict[str, Any] | None, fallback: str) -> str:
    if metadata is None:
        return fallback
    value = metadata.get("description")
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def delete_workspace_skill(
    workspace_path: Path,
    name: str,
    *,
    employee_store: Any | None = None,
) -> dict[str, Any]:
    """Delete a workspace skill directory.

    Only skills whose source is ``workspace`` (i.e. living under
    ``workspace/skills/<name>/``) can be deleted.  Built-in skills are
    read-only.

    A skill that is bundled with a digital employee (recorded in
    ``workspace/skill_owners.json``) cannot be deleted while that employee
    still exists; deleting the employee cascades and removes its bundled
    skills automatically.

    Args:
        employee_store: Optional ``EmployeeStore`` used to check whether a
            skill's owning employee still exists.  When omitted, an owned
            skill is treated as in-use and cannot be deleted.

    Raises:
        SkillDeletionError: On validation failure, missing skill, attempt to
            delete a built-in skill, or a skill still owned by a live employee.
    """
    if not name or "/" in name or "\\" in name or ".." in name or "\x00" in name:
        raise SkillDeletionError(400, "invalid skill name")
    if not name.replace("-", "").replace("_", "").isalnum():
        raise SkillDeletionError(400, "invalid skill name")

    # 归属校验：员工未删除时，其自带技能不可单独删除。
    from biscuitbot.agent.skill_owners import SkillOwnershipStore

    owners = SkillOwnershipStore(workspace_path)
    owner_id = owners.owner_of(name)
    if owner_id is not None:
        owner_exists = True
        if employee_store is not None:
            try:
                owner_exists = employee_store.get_employee(owner_id) is not None
            except Exception:
                owner_exists = True
        if owner_exists:
            raise SkillDeletionError(
                409, f"skill '{name}' is bundled with employee '{owner_id}'"
            )
        # 归属员工已删除：清理 stale 归属记录后继续删除
        owners.remove_owner(name)

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
