"""工具使用统计与冷存储轮转。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统中的使用统计与冷存储
管理组件。它跟踪每个工具的调用频率，将长时间未被调用的工具轮转入冷存储
——这些工具会从 INDEX.md 中移除，其 schema 不再发送给模型（除非通过
``cold_storage`` → ``discover_tools`` 显式发现）。

调用任何冷工具会自动将其恢复到活跃索引中。
"""

from __future__ import annotations

import json  # JSON 序列化，用于持久化统计与冷存储数据
import time  # 时间相关，用于记录最后调用时间戳
from dataclasses import asdict, dataclass  # 数据类支持
from pathlib import Path  # 路径处理
from typing import TYPE_CHECKING  # 仅类型检查时导入

if TYPE_CHECKING:  # 仅类型检查时导入，避免循环依赖
    from biscuitbot.agent.tools.registry import ToolRegistry


@dataclass
class ToolUsageStat:
    """单个工具的调用统计。"""

    call_count: int = 0  # 总调用次数
    last_called_at: float = 0.0  # 最后调用时间戳（Unix）；0 = 从未调用

    def record_call(self) -> None:
        """记录一次调用：计数加一并更新时间戳。"""
        self.call_count += 1
        self.last_called_at = time.time()


@dataclass
class ColdEntry:
    """已被轮转入冷存储的工具条目。"""

    name: str  # 工具名称
    capability: str  # 工具能力描述
    usage_md: str  # 使用说明文档路径
    source_file: str = ""  # 源文件模块路径
    cold_since: float = 0.0  # 进入冷存储的时间戳


class UsageStats:
    """跟踪工具调用频率并管理冷存储轮转。

    持久化到 ``.agent_tools/`` 工作区目录下的 ``usage_stats.json`` 和
    ``cold_storage.json``。
    """

    def __init__(self, base_dir: Path | None = None):
        self._stats: dict[str, ToolUsageStat] = {}  # 工具名 -> 调用统计
        self._cold: dict[str, ColdEntry] = {}  # 工具名 -> 冷存储条目
        self._stats_file: Path | None = None  # 统计文件路径
        self._cold_file: Path | None = None  # 冷存储文件路径
        if base_dir is not None:
            base_dir.mkdir(parents=True, exist_ok=True)
            self._stats_file = base_dir / "usage_stats.json"
            self._cold_file = base_dir / "cold_storage.json"
        self._load()

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------

    def record_call(self, name: str) -> None:
        """记录一次工具调用。自动从冷存储中恢复。"""
        stat = self._stats.setdefault(name, ToolUsageStat())
        stat.record_call()
        if name in self._cold:
            del self._cold[name]
        self._save()

    # ------------------------------------------------------------------
    # 冷存储查询
    # ------------------------------------------------------------------

    def is_cold(self, name: str) -> bool:
        """判断工具是否在冷存储中。"""
        return name in self._cold

    def cold_tool_names(self) -> list[str]:
        """返回所有冷存储工具名列表。"""
        return list(self._cold.keys())

    def get_cold_entry(self, name: str) -> ColdEntry | None:
        """获取指定工具的冷存储条目。"""
        return self._cold.get(name)

    def search_cold(self, query: str, limit: int = 10) -> list[ColdEntry]:
        """按关键词匹配搜索冷存储（匹配名称与能力描述）。

        优先按 token 重叠数排序；无重叠时回退到子串匹配。
        """
        query_lower = query.lower()
        tokens = set(query_lower.split())
        scored: list[tuple[float, ColdEntry]] = []
        for entry in self._cold.values():
            text = f"{entry.name} {entry.capability}".lower()
            entry_tokens = set(text.split())
            overlap = len(tokens & entry_tokens)
            if overlap == 0:
                # 回退：子串匹配
                if query_lower in text:
                    overlap = 1
                else:
                    continue
            scored.append((overlap, entry))
        scored.sort(key=lambda x: (-x[0], x[1].name))
        return [e for _, e in scored[:limit]]

    # ------------------------------------------------------------------
    # 轮转
    # ------------------------------------------------------------------

    def rotate_cold(
        self,
        registry: "ToolRegistry",
        threshold_days: int,
    ) -> list[str]:
        """将超过 *threshold_days* 天未调用的工具移入冷存储。

        always-include 工具永不轮转（它们是核心基础设施）。从未被调用的
        工具仅在注册时间超过阈值时才轮转（使用启发式：统计必须存在且
        ``last_called_at == 0``，且工具非新注册——通过检查是否已经历至少
        一个轮转周期来近似判断）。

        返回新进入冷存储的工具名列表。
        """
        if threshold_days <= 0:
            return []
        now = time.time()
        threshold_s = threshold_days * 86400
        newly_cold: list[str] = []
        for name in registry.tool_names:
            # 永不轮转 always-include 工具
            if registry._tool_is_always_include(name):
                continue
            if name in self._cold:
                continue
            stat = self._stats.get(name)
            if stat is None:
                # 工具从未被跟踪——跳过（可能刚注册）
                continue
            if stat.last_called_at == 0:
                # 从未被调用——暂时跳过（给它机会）
                continue
            if now - stat.last_called_at > threshold_s:
                tool = registry.get(name)
                if tool is None:
                    continue
                self._cold[name] = ColdEntry(
                    name=name,
                    capability=tool.capability,
                    usage_md=getattr(tool, "_usage_md", ""),
                    source_file=getattr(tool.__class__, "__module__", ""),
                    cold_since=now,
                )
                newly_cold.append(name)
        if newly_cold:
            self._save()
        return newly_cold

    def restore(self, name: str) -> bool:
        """手动恢复一个冷存储工具。返回是否原本在冷存储中。"""
        if name in self._cold:
            del self._cold[name]
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """将统计与冷存储数据持久化到 JSON 文件。"""
        if self._stats_file is not None:
            data = {k: asdict(v) for k, v in self._stats.items()}
            self._stats_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        if self._cold_file is not None:
            data = {k: asdict(v) for k, v in self._cold.items()}
            self._cold_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _load(self) -> None:
        """从 JSON 文件加载统计与冷存储数据。"""
        if self._stats_file is not None and self._stats_file.is_file():
            try:
                data = json.loads(self._stats_file.read_text(encoding="utf-8"))
                for k, v in data.items():
                    self._stats[k] = ToolUsageStat(**v)
            except (json.JSONDecodeError, TypeError):
                pass
        if self._cold_file is not None and self._cold_file.is_file():
            try:
                data = json.loads(self._cold_file.read_text(encoding="utf-8"))
                for k, v in data.items():
                    self._cold[k] = ColdEntry(**v)
            except (json.JSONDecodeError, TypeError):
                pass
