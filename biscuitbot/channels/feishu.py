"""飞书（Feishu/Lark）渠道实现，基于 lark-oapi SDK 的 WebSocket 长连接。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的飞书平台组件。
在项目架构中起到的作用：将飞书/Lark 的消息收发能力接入 biscuitbot 消息总线，
使飞书用户可与机器人进行多模态对话。

平台特点与接入方式
------------------
- 接入方式：使用 WebSocket 长连接接收事件，无需公网 IP 或 Webhook 配置。
- 鉴权：通过飞书开放平台的 App ID 和 App Secret 进行身份认证。
- 事件订阅：需在开放平台启用 ``im.message.receive_v1`` 事件，以及消息读取、
  表情回应、机器人进群等可选事件。
- 消息格式：支持纯文本、富文本（post）、交互卡片（interactive）三种格式，
  并根据内容自动选择最优格式（``_detect_msg_format``）。
- 流式输出：通过 CardKit 流式卡片 API 实现增量更新，支持打字机效果。
- 媒体处理：支持图片、音频、文件的下载与上传，语音消息以 opus/ogg 格式存储。
- 群聊策略：支持 open（全部响应）和 mention（仅响应 @机器人）两种模式。
- 话题隔离：群聊中每个话题（thread）可拥有独立会话，实现上下文隔离。
- 域名切换：支持飞书（feishu 域名）和国际版 Lark（lark 域名）。
"""

from __future__ import annotations

import asyncio  # 异步事件循环与并发原语
import importlib.util  # 运行时检测 SDK 是否安装
import json  # JSON 序列化/反序列化（消息内容解析）
import os  # 文件路径与扩展名处理
import re  # 正则表达式（@占位符替换、Markdown 解析）
import threading  # WebSocket 客户端运行于独立线程
import time  # 重连退避与节流控制
import uuid  # 生成媒体文件唯一标识
from contextlib import suppress  # 上下文管理器，抑制指定异常
from dataclasses import dataclass, field  # 数据类装饰器与字段（流式缓冲区等）
from typing import TYPE_CHECKING, Any, Literal  # 类型注解支持

from pydantic import Field  # Pydantic 模型字段定义

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_media_dir  # 媒体文件目录
from biscuitbot.config.schema import Base  # 配置模型基类
from biscuitbot.utils.helpers import MessageIdDedup, safe_filename  # 消息去重与文件名安全化共享工具
from biscuitbot.utils.logging_bridge import redirect_lib_logging  # 第三方库日志桥接

if TYPE_CHECKING:
    from lark_oapi.api.im.v1.model import (  # 飞书事件类型（仅类型检查时导入）
        MentionEvent,
        P2ImMessageReceiveV1,
    )

FEISHU_AVAILABLE = importlib.util.find_spec("lark_oapi") is not None  # 检测 lark_oapi SDK 是否已安装


def _load_lark_runtime() -> tuple[Any, str, str]:
    """延迟导入重量级飞书 SDK。

    lark_oapi 在模块导入时会加载大量生成的 API 接口，因此将其排除在
    渠道发现和构造函数路径之外，仅在真正启动时才导入。
    """
    import sys

    ws_client_already_imported = "lark_oapi.ws.client" in sys.modules
    import lark_oapi as lark
    import lark_oapi.ws.client as lark_ws_client
    from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN

    if (
        not ws_client_already_imported
        and threading.current_thread() is not threading.main_thread()
    ):
        import_loop = getattr(lark_ws_client, "loop", None)
        if (
            import_loop is not None
            and not import_loop.is_running()
            and not import_loop.is_closed()
        ):
            import_loop.close()
        lark_ws_client.loop = None
        with suppress(Exception):
            asyncio.set_event_loop(None)

    return lark, FEISHU_DOMAIN, LARK_DOMAIN

# 消息类型到展示文本的映射（非文本消息在入站时转换为占位符文本）
MSG_TYPE_MAP = {
    "image": "[image]",
    "audio": "[audio]",
    "file": "[file]",
    "sticker": "[sticker]",
}


def _extract_share_card_content(content_json: dict, msg_type: str) -> str:
    """从分享卡片和交互消息中提取文本表示。"""
    parts = []

    if msg_type == "share_chat":
        parts.append(f"[shared chat: {content_json.get('chat_id', '')}]")
    elif msg_type == "share_user":
        parts.append(f"[shared user: {content_json.get('user_id', '')}]")
    elif msg_type == "interactive":
        parts.extend(_extract_interactive_content(content_json))
    elif msg_type == "share_calendar_event":
        parts.append(f"[shared calendar event: {content_json.get('event_key', '')}]")
    elif msg_type == "system":
        parts.append("[system message]")
    elif msg_type == "merge_forward":
        parts.append("[merged forward messages]")

    return "\n".join(parts) if parts else f"[{msg_type}]"


def _extract_interactive_content(content: dict) -> list[str]:
    """递归地从交互卡片内容中提取文本和链接。"""
    parts = []

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return [content] if content.strip() else []

    if not isinstance(content, dict):
        return parts

    if "title" in content:
        title = content["title"]
        if isinstance(title, dict):
            title_content = title.get("content", "") or title.get("text", "")
            if title_content:
                parts.append(f"title: {title_content}")
        elif isinstance(title, str):
            parts.append(f"title: {title}")

    for elements in (
        content.get("elements", []) if isinstance(content.get("elements"), list) else []
    ):
        for element in elements:
            parts.extend(_extract_element_content(element))

    card = content.get("card", {})
    if card:
        parts.extend(_extract_interactive_content(card))

    header = content.get("header", {})
    if header:
        header_title = header.get("title", {})
        if isinstance(header_title, dict):
            header_text = header_title.get("content", "") or header_title.get("text", "")
            if header_text:
                parts.append(f"title: {header_text}")

    return parts


def _extract_element_content(element: dict) -> list[str]:
    """从单个卡片元素中提取内容。"""
    parts = []

    if not isinstance(element, dict):
        return parts

    tag = element.get("tag", "")

    if tag in ("markdown", "lark_md"):
        content = element.get("content", "")
        if content:
            parts.append(content)

    elif tag == "div":
        text = element.get("text", {})
        if isinstance(text, dict):
            text_content = text.get("content", "") or text.get("text", "")
            if text_content:
                parts.append(text_content)
        elif isinstance(text, str):
            parts.append(text)
        for field in element.get("fields", []):
            if isinstance(field, dict):
                field_text = field.get("text", {})
                if isinstance(field_text, dict):
                    c = field_text.get("content", "")
                    if c:
                        parts.append(c)

    elif tag == "a":
        href = element.get("href", "")
        text = element.get("text", "")
        if href:
            parts.append(f"link: {href}")
        if text:
            parts.append(text)

    elif tag == "button":
        text = element.get("text", {})
        if isinstance(text, dict):
            c = text.get("content", "")
            if c:
                parts.append(c)
        url = element.get("url", "") or element.get("multi_url", {}).get("url", "")
        if url:
            parts.append(f"link: {url}")

    elif tag == "img":
        alt = element.get("alt", {})
        parts.append(alt.get("content", "[image]") if isinstance(alt, dict) else "[image]")

    elif tag == "note":
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    elif tag == "column_set":
        for col in element.get("columns", []):
            for ce in col.get("elements", []):
                parts.extend(_extract_element_content(ce))

    elif tag == "plain_text":
        content = element.get("content", "")
        if content:
            parts.append(content)

    else:
        for ne in element.get("elements", []):
            parts.extend(_extract_element_content(ne))

    return parts


