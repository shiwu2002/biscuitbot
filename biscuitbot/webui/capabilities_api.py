"""Unified capability payloads for the WebUI.

把技能 / CLI 应用 / MCP 预设聚合为单一「能力」目录。列表走统一的
``CapabilityRegistry``；详情与动作仍由各运行时既有的端点负责（CLI 应用的
install/uninstall/test、MCP 的 enable/remove/test、工作区技能的 delete），
本模块只提供统一列表与轻量详情，避免重复实现 MCP 密钥头等复杂边界。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from biscuitbot.agent.skills import SkillsLoader
from biscuitbot.capabilities.registry import CapabilityRegistry


def capabilities_payload(
    workspace_path: Path,
    *,
    disabled_skills: set[str] | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """返回统一能力列表（含 installed_count）。``kind`` 可选过滤 runtime。"""
    registry = CapabilityRegistry(workspace_path, disabled_skills=disabled_skills)
    capabilities = registry.list(kind=kind)
    return {
        "capabilities": capabilities,
        "installed_count": sum(1 for cap in capabilities if cap["installed"]),
    }


def capability_detail_payload(
    workspace_path: Path,
    capability_id: str,
    *,
    disabled_skills: set[str] | None = None,
) -> dict[str, Any] | None:
    """返回单个能力详情；prompt 技能额外附带 raw_markdown 与完整依赖。"""
    registry = CapabilityRegistry(workspace_path, disabled_skills=disabled_skills)
    cap = registry.get(capability_id)
    if cap is None:
        return None
    if cap["runtime"] == "prompt":
        loader = SkillsLoader(workspace_path, disabled_skills=disabled_skills)
        cap = {
            **cap,
            "requirements": loader.get_skill_requirements(capability_id),
            "raw_markdown": loader.load_skill(capability_id) or "",
        }
    return cap
