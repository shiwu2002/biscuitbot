"""Agent 技能（Skills）加载器。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块中负责“技能加载与管理”的组件。
在项目架构中起到的作用：
- ``SkillsLoader`` 负责从工作区与内置目录发现、加载、校验技能（SKILL.md 文件），
  技能以 Markdown 编写，教 Agent 如何使用特定工具或执行特定任务；
- 支持技能的 YAML frontmatter 元数据解析（描述、依赖命令/环境变量、always 标记等），
  并据此判断技能是否可用、是否需始终注入上下文；
- 被 ``ContextBuilder`` 在装配 Agent 上下文时调用，实现“渐进式发现”——先注入技能
  摘要，Agent 按需通过 read_file 读取完整内容；
- 工作区技能优先于内置技能（同名覆盖）。
"""

import json  # 解析 frontmatter 中可能为 JSON 字符串的 metadata 字段
import os  # 检查环境变量是否设置（技能依赖校验）
import re  # 剥离 YAML frontmatter 的正则
import shutil  # 检查依赖命令是否存在于 PATH（which）
from pathlib import Path  # 路径处理

import yaml  # 解析 SKILL.md 的 YAML frontmatter

# Default builtin skills directory (relative to this file)
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"  # 内置技能目录（相对本文件的上级的 skills/）

# Opening ---, YAML body (group 1), closing --- on its own line; supports CRLF.
_STRIP_SKILL_FRONTMATTER = re.compile(  # 匹配开头 ---、YAML 正文（分组 1）、结尾 --- 的正则；兼容 CRLF
    r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?",
    re.DOTALL,
)


