"""Unified capability model (技能 / 应用 / MCP 的单一抽象)。

``CapabilityRegistry`` 在读取时聚合三类来源（技能、CLI 应用、MCP 预设），
输出统一的 ``CapabilityRecord`` 列表。持久状态仍留在各自原有的 JSON/config
文件里，本模块不复制状态、不引入数据库。
"""

from biscuitbot.capabilities.registry import CapabilityRegistry

__all__ = ["CapabilityRegistry"]
