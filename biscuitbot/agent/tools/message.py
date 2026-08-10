"""消息工具：向用户或频道主动发送消息。

所属模块与项目作用
==================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的主动消息投递组件。
在项目架构中起到的作用：让 agent 能够主动向用户/频道发送消息或文件附件，
支持跨渠道投递、提醒、生成图片的 artifact 附件投递等场景。与普通回复
（直接在当前对话中作答）不同，本工具用于显式的主动发送。
"""

from contextvars import ContextVar  # 上下文变量，用于在异步任务间隔离请求上下文
from pathlib import Path  # 路径处理
from typing import Any, Awaitable, Callable  # 任意类型、可等待对象与可调用对象类型

from loguru import logger  # 日志库

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.context import ContextAware, RequestContext  # 上下文感知混入与请求上下文
from biscuitbot.agent.tools.path_utils import resolve_workspace_path  # 工作区路径解析
from biscuitbot.agent.tools.schema import ArraySchema, StringSchema, tool_parameters_schema  # schema 构造器
from biscuitbot.security.workspace_access import current_tool_workspace  # 当前工具工作区访问
from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.config.paths import get_workspace_path  # 工作区路径获取函数


@tool_parameters(
    tool_parameters_schema(
        content=StringSchema(
            "Message content for proactive or cross-channel delivery. "
            "Do not use this for a normal reply in the current chat."
        ),
        channel=StringSchema(
            "Optional target channel for cross-channel/proactive delivery. "
            "Do not set this to the current runtime channel for a normal reply."
        ),
        chat_id=StringSchema(
            "Optional target chat/user ID for cross-channel/proactive delivery. "
            "On WebSocket/WebUI turns: omit chat_id to use the server's conversation id "
            "(never pass client_id values like anon-…). "
            "Do not set this to the current runtime chat for a normal reply."
        ),
        media=ArraySchema(
            StringSchema(""),
            description=(
                "Optional list of existing file paths to attach. "
                "Use artifact paths returned by generate_image here when delivering generated images."
            ),
        ),
        buttons=ArraySchema(
            ArraySchema(StringSchema("Button label")),
            description="Optional: inline keyboard buttons as list of rows, each row is list of button labels.",
        ),
        required=["content"],
    )
)
class MessageTool(Tool, ContextAware):
    """向聊天频道用户发送消息的工具。

    职责：将 agent 主动发送的消息（含可选附件/按钮）通过消息总线投递到
    指定渠道与聊天。支持跨渠道投递、提醒、生成图片 artifact 的附件投递。
    使用 ContextVar 在异步任务间隔离每轮的默认渠道、聊天 id、消息 id 等
    上下文，并追踪本轮是否已发送、已投递的附件路径等状态。
    """

    _capability = (
        "Proactively send messages or file attachments to users/channels "
        "(reminders, cross-channel delivery)."
    )
    _always_include = True  # 该工具的完整 schema 始终发送给模型
    _usage_md = "docs/message.md"  # 工具使用说明文档路径

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
        default_message_id: str | None = None,
        workspace: str | Path | None = None,
        restrict_to_workspace: bool = False,
    ):
        self._send_callback = send_callback  # 消息发送回调
        self._workspace = (
            Path(workspace).expanduser() if workspace is not None else get_workspace_path()
        )  # 工作区路径
        self._restrict_to_workspace = restrict_to_workspace  # 是否限制在工作区内
        # 以下 ContextVar 用于在异步任务间隔离每轮的默认上下文
        self._default_channel: ContextVar[str] = ContextVar(
            "message_default_channel", default=default_channel
        )  # 默认渠道
        self._default_chat_id: ContextVar[str] = ContextVar(
            "message_default_chat_id", default=default_chat_id
        )  # 默认聊天 id
        self._default_message_id: ContextVar[str | None] = ContextVar(
            "message_default_message_id",
            default=default_message_id,
        )  # 默认消息 id（用于回复场景）
        self._default_metadata: ContextVar[dict[str, Any]] = ContextVar(
            "message_default_metadata",
            default={},
        )  # 默认元数据
        self._sent_in_turn_var: ContextVar[bool] = ContextVar("message_sent_in_turn", default=False)  # 本轮是否已发送
        self._turn_delivered_media_var: ContextVar[tuple[str, ...]] = ContextVar(
            "message_turn_delivered_media",
            default=(),
        )  # 本轮已投递的附件路径
        self._record_channel_delivery_var: ContextVar[bool] = ContextVar(
            "message_record_channel_delivery",
            default=False,
        )  # 是否记录为主动渠道投递
        self._suppress_delivery_var: ContextVar[bool] = ContextVar(
            "message_suppress_delivery",
            default=False,
        )  # 是否抑制实际投递（心跳内部检查用）

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        send_callback = ctx.bus.publish_outbound if ctx.bus else None
        return cls(
            send_callback=send_callback,
            workspace=ctx.workspace,
            restrict_to_workspace=ctx.config.restrict_to_workspace,
        )

    def set_context(self, ctx: RequestContext) -> None:
        """设置当前消息上下文。

        参数:
            ctx: 当前轮次的请求上下文，包含渠道、聊天 id、消息 id 与元数据。
        """
        self._default_channel.set(ctx.channel)
        self._default_chat_id.set(ctx.chat_id)
        self._default_message_id.set(ctx.message_id)
        self._default_metadata.set(dict(ctx.metadata or {}))

    def set_send_callback(self, callback: Callable[[OutboundMessage], Awaitable[None]]) -> None:
        """设置消息发送回调。"""
        self._send_callback = callback

    def start_turn(self) -> None:
        """重置本轮发送追踪状态。"""
        self._sent_in_turn = False
        self._turn_delivered_media_var.set(())

    def turn_delivered_media_paths(self) -> list[str]:
        """返回本轮通过本工具投递到当前聊天的附件绝对路径列表。"""
        return list(self._turn_delivered_media_var.get())

    def set_record_channel_delivery(self, active: bool):
        """标记工具发送的消息为主动渠道投递。"""
        return self._record_channel_delivery_var.set(active)

    def reset_record_channel_delivery(self, token) -> None:
        """恢复之前的主动投递记录状态。"""
        self._record_channel_delivery_var.reset(token)

    def set_suppress_delivery(self, active: bool):
        """确认但不实际投递工具发送（心跳内部检查用）。"""
        return self._suppress_delivery_var.set(active)

    def reset_suppress_delivery(self, token) -> None:
        """恢复之前的投递抑制状态。"""
        self._suppress_delivery_var.reset(token)

    @property
    def _sent_in_turn(self) -> bool:
        """本轮是否已发送消息。"""
        return self._sent_in_turn_var.get()

    @_sent_in_turn.setter
    def _sent_in_turn(self, value: bool) -> None:
        self._sent_in_turn_var.set(value)

    @property
    def name(self) -> str:
        """工具名称。"""
        return "message"

    @property
    def description(self) -> str:
        """工具描述，指导模型何时及如何调用。"""
        return (
            "Proactively send a message to a user/channel, optionally with file attachments. "
            "Use this for reminders, cross-channel delivery, or explicit proactive sends. "
            "Do not use this for the normal reply in the current chat: answer naturally instead. "
            "If channel/chat_id would target the current runtime conversation, do not call this tool "
            "unless the user explicitly asked you to proactively send an existing file attachment. "
            "When generate_image creates images in the current chat, use the message tool "
            "with the artifact paths in the media parameter to deliver the images to the user. "
            "For proactive attachment delivery, use the 'media' parameter with file paths. "
            "Do NOT use read_file to send files — that only reads content for your own analysis."
        )

    def _resolve_media(self, media: list[str]) -> list[str]:
        """解析本地媒体附件路径，启用时强制工作区范围限制。

        参数:
            media: 媒体路径列表（可为 URL 或本地路径）。

        返回:
            解析后的路径列表。
        """
        resolved: list[str] = []
        access = current_tool_workspace(
            self._workspace,
            restrict_to_workspace=self._restrict_to_workspace,
        )
        workspace = access.project_path or self._workspace
        for p in media:
            # URL 直接保留
            if p.startswith(("http://", "https://")):
                resolved.append(p)
            elif not access.restrict_to_workspace:
                # 不限制工作区时，相对路径基于工作区解析
                path = Path(p).expanduser()
                resolved.append(p if path.is_absolute() else str(workspace / path))
            else:
                # 限制工作区时，强制校验路径在允许范围内
                resolved.append(str(resolve_workspace_path(p, workspace, access.allowed_root)))
        return resolved

    async def execute(
        self,
        content: str,
        channel: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        media: list[str] | None = None,
        buttons: list[list[str]] | None = None,
        **kwargs: Any,
    ) -> str:
        """执行消息发送。

        参数:
            content: 消息内容。
            channel: 目标渠道（可选，默认为当前渠道）。
            chat_id: 目标聊天/用户 id（可选，默认为当前聊天）。
            message_id: 关联的消息 id（可选，仅同目标时继承默认值）。
            media: 可选的附件路径列表。
            buttons: 可选的内联键盘按钮（按行分组）。

        返回:
            发送结果字符串；出错时返回错误信息。
        """
        from biscuitbot.utils.helpers import strip_think

        content = strip_think(content)  # 去除 think 标签内容

        # 校验 buttons 参数格式
        if buttons is not None:
            if not isinstance(buttons, list) or any(
                not isinstance(row, list) or any(not isinstance(label, str) for label in row)
                for row in buttons
            ):
                return "Error: buttons must be a list of list of strings"
        default_channel = self._default_channel.get()
        default_chat_id = self._default_chat_id.get()
        channel = channel or default_channel
        explicit_chat_id = chat_id
        # WebSocket 渠道校验：chat_id 必须匹配当前会话，client_id 非法
        if (
            default_channel == "websocket"
            and channel == "websocket"
            and explicit_chat_id is not None
            and str(explicit_chat_id).strip() != ""
            and str(explicit_chat_id).strip() != str(default_chat_id).strip()
        ):
            return (
                "Error: chat_id does not match the active WebSocket conversation. "
                "Omit chat_id (and usually channel) so delivery uses the current "
                "conversation id from context — WebSocket client_id strings "
                "(e.g. anon-…) are not chat ids."
            )
        chat_id = chat_id or default_chat_id
        # 仅当目标渠道+聊天与默认一致时才继承默认 message_id。
        # 跨聊天发送不能携带原 message_id，因为某些渠道（如飞书）会通过
        # Reply API 用 message_id 决定目标会话，会导致消息投递到错误的聊天。
        same_target = channel == default_channel and chat_id == default_chat_id
        if same_target:
            message_id = message_id or self._default_message_id.get()
        else:
            message_id = None

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"

        if not self._send_callback:
            return "Error: Message sending not configured"

        # 解析媒体附件路径
        if media:
            try:
                media = self._resolve_media(media)
            except (OSError, PermissionError, ValueError) as e:
                return f"Error: media path is not allowed: {str(e)}"

        # 构造元数据：同目标时继承默认元数据
        metadata = dict(self._default_metadata.get()) if same_target else {}
        if message_id:
            metadata["message_id"] = message_id
        # 主动投递或带附件时标记记录
        if self._record_channel_delivery_var.get() or media:
            metadata["_record_channel_delivery"] = True

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            media=media or [],
            buttons=buttons or [],
            metadata=metadata,
        )

        # 抑制投递模式：仅确认不实际发送（心跳内部检查）
        if self._suppress_delivery_var.get():
            logger.debug("MessageTool: delivery suppressed during internal check")
            return f"Message acknowledged for {channel}:{chat_id} (not delivered)"

        try:
            await self._send_callback(msg)
            # 同目标时记录本轮发送状态与附件路径
            if channel == default_channel and chat_id == default_chat_id:
                self._sent_in_turn = True
                if media:
                    prev = self._turn_delivered_media_var.get()
                    self._turn_delivered_media_var.set(prev + tuple(str(p) for p in media))
            media_info = f" with {len(media)} attachments" if media else ""
            button_info = f" with {sum(len(row) for row in buttons)} button(s)" if buttons else ""
            return f"Message sent to {channel}:{chat_id}{media_info}{button_info}"
        except Exception as e:
            return f"Error sending message: {str(e)}"