class SkillsLoader:
    """
    Loader for agent skills.

    Skills are markdown files (SKILL.md) that teach the agent how to use
    specific tools or perform certain tasks.

    技能加载器。

    职责与项目角色：
    - 从工作区（``workspace/skills``）与内置目录发现并加载技能；
    - 解析 SKILL.md 的 YAML frontmatter，提取描述、依赖与标记；
    - 校验技能依赖（CLI 命令、环境变量）是否满足，判断可用性；
    - 为 ``ContextBuilder`` 提供技能摘要与上下文注入内容。

    典型用法：由 ``ContextBuilder`` 持有并调用 ``build_skills_summary``/
    ``load_skills_for_context``/``get_always_skills``。
    """

    def __init__(self, workspace: Path, builtin_skills_dir: Path | None = None, disabled_skills: set[str] | None = None):
        self.workspace = workspace  # 工作区根目录
        self.workspace_skills = workspace / "skills"  # 工作区技能目录
        self.builtin_skills = builtin_skills_dir or BUILTIN_SKILLS_DIR  # 内置技能目录
        self.disabled_skills = disabled_skills or set()  # 被禁用的技能名集合

    def _skill_entries_from_dir(self, base: Path, source: str, *, skip_names: set[str] | None = None) -> list[dict[str, str]]:
        """从指定目录扫描技能条目（含 SKILL.md 的子目录）。

        参数:
            base: 技能根目录；
            source: 来源标识（"workspace" 或 "builtin"）；
            skip_names: 需跳过的技能名集合（用于内置技能被工作区覆盖时）。

        返回:
            技能条目字典列表（含 name/path/source）。
        """
        if not base.exists():
            return []
        entries: list[dict[str, str]] = []
        for skill_dir in base.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue
            name = skill_dir.name
            if skip_names is not None and name in skip_names:  # 跳过已被工作区覆盖的同名技能
                continue
            entries.append({"name": name, "path": str(skill_file), "source": source})
        return entries

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        """
        List all available skills.

        Args:
            filter_unavailable: If True, filter out skills with unmet requirements.

        Returns:
            List of skill info dicts with 'name', 'path', 'source'.

        列出所有可用技能。

        参数:
            filter_unavailable: 为 True 时过滤掉依赖未满足的技能。

        返回:
            技能信息字典列表（含 name/path/source）。
        """
        skills = self._skill_entries_from_dir(self.workspace_skills, "workspace")
        workspace_names = {entry["name"] for entry in skills}  # 工作区技能名集合（用于覆盖内置同名）
        if self.builtin_skills and self.builtin_skills.exists():
            skills.extend(
                self._skill_entries_from_dir(self.builtin_skills, "builtin", skip_names=workspace_names)
            )

        if self.disabled_skills:  # 过滤被禁用的技能
            skills = [s for s in skills if s["name"] not in self.disabled_skills]

        if filter_unavailable:  # 过滤依赖未满足的技能
            return [skill for skill in skills if self._check_requirements(self._get_skill_meta(skill["name"]))]
        return skills

    def load_skill(self, name: str) -> str | None:
        """
        Load a skill by name.

        Args:
            name: Skill name (directory name).

        Returns:
            Skill content or None if not found.

        按名称加载技能内容。

        参数:
            name: 技能名（目录名）。

        返回:
            SKILL.md 的文本内容；未找到时返回 None。
        """
        roots = [self.workspace_skills]
        if self.builtin_skills:
            roots.append(self.builtin_skills)
        for root in roots:  # 工作区优先于内置
            path = root / name / "SKILL.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return None

    def load_skills_for_context(self, skill_names: list[str]) -> str:
        """
        Load specific skills for inclusion in agent context.

        Args:
            skill_names: List of skill names to load.

        Returns:
            Formatted skills content.

        加载指定技能并格式化为上下文注入内容。

        参数:
            skill_names: 技能名列表。

        返回:
            以 ``### Skill: <name>`` 分节、用分隔线连接的格式化内容。
        """
        parts = [
            f"### Skill: {name}\n\n{self._strip_frontmatter(markdown)}"
            for name in skill_names
            if (markdown := self.load_skill(name))
        ]
        return "\n\n---\n\n".join(parts)

    def build_skills_summary(self, exclude: set[str] | None = None) -> str:
        """
        Build a summary of all skills (name, description, path, availability).

        This is used for progressive loading - the agent can read the full
        skill content using read_file when needed.

        Args:
            exclude: Set of skill names to omit from the summary.

        Returns:
            Markdown-formatted skills summary.

        构建所有技能的摘要（名称、描述、路径、可用性）。

        用于渐进式加载：Agent 先看到摘要，需要时通过 read_file 读取完整内容。

        参数:
            exclude: 需排除的技能名集合。

        返回:
            Markdown 格式的技能摘要；无技能时返回空字符串。
        """
        all_skills = self.list_skills(filter_unavailable=False)
        if not all_skills:
            return ""

        lines: list[str] = []
        for entry in all_skills:
            skill_name = entry["name"]
            if exclude and skill_name in exclude:
                continue
            meta = self._get_skill_meta(skill_name)
            available = self._check_requirements(meta)
            desc = self._get_skill_description(skill_name)
            if available:  # 可用：显示名称、描述与路径
                lines.append(f"- **{skill_name}** — {desc}  `{entry['path']}`")
            else:  # 不可用：附加缺失依赖说明
                missing = self._get_missing_requirements(meta)
                suffix = f" (unavailable: {missing})" if missing else " (unavailable)"
                lines.append(f"- **{skill_name}** — {desc}{suffix}  `{entry['path']}`")
        return "\n".join(lines)

    def _get_missing_requirements(self, skill_meta: dict) -> str:
        """Get a description of missing requirements."""
        """获取缺失依赖的描述字符串。"""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return ", ".join(
            [f"CLI: {command_name}" for command_name in required_bins if not shutil.which(command_name)]
            + [f"ENV: {env_name}" for env_name in required_env_vars if not os.environ.get(env_name)]
        )

    def get_skill_availability(self, name: str) -> tuple[bool, str]:
        """Return whether a skill can run and why not when it cannot."""
        """返回技能是否可运行，以及不可运行时的原因。

        参数:
            name: 技能名。

        返回:
            (是否可用, 原因描述)；可用时原因为空字符串。
        """
        meta = self._get_skill_meta(name)
        available = self._check_requirements(meta)
        return available, "" if available else self._get_missing_requirements(meta)

    def get_skill_requirements(self, name: str) -> dict[str, list[str]]:
        """Return explicit command/env requirements and currently missing entries."""
        """返回技能的显式命令/环境变量依赖及当前缺失项。

        参数:
            name: 技能名。

        返回:
            包含 bins/env/missing_bins/missing_env 四个列表的字典。
        """
        requires = self._get_skill_meta(name).get("requires", {})
        bins = [str(value) for value in requires.get("bins", [])]
        env = [str(value) for value in requires.get("env", [])]
        return {
            "bins": bins,
            "env": env,
            "missing_bins": [value for value in bins if not shutil.which(value)],
            "missing_env": [value for value in env if not os.environ.get(value)],
        }

    def _get_skill_description(self, name: str) -> str:
        """Get the description of a skill from its frontmatter."""
        """从 frontmatter 获取技能描述；缺失时回退为技能名。"""
        meta = self.get_skill_metadata(name)
        if meta and meta.get("description"):
            return meta["description"]
        return name  # Fallback to skill name  # 回退为技能名

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        """剥离 Markdown 内容中的 YAML frontmatter。"""
        if not content.startswith("---"):
            return content
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if match:
            return content[match.end():].strip()
        return content

    def _parse_biscuitbot_metadata(self, raw: object) -> dict:
        """Extract biscuitbot/openclaw metadata from a frontmatter field.

        ``raw`` may be a dict (already parsed by yaml.safe_load) or a JSON str.
        """
        """从 frontmatter 的 metadata 字段提取 biscuitbot/openclaw 元数据。

        ``raw`` 可以是 dict（已被 yaml.safe_load 解析）或 JSON 字符串。

        参数:
            raw: 原始 metadata 值。

        返回:
            解析后的元数据字典；无法解析时返回空字典。
        """
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
        else:
            return {}
        if not isinstance(data, dict):
            return {}
        payload = data.get("biscuitbot", data.get("openclaw", {}))  # 兼容 biscuitbot/openclaw 两种键名
        return payload if isinstance(payload, dict) else {}

    def _check_requirements(self, skill_meta: dict) -> bool:
        """Check if skill requirements are met (bins, env vars)."""
        """检查技能依赖是否满足（CLI 命令与环境变量）。"""
        requires = skill_meta.get("requires", {})
        required_bins = requires.get("bins", [])
        required_env_vars = requires.get("env", [])
        return all(shutil.which(cmd) for cmd in required_bins) and all(
            os.environ.get(var) for var in required_env_vars
        )

    def _get_skill_meta(self, name: str) -> dict:
        """Get biscuitbot metadata for a skill (cached in frontmatter)."""
        """获取技能的 biscuitbot 元数据（存放在 frontmatter 的 metadata 字段中）。"""
        raw_meta = self.get_skill_metadata(name) or {}
        return self._parse_biscuitbot_metadata(raw_meta.get("metadata"))

    def get_always_skills(self) -> list[str]:
        """Get skills marked as always=true that meet requirements."""
        """获取标记为 always=true 且依赖满足的技能名列表。

        这些技能会被始终注入 Agent 上下文，无需 Agent 主动发现。
        """
        return [
            entry["name"]
            for entry in self.list_skills(filter_unavailable=True)
            if (meta := self.get_skill_metadata(entry["name"]) or {})
            and (
                self._parse_biscuitbot_metadata(meta.get("metadata")).get("always")
                or meta.get("always")
            )
        ]

    def get_skill_metadata(self, name: str) -> dict | None:
        """
        Get metadata from a skill's frontmatter.

        Args:
            name: Skill name.

        Returns:
            Metadata dict or None.

        从技能的 YAML frontmatter 获取完整元数据。

        参数:
            name: 技能名。

        返回:
            元数据字典；无 frontmatter 或解析失败时返回 None。
        """
        content = self.load_skill(name)
        if not content or not content.startswith("---"):
            return None
        match = _STRIP_SKILL_FRONTMATTER.match(content)
        if not match:
            return None
        try:
            parsed = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(parsed, dict):
            return None
        # yaml.safe_load returns native types (int, bool, list, etc.);
        # keep values as-is so downstream consumers get correct types.
        # yaml.safe_load 返回原生类型（int/bool/list 等），保持原值以便下游获取正确类型
        metadata: dict[str, object] = {}
        for key, value in parsed.items():
            metadata[str(key)] = value
        return metadata
