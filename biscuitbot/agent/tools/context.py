"""工具构造与执行所需的运行时上下文。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的上下文组件。
在项目架构中起到的作用：定义 ``ToolContext``（工具构造上下文）与
``RequestContext``（单次请求上下文），并通过 ContextVar 在异步调用链中
传递请求级信息（如 channel、chat_id、session_key），供工具在执行时获取
当前请求的会话与渠道信息。
"""
from __future__ import annotations

from contextvars import ContextVar, Token  # 上下文变量与令牌，用于异步上下文传递
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

# 当前请求上下文的 ContextVar，默认为 None
_CURRENT_REQUEST_CONTEXT: ContextVar["RequestContext | None"] = ContextVar(
    "biscuitbot_tool_request_context",
    default=None,
)


@dataclass(frozen=True)
class RequestContext:
    """单次请求的上下文，在消息处理时注入到工具中。

    该对象不可变（frozen），包含渠道、会话 ID 等请求级信息。
    """
    channel: str  # 消息渠道（如 slack、web 等）
    chat_id: str  # 会话 ID
    message_id: str | None = None  # 消息 ID
    session_key: str | None = None  # 会话键，用于关联持久化会话
    metadata: dict[str, Any] = field(default_factory=dict)  # 附加元数据


@runtime_checkable
class ContextAware(Protocol):
    """可感知请求上下文的协议，实现该协议的工具可接收上下文注入。"""

    def set_context(self, ctx: RequestContext) -> None:
        """设置当前请求上下文。

        参数:
            ctx: 请求上下文对象。
        """
        ...


def bind_request_context(ctx: RequestContext) -> Token[RequestContext | None]:
    """将请求上下文绑定到当前异步上下文。

    参数:
        ctx: 待绑定的请求上下文。

    返回:
        ContextVar 令牌，用于后续重置。
    """
    return _CURRENT_REQUEST_CONTEXT.set(ctx)


def reset_request_context(token: Token[RequestContext | None]) -> None:
    """重置请求上下文到绑定前的状态。

    参数:
        token: bind_request_context 返回的令牌。
    """
    _CURRENT_REQUEST_CONTEXT.reset(token)


def current_request_context() -> RequestContext | None:
    """获取当前异步上下文中的请求上下文，未绑定则返回 None。"""
    return _CURRENT_REQUEST_CONTEXT.get()


def current_request_session_key() -> str | None:
    """获取当前请求的会话键，无上下文时返回 None。"""
    ctx = current_request_context()
    return ctx.session_key if ctx else None


@dataclass
class ToolContext:
    """工具构造上下文。

    在创建工具实例时传入，包含工作区、配置、事件总线、子代理管理器、
    定时任务服务等依赖，供工具初始化与运行时使用。
    """
    config: Any  # 全局配置对象
    workspace: str  # 工作区路径
    bus: Any | None = None  # 事件总线
    subagent_manager: Any | None = None  # 子代理管理器
    cron_service: Any | None = None  # 定时任务服务
    sessions: Any | None = None  # 会话管理
    provider_snapshot_loader: Callable[[], Any] | None = None  # 模型快照加载器
    image_generation_provider_configs: dict[str, Any] | None = None  # 图像生成供应商配置
    vision_provider_loader: Callable[[], Any] | None = None  # 视觉模型加载器
    timezone: str = "UTC"  # 时区，默认 UTC
    file_state_store: Any = field(default=None)  # 文件状态存储
    workspace_sandbox: Any | None = None  # 工作区沙箱
    runtime_events: Any | None = None  # 运行时事件
