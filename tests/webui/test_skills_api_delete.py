"""Tests for ``hczkbot.webui.skills_api.delete_workspace_skill``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hczkbot.agent import skills as skills_module
from hczkbot.agent.skills import SkillsLoader
from hczkbot.webui.skills_api import (
    SkillDeletionError,
    delete_workspace_skill,
    webui_skills_payload,
)


def _write_skill(
    base: Path,
    name: str,
    *,
    metadata_json: dict | None = None,
    body: str = "# Skill\n",
) -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    lines = ["---"]
    if metadata_json is not None:
        payload = json.dumps({"hczkbot": metadata_json}, separators=(",", ":"))
        lines.append(f"metadata: {payload}")
    lines.extend(["---", "", body])
    path = skill_dir / "SKILL.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _workspace_with_skill(tmp_path: Path, skill_name: str = "alpha") -> Path:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    _write_skill(skills_root, skill_name, body=f"# {skill_name.capitalize()}")
    return workspace


@pytest.fixture(autouse=True)
def _isolated_builtin_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the module-level BUILTIN_SKILLS_DIR to an empty temp dir.

    ``delete_workspace_skill`` constructs its own ``SkillsLoader`` without a
    ``builtin_skills_dir`` argument, so we must patch the module-level default
    to avoid picking up the package's real built-in skills during tests.
    """
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    monkeypatch.setattr(skills_module, "BUILTIN_SKILLS_DIR", builtin)
    return builtin


def test_delete_workspace_skill_removes_directory(tmp_path: Path) -> None:
    workspace = _workspace_with_skill(tmp_path, "alpha")
    skill_dir = workspace / "skills" / "alpha"
    assert skill_dir.exists()

    result = delete_workspace_skill(workspace, "alpha")
    assert result == {"deleted": True, "name": "alpha"}
    assert not skill_dir.exists()

    # The skill should no longer be listed by the loader.
    loader = SkillsLoader(workspace)
    assert loader.list_skills(filter_unavailable=False) == []


def test_delete_workspace_skill_rejects_invalid_names(tmp_path: Path) -> None:
    workspace = _workspace_with_skill(tmp_path, "alpha")

    for bad in ["", "..", "a/b", "a\\b", "a\x00b", "a b"]:
        with pytest.raises(SkillDeletionError) as exc_info:
            delete_workspace_skill(workspace, bad)
        assert exc_info.value.status == 400
        assert exc_info.value.message == "invalid skill name"


def test_delete_workspace_skill_rejects_non_alnum_names(tmp_path: Path) -> None:
    workspace = _workspace_with_skill(tmp_path, "alpha")
    with pytest.raises(SkillDeletionError) as exc_info:
        delete_workspace_skill(workspace, "a.b")
    assert exc_info.value.status == 400


def test_delete_workspace_skill_returns_404_for_missing(tmp_path: Path) -> None:
    workspace = _workspace_with_skill(tmp_path, "alpha")
    with pytest.raises(SkillDeletionError) as exc_info:
        delete_workspace_skill(workspace, "ghost")
    assert exc_info.value.status == 404
    assert exc_info.value.message == "skill not found"


def test_delete_workspace_skill_refuses_builtin(
    tmp_path: Path, _isolated_builtin_skills: Path
) -> None:
    workspace = tmp_path / "ws"
    (workspace / "skills").mkdir(parents=True)
    builtin = _isolated_builtin_skills
    _write_skill(builtin, "core", body="# Core")

    loader = SkillsLoader(workspace)
    entries = loader.list_skills(filter_unavailable=False)
    assert any(e["name"] == "core" and e["source"] == "builtin" for e in entries)

    with pytest.raises(SkillDeletionError) as exc_info:
        delete_workspace_skill(workspace, "core")
    assert exc_info.value.status == 403
    assert exc_info.value.message == "built-in skills cannot be deleted"

    # The built-in skill file should still be on disk.
    assert (builtin / "core" / "SKILL.md").exists()


def test_delete_workspace_skill_keeps_other_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    skills_root = workspace / "skills"
    skills_root.mkdir(parents=True)
    _write_skill(skills_root, "alpha", body="# Alpha")
    _write_skill(skills_root, "beta", body="# Beta")

    result = delete_workspace_skill(workspace, "alpha")
    assert result["deleted"] is True

    entries = webui_skills_payload(workspace)["skills"]
    names = [entry["name"] for entry in entries]
    assert names == ["beta"]