def _extract_post_content(content_json: dict) -> tuple[str, list[str]]:
    """从飞书富文本（post）消息中提取文本和图片 key。

    处理三种载荷格式：
    - 直接格式：  {"title": "...", "content": [[...]]}
    - 本地化格式：{"zh_cn": {"title": "...", "content": [...]}}
    - 包装格式：  {"post": {"zh_cn": {"title": "...", "content": [...]}}}
    """

    def _parse_block(block: dict) -> tuple[str | None, list[str]]:
        if not isinstance(block, dict) or not isinstance(block.get("content"), list):
            return None, []
        texts, images = [], []
        if title := block.get("title"):
            texts.append(title)
        for row in block["content"]:
            if not isinstance(row, list):
                continue
            for el in row:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a"):
                    texts.append(el.get("text", ""))
                elif tag == "at":
                    texts.append(f"@{el.get('user_name', 'user')}")
                elif tag == "code_block":
                    lang = el.get("language", "")
                    code_text = el.get("text", "")
                    texts.append(f"\n```{lang}\n{code_text}\n```\n")
                elif tag == "img" and (key := el.get("image_key")):
                    images.append(key)
        return (" ".join(texts).strip() or None), images

    # Unwrap optional {"post": ...} envelope
    root = content_json
    if isinstance(root, dict) and isinstance(root.get("post"), dict):
        root = root["post"]
    if not isinstance(root, dict):
        return "", []

    # Direct format
    if "content" in root:
        text, imgs = _parse_block(root)
        if text or imgs:
            return text or "", imgs

    # Localized: prefer known locales, then fall back to any dict child
    for key in ("zh_cn", "en_us", "ja_jp"):
        if key in root:
            text, imgs = _parse_block(root[key])
            if text or imgs:
                return text or "", imgs
    for val in root.values():
        if isinstance(val, dict):
            text, imgs = _parse_block(val)
            if text or imgs:
                return text or "", imgs

    return "", []


def _extract_post_text(content_json: dict) -> str:
    """从飞书富文本（post）消息内容中提取纯文本。

    _extract_post_content 的遗留包装函数，仅返回文本部分。
    """
    text, _ = _extract_post_content(content_json)
    return text


class FeishuConfig(Base):
    """飞书/Lark 渠道配置（WebSocket 长连接模式）。"""

    enabled: bool = False
    app_id: str = ""  # 飞书开放平台应用 ID
    app_secret: str = ""  # 飞书开放平台应用密钥
    encrypt_key: str = ""  # 事件加密密钥（开放平台配置）
    verification_token: str = ""  # 事件验证令牌（开放平台配置）
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单（open_id）
    allow_all: bool = False  # 静默放行：为 True 时所有用户可直接私聊，无需白名单或配对码
    react_emoji: str = "THUMBSUP"  # 处理中使用的表情回应（默认点赞）
    done_emoji: str | None = None  # 任务完成时显示的表情（如 "DONE"、"OK"）
    tool_hint_prefix: str = "\U0001f527"  # 内联工具提示前缀（默认：🔧）
    group_policy: Literal["open", "mention"] = "mention"  # 群聊策略：open=全部响应，mention=仅@时响应
    reply_to_message: bool = False  # 是否引用回复用户原始消息
    streaming: bool = True  # 是否启用流式输出
    domain: Literal["feishu", "lark"] = "feishu"  # 域名选择：feishu=国内，lark=国际版
    topic_isolation: bool = True  # 群聊话题隔离：每个 topic 拥有独立会话


_STREAM_ELEMENT_ID = "streaming_md"  # 流式卡片中 Markdown 元素的 ID


@dataclass
class _FeishuStreamBuf:
    """基于 CardKit 流式 API 的每会话流式累加缓冲区。"""

    text: str = ""  # 已累加的文本内容
    card_id: str | None = None  # 流式卡片的 ID
    sequence: int = 0  # 更新序列号
    last_edit: float = 0.0  # 上次编辑时间戳（用于节流）
    created: float = field(default_factory=time.time)  # 创建时间戳（用于清理异常中断遗留的陈旧缓冲，防泄漏）


