"""冷门仓库（cold storage）只读载荷。

工具长期未被调用后，``nightly_maintenance`` 定时任务会把它们从活跃索引轮转
出去——schema 不再发送给模型（见 ``xianaibot/cli/commands.py`` 的轮转调用与
``xianaibot/agent/tools/usage_stats.py``）。本模块把这份名单整理成 WebUI 可渲染
的列表，让用户看得见「哪些工具被丢进了冷门仓库」。

只读：不提供恢复动作。恢复的既有路径是让 agent 调用那个工具，由
``UsageStats.record_call`` 自动把它移出冷存储。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from loguru import logger

from xianaibot.agent.tools.usage_stats import UsageStats

# 冷存储目录名；与 AgentLoop 使用的 ``workspace/.agent_tools`` 保持一致。
_AGENT_TOOLS_DIR = ".agent_tools"

_DAY_S = 86400.0


def _days_since(timestamp: float, now: float) -> int:
    """冷落天数。时间戳为 0（从未记录）或位于未来时按 0 天处理，不出现负数。"""
    if timestamp <= 0 or timestamp > now:
        return 0
    return int((now - timestamp) // _DAY_S)


def cold_storage_payload(
    workspace_path: Path,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """返回冷门仓库快照 ``{cold_count, entries}``。

    ``entries`` 按 ``cold_since`` 倒序（最近转入的在前），字段与 ``ColdEntry``
    一一对应，另加服务端算好的 ``cold_days``。文件缺失或损坏时返回空列表——
    冷门仓库坏掉不该让能力中心跟着 500。``now`` 仅供测试注入。
    """
    current = time.time() if now is None else now
    entries: list[dict[str, Any]] = []
    try:
        # 复用 UsageStats 读盘，而不是自己 json.loads：损坏容忍与字段映射
        # 只存在一处。
        stats = UsageStats(base_dir=workspace_path / _AGENT_TOOLS_DIR)
        for name in stats.cold_tool_names():
            entry = stats.get_cold_entry(name)
            if entry is None:
                continue
            cold_since = float(entry.cold_since or 0.0)
            entries.append(
                {
                    "name": entry.name,
                    "capability": entry.capability,
                    "usage_md": entry.usage_md,
                    "source_file": entry.source_file,
                    "cold_since": cold_since,
                    "cold_days": _days_since(cold_since, current),
                }
            )
    except Exception:
        # 合法 JSON 但不是对象（``[]``/``null``/``"x"``）会让 UsageStats._load
        # 抛 AttributeError——只有只读接口兜住它，轮转逻辑那边不受影响。
        logger.exception("failed to read cold storage under {}", workspace_path)
        entries = []
    entries.sort(key=lambda item: (-item["cold_since"], item["name"]))
    return {"cold_count": len(entries), "entries": entries}
