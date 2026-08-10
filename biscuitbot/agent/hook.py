"""Agent 运行过程的共享生命周期钩子原语。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块中生命周期钩子的核心定义文件。
在项目架构中起到的作用：
- 定义 ``AgentHookContext``（单次迭代状态）与 ``AgentRunHookContext``（整轮运行状态）
  两个上下文数据类，作为 Runner 与钩子之间传递状态的契约；
- 提供 ``AgentHook`` 基类，声明 Runner 在运行各阶段可调用的钩子方法
  （运行前/后、迭代前/后、流式增量、推理输出、工具执行前后等）；
- 提供 ``CompositeHook`` 组合钩子，将多个钩子按序委托执行并做错误隔离；
- 提供 ``SDKCaptureHook``，用于 SDK 调用方捕获使用的工具名与最终消息列表。
"""

from __future__ import annotations

from dataclasses import dataclass, field  # 用于定义带 slots 的数据类上下文
from typing import Any  # 类型注解支持

from loguru import logger  # 日志记录，用于钩子异常的输出

from biscuitbot.providers.base import LLMResponse, ToolCallRequest  # LLM 响应与工具调用请求类型


@dataclass(slots=True)
class AgentHookContext:
    """暴露给 Runner 钩子的“可变单次迭代状态”。

    职责与项目角色：
    - 承载一次 LLM 迭代过程中的关键数据（消息列表、响应、工具调用、流式状态等）；
    - 由 Runner 在迭代过程中就地更新，钩子读取/修改这些字段以实现自定义行为。

    典型用法：作为 ``before_iteration``/``on_stream``/``after_iteration`` 等钩子的入参。
    """

    iteration: int  # 当前迭代序号（从 0 或 1 起，由 Runner 约定）
    messages: list[dict[str, Any]]  # 当前消息列表（Runner 会就地修改）
    response: LLMResponse | None = None  # 本次迭代的 LLM 响应
    usage: dict[str, int] = field(default_factory=dict)  # token 使用量统计
    tool_calls: list[ToolCallRequest] = field(default_factory=list)  # 本次迭代产生的工具调用请求
    tool_results: list[Any] = field(default_factory=list)  # 工具执行结果列表
    tool_events: list[dict[str, str]] = field(default_factory=list)  # 工具事件记录（如状态/进度）
    streamed_content: bool = False  # 是否已流式输出正文内容
    streamed_reasoning: bool = False  # 是否已流式输出推理内容
    final_content: str | None = None  # 最终正文内容
    stop_reason: str | None = None  # 停止原因
    error: str | None = None  # 错误信息（若有）
    session_key: str | None = None  # 所属会话 key


@dataclass(slots=True)
class AgentRunHookContext:
    """暴露给 Runner 钩子的“整轮运行状态快照”。

    职责与项目角色：
    - 在一次 Agent 运行（可能含多次迭代）结束时，向钩子提供汇总状态；
    - 用于 ``before_run``/``after_run``/``on_error``/``on_finally`` 等运行级钩子。
    """

    messages: list[dict[str, Any]]  # 运行结束时的最终消息列表
    final_content: str | None = None  # 最终正文内容
    tools_used: list[str] = field(default_factory=list)  # 本轮使用的所有工具名
    usage: dict[str, int] = field(default_factory=dict)  # 本轮 token 使用量汇总
    stop_reason: str | None = None  # 停止原因
    error: str | None = None  # 错误信息（若有）
    tool_events: list[dict[str, str]] = field(default_factory=list)  # 本轮工具事件汇总
    had_injections: bool = False  # 本轮是否发生过消息注入
    exception: BaseException | None = None  # 运行过程中抛出的异常（若有）