class FeishuChannel(BaseChannel):
    """飞书/Lark 渠道，使用 WebSocket 长连接。

    通过 WebSocket 接收事件 —— 无需公网 IP 或 Webhook。

    前置条件：
    - 飞书开放平台的 App ID 和 App Secret
    - 已启用机器人能力
    - 已订阅事件（im.message.receive_v1）
    """

    name = "feishu"
    display_name = "飞书"
    requires_module = "lark_oapi"  # 必需 SDK 模块（缺失时自动安装）
    pip_requires = ["lark-oapi>=1.5.0"]

    _STREAM_EDIT_INTERVAL = 0.5  # CardKit 流式更新之间的节流间隔（秒）
    _STREAM_BUF_TTL_S = 24 * 60 * 60  # 流式缓冲区最长保留时间（流异常中断时防止 _stream_bufs 泄漏）

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return FeishuConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = FeishuConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: FeishuConfig = config
        self._client: Any = None  # lark-oapi 客户端（用于发送消息）
        self._ws_client: Any = None  # WebSocket 客户端（用于接收事件）
        self._ws_thread: threading.Thread | None = None  # WebSocket 运行线程
        self._processed_message_ids = MessageIdDedup(maxlen=1000)  # 有界去重缓存（消息 ID）
        self._loop: asyncio.AbstractEventLoop | None = None  # 主事件循环引用
        self._stream_bufs: dict[str, _FeishuStreamBuf] = {}  # 各会话的流式缓冲区
        self._bot_open_id: str | None = None  # 机器人自身的 open_id（用于 @匹配）
        self._background_tasks: set[asyncio.Task] = set()  # 后台任务集合（如表情回应）
        self._reaction_ids: dict[str, str] = {}  # message_id → reaction_id 映射（用于清理表情）

    @staticmethod
    def _register_optional_event(builder: Any, method_name: str, handler: Any) -> Any:
        """仅在 SDK 支持时注册事件处理器（兼容不同 SDK 版本）。"""
        method = getattr(builder, method_name, None)
        return method(handler) if callable(method) else builder

    async def start(self) -> None:
        """启动飞书机器人，建立 WebSocket 长连接。"""
        if not FEISHU_AVAILABLE:
            self.logger.error("SDK not installed. Run: pip install lark-oapi")
            return

        if not self.config.app_id or not self.config.app_secret:
            self.logger.error("app_id and app_secret not configured")
            return

        lark, feishu_domain, lark_domain = await asyncio.to_thread(_load_lark_runtime)

        redirect_lib_logging("Lark")

        self._running = True
        self._loop = asyncio.get_running_loop()

        # Create Lark client for sending messages
        domain = lark_domain if self.config.domain == "lark" else feishu_domain
        self._client = (
            lark.Client.builder()
            .app_id(self.config.app_id)
            .app_secret(self.config.app_secret)
            .domain(domain)
            .log_level(lark.LogLevel.INFO)
            .build()
        )
        builder = lark.EventDispatcherHandler.builder(
            self.config.encrypt_key or "",
            self.config.verification_token or "",
        ).register_p2_im_message_receive_v1(self._on_message_sync)
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_created_v1", self._on_reaction_created
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_reaction_deleted_v1", self._on_reaction_deleted
        )
        builder = self._register_optional_event(
            builder, "register_p2_im_message_message_read_v1", self._on_message_read
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            self._on_bot_p2p_chat_entered,
        )
        # Silence "processor not found" errors when bots are added/removed from groups.
        # These events carry no actionable data for the agent.
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_added_v1",
            lambda _: None,
        )
        builder = self._register_optional_event(
            builder,
            "register_p2_im_chat_member_bot_deleted_v1",
            lambda _: None,
        )
        event_handler = builder.build()

        # Create WebSocket client for long connection
        self._ws_client = lark.ws.Client(
            self.config.app_id,
            self.config.app_secret,
            domain=domain,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        # Start WebSocket client in a separate thread with reconnect loop.
        # A dedicated event loop is created for this thread so that lark_oapi's
        # module-level `loop = asyncio.get_event_loop()` picks up an idle loop
        # instead of the already-running main asyncio loop, which would cause
        # "This event loop is already running" errors.
        def run_ws():
            import time

            import lark_oapi.ws.client as _lark_ws_client

            previous_loop = getattr(_lark_ws_client, "loop", None)
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            # Patch the module-level loop used by lark's ws Client.start()
            _lark_ws_client.loop = ws_loop
            try:
                while self._running:
                    try:
                        self._ws_client.start()
                    except Exception as e:
                        self.logger.warning("WebSocket error: {}", e)
                    if self._running:
                        time.sleep(5)
            finally:
                if getattr(_lark_ws_client, "loop", None) is ws_loop:
                    _lark_ws_client.loop = previous_loop
                with suppress(Exception):
                    asyncio.set_event_loop(None)
                ws_loop.close()

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        # Fetch bot's own open_id for accurate @mention matching
        self._bot_open_id = await asyncio.get_running_loop().run_in_executor(
            None, self._fetch_bot_open_id
        )
        if self._bot_open_id:
            self.logger.info("bot open_id: {}", self._bot_open_id)
        else:
            self.logger.warning("Could not fetch bot open_id; @mention matching may be inaccurate")

        self.logger.info("bot started with WebSocket long connection")
        self.logger.info("No public IP required - using WebSocket to receive events")

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止飞书机器人。

        注意：lark.ws.Client 未暴露 stop 方法，直接退出程序即可关闭客户端。

        参考：https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py#L86
        """
        self._running = False
        self.logger.info("bot stopped")

    def _fetch_bot_open_id(self) -> str | None:
        """通过 GET /open-apis/bot/v3/info 获取机器人自身的 open_id。"""
        try:
            import lark_oapi as lark

            request = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.APP})
                .build()
            )
            response = self._client.request(request)
            if response.success():
                import json

                data = json.loads(response.raw.content)
                bot = (data.get("data") or data).get("bot") or data.get("bot") or {}
                return bot.get("open_id")
            self.logger.warning("Failed to get bot info: code={}, msg={}", response.code, response.msg)
            return None
        except Exception as e:
            self.logger.warning("Error fetching bot info: {}", e)
            return None

    @staticmethod
    def _resolve_mentions(text: str, mentions: list[MentionEvent] | None) -> str:
        """将 @_user_n 占位符替换为实际的 @用户信息。

        Args:
            text: 包含 @_user_n 占位符的消息文本
            mentions: 飞书消息中的 mention 对象列表

        Returns:
            占位符被替换为 @姓名 (open_id) 格式的文本
        """
        if not mentions or not text:
            return text

        for mention in mentions:
            key = mention.key or None
            if not key:
                continue
            # Feishu placeholders are numbered keys like @_user_1. Keep
            # punctuation-adjacent mentions valid without matching @_user_10.
            pattern = rf"{re.escape(key)}(?![A-Za-z0-9_])"
            if not re.search(pattern, text):
                continue

            user_id_obj = mention.id or None
            if not user_id_obj:
                continue

            open_id = user_id_obj.open_id
            user_id = user_id_obj.user_id
            name = mention.name or key

            # Format: @姓名 (open_id, user_id: xxx)
            if open_id and user_id:
                replacement = f"@{name} ({open_id}, user id: {user_id})"
            elif open_id:
                replacement = f"@{name} ({open_id})"
            else:
                replacement = f"@{name}"

            text = re.sub(pattern, replacement, text)

        return text

    def _is_bot_mention_event(self, mention: Any) -> bool:
        """判断该 mention 是否指向本机器人。"""
        mid = getattr(mention, "id", None)
        if not mid:
            return False

        mention_open_id = getattr(mid, "open_id", None) or ""
        bot_open_id = getattr(self, "_bot_open_id", None) or ""
        if bot_open_id:
            return mention_open_id == bot_open_id

        # Fallback heuristic when bot open_id is unavailable.
        return not getattr(mid, "user_id", None) and mention_open_id.startswith("ou_")

    def _strip_leading_bot_mention(
        self, text: str, mentions: list[MentionEvent] | None
    ) -> str:
        """在斜杠命令路由前，移除消息开头的必要 @机器人 提及。"""
        if not mentions or not text:
            return text

        candidate = text.lstrip()
        for mention in mentions:
            key = getattr(mention, "key", None) or ""
            if not key or not re.match(rf"{re.escape(key)}(?![A-Za-z0-9_])", candidate):
                continue
            if not self._is_bot_mention_event(mention):
                continue

            stripped = candidate[len(key) :].strip()
            return stripped or text

        return text

    def _is_bot_mentioned(self, message: Any) -> bool:
        """检查消息中是否 @了本机器人。"""
        raw_content = message.content or ""
        if "@_all" in raw_content:
            return True

        for mention in getattr(message, "mentions", None) or []:
            if self._is_bot_mention_event(mention):
                return True
        return False

    def _is_group_message_for_bot(self, message: Any) -> bool:
        """判断群消息是否应响应：open 策略全部响应，mention 策略仅响应 @机器人。"""
        if self.config.group_policy == "open":
            return True
        return self._is_bot_mentioned(message)

    def _add_reaction_sync(self, message_id: str, emoji_type: str) -> str | None:
        """添加表情回应的同步辅助方法（在线程池中运行）。"""
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        try:
            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                    .build()
                )
                .build()
            )

            response = self._client.im.v1.message_reaction.create(request)

            if not response.success():
                self.logger.warning(
                    "Failed to add reaction: code={}, msg={}", response.code, response.msg
                )
                return None
            else:
                self.logger.debug("Added {} reaction to message {}", emoji_type, message_id)
                return response.data.reaction_id if response.data else None
        except Exception as e:
            self.logger.warning("Error adding reaction: {}", e)
            return None

    async def _add_reaction(self, message_id: str, emoji_type: str = "THUMBSUP") -> str | None:
        """为消息添加表情回应。

        成功时返回 reaction_id，失败时返回 None。
        当通过追踪的后台任务调用时，返回的 reaction_id 会存储在
        ``_reaction_ids`` 中，供后续 ``send_delta`` 清理使用。

        常用表情类型：THUMBSUP、OK、EYES、DONE、OnIt、HEART
        """
        if not self._client:
            return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._add_reaction_sync, message_id, emoji_type)

    def _remove_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        """移除表情回应的同步辅助方法（在线程池中运行）。"""
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        try:
            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )

            response = self._client.im.v1.message_reaction.delete(request)
            if response.success():
                self.logger.debug("Removed reaction {} from message {}", reaction_id, message_id)
            else:
                self.logger.debug(
                    "Failed to remove reaction: code={}, msg={}", response.code, response.msg
                )
        except Exception as e:
            self.logger.debug("Error removing reaction: {}", e)

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        """从消息移除表情回应（非阻塞）。

        用于在机器人回复后清除"处理中"指示器。
        """
        if not self._client or not reaction_id:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._remove_reaction_sync, message_id, reaction_id)

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """回调：从追踪集合中移除任务并记录未处理异常。"""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self.logger.warning("Background task failed: {}", exc)

    def _on_reaction_added(self, message_id: str, task: asyncio.Task) -> None:
        """回调：后台添加表情回应完成后，存储 reaction_id。"""
        if task.cancelled():
            return
        # Failures already logged by _on_background_task_done.
        with suppress(Exception):
            reaction_id = task.result()
            if reaction_id:
                self._reaction_ids[message_id] = reaction_id
        # Trim cache to prevent unbounded growth
        if len(self._reaction_ids) > 500:
            self._reaction_ids.pop(next(iter(self._reaction_ids)))

    @staticmethod
    def _stream_key(chat_id: str, metadata: dict[str, Any] | None = None) -> str:
        """将流式缓冲区作用域限定到入站消息（优先使用 message_id）。"""
        meta = metadata or {}
        return meta.get("message_id") or chat_id

    def _prune_stale_stream_bufs(self) -> None:
        """清理超过 TTL 的陈旧流式缓冲区。

        _stream_bufs 条目通常仅在 _stream_end 时移除；流异常中断时该信号
        不会到达，导致条目永久滞留。因此在创建新缓冲区时顺带清理。
        """
        cutoff = time.time() - self._STREAM_BUF_TTL_S
        for key in [k for k, buf in self._stream_bufs.items() if buf.created < cutoff]:
            self._stream_bufs.pop(key, None)

    # 匹配 Markdown 表格的正则（表头 + 分隔行 + 数据行）
    _TABLE_RE = re.compile(
        r"((?:^[ \t]*\|.+\|[ \t]*\n)(?:^[ \t]*\|[-:\s|]+\|[ \t]*\n)(?:^[ \t]*\|.+\|[ \t]*\n?)+)",
        re.MULTILINE,
    )

    _HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)  # Markdown 标题正则

    _CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```)", re.MULTILINE)  # 代码块正则

    # 需要从表格单元格、标题等纯文本展示区域剥离的 Markdown 格式标记
    _MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")  # **粗体**
    _MD_BOLD_UNDERSCORE_RE = re.compile(r"__(.+?)__")  # __粗体__
    _MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")  # *斜体*
    _MD_STRIKE_RE = re.compile(r"~~(.+?)~~")  # ~~删除线~~

    @classmethod
    def _strip_md_formatting(cls, text: str) -> str:
        """从文本中剥离 Markdown 格式标记，用于纯文本展示。

        飞书表格单元格不支持 Markdown 渲染，因此移除格式标记以保持可读性。
        """
        # Remove bold markers
        text = cls._MD_BOLD_RE.sub(r"\1", text)
        text = cls._MD_BOLD_UNDERSCORE_RE.sub(r"\1", text)
        # Remove italic markers
        text = cls._MD_ITALIC_RE.sub(r"\1", text)
        # Remove strikethrough markers
        text = cls._MD_STRIKE_RE.sub(r"\1", text)
        return text

    @classmethod
    def _parse_md_table(cls, table_text: str) -> dict | None:
        """将 Markdown 表格解析为飞书表格元素。"""
        lines = [_line.strip() for _line in table_text.strip().split("\n") if _line.strip()]
        if len(lines) < 3:
            return None

        def split(_line: str) -> list[str]:
            return [c.strip() for c in _line.strip("|").split("|")]

        headers = [cls._strip_md_formatting(h) for h in split(lines[0])]
        rows = [[cls._strip_md_formatting(c) for c in split(_line)] for _line in lines[2:]]
        columns = [
            {"tag": "column", "name": f"c{i}", "display_name": h, "width": "auto"}
            for i, h in enumerate(headers)
        ]
        return {
            "tag": "table",
            "page_size": len(rows) + 1,
            "columns": columns,
            "rows": [
                {f"c{i}": r[i] if i < len(r) else "" for i in range(len(headers))} for r in rows
            ],
        }

    def _build_card_elements(self, content: str) -> list[dict]:
        """将内容拆分为 div/markdown + 表格元素，用于飞书卡片渲染。"""
        elements, last_end = [], 0
        for m in self._TABLE_RE.finditer(content):
            before = content[last_end : m.start()]
            if before.strip():
                elements.extend(self._split_headings(before))
            elements.append(
                self._parse_md_table(m.group(1)) or {"tag": "markdown", "content": m.group(1)}
            )
            last_end = m.end()
        remaining = content[last_end:]
        if remaining.strip():
            elements.extend(self._split_headings(remaining))
        return elements or [{"tag": "markdown", "content": content}]

    @staticmethod
    def _split_elements_by_table_limit(
        elements: list[dict], max_tables: int = 1
    ) -> list[list[dict]]:
        """将卡片元素按表格数量上限分组，每组最多 *max_tables* 个表格元素。

        飞书卡片有每张卡片仅允许一个表格的硬限制（API 错误码 11310）。
        当渲染内容包含多个 Markdown 表格时，每个表格会被放入单独的
        卡片消息中，以确保所有表格都能送达用户。
        """
        if not elements:
            return [[]]
        groups: list[list[dict]] = []
        current: list[dict] = []
        table_count = 0
        for el in elements:
            if el.get("tag") == "table":
                if table_count >= max_tables:
                    if current:
                        groups.append(current)
                    current = []
                    table_count = 0
                current.append(el)
                table_count += 1
            else:
                current.append(el)
        if current:
            groups.append(current)
        return groups or [[]]

    def _split_headings(self, content: str) -> list[dict]:
        """按标题拆分内容，将标题转换为 div 元素。"""
        protected = content
        code_blocks = []
        for m in self._CODE_BLOCK_RE.finditer(content):
            code_blocks.append(m.group(1))
            protected = protected.replace(m.group(1), f"\x00CODE{len(code_blocks) - 1}\x00", 1)

        elements = []
        last_end = 0
        for m in self._HEADING_RE.finditer(protected):
            before = protected[last_end : m.start()].strip()
            if before:
                elements.append({"tag": "markdown", "content": before})
            text = self._strip_md_formatting(m.group(2).strip())
            display_text = f"**{text}**" if text else ""
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": display_text,
                    },
                }
            )
            last_end = m.end()
        remaining = protected[last_end:].strip()
        if remaining:
            elements.append({"tag": "markdown", "content": remaining})

        for i, cb in enumerate(code_blocks):
            for el in elements:
                if el.get("tag") == "markdown":
                    el["content"] = el["content"].replace(f"\x00CODE{i}\x00", cb)

        return elements or [{"tag": "markdown", "content": content}]

    # ── 智能格式检测 ──────────────────────────────────────────
    # 指示"复杂" Markdown（需要卡片渲染）的正则模式
    _COMPLEX_MD_RE = re.compile(
        r"```"  # 围栏代码块
        r"|^\|.+\|.*\n\s*\|[-:\s|]+\|"  # Markdown 表格（表头 + 分隔行）
        r"|^#{1,6}\s+",  # 标题
        re.MULTILINE,
    )

    # 简单 Markdown 模式（粗体、斜体、删除线）
    _SIMPLE_MD_RE = re.compile(
        r"\*\*.+?\*\*"  # **粗体**
        r"|__.+?__"  # __粗体__
        r"|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"  # *斜体*（单星号）
        r"|~~.+?~~",  # ~~删除线~~
        re.DOTALL,
    )

    # Markdown 链接：[文本](URL)
    _MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")

    # 无序列表项
    _LIST_RE = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)

    # 有序列表项
    _OLIST_RE = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)

    # 纯文本格式的最大长度
    _TEXT_MAX_LEN = 200

    # 富文本（post）格式的最大长度；超过此长度则使用卡片
    _POST_MAX_LEN = 2000

    @classmethod
    def _detect_msg_format(cls, content: str) -> str:
        """为 *content* 确定最优的飞书消息格式。

        返回以下之一：
        - ``"text"``        – 纯文本，短且无 Markdown
        - ``"post"``        – 富文本（仅链接，中等长度）
        - ``"interactive"`` – 卡片，完整 Markdown 渲染
        """
        stripped = content.strip()

        # Complex markdown (code blocks, tables, headings) → always card
        if cls._COMPLEX_MD_RE.search(stripped):
            return "interactive"

        # Long content → card (better readability with card layout)
        if len(stripped) > cls._POST_MAX_LEN:
            return "interactive"

        # Has bold/italic/strikethrough → card (post format can't render these)
        if cls._SIMPLE_MD_RE.search(stripped):
            return "interactive"

        # Has list items → card (post format can't render list bullets well)
        if cls._LIST_RE.search(stripped) or cls._OLIST_RE.search(stripped):
            return "interactive"

        # Has links → post format (supports <a> tags)
        if cls._MD_LINK_RE.search(stripped):
            return "post"

        # Short plain text → text format
        if len(stripped) <= cls._TEXT_MAX_LEN:
            return "text"

        # Medium plain text without any formatting → post format
        return "post"

    @classmethod
    def _markdown_to_post(cls, content: str) -> str:
        """将 Markdown 内容转换为飞书富文本（post）消息 JSON。

        将链接 ``[文本](URL)`` 处理为 ``a`` 标签，其余内容作为 ``text`` 标签。
        每一行成为富文本正文中的一个段落（行）。
        """
        lines = content.strip().split("\n")
        paragraphs: list[list[dict]] = []

        for line in lines:
            elements: list[dict] = []
            last_end = 0

            for m in cls._MD_LINK_RE.finditer(line):
                # Text before this link
                before = line[last_end : m.start()]
                if before:
                    elements.append({"tag": "text", "text": before})
                elements.append(
                    {
                        "tag": "a",
                        "text": m.group(1),
                        "href": m.group(2),
                    }
                )
                last_end = m.end()

            # Remaining text after last link
            remaining = line[last_end:]
            if remaining:
                elements.append({"tag": "text", "text": remaining})

            # Empty line → empty paragraph for spacing
            if not elements:
                elements.append({"tag": "text", "text": ""})

            paragraphs.append(elements)

        post_body = {
            "zh_cn": {
                "content": paragraphs,
            }
        }
        return json.dumps(post_body, ensure_ascii=False)

    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif"}  # 图片扩展名集合
    _AUDIO_EXTS = {".opus"}  # 音频扩展名集合
    _VIDEO_EXTS = {".mp4", ".mov", ".avi"}  # 视频扩展名集合
    _FILE_TYPE_MAP = {  # 扩展名到飞书文件类型的映射
        ".opus": "opus",
        ".mp4": "mp4",
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "doc",
        ".xls": "xls",
        ".xlsx": "xls",
        ".ppt": "ppt",
        ".pptx": "ppt",
    }

    def _upload_image_sync(self, file_path: str) -> str | None:
        """上传图片到飞书并返回 image_key。"""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateImageRequest.builder()
                    .request_body(
                        CreateImageRequestBody.builder().image_type("message").image(f).build()
                    )
                    .build()
                )
                response = self._client.im.v1.image.create(request)
                if response.success():
                    image_key = response.data.image_key
                    self.logger.debug("Uploaded image {}: {}", os.path.basename(file_path), image_key)
                    return image_key
                else:
                    self.logger.error(
                        "Failed to upload image: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            self.logger.exception("Error uploading image {}", file_path)
            return None

    def _upload_file_sync(self, file_path: str) -> str | None:
        """上传文件到飞书并返回 file_key。"""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        ext = os.path.splitext(file_path)[1].lower()
        file_type = self._FILE_TYPE_MAP.get(ext, "stream")
        file_name = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                request = (
                    CreateFileRequest.builder()
                    .request_body(
                        CreateFileRequestBody.builder()
                        .file_type(file_type)
                        .file_name(file_name)
                        .file(f)
                        .build()
                    )
                    .build()
                )
                response = self._client.im.v1.file.create(request)
                if response.success():
                    file_key = response.data.file_key
                    self.logger.debug("Uploaded file {}: {}", file_name, file_key)
                    return file_key
                else:
                    self.logger.error(
                        "Failed to upload file: code={}, msg={}", response.code, response.msg
                    )
                    return None
        except Exception:
            self.logger.exception("Error uploading file {}", file_path)
            return None

    def _download_image_sync(
        self, message_id: str, image_key: str
    ) -> tuple[bytes | None, str | None]:
        """通过 message_id 和 image_key 从飞书消息下载图片。"""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(image_key)
                .type("image")
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                # GetMessageResourceRequest returns BytesIO, need to read bytes
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                self.logger.error(
                    "Failed to download image: code={}, msg={}", response.code, response.msg
                )
                return None, None
        except Exception:
            self.logger.exception("Error downloading image {}", image_key)
            return None, None

    def _download_file_sync(
        self, message_id: str, file_key: str, resource_type: str = "file"
    ) -> tuple[bytes | None, str | None]:
        """通过 message_id 和 file_key 从飞书消息下载文件/音频/视频等媒体。"""
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        # Feishu resource download API only accepts 'image' or 'file' as type.
        # Both 'audio' and 'media' (video) messages use type='file' for download.
        if resource_type in ("audio", "media"):
            resource_type = "file"

        try:
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            response = self._client.im.v1.message_resource.get(request)
            if response.success():
                file_data = response.file
                if hasattr(file_data, "read"):
                    file_data = file_data.read()
                return file_data, response.file_name
            else:
                self.logger.error(
                    "Failed to download {}: code={}, msg={}",
                    resource_type,
                    response.code,
                    response.msg,
                )
                return None, None
        except Exception:
            self.logger.exception("Error downloading {} {}", resource_type, file_key)
            return None, None

    @staticmethod
    def _safe_media_filename(filename: str | None, fallback: str) -> str:
        """为下载的飞书媒体返回本地安全的文件名。"""
        candidate = filename or fallback
        # Feishu/Lark filenames come from message metadata. Treat both POSIX
        # and Windows separators as path boundaries before applying the shared
        # filename sanitizer so downloads cannot escape the channel media dir.
        candidate = os.path.basename(candidate.replace("\\", "/"))
        candidate = safe_filename(candidate)
        if candidate in ("", ".", ".."):
            return safe_filename(fallback) or uuid.uuid4().hex
        return candidate

    async def _download_and_save_media(
        self, msg_type: str, content_json: dict, message_id: str | None = None
    ) -> tuple[str | None, str]:
        """从飞书下载媒体并保存到本地磁盘。

        Returns:
            (file_path, content_text) - 下载失败时 file_path 为 None
        """
        loop = asyncio.get_running_loop()
        media_dir = get_media_dir("channels/feishu")

        data, filename = None, None
        fallback_filename = uuid.uuid4().hex

        if msg_type == "image":
            image_key = content_json.get("image_key")
            if image_key and message_id:
                fallback_filename = f"{image_key[:16]}.jpg"
                data, filename = await loop.run_in_executor(
                    None, self._download_image_sync, message_id, image_key
                )
                if not filename:
                    filename = fallback_filename

        elif msg_type in ("audio", "file", "media"):
            file_key = content_json.get("file_key")
            if not file_key:
                self.logger.warning("{} message missing file_key: {}", msg_type, content_json)
                return None, f"[{msg_type}: missing file_key]"
            if not message_id:
                self.logger.warning("{} message missing message_id", msg_type)
                return None, f"[{msg_type}: missing message_id]"

            fallback_filename = file_key[:16]
            data, filename = await loop.run_in_executor(
                None, self._download_file_sync, message_id, file_key, msg_type
            )

            if not data:
                self.logger.warning("{} download failed: file_key={}", msg_type, file_key)
                return None, f"[{msg_type}: download failed]"

            if not filename:
                filename = fallback_filename

            # Feishu voice messages are opus in OGG container.
            # Use .ogg extension for better Whisper compatibility.
            if msg_type == "audio":
                if not any(filename.endswith(ext) for ext in (".opus", ".ogg", ".oga")):
                    filename = f"{filename}.ogg"

        if data and filename:
            filename = self._safe_media_filename(filename, fallback_filename)
            file_path = media_dir / filename
            file_path.write_bytes(data)
            path_str = str(file_path)
            self.logger.debug("Downloaded {} to {}", msg_type, path_str)
            return path_str, f"[{msg_type}: {path_str}]"

        return None, f"[{msg_type}: download failed]"

    _REPLY_CONTEXT_MAX_LEN = 200  # 回复上下文的最大字符长度

    def _get_message_content_sync(self, message_id: str) -> str | None:
        """根据消息 ID 获取飞书消息的文本内容（同步方法）。

        返回 "[Reply to: ...]" 格式的上下文字符串，失败时返回 None。
        """
        from lark_oapi.api.im.v1 import GetMessageRequest

        try:
            request = GetMessageRequest.builder().message_id(message_id).build()
            response = self._client.im.v1.message.get(request)
            if not response.success():
                self.logger.debug(
                    "could not fetch parent message {}: code={}, msg={}",
                    message_id,
                    response.code,
                    response.msg,
                )
                return None
            items = getattr(response.data, "items", None)
            if not items:
                return None
            msg_obj = items[0]
            raw_content = getattr(msg_obj, "body", None)
            raw_content = getattr(raw_content, "content", None) if raw_content else None
            if not raw_content:
                return None
            try:
                content_json = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                return None
            msg_type = getattr(msg_obj, "msg_type", "")
            if msg_type == "text":
                text = content_json.get("text", "").strip()
            elif msg_type == "post":
                text, _ = _extract_post_content(content_json)
                text = text.strip()
            else:
                text = ""
            if not text:
                return None
            if len(text) > self._REPLY_CONTEXT_MAX_LEN:
                text = text[: self._REPLY_CONTEXT_MAX_LEN] + "..."
            return f"[Reply to: {text}]"
        except Exception as e:
            self.logger.debug("error fetching parent message {}: {}", message_id, e)
            return None

    def _reply_message_sync(self, parent_message_id: str, msg_type: str, content: str, *, reply_in_thread: bool = False) -> bool:
        """使用 Reply API 回复已存在的飞书消息（同步方法）。

        Args:
            reply_in_thread: 为 True 时，在飞书客户端中作为话题/主题消息回复。
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        try:
            body_builder = ReplyMessageRequestBody.builder().msg_type(msg_type).content(content)
            if reply_in_thread:
                body_builder = body_builder.reply_in_thread(True)
            request = (
                ReplyMessageRequest.builder()
                .message_id(parent_message_id)
                .request_body(body_builder.build())
                .build()
            )
            response = self._client.im.v1.message.reply(request)
            if not response.success():
                self.logger.error(
                    "Failed to reply to message {}: code={}, msg={}, log_id={}",
                    parent_message_id,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return False
            self.logger.debug("reply sent to message {}", parent_message_id)
            return True
        except Exception:
            self.logger.exception("Error replying to message {}", parent_message_id)
            return False

    def _should_use_reply_in_thread(self, metadata: dict[str, Any]) -> bool:
        """判断群聊回复是否应创建飞书话题/主题。"""
        return metadata.get("chat_type", "group") == "group" and self.config.reply_to_message

    def _thread_reply_target(self, metadata: dict[str, Any]) -> str | None:
        """返回应接收 Reply API 响应的 message_id。"""
        if metadata.get("chat_type", "group") != "group":
            return None
        message_id = metadata.get("message_id")
        if not message_id:
            return None
        if metadata.get("thread_id") or self.config.reply_to_message:
            return message_id
        return None

    def _send_message_sync(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> str | None:
        """发送单条消息，成功时返回 message_id。"""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message.create(request)
            if not response.success():
                self.logger.error(
                    "Failed to send {} message: code={}, msg={}, log_id={}",
                    msg_type,
                    response.code,
                    response.msg,
                    response.get_log_id(),
                )
                return None
            msg_id = getattr(response.data, "message_id", None)
            self.logger.debug("{} message sent to {}: {}", msg_type, receive_id, msg_id)
            return msg_id
        except Exception:
            self.logger.exception("Error sending {} message", msg_type)
            return None

    def _create_streaming_card_sync(
        self,
        receive_id_type: str,
        chat_id: str,
        reply_message_id: str | None = None,
        *,
        reply_in_thread: bool = False,
    ) -> str | None:
        """创建 CardKit 流式卡片，发送到会话，返回 card_id。

        当提供 *reply_message_id* 时，卡片通过 Reply API 投递。
        *reply_in_thread* 控制飞书是否为该回复创建话题/主题。
        否则使用普通的创建消息 API。
        """
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

        card_json = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True, "update_multi": True, "streaming_mode": True},
            "body": {
                "elements": [{"tag": "markdown", "content": "", "element_id": _STREAM_ELEMENT_ID}]
            },
        }
        try:
            request = (
                CreateCardRequest.builder()
                .request_body(
                    CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(card_json, ensure_ascii=False))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.create(request)
            if not response.success():
                self.logger.warning(
                    "Failed to create streaming card: code={}, msg={}", response.code, response.msg
                )
                return None
            card_id = getattr(response.data, "card_id", None)
            if card_id:
                card_content = json.dumps(
                    {"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False
                )
                if reply_message_id:
                    sent = self._reply_message_sync(
                        reply_message_id, "interactive", card_content,
                        reply_in_thread=reply_in_thread,
                    )
                else:
                    sent = self._send_message_sync(
                        receive_id_type, chat_id, "interactive", card_content,
                    ) is not None
                if sent:
                    return card_id
                self.logger.warning(
                    "Created streaming card {} but failed to send it to {}", card_id, chat_id
                )
            return None
        except Exception as e:
            self.logger.warning("Error creating streaming card: {}", e)
            return None

    def _stream_update_text_sync(self, card_id: str, content: str, sequence: int) -> bool:
        """流式更新 CardKit 卡片上的 Markdown 元素（打字机效果）。"""
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        try:
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(_STREAM_ELEMENT_ID)
                .request_body(
                    ContentCardElementRequestBody.builder()
                    .content(content)
                    .sequence(sequence)
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card_element.content(request)
            if not response.success():
                self.logger.warning(
                    "Failed to stream-update card {}: code={}, msg={}",
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error stream-updating card {}: {}", card_id, e)
            return False

    def _close_streaming_mode_sync(self, card_id: str, sequence: int) -> bool:
        """关闭 CardKit 的 streaming_mode，使会话列表预览退出流式占位状态。

        根据飞书文档，流式卡片在会话列表中会保持"生成中"样式的摘要，
        直到通过卡片设置将 streaming_mode 设为 false（在最终内容更新后）。
        sequence 必须严格大于此前对该实体的上一次 OpenAPI 操作。
        """
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        settings_payload = json.dumps({"config": {"streaming_mode": False}}, ensure_ascii=False)
        try:
            request = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .request_body(
                    SettingsCardRequestBody.builder()
                    .settings(settings_payload)
                    .sequence(sequence)
                    .uuid(str(uuid.uuid4()))
                    .build()
                )
                .build()
            )
            response = self._client.cardkit.v1.card.settings(request)
            if not response.success():
                self.logger.warning(
                    "Failed to close streaming on card {}: code={}, msg={}",
                    card_id,
                    response.code,
                    response.msg,
                )
                return False
            return True
        except Exception as e:
            self.logger.warning("Error closing streaming on card {}: {}", card_id, e)
            return False

    async def send_delta(
        self, chat_id: str, delta: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """Progressive streaming via CardKit: create card on first delta, stream-update on subsequent.

        Supported metadata keys:
            _stream_end: Finalize the streaming card.
            _tool_hint:  Delta is a formatted tool hint (for display only).
            message_id:  Original message id (used with _stream_end for reaction cleanup).
            chat_type:   "group" or "p2p" — controls reply-in-thread for streaming cards.
        """
        if not self._client:
            return
        meta = metadata or {}
        stream_key = self._stream_key(chat_id, meta)
        loop = asyncio.get_running_loop()
        rid_type = "chat_id" if chat_id.startswith("oc_") else "open_id"

        # --- stream end: final update or fallback ---
        if meta.get("_stream_end"):
            message_id = meta.get("message_id")
            # Only finalize the OnIt -> DONE reaction transition on the truly
            # final stream end. _resuming=True means the agent will keep
            # working (more tool-call rounds), so leave the reaction state
            # in place — otherwise the OnIt indicator disappears prematurely
            # and the DONE reaction fires after every tool call.
            if message_id and not meta.get("_resuming"):
                reaction_id = self._reaction_ids.pop(message_id, None)
                if reaction_id:
                    await self._remove_reaction(message_id, reaction_id)
                # Add completion emoji if configured
                if self.config.done_emoji:
                    await self._add_reaction(message_id, self.config.done_emoji)

            buf = self._stream_bufs.pop(stream_key, None)
            if not buf or not buf.text:
                return
            # Try to finalize via streaming card; if that fails (e.g.
            # streaming mode was closed by Feishu due to timeout), fall
            # back to sending a regular interactive card.
            if buf.card_id:
                buf.sequence += 1
                ok = await loop.run_in_executor(
                    None,
                    self._stream_update_text_sync,
                    buf.card_id,
                    buf.text,
                    buf.sequence,
                )
                if ok:
                    buf.sequence += 1
                    await loop.run_in_executor(
                        None,
                        self._close_streaming_mode_sync,
                        buf.card_id,
                        buf.sequence,
                    )
                    return
                self.logger.warning(
                    "Streaming card {} final update failed, falling back to regular card",
                    buf.card_id,
                )
            for chunk in self._split_elements_by_table_limit(
                self._build_card_elements(buf.text)
            ):
                card = json.dumps(
                    {"config": {"wide_screen_mode": True}, "elements": chunk},
                    ensure_ascii=False,
                )
                # Fallback replies stay in existing topics, but only create a
                # new topic when reply-to-message is enabled.
                fallback_msg_id = self._thread_reply_target(meta)
                if fallback_msg_id:
                    await loop.run_in_executor(
                        None, lambda: self._reply_message_sync(
                            fallback_msg_id, "interactive", card,
                            reply_in_thread=self._should_use_reply_in_thread(meta),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_message_sync, rid_type, chat_id, "interactive", card
                    )
            return

        # --- accumulate delta ---
        buf = self._stream_bufs.get(stream_key)
        if buf is None:
            self._prune_stale_stream_bufs()
            buf = _FeishuStreamBuf()
            self._stream_bufs[stream_key] = buf
        buf.text += delta
        if not buf.text.strip():
            return

        now = time.monotonic()
        if buf.card_id is None:
            # Use the Reply API for existing topics, and only create new topics
            # when reply-to-message is enabled.
            use_reply_in_thread = self._should_use_reply_in_thread(meta)
            reply_msg_id = self._thread_reply_target(meta)
            card_id = await loop.run_in_executor(
                None,
                lambda: self._create_streaming_card_sync(
                    rid_type,
                    chat_id,
                    reply_msg_id,
                    reply_in_thread=use_reply_in_thread,
                ),
            )
            if card_id:
                buf.card_id = card_id
                buf.sequence = 1
                await loop.run_in_executor(
                    None, self._stream_update_text_sync, card_id, buf.text, 1
                )
                buf.last_edit = now
        elif (now - buf.last_edit) >= self._STREAM_EDIT_INTERVAL:
            buf.sequence += 1
            await loop.run_in_executor(
                None, self._stream_update_text_sync, buf.card_id, buf.text, buf.sequence
            )
            buf.last_edit = now

    async def send(self, msg: OutboundMessage) -> None:
        """通过飞书发送消息，包括媒体（图片/文件）附件（如有）。"""
        if not self._client:
            self.logger.warning("client not initialized")
            return

        try:
            receive_id_type = "chat_id" if msg.chat_id.startswith("oc_") else "open_id"
            loop = asyncio.get_running_loop()

            # Handle tool hint messages.  When a streaming card is active for
            # this chat, inline the hint into the card instead of sending a
            # separate message so the user experience stays cohesive.
            if msg.metadata.get("_tool_hint"):
                hint = (msg.content or "").strip()
                if not hint:
                    return
                buf = self._stream_bufs.get(self._stream_key(msg.chat_id, msg.metadata))
                if buf and buf.card_id:
                    # Delegate to send_delta so tool hints get the same
                    # throttling (and card creation) as regular text deltas.
                    # metadata 必须透传：stream_key 依赖 message_id，
                    # 缺失时会退化成 chat_id 维度，产生第二张孤立卡片。
                    await self.send_delta(
                        msg.chat_id,
                        "\n\n" + self._format_tool_hint_delta(hint) + "\n\n",
                        msg.metadata,
                    )
                    return
                # No active streaming card — send as a regular interactive card
                # with the same 🔧 prefix style. Existing topics stay threaded;
                # new topics are created only when reply-to-message is enabled.
                card = json.dumps(
                    {"config": {"wide_screen_mode": True}, "elements": [
                        {"tag": "markdown", "content": self._format_tool_hint_delta(hint)},
                    ]},
                    ensure_ascii=False,
                )
                _th_msg_id = self._thread_reply_target(msg.metadata)
                if _th_msg_id:
                    await loop.run_in_executor(
                        None, lambda: self._reply_message_sync(
                            _th_msg_id, "interactive", card,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        ),
                    )
                else:
                    await loop.run_in_executor(
                        None, self._send_message_sync, receive_id_type, msg.chat_id, "interactive", card
                    )
                return

            # Determine whether the first message should quote the user's message.
            # Only the very first send (media or text) in this call uses reply; subsequent
            # chunks/media fall back to plain create to avoid redundant quote bubbles.
            # Always target message_id — the Feishu Reply API keeps replies in the
            # same topic automatically when the target message is inside a topic.
            reply_message_id: str | None = None
            _msg_id = msg.metadata.get("message_id")
            has_thread_id = msg.metadata.get("thread_id")
            if self.config.reply_to_message and not msg.metadata.get("_progress", False):
                reply_message_id = _msg_id
            # For topic group messages, always reply to keep context in thread
            elif has_thread_id:
                reply_message_id = _msg_id

            first_send = True  # tracks whether the reply has already been used

            def _do_send(m_type: str, content: str) -> None:
                """Send via reply (first message) or create (subsequent).

                Group chats only set reply_in_thread=True when
                reply_to_message is enabled; otherwise a Reply API call for an
                existing topic must not create a new topic.
                """
                nonlocal first_send
                if reply_message_id:
                    # If we're in a topic, always use reply to stay in the topic
                    if has_thread_id:
                        ok = self._reply_message_sync(
                            reply_message_id, m_type, content,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        )
                        if ok:
                            return
                    elif first_send:
                        # If we're not in a topic but replying to message, only first uses reply
                        first_send = False
                        ok = self._reply_message_sync(
                            reply_message_id, m_type, content,
                            reply_in_thread=self._should_use_reply_in_thread(msg.metadata),
                        )
                        if ok:
                            return
                    # Fall back to regular send if reply fails
                self._send_message_sync(receive_id_type, msg.chat_id, m_type, content)

            for file_path in msg.media:
                if not os.path.isfile(file_path):
                    self.logger.warning("Media file not found: {}", file_path)
                    continue
                ext = os.path.splitext(file_path)[1].lower()
                if ext in self._IMAGE_EXTS:
                    key = await loop.run_in_executor(None, self._upload_image_sync, file_path)
                    if key:
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "image",
                            json.dumps({"image_key": key}, ensure_ascii=False),
                        )
                else:
                    key = await loop.run_in_executor(None, self._upload_file_sync, file_path)
                    if key:
                        # Feishu's OpenAPI names video messages "media".
                        # Use "audio" for audio, "media" for video, "file" for documents.
                        # Feishu requires these specific msg_types for inline playback.
                        if ext in self._AUDIO_EXTS:
                            media_type = "audio"
                        elif ext in self._VIDEO_EXTS:
                            media_type = "media"
                        else:
                            media_type = "file"
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            media_type,
                            json.dumps({"file_key": key}, ensure_ascii=False),
                        )

            if msg.content and msg.content.strip():
                fmt = self._detect_msg_format(msg.content)

                if fmt == "text":
                    # Short plain text – send as simple text message
                    text_body = json.dumps({"text": msg.content.strip()}, ensure_ascii=False)
                    await loop.run_in_executor(None, _do_send, "text", text_body)

                elif fmt == "post":
                    # Medium content with links – send as rich-text post
                    post_body = self._markdown_to_post(msg.content)
                    await loop.run_in_executor(None, _do_send, "post", post_body)

                else:
                    # Complex / long content – send as interactive card
                    elements = self._build_card_elements(msg.content)
                    for chunk in self._split_elements_by_table_limit(elements):
                        card = {"config": {"wide_screen_mode": True}, "elements": chunk}
                        await loop.run_in_executor(
                            None,
                            _do_send,
                            "interactive",
                            json.dumps(card, ensure_ascii=False),
                        )

        except Exception:
            self.logger.exception("Error sending message")
            raise

    def _on_message_sync(self, data: Any) -> None:
        """
        Sync handler for incoming messages (called from WebSocket thread).
        Schedules async handling in the main event loop.
        """
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._on_message(data), self._loop)

    async def _on_message(self, data: P2ImMessageReceiveV1) -> None:
        """处理来自飞书的入站消息。"""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            self.logger.debug("raw message: {}", message.content)
            self.logger.debug("mentions: {}", getattr(message, "mentions", None))

            message_id = message.message_id

            # Skip bot messages
            if sender.sender_type == "bot":
                return

            sender_id = sender.sender_id.open_id if sender.sender_id else "unknown"
            chat_id = message.chat_id
            chat_type = message.chat_type
            msg_type = message.message_type

            if chat_type == "group" and not self._is_group_message_for_bot(message):
                self.logger.debug("skipping group message (not mentioned)")
                return

            # Deduplication check（check_and_mark 首次出现返回 True，重复返回 False）
            if not self._processed_message_ids.check_and_mark(message_id):
                return

            # Early permission check — avoid side effects for unauthorized users.
            # Group chats are silently ignored; DMs get a pairing code.
            if not self.is_allowed(sender_id):
                if chat_type == "p2p":
                    # content="" because the pairing reply is generated by
                    # BaseChannel._handle_message, not from the original message.
                    await self._handle_message(
                        sender_id=sender_id,
                        chat_id=sender_id,
                        content="",
                        is_dm=True,
                    )
                return

            # Add reaction (non-blocking — tracked background task)
            task = asyncio.create_task(
                self._add_reaction(message_id, self.config.react_emoji)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)
            task.add_done_callback(lambda t: self._on_reaction_added(message_id, t))

            # Parse content
            content_parts = []
            media_paths = []

            try:
                content_json = json.loads(message.content) if message.content else {}
            except json.JSONDecodeError:
                content_json = {}

            if msg_type == "text":
                text = content_json.get("text", "")
                if text:
                    mentions = getattr(message, "mentions", None)
                    text = self._strip_leading_bot_mention(text, mentions)
                    text = self._resolve_mentions(text, mentions)
                    content_parts.append(text)

            elif msg_type == "post":
                text, image_keys = _extract_post_content(content_json)
                if text:
                    content_parts.append(text)
                # Download images embedded in post
                for img_key in image_keys:
                    file_path, content_text = await self._download_and_save_media(
                        "image", {"image_key": img_key}, message_id
                    )
                    if file_path:
                        media_paths.append(file_path)
                    content_parts.append(content_text)

            elif msg_type in ("image", "audio", "file", "media"):
                file_path, content_text = await self._download_and_save_media(
                    msg_type, content_json, message_id
                )
                if file_path:
                    media_paths.append(file_path)

                if msg_type == "audio" and file_path:
                    transcription = await self.transcribe_audio(file_path)
                    if transcription:
                        content_text = f"[transcription: {transcription}]"

                content_parts.append(content_text)

            elif msg_type in (
                "share_chat",
                "share_user",
                "interactive",
                "share_calendar_event",
                "system",
                "merge_forward",
            ):
                # Handle share cards and interactive messages
                text = _extract_share_card_content(content_json, msg_type)
                if text:
                    content_parts.append(text)

            else:
                content_parts.append(MSG_TYPE_MAP.get(msg_type, f"[{msg_type}]"))

            # Extract reply context (parent/root message IDs)
            parent_id = getattr(message, "parent_id", None) or None
            root_id = getattr(message, "root_id", None) or None
            thread_id = getattr(message, "thread_id", None) or None

            # Prepend quoted message text when the user replied to another message
            if parent_id and self._client:
                loop = asyncio.get_running_loop()
                reply_ctx = await loop.run_in_executor(
                    None, self._get_message_content_sync, parent_id
                )
                if reply_ctx:
                    content_parts.insert(0, reply_ctx)

            content = "\n".join(content_parts) if content_parts else ""

            if not content and not media_paths:
                return

            # Build session key for conversation isolation.
            # If topic_isolation is True: each topic gets its own session via root_id/message_id.
            # If topic_isolation is False: all messages in group share the same session.
            # Private chat: no override — same behavior as Telegram/Slack.
            if chat_type == "group":
                if self.config.topic_isolation:
                    session_key = f"feishu:{chat_id}:{root_id or message_id}"
                else:
                    session_key = f"feishu:{chat_id}"
            else:
                session_key = None

            # Forward to message bus
            reply_to = chat_id if chat_type == "group" else sender_id
            await self._handle_message(
                sender_id=sender_id,
                chat_id=reply_to,
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "chat_type": chat_type,
                    "msg_type": msg_type,
                    "parent_id": parent_id,
                    "root_id": root_id,
                    "thread_id": thread_id,
                },
                session_key=session_key,
                is_dm=chat_type == "p2p",
            )

        except Exception:
            self.logger.exception("Error processing message")

    def _on_reaction_created(self, data: Any) -> None:
        """Ignore reaction events so they do not generate SDK noise."""
        pass

    def _on_reaction_deleted(self, data: Any) -> None:
        """Ignore reaction deleted events so they do not generate SDK noise."""
        pass

    def _on_message_read(self, data: Any) -> None:
        """Ignore read events so they do not generate SDK noise."""
        pass

    def _on_bot_p2p_chat_entered(self, data: Any) -> None:
        """Ignore p2p-enter events when a user opens a bot chat."""
        self.logger.debug("Bot entered p2p chat (user opened chat window)")
        pass

    @staticmethod
    def _format_tool_hint_lines(tool_hint: str) -> str:
        """Split tool hints across lines on top-level call separators only."""
        parts: list[str] = []
        buf: list[str] = []
        depth = 0
        in_string = False
        quote_char = ""
        escaped = False

        for i, ch in enumerate(tool_hint):
            buf.append(ch)

            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote_char:
                    in_string = False
                continue

            if ch in {'"', "'"}:
                in_string = True
                quote_char = ch
                continue

            if ch == "(":
                depth += 1
                continue

            if ch == ")" and depth > 0:
                depth -= 1
                continue

            if ch == "," and depth == 0:
                next_char = tool_hint[i + 1] if i + 1 < len(tool_hint) else ""
                if next_char == " ":
                    parts.append("".join(buf).rstrip())
                    buf = []

        if buf:
            parts.append("".join(buf).strip())

        return "\n".join(part for part in parts if part)

    def _format_tool_hint_delta(self, tool_hint: str) -> str:
        """为工具提示字符串的每一行添加 🔧 前缀进行格式化。"""
        lines = self.__class__._format_tool_hint_lines(tool_hint).split("\n")
        return "\n".join(
            f"{self.config.tool_hint_prefix} {ln}" for ln in lines if ln.strip()
        )
