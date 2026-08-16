"""CapabilityRegistry 聚合层测试。

覆盖：kind 归一化、技能（prompt/process）映射、内置应用型技能归 process、
CLI 应用与 MCP 预设的运行时分类，以及读时聚合不复制状态的行为。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from biscuitbot.capabilities.registry import CapabilityRegistry, normalize_kind


def _write_skill(workspace: Path, name: str, frontmatter: str, body: str = "") -> None:
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}\n---\n{body}\n", encoding="utf-8"
    )


class _FakeCliManager:
    """避免测试触发真实 CliAppManager 的目录/网络拉取与配置读取。"""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def payload(self, *, force_refresh: bool = False) -> dict[str, Any]:
        return {"apps": self._rows}


@pytest.fixture
def empty_mcp(monkeypatch: pytest.MonkeyPatch):
    """让 MCP 来源返回空预设，隔离 load_config 副作用。"""
    monkeypatch.setattr(
        "biscuitbot.webui.mcp_presets_api.mcp_presets_payload",
        lambda **_: {"presets": []},
    )


def test_normalize_kind_maps_aliases() -> None:
    assert normalize_kind(None) is None
    assert normalize_kind("") is None
    assert normalize_kind("skill") == "prompt"
    assert normalize_kind("skills") == "prompt"
    assert normalize_kind("prompt") == "prompt"
    assert normalize_kind("app") == "process"
    assert normalize_kind("apps") == "process"
    assert normalize_kind("cli") == "process"
    assert normalize_kind("process") == "process"
    assert normalize_kind("mcp") == "mcp"
    assert normalize_kind("unknown") is None


def test_registry_lists_prompt_skill(tmp_path: Path, empty_mcp) -> None:
    _write_skill(
        tmp_path,
        "my-prompt",
        "name: Prompt Skill\ndescription: Just instructions\n",
    )

    registry = CapabilityRegistry(tmp_path, cli_manager=_FakeCliManager([]))
    cap = registry.get("my-prompt")

    assert cap is not None
    assert cap["runtime"] == "prompt"
    assert cap["source"] == "workspace"
    assert cap["category"] == "skill"
    assert cap["installed"] is True
    assert cap["skill_installed"] is True
    assert cap["instructions"]["source"] == "skill_md"
    assert cap["manifest"]["schema"] == "capability.v1"


def test_registry_maps_process_skill_frontmatter(tmp_path: Path, empty_mcp) -> None:
    _write_skill(
        tmp_path,
        "my-tool",
        "\n".join(
            [
                "name: My Tool",
                "description: Does things",
                "runtime: process",
                "execution:",
                "  entry_point: my-tool",
                "provisioning:",
                "  strategy: pip",
                "  installers:",
                "    - manager: pip",
                "      command: my-tool",
                "requirements:",
                "  bins:",
                "    - python3",
                "  env:",
                "    - MY_KEY",
            ]
        ),
    )

    registry = CapabilityRegistry(tmp_path, cli_manager=_FakeCliManager([]))
    cap = registry.get("my-tool")

    assert cap is not None
    assert cap["runtime"] == "process"
    assert cap["execution"]["entry_point"] == "my-tool"
    assert cap["provisioning"]["strategy"] == "pip"
    assert cap["requirements"]["bins"] == ["python3"]
    assert cap["requirements"]["env"] == ["MY_KEY"]


def test_registry_maps_builtin_process_skills(tmp_path: Path, empty_mcp) -> None:
    # jianying-editor 是内置技能里的「应用型」条目，即使 frontmatter 未声明
    # runtime，也应被迁移映射归为 process（bundled）。
    registry = CapabilityRegistry(tmp_path, cli_manager=_FakeCliManager([]))
    cap = registry.get("jianying-editor")

    assert cap is not None
    assert cap["runtime"] == "process"
    assert cap["source"] == "builtin"


def test_registry_kind_filter_isolates_runtime(tmp_path: Path, empty_mcp) -> None:
    _write_skill(tmp_path, "p-skill", "name: P\ndescription: prompt\n")
    _write_skill(
        tmp_path,
        "c-skill",
        "name: C\ndescription: process\nruntime: process\n",
    )

    registry = CapabilityRegistry(
        tmp_path,
        cli_manager=_FakeCliManager(
            [
                {
                    "name": "ffmpeg",
                    "display_name": "FFmpeg",
                    "description": "video",
                    "category": "media",
                    "skill_installed": True,
                    "installed": True,
                    "available": True,
                    "install_supported": True,
                    "requires": "",
                    "logo_url": None,
                    "brand_color": None,
                    "status": "installed",
                    "entry_point": "ffmpeg",
                    "manifest": {
                        "execution": {"entry_point": "ffmpeg"},
                        "requirements": {},
                        "provisioning": {"strategy": "brew"},
                    },
                }
            ]
        ),
    )

    prompt_ids = {cap["id"] for cap in registry.list(kind="prompt")}
    process_ids = {cap["id"] for cap in registry.list(kind="process")}

    assert "p-skill" in prompt_ids
    assert "p-skill" not in process_ids
    assert "c-skill" in process_ids
    assert "ffmpeg" in process_ids
    # prompt 过滤不触发 CLI / MCP 来源。
    assert all(cap["runtime"] == "prompt" for cap in registry.list(kind="prompt"))


def test_registry_cli_apps_are_process(tmp_path: Path, empty_mcp) -> None:
    registry = CapabilityRegistry(
        tmp_path,
        cli_manager=_FakeCliManager(
            [
                {
                    "name": "ffmpeg",
                    "display_name": "FFmpeg",
                    "description": "video",
                    "category": "media",
                    "skill_installed": False,
                    "installed": False,
                    "available": True,
                    "install_supported": True,
                    "requires": "",
                    "logo_url": None,
                    "brand_color": None,
                    "status": "not_installed",
                    "entry_point": "ffmpeg",
                    "manifest": {
                        "execution": {"entry_point": "ffmpeg"},
                        "requirements": {},
                        "provisioning": {"strategy": "brew"},
                    },
                }
            ]
        ),
    )

    cap = registry.get("ffmpeg")
    assert cap is not None
    assert cap["runtime"] == "process"
    assert cap["source"] == "cli-anything"
    assert cap["installed"] is False
    assert cap["install_supported"] is True
    assert cap["instructions"]["available"] is False


def test_registry_mcp_presets_are_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "biscuitbot.webui.mcp_presets_api.mcp_presets_payload",
        lambda **_: {
            "presets": [
                {
                    "name": "github",
                    "display_name": "GitHub",
                    "description": "gh",
                    "category": "mcp",
                    "source": "preset",
                    "transport": "streamableHttp",
                    "installed": False,
                    "configured": False,
                    "available": False,
                    "install_supported": True,
                    "requires": "",
                    "status": "not_installed",
                    "logo_url": None,
                    "brand_color": None,
                    "docs_url": None,
                    "manifest": {
                        "execution": {"transport": "streamableHttp"},
                        "requirements": {},
                        "provisioning": {"strategy": "config"},
                    },
                }
            ]
        },
    )

    registry = CapabilityRegistry(tmp_path, cli_manager=_FakeCliManager([]))
    cap = registry.get("github")

    assert cap is not None
    assert cap["runtime"] == "mcp"
    assert cap["source"] == "preset"
    assert cap["execution"]["transport"] == "streamableHttp"
    assert cap["installed"] is False
