"""消息总线模块。

所属模块与项目作用
===================
本文件位于 biscuitbot/bus 目录，是 bus 消息总线模块的入口。
在项目架构中起到的作用：为聊天渠道（Telegram/Discord/Slack 等）与智能体核心之间
提供解耦的异步通信层。渠道通过消息总线推送入站消息，智能体处理后通过同一总线
发布出站消息，从而让渠道与智能体互不依赖实现。
"""

# 导入入站/出站消息数据类，作为总线流转的数据载体
from biscuitbot.bus.events import InboundMessage, OutboundMessage
# 导入核心异步消息总线实现
from biscuitbot.bus.queue import MessageBus

# 对外暴露的核心 API：消息总线与消息类型
__all__ = ["MessageBus", "InboundMessage", "OutboundMessage"]
