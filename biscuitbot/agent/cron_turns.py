"""调度 cron 轮次的协调器。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块中负责调度型 cron 轮次的协调组件。
在项目架构中起到的作用：
- ``CronTurnCoordinator`` 负责将定时触发的 cron 轮次安全地提交给会话，
  并等待对应会话的响应，避免与实时注入消息混淆；
- 当目标会话正忙时，将 cron 轮次延迟到会话空闲后再发布，保证顺序与一致性；
- 维护 run_id 到 Future 的映射，使提交方能够同步等待异步结果，
  并支持在会话空闲时依次发布延迟队列中的待执行轮次。
"""

from __future__ import annotations

import asyncio  # 提供 Future 与事件循环支持，用于等待异步响应
import dataclasses  # 用于不可变消息的替换（replace）以构造带 override 的副本
from collections.abc import Awaitable, Callable, Iterable  # 类型注解支持

from biscuitbot.bus.events import InboundMessage, OutboundMessage  # 入/出站消息类型
from biscuitbot.cron.session_turns import (  # cron 会话轮次相关辅助函数
    cron_run_id,  # 从元数据提取 cron 运行 ID
    cron_trigger,  # 从元数据提取 cron 触发信息
    defer_cron_until_session_idle,  # 判断该 cron 轮次是否应延迟到会话空闲
)


