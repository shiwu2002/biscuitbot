"""用于解耦渠道与智能体通信的异步消息队列。

所属模块与项目作用
===================
本文件位于 biscuitbot/bus 目录，提供消息总线的核心异步队列实现。
在项目架构中起到的作用：作为整个系统的异步通信枢纽，渠道将入站消息放入 inbound 队列，
智能体从中消费并处理，再将响应放入 outbound 队列供渠道消费，实现生产者与消费者解耦。
"""

import asyncio  # 提供异步队列与事件循环支持

from biscuitbot.bus.events import InboundMessage, OutboundMessage  # 消息数据类


class MessageBus:
    """异步消息总线，解耦聊天渠道与智能体核心。

    渠道将消息推入 inbound 队列，智能体处理后把响应推入 outbound 队列。
    两个队列均为 asyncio.Queue，支持协程间安全的阻塞式消费。
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()  # 入站消息队列
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()  # 出站消息队列

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """将来自渠道的消息发布给智能体（放入入站队列）。"""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """消费下一条入站消息（在消息可用前会阻塞等待）。"""
        return await self.inbound.get()

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """将智能体的响应发布给渠道（放入出站队列）。"""
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """消费下一条出站消息（在消息可用前会阻塞等待）。"""
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """当前入站队列中待处理消息的数量。"""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """当前出站队列中待处理消息的数量。"""
        return self.outbound.qsize()
