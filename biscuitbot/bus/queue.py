"""用于解耦渠道与智能体通信的异步消息队列。

所属模块与项目作用
===================
本文件位于 biscuitbot/bus 目录，提供消息总线的核心异步队列实现。
在项目架构中起到的作用：作为整个系统的异步通信枢纽，渠道将入站消息放入 inbound 队列，
智能体从中消费并处理，再将响应放入 outbound 队列供渠道消费，实现生产者与消费者解耦。
"""

import asyncio  # 提供异步队列与事件循环支持

from loguru import logger  # 队列积压告警日志

from biscuitbot.bus.events import InboundMessage, OutboundMessage  # 消息数据类

# 队列积压告警阈值：超过该数量记一次 warning（每方向节流，避免日志风暴）。
# 队列保持无界（消息不允许静默丢弃），但消费端卡死时需要有可观测信号。
_BACKLOG_WARN_THRESHOLD = 500


class MessageBus:
    """异步消息总线，解耦聊天渠道与智能体核心。

    渠道将消息推入 inbound 队列，智能体处理后把响应推入 outbound 队列。
    两个队列均为 asyncio.Queue，支持协程间安全的阻塞式消费。
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()  # 入站消息队列
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()  # 出站消息队列
        self._backlog_warned: set[str] = set()  # 已告警的方向（"inbound"/"outbound"），回落后重置

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """将来自渠道的消息发布给智能体（放入入站队列）。"""
        self._warn_if_backlogged("inbound", self.inbound.qsize())
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """消费下一条入站消息（在消息可用前会阻塞等待）。"""
        msg = await self.inbound.get()
        if self.inbound.qsize() < _BACKLOG_WARN_THRESHOLD:
            self._backlog_warned.discard("inbound")
        return msg

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """将智能体的响应发布给渠道（放入出站队列）。"""
        self._warn_if_backlogged("outbound", self.outbound.qsize())
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """消费下一条出站消息（在消息可用前会阻塞等待）。"""
        msg = await self.outbound.get()
        if self.outbound.qsize() < _BACKLOG_WARN_THRESHOLD:
            self._backlog_warned.discard("outbound")
        return msg

    def _warn_if_backlogged(self, direction: str, size: int) -> None:
        """队列积压超过阈值时按方向节流告警一次，回落后再重新武装。"""
        if size < _BACKLOG_WARN_THRESHOLD:
            return
        if direction in self._backlog_warned:
            return
        self._backlog_warned.add(direction)
        logger.warning(
            "MessageBus {} queue backlog reached {} messages — "
            "consumer may be stuck or slower than producers",
            direction,
            size,
        )

    @property
    def inbound_size(self) -> int:
        """当前入站队列中待处理消息的数量。"""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """当前出站队列中待处理消息的数量。"""
        return self.outbound.qsize()
