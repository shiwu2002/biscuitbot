"""智能体运行时状态事件总线。

所属模块与项目作用
===================
本文件位于 biscuitbot/bus 目录，定义运行时状态事件的发布/订阅机制。
在项目架构中起到的作用：与 :mod:`biscuitbot.bus.queue` 不同——消息总线负责用户/聊天
消息的投递，而运行时事件总线负责进程内状态通知（如 turn 生命周期、模型切换、目标状态
变更、全链路追踪），供可选订阅者（如 WebUI 适配器、CLI 追踪日志）渲染展示。
"""

from __future__ import annotations

import asyncio  # 用于获取事件循环与创建任务
import contextlib  # 提供上下文管理工具，用于忽略异常
import inspect  # 用于检测可等待对象
from collections.abc import Awaitable, Callable  # 可调用与可等待类型标注
from dataclasses import dataclass, field  # 用于定义不可变事件数据类
from typing import Any  # 任意类型标注

from loguru import logger  # 日志输出

from biscuitbot.bus.events import InboundMessage  # 入站消息类型


@dataclass(frozen=True)
class RuntimeEventContext:
    """turn 级运行时事件通用的路由上下文。

    封装渠道、聊天 ID、会话键及元数据，所有 turn 级事件共享该上下文以保持路由一致性。
    """

    channel: str  # 渠道标识
    chat_id: str  # 聊天/会话标识
    session_key: str  # 会话唯一键
    metadata: dict[str, Any] = field(default_factory=dict)  # 附加元数据


@dataclass(frozen=True)
class SessionTurnStarted:
    """用户/系统 turn 已加载会话，即将构建上下文。"""

    context: RuntimeEventContext


@dataclass(frozen=True)
class TurnRunStatusChanged:
    """turn 的可见运行状态发生变更（如开始思考、运行中、完成）。"""

    context: RuntimeEventContext
    status: str  # 新的运行状态
    started_at: float | None = None  # 状态开始的时间戳（可选）


@dataclass(frozen=True)
class TurnCompleted:
    """一个 turn 已向用户交付最终可见响应。"""

    context: RuntimeEventContext
    latency_ms: int | None = None  # turn 整体耗时（毫秒）
    runtime: Any | None = None  # 关联的运行时对象（可选）


@dataclass(frozen=True)
class GoalStateChanged:
    """会话的持久化目标状态发生变更。"""

    context: RuntimeEventContext
    session_metadata: dict[str, Any] = field(default_factory=dict)  # 会话元数据快照


@dataclass(frozen=True)
class RuntimeModelChanged:
    """当前激活的运行时模型/预设发生切换。"""

    model: str  # 新模型标识
    model_preset: str | None  # 新预设标识（可选）


@dataclass(frozen=True)
class AgentTraceEvent:
    """全链路追踪事件：状态转换、工具调用、LLM 调用、错误。

    phase 取值：
    - "turn_state": AgentLoop 状态机步骤（RESTORE/COMPACT/COMMAND/BUILD/RUN/SAVE/RESPOND）
    - "tool_call": 工具调用（prepare/execute/classify）
    - "llm_call": LLM 调用（request/response）
    - "error": 错误或违规

    status 取值："started" | "completed" | "failed"
    """

    context: RuntimeEventContext
    turn_id: str  # 所属 turn 的唯一标识
    phase: str  # 事件阶段
    step: str  # 具体步骤名
    status: str  # 事件状态
    duration_ms: float | None = None  # 步骤耗时（毫秒）
    detail: dict[str, Any] = field(default_factory=dict)  # 额外详情


