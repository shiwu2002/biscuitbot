"""Capability registry: 读时聚合技能 / CLI 应用 / MCP 预设为统一能力列表。

设计要点：
- 不新建持久化文件、不引入数据库——能力视图是对既有来源（``SkillsLoader``、
  ``CliAppManager``、``MCP_PRESETS``）的读时组合；
- 每个能力统一携带 ``runtime``（prompt / process / mcp）、``requirements``、
  ``provisioning``、``instructions``、``execution``、``status`` 等字段；
- ``kind`` 过滤把前端「技能 / 应用 / MCP」映射到 runtime。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from biscuitbot.agent.skills import SkillsLoader
from biscuitbot.apps.protocol import capability_manifest

# runtime 显示顺序（默认「全部」列表按此分组）
_RUNTIME_ORDER = {"prompt": 0, "process": 1, "mcp": 2}

# kind 别名 → runtime
_KIND_ALIASES = {
    "skill": "prompt",
    "skills": "prompt",
    "prompt": "prompt",
    "app": "process",
    "apps": "process",
    "cli": "process",
    "process": "process",
    "mcp": "mcp",
}

# 内置技能里「自带可执行代码、本质是应用」的条目 → 归为 process（bundled）。
# 其余内置技能（含 douyin-windows / seedance，它们只是带 bins/env 依赖的指令）
# 仍为 prompt。这是「内置技能 → capability runtime」迁移映射表。
_BUILTIN_PROCESS_SKILLS = {
    "jianying-editor": "bundled",
    "douyin-playwright": "bundled",
}


def normalize_kind(kind: str | None) -> str | None:
    """把前端 kind（技能/应用/MCP 及其别名）归一化为 runtime；未知返回 None。"""
    if not kind:
        return None
    return _KIND_ALIASES.get(str(kind).strip().lower())


class CapabilityRegistry:
    """聚合技能 / CLI 应用 / MCP 预设，输出统一能力列表。

    参数:
        workspace: 工作区根目录（技能、CLI 应用生成技能都落在这里）；
        disabled_skills: 被禁用的技能名集合；
        cli_manager: 可选，注入已构造的 ``CliAppManager``（避免重复读取配置）。
    """

    def __init__(
        self,
        workspace: Path,
        *,
        disabled_skills: set[str] | None = None,
        cli_manager: Any | None = None,
    ) -> None:
        self.workspace = workspace
        self._disabled = disabled_skills or set()
        self._skills = SkillsLoader(workspace, disabled_skills=self._disabled)
        self._cli = cli_manager

    # ---- 来源构造（惰性，避免 import 期开销 / 循环） ---------------------

    def _cli_manager(self) -> Any:
        if self._cli is not None:
            return self._cli
        from biscuitbot.apps.cli import CliAppManager, CliAppsRuntimeConfig
        from biscuitbot.config.loader import load_config

        config = load_config()
        cli_cfg = config.tools.cli_apps
        return CliAppManager(
            workspace=config.workspace_path,
            runtime=CliAppsRuntimeConfig(
                install_timeout=cli_cfg.install_timeout,
                run_timeout=cli_cfg.run_timeout,
                catalog_ttl_seconds=cli_cfg.catalog_ttl_seconds,
            ),
        )

    # ---- 统一列表 ---------------------------------------------------------

    def list(self, kind: str | None = None) -> list[dict[str, Any]]:
        """返回统一能力列表；``kind`` 为 None 返回全部，否则按 runtime 过滤。

        按 runtime 短路，避免在只需要某类能力时触发无关来源（如 CLI 目录的网络拉取）。
        """
        runtime = normalize_kind(kind)
        capabilities: list[dict[str, Any]] = []
        # 技能来源可能同时含 prompt 与 process（内置应用型技能），故 process 也需读技能。
        if runtime is None or runtime in ("prompt", "process"):
            capabilities.extend(self._skill_capabilities())
        if runtime is None or runtime == "process":
            capabilities.extend(self._cli_app_capabilities())
        if runtime is None or runtime == "mcp":
            capabilities.extend(self._mcp_capabilities())
        if runtime is not None:
            capabilities = [cap for cap in capabilities if cap["runtime"] == runtime]
        capabilities.sort(
            key=lambda cap: (
                _RUNTIME_ORDER.get(cap["runtime"], 9),
                str(cap["display_name"]).lower(),
            )
        )
        return capabilities

    def get(self, capability_id: str) -> dict[str, Any] | None:
        for cap in self.list():
            if cap["id"] == capability_id:
                return cap
        return None

    def installed_count(self) -> int:
        return sum(1 for cap in self.list() if cap["installed"])

    # ---- 技能来源 ---------------------------------------------------------

    def _skill_capabilities(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in self._skills.list_skills(filter_unavailable=False):
            name = entry["name"]
            cap = self._skills.get_skill_capability(name)
            runtime = cap["runtime"]
            # 内置「应用型」技能归为 process（bundled），见 _BUILTIN_PROCESS_SKILLS。
            if entry.get("source") == "builtin" and name in _BUILTIN_PROCESS_SKILLS:
                runtime = "process"
            metadata = self._skills.get_skill_metadata(name) or {}
            available, reason = self._skills.get_skill_availability(name)
            reqs = cap["requirements"]
            source = entry.get("source", "builtin")

            instructions = {
                "source": "skill_md",
                "path": f"skills/{name}/SKILL.md",
                "available": True,
            }
            provisioning = self._normalize_provisioning(cap["provisioning"], runtime)
            display_name = str(metadata.get("name") or name)

            manifest = capability_manifest(
                capability_id=name,
                display_name=display_name,
                description=self._description(metadata, name),
                category="skill",
                source=source,
                runtime=runtime,
                instructions=instructions,
                execution=cap["execution"],
                requirements=reqs,
                provisioning=provisioning,
                install={"supported": False, "strategy": "none"},
                remove={"supported": source == "workspace", "strategy": "none"},
                trust={
                    "registry": "biscuitbot-skills",
                    "level": source,
                    "review_status": "bundled" if source == "builtin" else "workspace",
                },
                icon=str(metadata.get("emoji")) if metadata.get("emoji") else None,
            )

            out.append({
                "id": name,
                "name": name,
                "display_name": display_name,
                "description": self._description(metadata, name),
                "icon": str(metadata.get("emoji")) if metadata.get("emoji") else None,
                "category": "skill",
                "tags": [],
                "runtime": runtime,
                "source": source,
                "instructions": instructions,
                "execution": cap["execution"],
                "requirements": reqs,
                "provisioning": provisioning,
                "status": "available" if available else "missing",
                "installed": True,
                "available": available,
                "unavailable_reason": reason,
                "install_supported": False,
                "skill_installed": True,
                "requires": self._requires_str(reqs),
                "tier": self._tier(metadata),
                "logo_url": None,
                "brand_color": None,
                "docs_url": None,
                "manifest": manifest,
            })
        return out

    # ---- CLI 应用来源 -----------------------------------------------------

    def _cli_app_capabilities(self) -> list[dict[str, Any]]:
        try:
            payload = self._cli_manager().payload()
        except Exception:
            # 目录拉取失败（离线等）时不影响技能 / MCP 的聚合。
            payload = {"apps": []}
        out: list[dict[str, Any]] = []
        for row in payload.get("apps", []):
            name = str(row.get("name") or "")
            manifest = row.get("manifest") or {}
            out.append({
                "id": name,
                "name": name,
                "display_name": row.get("display_name") or name,
                "description": row.get("description") or "",
                "icon": None,
                "category": row.get("category") or "uncategorized",
                "tags": [],
                "runtime": "process",
                "source": "cli-anything",
                "instructions": {
                    "source": "skill_md" if row.get("skill_installed") else "generated",
                    "path": f"skills/cli-app-{name}/SKILL.md",
                    "available": bool(row.get("skill_installed")),
                },
                "execution": manifest.get("execution") or {"entry_point": row.get("entry_point") or ""},
                "requirements": manifest.get("requirements") or {},
                "provisioning": manifest.get("provisioning") or {},
                "status": row.get("status") or "not_installed",
                "installed": bool(row.get("installed")),
                "available": bool(row.get("available")),
                "unavailable_reason": "",
                "install_supported": bool(row.get("install_supported")),
                "skill_installed": bool(row.get("skill_installed")),
                "requires": row.get("requires") or "",
                "tier": None,
                "logo_url": row.get("logo_url"),
                "brand_color": row.get("brand_color"),
                "docs_url": None,
                "manifest": manifest,
            })
        return out

    # ---- MCP 预设来源 -----------------------------------------------------

    def _mcp_capabilities(self) -> list[dict[str, Any]]:
        try:
            from biscuitbot.webui.mcp_presets_api import mcp_presets_payload

            payload = mcp_presets_payload()
        except Exception:
            payload = {"presets": []}
        out: list[dict[str, Any]] = []
        for row in payload.get("presets", []):
            name = str(row.get("name") or "")
            manifest = row.get("manifest") or {}
            out.append({
                "id": name,
                "name": name,
                "display_name": row.get("display_name") or name,
                "description": row.get("description") or "",
                "icon": None,
                "category": row.get("category") or "mcp",
                "tags": [],
                "runtime": "mcp",
                "source": row.get("source") or "mcp-preset",
                "instructions": {"source": "none", "path": None, "available": False},
                "execution": manifest.get("execution") or {"transport": row.get("transport") or ""},
                "requirements": manifest.get("requirements") or {},
                "provisioning": manifest.get("provisioning") or {},
                "status": row.get("status") or "not_installed",
                "installed": bool(row.get("installed") or row.get("configured")),
                "available": bool(row.get("available")),
                "unavailable_reason": "",
                "install_supported": bool(row.get("install_supported")),
                "skill_installed": False,
                "requires": row.get("requires") or "",
                "tier": None,
                "logo_url": row.get("logo_url"),
                "brand_color": row.get("brand_color"),
                "docs_url": row.get("docs_url"),
                "manifest": manifest,
            })
        return out

    # ---- 工具方法 ---------------------------------------------------------

    @staticmethod
    def _normalize_provisioning(provisioning: dict, runtime: str) -> dict:
        if provisioning:
            return provisioning
        # 内置 process 型技能（jianying-editor 等）无需安装，视为 bundled。
        if runtime == "process":
            return {"strategy": "bundled", "installers": []}
        return {}

    @staticmethod
    def _description(metadata: dict, fallback: str) -> str:
        value = metadata.get("description") if metadata else None
        if isinstance(value, str) and value.strip():
            return value.strip()
        return fallback

    @staticmethod
    def _tier(metadata: dict) -> str | None:
        value = metadata.get("tier") if metadata else None
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
        return "user"

    @staticmethod
    def _requires_str(reqs: dict) -> str:
        bins = [str(b) for b in reqs.get("bins", [])]
        env = [str(e) for e in reqs.get("env", [])]
        pkgs = [str(p) for p in reqs.get("pkgs", [])]
        parts: list[str] = []
        if bins:
            parts.append("CLI: " + ", ".join(bins))
        if env:
            parts.append("ENV: " + ", ".join(env))
        if pkgs:
            parts.append("PKG: " + ", ".join(pkgs))
        return "; ".join(parts)