class AgentHook:
    """Runner 共享自定义的最小生命周期接口。

    职责与项目角色：
    - 作为所有钩子的基类，提供默认空实现，子类按需覆盖感兴趣的方法；
    - 通过 ``wants_streaming`` 声明是否需要流式增量；
    - ``_reraise`` 标志控制异常是否向上抛出（供 ``CompositeHook`` 做错误隔离判断）。

    典型用法：子类化并覆盖 ``before_run``/``on_stream``/``after_iteration`` 等方法。
    """

    def __init__(self, reraise: bool = False) -> None:
        self._reraise = reraise  # 是否在异常时向上抛出（False 时由组合钩子捕获并记录）

    def wants_streaming(self) -> bool:
        """是否需要流式增量回调。默认 False。"""
        return False

    async def before_run(self, context: AgentRunHookContext) -> None:
        """运行开始前回调。"""
        pass

    async def after_run(self, context: AgentRunHookContext) -> None:
        """运行正常结束后回调。"""
        pass

    async def on_error(self, context: AgentRunHookContext) -> None:
        """运行出错时回调。"""
        pass

    async def on_finally(self, context: AgentRunHookContext) -> None:
        """运行结束（无论成功与否）后回调。"""
        pass

    async def before_iteration(self, context: AgentHookContext) -> None:
        """每次迭代开始前回调。"""
        pass

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        """流式正文增量回调。"""
        pass

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        """流式输出结束回调。"""
        pass

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        """执行工具前回调。"""
        pass

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        """推理内容增量回调。"""
        pass

    async def emit_reasoning_end(self) -> None:
        """标记一次进行中的推理流结束。

        对 ``emit_reasoning`` 分片做缓冲（用于就地 UI 更新）的钩子在此处
        刷新并冻结已渲染的分组；一次性钩子可忽略。
        """
        pass

    async def after_iteration(self, context: AgentHookContext) -> None:
        """每次迭代结束后回调。"""
        pass

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        """对最终正文内容做后处理，返回处理后的内容。"""
        return content


class CompositeHook(AgentHook):
    """扇出式组合钩子，将调用按序委托给一组钩子。

    错误隔离：异步方法会逐个捕获并记录每个钩子的异常，
    因此单个有缺陷的自定义钩子不会导致 Agent 主循环崩溃。
    ``finalize_content`` 作为管道执行（不做隔离——bug 应当暴露）。
    """

    __slots__ = ("_hooks",)

    def __init__(self, hooks: list[AgentHook]) -> None:
        super().__init__()
        self._hooks = list(hooks)  # 按序执行的钩子列表

    def wants_streaming(self) -> bool:
        # 任一子钩子需要流式，则整体需要流式
        return any(h.wants_streaming() for h in self._hooks)

    async def _for_each_hook_safe(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        """按序对每个子钩子调用指定方法，并对未设 reraise 的钩子做异常隔离。"""
        for h in self._hooks:
            if getattr(h, "_reraise", False):
                # reraise 钩子：异常直接向上抛出
                await getattr(h, method_name)(*args, **kwargs)
                continue

            try:
                await getattr(h, method_name)(*args, **kwargs)
            except Exception:
                # 捕获并记录，避免单个钩子影响后续钩子或主循环
                logger.exception("AgentHook.{} error in {}", method_name, type(h).__name__)

    async def before_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_iteration", context)

    async def before_run(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("before_run", context)

    async def after_run(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("after_run", context)

    async def on_error(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("on_error", context)

    async def on_finally(self, context: AgentRunHookContext) -> None:
        await self._for_each_hook_safe("on_finally", context)

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        await self._for_each_hook_safe("on_stream", context, delta)

    async def on_stream_end(self, context: AgentHookContext, *, resuming: bool) -> None:
        await self._for_each_hook_safe("on_stream_end", context, resuming=resuming)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("before_execute_tools", context)

    async def emit_reasoning(self, reasoning_content: str | None) -> None:
        await self._for_each_hook_safe("emit_reasoning", reasoning_content)

    async def emit_reasoning_end(self) -> None:
        await self._for_each_hook_safe("emit_reasoning_end")

    async def after_iteration(self, context: AgentHookContext) -> None:
        await self._for_each_hook_safe("after_iteration", context)

    def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        # 管道式执行：每个钩子的输出作为下一个钩子的输入
        for h in self._hooks:
            content = h.finalize_content(context, content)
        return content


class SDKCaptureHook(AgentHook):
    """为 ``RunResult`` 记录使用的工具名与最终消息列表。

    Runner 在迭代过程中会就地修改 ``context.messages``，因此快照在每次
    ``after_iteration`` 调用时刷新；最后一次调用反映 SDK 调用方关心的轮次
    结束状态。运行级快照在可用时为权威来源，并覆盖没有最终迭代回调的路径。
    """

    def __init__(self) -> None:
        super().__init__()
        self.tools_used: list[str] = []  # 捕获的工具名列表
        self.messages: list[dict[str, Any]] = []  # 捕获的最终消息列表

    async def after_iteration(self, context: AgentHookContext) -> None:
        # 每次迭代后追加本批工具调用，并刷新消息快照
        for call in context.tool_calls:
            self.tools_used.append(call.name)
        self.messages = list(context.messages)

    async def after_run(self, context: AgentRunHookContext) -> None:
        # 运行级快照为权威来源，覆盖迭代级捕获
        self.tools_used = list(context.tools_used)
        self.messages = list(context.messages)
