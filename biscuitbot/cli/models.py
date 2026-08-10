"""模型信息辅助工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/cli 目录，为 onboard 配置引导问卷提供模型信息查询能力。
在项目架构中起到的作用：
- 在 litellm 替换期间，模型数据库 / 自动补全功能被临时禁用；
- 保留所有公共函数签名，使调用方无需改动即可继续工作；
- 为引导问卷提供模型列表、上下文窗口限制、建议项与 token 计数格式化等能力。
"""

from __future__ import annotations

from typing import Any  # 类型提示工具，提供 Any 等泛型支持


def get_all_models() -> list[str]:
    """获取所有可用模型名称列表（当前已禁用，返回空列表）。"""
    return []


def find_model_info(model_name: str) -> dict[str, Any] | None:
    """根据模型名称查询模型详细信息（当前已禁用，返回 None）。"""
    return None


def get_model_context_limit(model: str, provider: str = "auto") -> int | None:
    """查询指定模型的上下文 token 上限（当前已禁用，返回 None）。"""
    return None


def get_model_suggestions(_partial: str, provider: str = "auto", limit: int = 20) -> list[str]:
    """根据已输入的部分文本获取模型自动补全建议（当前已禁用，返回空列表）。"""
    return []


def format_token_count(tokens: int) -> str:
    """格式化 token 数量用于显示（例如 200000 -> '200,000'）。"""
    return f"{tokens:,}"
