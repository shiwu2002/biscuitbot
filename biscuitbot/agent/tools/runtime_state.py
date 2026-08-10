"""RuntimeState 协议：暴露给 MyTool 的 agent 循环状态。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，定义了 MyTool（自省工具）所需的
运行时状态协议。实际实现始终由 ``AgentLoop`` 满足。MyTool 还会通过
``getattr`` / ``setattr`` 动态访问任意属性，用于点路径检查与修改；这些
路径在运行时校验，而非由本协议约束。
"""

from pathlib import Path  # 路径类型
from typing import Any, Protocol  # 任意类型与协议基类


class RuntimeState(Protocol):
    """MyTool 要求其运行时状态提供者满足的最小契约。

    实际实现始终由 ``AgentLoop`` 满足。MyTool 还会通过 ``getattr`` /
    ``setattr`` 动态访问任意属性，用于点路径检查与修改；这些路径在运行时
    校验，而非由本协议约束。
    """

    @property
    def model(self) -> str: ...
    """当前使用的模型名称。"""

    @property
    def max_iterations(self) -> int: ...
    """单轮对话允许的最大迭代次数。"""

    @property
    def current_iteration(self) -> int: ...
    """当前迭代序号。"""

    @property
    def tool_names(self) -> list[str]: ...
    """已注册工具名称列表。"""

    @property
    def workspace(self) -> Path | str: ...
    """工作区路径。"""

    @property
    def provider_retry_mode(self) -> str: ...
    """提供商重试模式。"""

    @property
    def max_tool_result_chars(self) -> int: ...
    """工具结果最大字符数。"""

    @property
    def context_window_tokens(self) -> int: ...
    """上下文窗口 token 数。"""

    @property
    def web_config(self) -> Any: ...
    """Web 相关配置。"""

    @property
    def exec_config(self) -> Any: ...
    """执行（exec）相关配置。"""

    @property
    def workspace_sandbox(self) -> Any: ...
    """工作区沙箱配置。"""

    @property
    def subagents(self) -> Any: ...
    """子 Agent 管理器。"""

    @property
    def _runtime_vars(self) -> dict[str, Any]: ...
    """运行时变量字典（可被 MyTool 读写）。"""

    @property
    def _last_usage(self) -> Any: ...
    """上一次 LLM 调用的 usage 信息。"""

    def _sync_subagent_runtime_limits(self) -> None: ...
    """同步子 Agent 的运行时限制。"""

    @property
    def model_preset(self) -> str | None: ...
    """当前模型预设名称（可为空）。"""

    _active_preset: str | None
    """当前活跃的预设名称（可为空）。"""
