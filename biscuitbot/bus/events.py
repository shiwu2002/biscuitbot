"""消息总线事件类型。

所属模块与项目作用
===================
本文件位于 biscuitbot/bus 目录，定义消息总线流转的核心数据结构。
在项目架构中起到的作用：提供 InboundMessage（入站消息）与 OutboundMessage（出站消息）
两个数据类，以及渠道与智能体之间约定使用的元数据键，作为整个消息总线通信的统一契约。
"""

from dataclasses import dataclass, field  # 用于定义不可变/可变数据类
from datetime import datetime  # 用于消息时间戳
from typing import Any  # 用于元数据的任意类型标注

# ``OutboundMessage.metadata`` 中的可选键，用于承载结构化、与渠道无关的 UI 负载。
# 值需为 JSON 可序列化且至少包含 ``kind`` 字段；富客户端（如 WebUI）可渲染该内容，
# 其他渠道可忽略未知键。
OUTBOUND_META_AGENT_UI = "_agent_ui"

# 仅内部使用的入站元数据键，供进程内渠道请求智能体循环更新运行时状态，
# 而无需走完整的用户会话流程。
INBOUND_META_RUNTIME_CONTROL = "_runtime_control"
RUNTIME_CONTROL_ACK = "_ack"  # 运行时控制确认标识
RUNTIME_CONTROL_MCP_RELOAD = "mcp_reload"  # 请求重新加载 MCP 工具集


@dataclass
class InboundMessage:
    """从聊天渠道接收到的入站消息。

    封装来自 Telegram/Discord/Slack 等渠道的用户消息，包含发送者、内容、媒体
    以及渠道特有元数据，作为智能体处理流程的输入。
    """

    channel: str  # 渠道标识：telegram, discord, slack, whatsapp
    sender_id: str  # 发送者唯一标识
    chat_id: str  # 聊天/会话标识
    content: str  # 消息文本内容
    timestamp: datetime = field(default_factory=datetime.now)  # 消息时间戳，默认当前时间
    media: list[str] = field(default_factory=list)  # 媒体资源 URL 列表
    metadata: dict[str, Any] = field(default_factory=dict)  # 渠道特有数据
    session_key_override: str | None = None  # 可选的会话键覆盖，用于线程级会话隔离

    @property
    def session_key(self) -> str:
        """生成用于会话标识的唯一键。

        优先使用显式覆盖值，否则由渠道与聊天 ID 拼接而成。
        """
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """发送至聊天渠道的出站消息。

    ``metadata`` 可携带路由信息（``message_id`` 等）、追踪标记（``_progress``），
    以及可选的 ``OUTBOUND_META_AGENT_UI`` 富客户端负载；非 WebUI 渠道可忽略未知键。
    """

    channel: str  # 目标渠道
    chat_id: str  # 目标聊天/会话
    content: str  # 要发送的文本内容
    reply_to: str | None = None  # 可选的回复目标消息 ID
    media: list[str] = field(default_factory=list)  # 附带媒体资源 URL 列表
    metadata: dict[str, Any] = field(default_factory=dict)  # 路由、追踪等元数据
    buttons: list[list[str]] = field(default_factory=list)  # 可选的内联按钮布局
