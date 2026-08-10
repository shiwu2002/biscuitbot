"""聊天渠道基类接口。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的抽象基类组件。
在项目架构中起到的作用：定义所有聊天平台渠道（Telegram、Discord、飞书、钉钉等）必须
实现的统一接口（``start``/``stop``/``send`` 等），并内置权限校验、配对码下发、流式输出、
音频转写等通用能力。具体平台子类只需关注平台协议细节即可接入消息总线（MessageBus），
从而实现「一处实现，多平台复用」的插件化架构。
"""

from __future__ import annotations

from abc import ABC, abstractmethod  # ABC：抽象基类；abstractmethod：声明子类必须实现的方法
from pathlib import Path
from typing import Any

from loguru import logger  # 日志库，用于记录渠道运行日志

from biscuitbot.bus.events import InboundMessage, OutboundMessage  # 消息总线事件类型（入站/出站）
from biscuitbot.bus.queue import MessageBus  # 消息总线，渠道与核心之间的通信通道
from biscuitbot.pairing import (  # 配对码模块：未授权用户首次私聊时下发一次性配对码
    PAIRING_CODE_META_KEY,
    format_pairing_reply,
    generate_code,
    is_approved,
)


class BaseChannel(ABC):
    """聊天渠道抽象基类。

    每个具体渠道（Telegram、Discord 等）应实现该接口，以便接入 biscuitbot 的消息总线。
    子类通过实现 ``start``/``stop``/``send`` 等抽象方法完成平台对接，通用逻辑（权限校验、
    配对码、流式输出、音频转写）由本基类统一提供。
    """

    name: str = "base"  # 渠道唯一标识名（小写），用于配置、日志与配对存储
    display_name: str = "Base"  # 渠道展示名（用于 UI/日志展示）
    send_progress: bool = True  # 是否在工具调用过程中向用户发送进度提示
    send_tool_hints: bool = False  # 是否发送工具调用提示信息
    show_reasoning: bool = True  # 是否展示模型推理/思考过程

    def __init__(self, config: Any, bus: MessageBus | None):
        """初始化渠道实例。

        Args:
            config: 渠道专属配置（dict 或 pydantic 配置对象）。
            bus: 用于通信的消息总线；在独立登录流程（如扫码登录）中可为 None。
        """
        self.config = config
        self.logger = logger.bind(channel=self.name)  # 绑定渠道名的日志器，便于按渠道过滤
        self.bus = bus
        self._running = False  # 渠道运行状态标志

    async def transcribe_audio(self, file_path: str | Path) -> str:
        """通过 Whisper（OpenAI 或 Groq）转写音频文件，失败时返回空字符串。"""
        try:
            from biscuitbot.audio.transcription import (
                resolve_transcription_config,
                transcribe_audio_file,
            )
            from biscuitbot.config.loader import load_config

            return await transcribe_audio_file(file_path, resolve_transcription_config(load_config()))
        except Exception:
            self.logger.exception("Audio transcription failed")
            return ""

    async def login(self, force: bool = False) -> bool:
        """执行渠道专属的交互式登录（如扫码登录）。

        Args:
            force: 为 True 时忽略已有凭证，强制重新认证。

        若已认证或登录成功则返回 True。支持交互式登录的子类应覆盖此方法。
        """
        return True

    @abstractmethod
    async def start(self) -> None:
        """启动渠道并开始监听消息。

        该方法应为长时间运行的异步任务，完成以下工作：
        1. 连接到聊天平台；
        2. 监听入站消息；
        3. 通过 ``_handle_message()`` 将消息转发至消息总线。
        """
        pass

    @abstractmethod
    async def stop(self) -> None:
        """停止渠道并清理资源。"""
        pass

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """通过本渠道发送一条消息。

        Args:
            msg: 待发送的出站消息。

        实现方在投递失败时应抛出异常，以便渠道管理器统一应用重试策略。
        """
        pass

    async def send_delta(self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None) -> None:
        """投递一个流式文本片段。

        子类可覆盖以启用流式输出。实现方在投递失败时应抛出异常，以便渠道管理器重试。

        流式协议约定：``_stream_delta`` 表示一个片段，``_stream_end`` 表示当前片段结束；
        有状态的实现必须按 ``_stream_id``（而非仅按 ``chat_id``）为缓冲区建立索引。
        """
        pass

    async def send_reasoning_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """流式投递一段模型推理/思考内容。

        默认为空操作。具备原生「低强调」展示能力的渠道（Slack 上下文块、Telegram 可折叠
        引用块、Discord 子文本、WebUI 斜体气泡等）可覆盖此方法，将推理渲染为可就地更新的
        次级追踪轨迹，随模型思考过程实时刷新。

        流式协议与 :meth:`send_delta` 一致：``_reasoning_delta`` 为片段，``_reasoning_end``
        为当前推理段结束；有状态的实现应按 ``_stream_id`` 建立索引。
        """
        return

    async def send_reasoning_end(
        self, chat_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """标记一段推理流的结束。

        默认为空操作。对 ``send_reasoning_delta`` 片段进行缓冲以实现就地更新的渠道，可在此
        信号处刷新并冻结已渲染的内容；一次性投递的渠道可完全忽略此信号。
        """
        return

    async def send_file_edit_events(
        self,
        chat_id: str,
        edits: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """投递结构化的实时文件编辑事件。

        默认为空操作。具备富活动展示面的渠道可覆盖此方法，在不接收空文本消息的前提下
        渲染文件编辑进度。
        """
        return

    async def send_reasoning(self, msg: OutboundMessage) -> None:
        """投递一段完整的推理内容。

        默认实现复用流式方法对（delta/end），使插件只需覆盖 delta/end 方法即可。
        等价于一次包含完整内容的 delta 后紧跟一个 end 标记——从而为流式与一次性推理
        （如 DeepSeek-R1 最终响应中的 ``reasoning_content``）保留单一渲染路径。
        """
        if not msg.content:
            return
        meta = dict(msg.metadata or {})
        meta.setdefault("_reasoning_delta", True)
        await self.send_reasoning_delta(msg.chat_id, msg.content, meta)
        end_meta = dict(meta)
        end_meta.pop("_reasoning_delta", None)
        end_meta["_reasoning_end"] = True
        await self.send_reasoning_end(msg.chat_id, end_meta)

    @property
    def supports_streaming(self) -> bool:
        """当配置启用了流式输出且本子类实现了 send_delta 时返回 True。"""
        cfg = self.config
        streaming = cfg.get("streaming", False) if isinstance(cfg, dict) else getattr(cfg, "streaming", False)
        return bool(streaming) and type(self).send_delta is not BaseChannel.send_delta

    def is_allowed(self, sender_id: str) -> bool:
        """校验发送者权限，优先级依次为：通配符 > 白名单 > 配对存储 > 拒绝。"""
        if isinstance(self.config, dict):
            allow_list = self.config.get("allow_from") or self.config.get("allowFrom") or []
        else:
            allow_list = getattr(self.config, "allow_from", None) or []
        if "*" in allow_list:  # 通配符 "*" 表示允许所有人
            return True
        # allowFrom 中的条目是不透明令牌，必须精确匹配
        if str(sender_id) in allow_list:
            return True
        if is_approved(self.name, str(sender_id)):  # 已通过配对码授权的用户
            return True
        return False

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        is_dm: bool = False,
    ) -> None:
        """处理入站消息：校验权限，在私聊中下发配对码，或将消息转发至消息总线。"""
        if not self.is_allowed(sender_id):
            if is_dm:
                # 未授权的私聊用户：生成一次性配对码并回复
                code = generate_code(self.name, str(sender_id))
                await self.send(
                    OutboundMessage(
                        channel=self.name,
                        chat_id=str(chat_id),
                        content=format_pairing_reply(code),
                        metadata={PAIRING_CODE_META_KEY: code},
                    )
                )
                self.logger.info(
                    "Sent pairing code {} to sender {} in chat {}",
                    code, sender_id, chat_id,
                )
            else:
                # 群聊中未授权用户：仅记录警告，不下发配对码
                self.logger.warning(
                    "Access denied for sender {}. "
                    "Add them to allowFrom list in config to grant access.",
                    sender_id,
                )
            return

        meta = metadata or {}
        if self.supports_streaming:
            meta = {**meta, "_wants_stream": True}  # 标记希望以流式接收回复

        msg = InboundMessage(
            channel=self.name,
            sender_id=str(sender_id),
            chat_id=str(chat_id),
            content=content,
            media=media or [],
            metadata=meta,
            session_key_override=session_key,
        )

        await self.bus.publish_inbound(msg)  # 发布到消息总线，交由核心处理

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回用于 onboarding 的默认配置。插件可覆盖以自动填充 config.json。"""
        return {"enabled": False}

    @property
    def is_running(self) -> bool:
        """检查渠道是否正在运行。"""
        return self._running