class CronTurnCoordinator:
    """调度型 cron 轮次协调器。

    职责与项目角色：
    - 接收来自 cron 调度器的轮次消息，按 run_id 注册等待 Future；
    - 根据 Agent 是否在运行，选择通过发布或直接派发方式投递轮次；
    - 在目标会话忙碌时，将轮次放入延迟队列，待会话空闲后顺序发布；
    - 提供 ``complete`` 由 Agent 在轮次完成后回填结果或异常。

    典型用法：cron 触发器调用 ``submit`` 提交轮次并 await 其结果；
    AgentLoop 在会话空闲时调用 ``publish_next_deferred`` 推进延迟队列。
    """

    def __init__(
        self,
        *,
        publish_inbound: Callable[[InboundMessage], Awaitable[None]],
        dispatch: Callable[[InboundMessage], Awaitable[object]],
        is_running: Callable[[], bool],
    ) -> None:
        self._publish_inbound = publish_inbound  # 发布入站消息的回调（Agent 运行中时使用）
        self._dispatch = dispatch  # 直接派发消息的回调（Agent 未运行时使用）
        self._is_running = is_running  # 判断 Agent 是否正在运行的回调
        self.deferred_queues: dict[str, list[InboundMessage]] = {}  # 各会话的延迟轮次队列：session_key -> 待发布消息列表
        self._waiters: dict[str, asyncio.Future[OutboundMessage | None]] = {}  # run_id -> 等待结果的 Future
        self._pending_messages_by_run_id: dict[str, InboundMessage] = {}  # run_id -> 正在处理中的消息，用于查询与去重

    async def submit(self, msg: InboundMessage) -> OutboundMessage | None:
        """提交一个调度型 cron 轮次，并等待其会话响应。

        参数:
            msg: 入站消息，其元数据需包含 run_id。

        返回:
            会话产生的出站消息；若出错则抛出对应异常。

        异常:
            ValueError: 元数据缺少 run_id；
            RuntimeError: 同一 run_id 已在等待中。
        """
        run_id = cron_run_id(msg.metadata)
        if not run_id:
            raise ValueError("cron turn metadata must include a run_id")
        if run_id in self._waiters:
            raise RuntimeError(f"cron run {run_id!r} is already pending")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[OutboundMessage | None] = loop.create_future()
        self._waiters[run_id] = future
        self._pending_messages_by_run_id[run_id] = msg
        try:
            # Agent 运行中走发布通道（进入会话消息流）；否则直接派发
            if self._is_running():
                await self._publish_inbound(msg)
            else:
                await self._dispatch(msg)
            return await future
        finally:
            self._waiters.pop(run_id, None)
            self._pending_messages_by_run_id.pop(run_id, None)

    def should_defer(
        self,
        msg: InboundMessage,
        *,
        session_key: str,
        active_session_keys: Iterable[str],
    ) -> bool:
        """判断该 cron 轮次是否应当被延迟。

        参数:
            msg: 入站消息；
            session_key: 目标会话 key；
            active_session_keys: 当前活跃会话 key 集合。

        返回:
            当元数据要求延迟且目标会话正活跃时返回 True。
        """
        return (
            defer_cron_until_session_idle(msg.metadata)
            and session_key in active_session_keys
        )

    def defer_if_active(
        self,
        msg: InboundMessage,
        *,
        session_key: str,
        active_session_keys: Iterable[str],
    ) -> bool:
        """当目标会话正活跃时，将 cron 轮次延迟。

        参数:
            msg: 入站消息；
            session_key: 目标会话 key；
            active_session_keys: 当前活跃会话 key 集合。

        返回:
            若已延迟则返回 True，否则 False（表示无需延迟）。
        """
        if not self.should_defer(
            msg,
            session_key=session_key,
            active_session_keys=active_session_keys,
        ):
            return False
        pending_msg = msg
        # 若目标会话与消息原 session_key 不同，构造带 override 的副本
        if session_key != msg.session_key:
            pending_msg = dataclasses.replace(
                msg,
                session_key_override=session_key,
            )
        self.defer(session_key, pending_msg)
        return True

    def complete(
        self,
        msg: InboundMessage,
        *,
        response: OutboundMessage | None = None,
        error: BaseException | None = None,
    ) -> None:
        """回填某个 cron 轮次的处理结果或异常。

        参数:
            msg: 已处理的入站消息；
            response: 处理产生的出站消息（成功时）；
            error: 处理过程中发生的异常（失败时）。
        """
        run_id = cron_run_id(msg.metadata)
        if not run_id:
            return
        future = self._waiters.get(run_id)
        # Future 不存在或已完成则忽略，避免重复设置
        if future is None or future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(response)

    def defer(self, session_key: str, msg: InboundMessage) -> None:
        """将消息追加到指定会话的延迟队列。"""
        self.deferred_queues.setdefault(session_key, []).append(msg)

    def pending_job_ids_for_session(self, session_key: str) -> set[str]:
        """返回在 *session_key* 中等待或正在运行的 cron 作业 ID 集合。

        参数:
            session_key: 目标会话 key。

        返回:
            该会话相关的 cron job_id 集合（无 job_id 的条目被忽略）。
        """
        job_ids: set[str] = set()
        # 延迟队列中的作业
        for msg in self.deferred_queues.get(session_key, []):
            job_id = _cron_job_id(msg)
            if job_id:
                job_ids.add(job_id)
        # 正在处理中的作业
        for msg in self._pending_messages_by_run_id.values():
            if msg.session_key != session_key:
                continue
            job_id = _cron_job_id(msg)
            if job_id:
                job_ids.add(job_id)
        return job_ids

    async def publish_next_deferred(self, session_key: str) -> None:
        """发布指定会话延迟队列中的下一条待执行轮次。

        参数:
            session_key: 目标会话 key。
        """
        queue = self.deferred_queues.get(session_key)
        if not queue:
            return
        msg = queue.pop(0)  # 取出队首
        if not queue:
            self.deferred_queues.pop(session_key, None)  # 队列空则清理
        await self._publish_inbound(msg)


def _cron_job_id(msg: InboundMessage) -> str | None:
    """从消息元数据中提取 cron 作业 ID。

    参数:
        msg: 入站消息。

    返回:
        有效的 job_id 字符串；若无触发信息或 job_id 非法则返回 None。
    """
    trigger = cron_trigger(msg.metadata)
    if not trigger:
        return None
    value = trigger.get("job_id")
    return value if isinstance(value, str) and value else None
