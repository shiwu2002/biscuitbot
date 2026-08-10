"""LLM Provider 抽象模块入口。

所属模块与项目作用
===================
本文件位于 biscuitbot/providers 目录，是 LLM Provider 层的对外入口。
在项目架构中起到的作用：
- 统一对外暴露 Provider 层的核心抽象与具体实现（LLMProvider、LLMResponse
  以及 AnthropicProvider、OpenAICompatProvider 等具体后端）。
- 通过懒加载（lazy import）机制避免在模块导入阶段就加载全部后端 SDK，
  降低启动开销，并允许按需加载重型依赖（如 anthropic、openai）。
"""

from __future__ import annotations

from importlib import import_module  # 用于按需动态导入具体 Provider 子模块
from typing import TYPE_CHECKING

# Provider 基类与响应数据类，属于核心抽象，始终需要直接导出
from biscuitbot.providers.base import LLMProvider, LLMResponse

__all__ = [
    # 对外公开的符号集合，便于上层 from biscuitbot.providers import ... 使用
    "LLMProvider",
    "LLMResponse",
    "AnthropicProvider",
    "OpenAICompatProvider",
]

# 具体实现类名到其所在相对模块路径的映射，驱动 __getattr__ 的懒加载
_LAZY_IMPORTS = {
    "AnthropicProvider": ".anthropic_provider",
    "OpenAICompatProvider": ".openai_compat_provider",
}

if TYPE_CHECKING:
    # 仅用于类型检查阶段提供类型信息，运行时不会真正导入，避免循环依赖与启动开销
    from biscuitbot.providers.anthropic_provider import AnthropicProvider
    from biscuitbot.providers.openai_compat_provider import OpenAICompatProvider


def __getattr__(name: str):
    """模块级懒加载入口。

    当外部访问未直接导入的 Provider 实现类时（如
    ``biscuitbot.providers.AnthropicProvider``），通过本函数按需导入对应
    子模块，避免在模块初始化阶段一次性加载所有后端 SDK，从而降低启动
    开销并支持可选依赖。
    """
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        # 不在懒加载映射中的属性，抛出标准的 AttributeError
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # 以当前包为基准动态导入相对子模块并返回目标属性
    module = import_module(module_name, __name__)
    return getattr(module, name)