# 所有运行时事件的联合类型
RuntimeEvent = (
    SessionTurnStarted
    | TurnRunStatusChanged
    | TurnCompleted
    | GoalStateChanged
    | RuntimeModelChanged
    | AgentTraceEvent
)
# 事件类型本身的联合类型，用于订阅时按类型过滤
RuntimeEventType = (
    type[SessionTurnStarted]
    | type[TurnRunStatusChanged]
    | type[TurnCompleted]
    | type[GoalStateChanged]
    | type[RuntimeModelChanged]
    | type[AgentTraceEvent]
)
RuntimeEventHandler = Callable[[Any], Awaitable[None] | None]  # 事件处理器签名（可同步可异步）
_HandlerEntry = tuple[RuntimeEventType | None, RuntimeEventHandler]  # 订阅条目：事件类型 + 处理器


class RuntimeEventBus:
    """进程内运行时状态的发布/订阅总线。

    订阅者按注册顺序执行。``publish`` 会等待异步处理器完成，因此调用方可在需要
    保证运行时事件紧跟用户消息的顺序时使用。``publish_nowait`` 则用于同步调用点，
    不会阻塞当前流程。
    """

    def __init__(self) -> None:
        self._handlers: list[_HandlerEntry] = []  # 已注册的处理器列表

    def subscribe(
        self,
        handler: RuntimeEventHandler,
        event_type: RuntimeEventType | None = None,
    ) -> Callable[[], None]:
        """订阅运行时事件，可选按事件类型过滤。

        返回一个取消订阅的回调，调用即可从处理器列表中移除该订阅。
        """
        entry = (event_type, handler)
        self._handlers.append(entry)

        def _unsubscribe() -> None:
            """取消订阅，若条目已被移除则忽略 ValueError。"""
            with contextlib.suppress(ValueError):
                self._handlers.remove(entry)

        return _unsubscribe

    async def publish(self, event: RuntimeEvent) -> None:
        """向所有匹配的订阅者发布事件，等待异步处理器完成。

        按注册顺序遍历处理器，跳过不匹配事件类型的订阅，捕获并记录处理器异常
        以避免单个处理器失败影响其他订阅者。
        """
        for event_type, handler in list(self._handlers):
            # 若订阅指定了事件类型且当前事件不匹配，则跳过
            if event_type is not None and not isinstance(event, event_type):
                continue
            try:
                result = handler(event)
                # 处理器可能是协程，统一 await
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("runtime event handler failed for {}", type(event).__name__)

    def publish_nowait(self, event: RuntimeEvent) -> None:
        """非阻塞发布：在运行中的事件循环上创建任务来异步发布事件。

        若当前没有运行的事件循环，则丢弃事件并记录调试日志。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("dropping runtime event without a running loop: {}", type(event).__name__)
            return
        loop.create_task(self.publish(event))


class RuntimeEventPublisher:
    """turn 级运行时事件的便捷发布器。

    智能体代码负责决定何时发生状态转换，本类负责构建事件上下文、携带 turn 级
    元数据，并维护每个 turn 的延迟与运行时记录，供 turn 完成时一并发布。
    """

    def __init__(self, bus: RuntimeEventBus | None = None) -> None:
        self.bus = bus or RuntimeEventBus()  # 关联的事件总线
        self._turn_latency_ms: dict[str, int] = {}  # 按 session_key 记录的 turn 延迟
        self._turn_runtime: dict[str, Any] = {}  # 按 session_key 记录的运行时对象

    @staticmethod
    def _context(
        *,
        channel: str,
        chat_id: str,
        session_key: str,
        metadata: dict[str, Any] | None,
    ) -> RuntimeEventContext:
        """构建运行时事件路由上下文，复制元数据以避免外部修改。"""
        return RuntimeEventContext(
            channel=channel,
            chat_id=chat_id,
            session_key=session_key,
            metadata=dict(metadata or {}),
        )

    def record_turn_runtime(self, session_key: str, runtime: Any) -> None:
        """记录某 turn 关联的运行时对象，供 turn 完成事件携带。"""
        self._turn_runtime[session_key] = runtime

    def record_turn_latency(self, session_key: str, latency_ms: int | None) -> None:
        """记录某 turn 的延迟（毫秒），仅在传入非空时存储。"""
        if latency_ms is not None:
            self._turn_latency_ms[session_key] = int(latency_ms)

    def clear_turn(self, session_key: str) -> None:
        """清理某 turn 的延迟与运行时记录（用于 turn 结束后回收）。"""
        self._turn_latency_ms.pop(session_key, None)
        self._turn_runtime.pop(session_key, None)

    async def session_turn_started(
        self,
        msg: InboundMessage,
        session_key: str,
    ) -> None:
        """发布 turn 已启动事件，从入站消息提取路由信息。"""
        await self.bus.publish(
            SessionTurnStarted(
                context=self._context(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_key=session_key,
                    metadata=msg.metadata,
                )
            )
        )

    async def run_status_changed(
        self,
        msg: InboundMessage,
        session_key: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """发布 turn 运行状态变更事件。"""
        await self.bus.publish(
            TurnRunStatusChanged(
                context=self._context(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    session_key=session_key,
                    metadata=msg.metadata,
                ),
                status=status,
                started_at=started_at,
            )
        )

    async def turn_completed(
        self,
        *,
        channel: str,
        chat_id: str,
        session_key: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        """发布 turn 完成事件，并附带已记录的延迟与运行时信息。"""
        await self.bus.publish(
            TurnCompleted(
                context=self._context(
                    channel=channel,
                    chat_id=chat_id,
                    session_key=session_key,
                    metadata=metadata,
                ),
                latency_ms=self._turn_latency_ms.pop(session_key, None),  # 弹出并清除延迟记录
                runtime=self._turn_runtime.pop(session_key, None),  # 弹出并清除运行时记录
            )
        )

    def runtime_model_changed(self, model: str, model_preset: str | None) -> None:
        """发布运行时模型/预设变更事件（非阻塞）。"""
        self.bus.publish_nowait(
            RuntimeModelChanged(model=model, model_preset=model_preset)
        )

    def publish_trace(
        self,
        *,
        msg: InboundMessage | None,
        session_key: str,
        turn_id: str,
        phase: str,
        step: str,
        status: str,
        duration_ms: float | None = None,
        detail: dict[str, Any] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """发布全链路追踪事件（非阻塞）。

        msg 非空时从 msg 提取 channel/chat_id/metadata；
        msg 为 None 时使用显式传入的 channel/chat_id/metadata。
        """
        if msg is not None:
            ctx_channel = msg.channel
            ctx_chat_id = msg.chat_id
            ctx_metadata = msg.metadata
        else:
            # 无入站消息时使用显式参数，缺省回退到 cli/direct
            ctx_channel = channel or "cli"
            ctx_chat_id = chat_id or "direct"
            ctx_metadata = metadata
        self.bus.publish_nowait(
            AgentTraceEvent(
                context=self._context(
                    channel=ctx_channel,
                    chat_id=ctx_chat_id,
                    session_key=session_key,
                    metadata=ctx_metadata,
                ),
                turn_id=turn_id,
                phase=phase,
                step=step,
                status=status,
                duration_ms=duration_ms,
                detail=dict(detail or {}),
            )
        )


def ensure_runtime_event_publisher(owner: Any) -> RuntimeEventPublisher:
    """返回 owner 的运行时发布器，若缺失则惰性创建。

    会同步确保 owner 上存在 RuntimeEventBus 与 RuntimeEventPublisher 两个属性，
    便于在已有对象上挂载运行时事件能力。
    """
    publisher = getattr(owner, "runtime_event_publisher", None)
    if isinstance(publisher, RuntimeEventPublisher):
        return publisher

    # 确保存在可用的运行时事件总线
    bus = getattr(owner, "runtime_events", None)
    if not isinstance(bus, RuntimeEventBus):
        bus = RuntimeEventBus()
        owner.runtime_events = bus

    publisher = RuntimeEventPublisher(bus)
    owner.runtime_event_publisher = publisher
    return publisher
