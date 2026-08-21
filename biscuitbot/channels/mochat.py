"""Mochat 渠道实现，使用 Socket.IO（含 HTTP 轮询降级）。

所属模块与项目作用
====================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 Mochat 平台组件。
在项目架构中起到的作用：通过 Socket.IO 长连接（或 HTTP 轮询降级）将 Mochat 平台的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：优先使用 Socket.IO WebSocket 连接；连接失败时自动降级为 HTTP 长轮询（watch/poll）。
- 鉴权：通过 claw_token（X-Claw-Token 头）进行身份认证。
- 目标类型：支持 session（会话）和 panel（群组面板）两种目标；可通过前缀（mochat:/group:/channel:/panel:）或 session_ 前缀推断。
- 自动发现：sessions/panels 配置为 ["*"] 时启用自动发现，周期性拉取目录并订阅新目标。
- 游标持久化：session 消息通过游标（cursor）记录消费进度，持久化到本地文件，重启后从断点继续。
- 消息去重：通过 seen_set + seen_queue（LRU）对每个目标进行消息 ID 去重。
- 延迟聚合：panel 场景下支持非 @mention 消息延迟聚合（reply_delay_ms），@mention 时立即 flush。
- 冷启动跳过：session 首次订阅时跳过历史消息（cold_sessions），仅处理新消息。
"""

from __future__ import annotations

import asyncio  # 异步事件循环与并发原语
import json  # JSON 序列化/反序列化
from collections import deque  # 固定长度去重队列（消息 ID）
from contextlib import suppress  # 上下文管理器，抑制指定异常
from dataclasses import dataclass, field  # 数据类装饰器与字段
from datetime import datetime  # 时间戳解析与生成
from typing import Any  # 类型注解支持

import httpx  # 异步 HTTP 客户端

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_runtime_subdir  # 运行时子目录
from biscuitbot.config.schema import Base  # 配置模型基类
from pydantic import Field  # Pydantic 模型字段定义

try:
    import socketio  # python-socketio 客户端
    SOCKETIO_AVAILABLE = True
except ImportError:
    socketio = None
    SOCKETIO_AVAILABLE = False

try:
    import msgpack  # noqa: F401  # MessagePack 序列化（可选，用于 Socket.IO）
    MSGPACK_AVAILABLE = True
except ImportError:
    MSGPACK_AVAILABLE = False

MAX_SEEN_MESSAGE_IDS = 2000  # 每个目标保留的已见消息 ID 上限（LRU）
CURSOR_SAVE_DEBOUNCE_S = 0.5  # 游标保存防抖时间（秒）


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class MochatBufferedEntry:
    """延迟派发的入站缓冲条目。"""
    raw_body: str
    author: str
    sender_name: str = ""
    sender_username: str = ""
    timestamp: int | None = None
    message_id: str = ""
    group_id: str = ""


@dataclass
class DelayState:
    """每个目标的延迟消息状态。"""
    entries: list[MochatBufferedEntry] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: asyncio.Task | None = None


@dataclass
class MochatTarget:
    """出站目标解析结果。"""
    id: str
    is_panel: bool


# ---------------------------------------------------------------------------
# 纯辅助函数
# ---------------------------------------------------------------------------

def _safe_dict(value: Any) -> dict:
    """若 value 为 dict 则返回，否则返回空 dict。"""
    return value if isinstance(value, dict) else {}


def _str_field(src: dict, *keys: str) -> str:
    """返回 keys 中首个非空字符串值（已 strip）。"""
    for k in keys:
        v = src.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _make_synthetic_event(
    message_id: str, author: str, content: Any,
    meta: Any, group_id: str, converse_id: str,
    timestamp: Any = None, *, author_info: Any = None,
) -> dict[str, Any]:
    """构建合成的 message.add 事件字典（用于轮询结果归一化）。"""
    payload: dict[str, Any] = {
        "messageId": message_id, "author": author,
        "content": content, "meta": _safe_dict(meta),
        "groupId": group_id, "converseId": converse_id,
    }
    if author_info is not None:
        payload["authorInfo"] = _safe_dict(author_info)
    return {
        "type": "message.add",
        "timestamp": timestamp or datetime.utcnow().isoformat(),
        "payload": payload,
    }


def normalize_mochat_content(content: Any) -> str:
    """将内容 payload 归一化为文本。"""
    if isinstance(content, str):
        return content.strip()
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return str(content)


def resolve_mochat_target(raw: str) -> MochatTarget:
    """从用户输入的目标字符串解析 ID 和目标类型。

    支持前缀：
    - mochat: —— 通用前缀，按 session_ 前缀推断类型
    - group:/channel:/panel: —— 强制为 panel
    - 无前缀且以 session_ 开头 —— session；否则视为 panel
    """
    trimmed = (raw or "").strip()
    if not trimmed:
        return MochatTarget(id="", is_panel=False)

    lowered = trimmed.lower()
    cleaned, forced_panel = trimmed, False
    for prefix in ("mochat:", "group:", "channel:", "panel:"):
        if lowered.startswith(prefix):
            cleaned = trimmed[len(prefix):].strip()
            forced_panel = prefix in {"group:", "channel:", "panel:"}
            break

    if not cleaned:
        return MochatTarget(id="", is_panel=False)
    return MochatTarget(id=cleaned, is_panel=forced_panel or not cleaned.startswith("session_"))


def extract_mention_ids(value: Any) -> list[str]:
    """从异构的 mention payload 中提取 mention 用户 ID 列表。"""
    if not isinstance(value, list):
        return []
    ids: list[str] = []
    for item in value:
        if isinstance(item, str):
            if item.strip():
                ids.append(item.strip())
        elif isinstance(item, dict):
            # 尝试多个可能的 ID 字段名
            for key in ("id", "userId", "_id"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    ids.append(candidate.strip())
                    break
    return ids


def resolve_was_mentioned(payload: dict[str, Any], agent_user_id: str) -> bool:
    """从 payload 元数据和文本回退推断是否被 @mention。"""
    meta = payload.get("meta")
    if isinstance(meta, dict):
        # 显式标记
        if meta.get("mentioned") is True or meta.get("wasMentioned") is True:
            return True
        # 从多个可能的 mention 字段中查找 agent_user_id
        for f in ("mentions", "mentionIds", "mentionedUserIds", "mentionedUsers"):
            if agent_user_id and agent_user_id in extract_mention_ids(meta.get(f)):
                return True
    if not agent_user_id:
        return False
    # 文本回退：检查 <@id> 或 @id 格式
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        return False
    return f"<@{agent_user_id}>" in content or f"@{agent_user_id}" in content


def resolve_require_mention(config: MochatConfig, session_id: str, group_id: str) -> bool:
    """解析群组/panel 会话是否要求 @mention 才响应。"""
    groups = config.groups or {}
    # 优先级：group_id > session_id > 通配 "*"
    for key in (group_id, session_id, "*"):
        if key and key in groups:
            return bool(groups[key].require_mention)
    return bool(config.mention.require_in_groups)


def build_buffered_body(entries: list[MochatBufferedEntry], is_group: bool) -> str:
    """从一个或多个缓冲条目构建文本正文。"""
    if not entries:
        return ""
    if len(entries) == 1:
        return entries[0].raw_body
    lines: list[str] = []
    for entry in entries:
        if not entry.raw_body:
            continue
        # 群聊场景下为每条消息添加发送者标签
        if is_group:
            label = entry.sender_name.strip() or entry.sender_username.strip() or entry.author
            if label:
                lines.append(f"{label}: {entry.raw_body}")
                continue
        lines.append(entry.raw_body)
    return "\n".join(lines).strip()


def parse_timestamp(value: Any) -> int | None:
    """将事件时间戳解析为 epoch 毫秒。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 配置类
# ---------------------------------------------------------------------------

class MochatMentionConfig(Base):
    """Mochat @mention 行为配置。"""

    require_in_groups: bool = False  # 群聊是否要求 @mention 才响应


class MochatGroupRule(Base):
    """Mochat 单个群组的 @mention 要求。"""

    require_mention: bool = False


class MochatConfig(Base):
    """Mochat 渠道配置。"""

    enabled: bool = False
    base_url: str = "https://mochat.io"  # Mochat 平台基础 URL
    socket_url: str = ""  # Socket.IO 服务地址（为空时使用 base_url）
    socket_path: str = "/socket.io"  # Socket.IO 路径
    socket_disable_msgpack: bool = False  # 是否禁用 msgpack 序列化
    socket_reconnect_delay_ms: int = 1000  # 重连初始延迟（毫秒）
    socket_max_reconnect_delay_ms: int = 10000  # 重连最大延迟（毫秒）
    socket_connect_timeout_ms: int = 10000  # 连接超时（毫秒）
    refresh_interval_ms: int = 30000  # 目录刷新间隔（毫秒）
    watch_timeout_ms: int = 25000  # watch 长轮询超时（毫秒）
    watch_limit: int = 100  # watch 每次拉取消息上限
    retry_delay_ms: int = 500  # 失败重试延迟（毫秒）
    max_retry_attempts: int = 0  # 最大重试次数（0=无限）
    claw_token: str = ""  # Mochat 鉴权令牌
    agent_user_id: str = ""  # 机器人自身的用户 ID（用于 @mention 检测）
    sessions: list[str] = Field(default_factory=list)  # 订阅的 session ID 列表（["*"] 表示自动发现）
    panels: list[str] = Field(default_factory=list)  # 订阅的 panel ID 列表（["*"] 表示自动发现）
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单
    allow_all: bool = False  # 静默放行：为 True 时所有用户可直接私聊，无需白名单或配对码
    mention: MochatMentionConfig = Field(default_factory=MochatMentionConfig)  # @mention 行为配置
    groups: dict[str, MochatGroupRule] = Field(default_factory=dict)  # 按群组 ID 覆盖的 @mention 规则
    reply_delay_mode: str = "non-mention"  # 延迟回复模式（non-mention=仅非@消息延迟）
    reply_delay_ms: int = 120000  # 延迟回复聚合窗口（毫秒）


# ---------------------------------------------------------------------------
# 渠道
# ---------------------------------------------------------------------------

class MochatChannel(BaseChannel):
    """Mochat 渠道，使用 socket.io 连接，失败时降级为轮询工作线程。"""

    name = "mochat"
    display_name = "MoChat"
    requires_module = "socketio"  # 必需 SDK 模块（缺失时自动安装）
    pip_requires = ["python-socketio>=5.16.0"]

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return MochatConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = MochatConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: MochatConfig = config
        self._http: httpx.AsyncClient | None = None  # HTTP 客户端
        self._socket: Any = None  # socketio.AsyncClient 实例
        self._ws_connected = self._ws_ready = False  # WebSocket 连接/就绪状态

        self._state_dir = get_runtime_subdir("mochat")  # 运行时状态目录
        self._cursor_path = self._state_dir / "session_cursors.json"  # 游标持久化文件路径
        self._session_cursor: dict[str, int] = {}  # session 游标（session_id → cursor）
        self._cursor_save_task: asyncio.Task | None = None  # 游标防抖保存任务

        self._session_set: set[str] = set()  # 已知的 session ID 集合
        self._panel_set: set[str] = set()  # 已知的 panel ID 集合
        self._auto_discover_sessions = self._auto_discover_panels = False  # 是否启用自动发现

        self._cold_sessions: set[str] = set()  # 冷启动 session（首次订阅跳过历史消息）
        self._session_by_converse: dict[str, str] = {}  # converseId → session_id 映射

        self._seen_set: dict[str, set[str]] = {}  # 每个目标的已见消息 ID 集合
        self._seen_queue: dict[str, deque[str]] = {}  # 每个目标的已见消息 ID 队列（LRU）
        self._delay_states: dict[str, DelayState] = {}  # 每个目标的延迟聚合状态

        self._fallback_mode = False  # 是否处于轮询降级模式
        self._session_fallback_tasks: dict[str, asyncio.Task] = {}  # session 轮询工作线程
        self._panel_fallback_tasks: dict[str, asyncio.Task] = {}  # panel 轮询工作线程
        self._refresh_task: asyncio.Task | None = None  # 目录刷新任务
        self._target_locks: dict[str, asyncio.Lock] = {}  # 每个目标的处理锁（避免并发处理）

    # ---- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        """启动 Mochat 渠道工作线程和 WebSocket 连接。"""
        if not self.config.claw_token:
            self.logger.error("claw_token not configured")
            return

        self._running = True
        self._http = httpx.AsyncClient(timeout=30.0)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        await self._load_session_cursors()
        self._seed_targets_from_config()
        await self._refresh_targets(subscribe_new=False)

        # 优先尝试 Socket.IO；失败则降级为轮询
        if not await self._start_socket_client():
            await self._ensure_fallback_workers()

        self._refresh_task = asyncio.create_task(self._refresh_loop())
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        """停止所有工作线程并清理资源。"""
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None

        await self._stop_fallback_workers()
        await self._cancel_delay_timers()

        if self._socket:
            with suppress(Exception):
                await self._socket.disconnect()
            self._socket = None

        if self._cursor_save_task:
            self._cursor_save_task.cancel()
            self._cursor_save_task = None
        await self._save_session_cursors()

        if self._http:
            await self._http.aclose()
            self._http = None
        self._ws_connected = self._ws_ready = False

    async def send(self, msg: OutboundMessage) -> None:
        """发送出站消息到 session 或 panel。"""
        if not self.config.claw_token:
            self.logger.warning("claw_token missing, skip send")
            return

        # 合并文本与媒体引用
        parts = ([msg.content.strip()] if msg.content and msg.content.strip() else [])
        if msg.media:
            parts.extend(m for m in msg.media if isinstance(m, str) and m.strip())
        content = "\n".join(parts).strip()
        if not content:
            return

        target = resolve_mochat_target(msg.chat_id)
        if not target.id:
            self.logger.warning("outbound target is empty")
            return

        # 判断目标类型：panel 优先看显式标记，再看集合成员，最后看 session_ 前缀
        is_panel = (target.is_panel or target.id in self._panel_set) and not target.id.startswith("session_")
        try:
            if is_panel:
                await self._api_send("/api/claw/groups/panels/send", "panelId", target.id,
                                     content, msg.reply_to, self._read_group_id(msg.metadata))
            else:
                await self._api_send("/api/claw/sessions/send", "sessionId", target.id,
                                     content, msg.reply_to)
        except Exception:
            self.logger.exception("Failed to send message")
            raise

    # ---- 配置/初始化辅助 --------------------------------------------------

    def _seed_targets_from_config(self) -> None:
        """从配置初始化目标集合，识别自动发现通配符。"""
        sessions, self._auto_discover_sessions = self._normalize_id_list(self.config.sessions)
        panels, self._auto_discover_panels = self._normalize_id_list(self.config.panels)
        self._session_set.update(sessions)
        self._panel_set.update(panels)
        # 新增的 session 标记为冷启动
        for sid in sessions:
            if sid not in self._session_cursor:
                self._cold_sessions.add(sid)

    @staticmethod
    def _normalize_id_list(values: list[str]) -> tuple[list[str], bool]:
        """规范化 ID 列表，分离通配符 "*" 并返回 (排序去重列表, 是否自动发现)。"""
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        return sorted({v for v in cleaned if v != "*"}), "*" in cleaned

    # ---- WebSocket --------------------------------------------------------

    async def _start_socket_client(self) -> bool:
        """启动 Socket.IO 客户端，成功返回 True。"""
        if not SOCKETIO_AVAILABLE:
            self.logger.warning("python-socketio not installed, using polling fallback")
            return False

        # 选择序列化器：优先 msgpack，否则 JSON
        serializer = "default"
        if not self.config.socket_disable_msgpack:
            if MSGPACK_AVAILABLE:
                serializer = "msgpack"
            else:
                self.logger.warning("msgpack not installed but socket_disable_msgpack=false; using JSON")

        client = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=self.config.max_retry_attempts or None,
            reconnection_delay=max(0.1, self.config.socket_reconnect_delay_ms / 1000.0),
            reconnection_delay_max=max(0.1, self.config.socket_max_reconnect_delay_ms / 1000.0),
            logger=False, engineio_logger=False, serializer=serializer,
        )

        @client.event
        async def connect() -> None:
            self._ws_connected, self._ws_ready = True, False
            self.logger.info("websocket connected")
            # 连接成功后订阅所有目标
            subscribed = await self._subscribe_all()
            self._ws_ready = subscribed
            # 订阅成功则停止轮询；失败则启用轮询
            await (self._stop_fallback_workers() if subscribed else self._ensure_fallback_workers())

        @client.event
        async def disconnect() -> None:
            if not self._running:
                return
            self._ws_connected = self._ws_ready = False
            self.logger.warning("websocket disconnected")
            # 断线后启用轮询降级
            await self._ensure_fallback_workers()

        @client.event
        async def connect_error(data: Any) -> None:
            self.logger.error("websocket connect error: {}", data)

        @client.on("claw.session.events")
        async def on_session_events(payload: dict[str, Any]) -> None:
            await self._handle_watch_payload(payload, "session")

        @client.on("claw.panel.events")
        async def on_panel_events(payload: dict[str, Any]) -> None:
            await self._handle_watch_payload(payload, "panel")

        # 注册 notify 类事件处理器
        for ev in ("notify:chat.inbox.append", "notify:chat.message.add",
                    "notify:chat.message.update", "notify:chat.message.recall",
                    "notify:chat.message.delete"):
            client.on(ev, self._build_notify_handler(ev))

        socket_url = (self.config.socket_url or self.config.base_url).strip().rstrip("/")
        socket_path = (self.config.socket_path or "/socket.io").strip().lstrip("/")

        try:
            self._socket = client
            await client.connect(
                socket_url, transports=["websocket"], socketio_path=socket_path,
                auth={"token": self.config.claw_token},
                wait_timeout=max(1.0, self.config.socket_connect_timeout_ms / 1000.0),
            )
            return True
        except Exception:
            self.logger.exception("Failed to connect websocket")
            with suppress(Exception):
                await client.disconnect()
            self._socket = None
            return False

    def _build_notify_handler(self, event_name: str):
        """构建 notify 事件处理器（按事件名分发）。"""
        async def handler(payload: Any) -> None:
            if event_name == "notify:chat.inbox.append":
                await self._handle_notify_inbox_append(payload)
            elif event_name.startswith("notify:chat.message."):
                await self._handle_notify_chat_message(payload)
        return handler

    # ---- 订阅 -------------------------------------------------------------

    async def _subscribe_all(self) -> bool:
        """订阅所有已知的 session 和 panel。"""
        ok = await self._subscribe_sessions(sorted(self._session_set))
        ok = await self._subscribe_panels(sorted(self._panel_set)) and ok
        if self._auto_discover_sessions or self._auto_discover_panels:
            await self._refresh_targets(subscribe_new=True)
        return ok

    async def _subscribe_sessions(self, session_ids: list[str]) -> bool:
        """通过 Socket.IO 订阅 session 列表。"""
        if not session_ids:
            return True
        for sid in session_ids:
            if sid not in self._session_cursor:
                self._cold_sessions.add(sid)

        ack = await self._socket_call("com.claw.im.subscribeSessions", {
            "sessionIds": session_ids, "cursors": self._session_cursor,
            "limit": self.config.watch_limit,
        })
        if not ack.get("result"):
            self.logger.error("subscribeSessions failed: {}", ack.get('message', 'unknown error'))
            return False

        # 处理订阅返回的初始事件
        data = ack.get("data")
        items: list[dict[str, Any]] = []
        if isinstance(data, list):
            items = [i for i in data if isinstance(i, dict)]
        elif isinstance(data, dict):
            sessions = data.get("sessions")
            if isinstance(sessions, list):
                items = [i for i in sessions if isinstance(i, dict)]
            elif "sessionId" in data:
                items = [data]
        for p in items:
            await self._handle_watch_payload(p, "session")
        return True

    async def _subscribe_panels(self, panel_ids: list[str]) -> bool:
        """通过 Socket.IO 订阅 panel 列表。"""
        if not self._auto_discover_panels and not panel_ids:
            return True
        ack = await self._socket_call("com.claw.im.subscribePanels", {"panelIds": panel_ids})
        if not ack.get("result"):
            self.logger.error("subscribePanels failed: {}", ack.get('message', 'unknown error'))
            return False
        return True

    async def _socket_call(self, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """发起 Socket.IO 请求-响应调用。"""
        if not self._socket:
            return {"result": False, "message": "socket not connected"}
        try:
            raw = await self._socket.call(event_name, payload, timeout=10)
        except Exception as e:
            return {"result": False, "message": str(e)}
        return raw if isinstance(raw, dict) else {"result": True, "data": raw}

    # ---- 刷新/发现 --------------------------------------------------------

    async def _refresh_loop(self) -> None:
        """周期性刷新目标目录（自动发现新 session/panel）。"""
        interval_s = max(1.0, self.config.refresh_interval_ms / 1000.0)
        while self._running:
            await asyncio.sleep(interval_s)
            try:
                await self._refresh_targets(subscribe_new=self._ws_ready)
            except Exception as e:
                self.logger.warning("refresh failed: {}", e)
            if self._fallback_mode:
                await self._ensure_fallback_workers()

    async def _refresh_targets(self, subscribe_new: bool) -> None:
        """按需刷新 session 和 panel 目录。"""
        if self._auto_discover_sessions:
            await self._refresh_sessions_directory(subscribe_new)
        if self._auto_discover_panels:
            await self._refresh_panels(subscribe_new)

    async def _refresh_sessions_directory(self, subscribe_new: bool) -> None:
        """拉取 session 目录，发现新 session 时订阅或启用轮询。"""
        try:
            response = await self._post_json("/api/claw/sessions/list", {})
        except Exception as e:
            self.logger.warning("listSessions failed: {}", e)
            return

        sessions = response.get("sessions")
        if not isinstance(sessions, list):
            return

        new_ids: list[str] = []
        for s in sessions:
            if not isinstance(s, dict):
                continue
            sid = _str_field(s, "sessionId")
            if not sid:
                continue
            if sid not in self._session_set:
                self._session_set.add(sid)
                new_ids.append(sid)
                if sid not in self._session_cursor:
                    self._cold_sessions.add(sid)
            # 维护 converseId → session_id 映射
            cid = _str_field(s, "converseId")
            if cid:
                self._session_by_converse[cid] = sid

        if not new_ids:
            return
        if self._ws_ready and subscribe_new:
            await self._subscribe_sessions(new_ids)
        if self._fallback_mode:
            await self._ensure_fallback_workers()

    async def _refresh_panels(self, subscribe_new: bool) -> None:
        """拉取 panel 目录，发现新 panel 时订阅或启用轮询。"""
        try:
            response = await self._post_json("/api/claw/groups/get", {})
        except Exception as e:
            self.logger.warning("getWorkspaceGroup failed: {}", e)
            return

        raw_panels = response.get("panels")
        if not isinstance(raw_panels, list):
            return

        new_ids: list[str] = []
        for p in raw_panels:
            if not isinstance(p, dict):
                continue
            # type != 0 的 panel 跳过（非普通面板）
            pt = p.get("type")
            if isinstance(pt, int) and pt != 0:
                continue
            pid = _str_field(p, "id", "_id")
            if pid and pid not in self._panel_set:
                self._panel_set.add(pid)
                new_ids.append(pid)

        if not new_ids:
            return
        if self._ws_ready and subscribe_new:
            await self._subscribe_panels(new_ids)
        if self._fallback_mode:
            await self._ensure_fallback_workers()

    # ---- 轮询降级工作线程 -------------------------------------------------

    async def _ensure_fallback_workers(self) -> None:
        """为所有目标启用轮询降级工作线程。"""
        if not self._running:
            return
        self._fallback_mode = True
        for sid in sorted(self._session_set):
            t = self._session_fallback_tasks.get(sid)
            if not t or t.done():
                self._session_fallback_tasks[sid] = asyncio.create_task(self._session_watch_worker(sid))
        for pid in sorted(self._panel_set):
            t = self._panel_fallback_tasks.get(pid)
            if not t or t.done():
                self._panel_fallback_tasks[pid] = asyncio.create_task(self._panel_poll_worker(pid))

    async def _stop_fallback_workers(self) -> None:
        """停止所有轮询降级工作线程。"""
        self._fallback_mode = False
        tasks = [*self._session_fallback_tasks.values(), *self._panel_fallback_tasks.values()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._session_fallback_tasks.clear()
        self._panel_fallback_tasks.clear()

    async def _session_watch_worker(self, session_id: str) -> None:
        """session 轮询工作线程：通过 watch 长轮询拉取新消息。"""
        while self._running and self._fallback_mode:
            try:
                payload = await self._post_json("/api/claw/sessions/watch", {
                    "sessionId": session_id, "cursor": self._session_cursor.get(session_id, 0),
                    "timeoutMs": self.config.watch_timeout_ms, "limit": self.config.watch_limit,
                })
                await self._handle_watch_payload(payload, "session")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning("watch fallback error ({}): {}", session_id, e)
                await asyncio.sleep(max(0.1, self.config.retry_delay_ms / 1000.0))

    async def _panel_poll_worker(self, panel_id: str) -> None:
        """panel 轮询工作线程：周期性拉取消息列表。"""
        sleep_s = max(1.0, self.config.refresh_interval_ms / 1000.0)
        while self._running and self._fallback_mode:
            try:
                resp = await self._post_json("/api/claw/groups/panels/messages", {
                    "panelId": panel_id, "limit": min(100, max(1, self.config.watch_limit)),
                })
                msgs = resp.get("messages")
                if isinstance(msgs, list):
                    # 逆序处理（旧到新）
                    for m in reversed(msgs):
                        if not isinstance(m, dict):
                            continue
                        evt = _make_synthetic_event(
                            message_id=str(m.get("messageId") or ""),
                            author=str(m.get("author") or ""),
                            content=m.get("content"),
                            meta=m.get("meta"), group_id=str(resp.get("groupId") or ""),
                            converse_id=panel_id, timestamp=m.get("createdAt"),
                            author_info=m.get("authorInfo"),
                        )
                        await self._process_inbound_event(panel_id, evt, "panel")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.warning("panel polling error ({}): {}", panel_id, e)
            await asyncio.sleep(sleep_s)

    # ---- 入站事件处理 -----------------------------------------------------

    async def _handle_watch_payload(self, payload: dict[str, Any], target_kind: str) -> None:
        """处理 watch 或 Socket.IO 推送的 payload。"""
        if not isinstance(payload, dict):
            return
        target_id = _str_field(payload, "sessionId")
        if not target_id:
            return

        # 每个目标加锁，避免并发处理
        lock = self._target_locks.setdefault(f"{target_kind}:{target_id}", asyncio.Lock())
        async with lock:
            prev = self._session_cursor.get(target_id, 0) if target_kind == "session" else 0
            pc = payload.get("cursor")
            if target_kind == "session" and isinstance(pc, int) and pc >= 0:
                self._mark_session_cursor(target_id, pc)

            raw_events = payload.get("events")
            if not isinstance(raw_events, list):
                return
            # 冷启动 session 跳过历史消息
            if target_kind == "session" and target_id in self._cold_sessions:
                self._cold_sessions.discard(target_id)
                return

            for event in raw_events:
                if not isinstance(event, dict):
                    continue
                seq = event.get("seq")
                # 更新游标到最新 seq
                if target_kind == "session" and isinstance(seq, int) and seq > self._session_cursor.get(target_id, prev):
                    self._mark_session_cursor(target_id, seq)
                if event.get("type") == "message.add":
                    await self._process_inbound_event(target_id, event, target_kind)

    async def _process_inbound_event(self, target_id: str, event: dict[str, Any], target_kind: str) -> None:
        """处理单条入站事件，执行鉴权、去重、延迟聚合等逻辑。"""
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return

        author = _str_field(payload, "author")
        # 跳过机器人自身消息和未授权用户
        if not author or (self.config.agent_user_id and author == self.config.agent_user_id):
            return
        if not self.is_allowed(author):
            return

        message_id = _str_field(payload, "messageId")
        seen_key = f"{target_kind}:{target_id}"
        # 消息 ID 去重
        if message_id and self._remember_message_id(seen_key, message_id):
            return

        raw_body = normalize_mochat_content(payload.get("content")) or "[empty message]"
        ai = _safe_dict(payload.get("authorInfo"))
        sender_name = _str_field(ai, "nickname", "email")
        sender_username = _str_field(ai, "agentId")

        group_id = _str_field(payload, "groupId")
        is_group = bool(group_id)
        was_mentioned = resolve_was_mentioned(payload, self.config.agent_user_id)
        require_mention = target_kind == "panel" and is_group and resolve_require_mention(self.config, target_id, group_id)
        use_delay = target_kind == "panel" and self.config.reply_delay_mode == "non-mention"

        # 要求 @mention 但未被 @ 且不使用延迟 → 跳过
        if require_mention and not was_mentioned and not use_delay:
            return

        entry = MochatBufferedEntry(
            raw_body=raw_body, author=author, sender_name=sender_name,
            sender_username=sender_username, timestamp=parse_timestamp(event.get("timestamp")),
            message_id=message_id, group_id=group_id,
        )

        # panel 非@消息走延迟聚合；@mention 立即 flush
        if use_delay:
            delay_key = seen_key
            if was_mentioned:
                await self._flush_delayed_entries(delay_key, target_id, target_kind, "mention", entry)
            else:
                await self._enqueue_delayed_entry(delay_key, target_id, target_kind, entry)
            return

        await self._dispatch_entries(target_id, target_kind, [entry], was_mentioned)

    # ---- 去重/缓冲 --------------------------------------------------------

    def _remember_message_id(self, key: str, message_id: str) -> bool:
        """记录已见消息 ID，返回 True 表示已见过（应跳过）。"""
        seen_set = self._seen_set.setdefault(key, set())
        seen_queue = self._seen_queue.setdefault(key, deque())
        if message_id in seen_set:
            return True
        seen_set.add(message_id)
        seen_queue.append(message_id)
        # 超过上限时移除最旧的 ID（LRU 淘汰）
        while len(seen_queue) > MAX_SEEN_MESSAGE_IDS:
            seen_set.discard(seen_queue.popleft())
        return False

    async def _enqueue_delayed_entry(self, key: str, target_id: str, target_kind: str, entry: MochatBufferedEntry) -> None:
        """将条目加入延迟队列，并（重）启动延迟 flush 定时器。"""
        state = self._delay_states.setdefault(key, DelayState())
        async with state.lock:
            state.entries.append(entry)
            if state.timer:
                state.timer.cancel()
            state.timer = asyncio.create_task(self._delay_flush_after(key, target_id, target_kind))

    async def _delay_flush_after(self, key: str, target_id: str, target_kind: str) -> None:
        """延迟 reply_delay_ms 后 flush 队列。"""
        await asyncio.sleep(max(0, self.config.reply_delay_ms) / 1000.0)
        await self._flush_delayed_entries(key, target_id, target_kind, "timer", None)

    async def _flush_delayed_entries(self, key: str, target_id: str, target_kind: str, reason: str, entry: MochatBufferedEntry | None) -> None:
        """flush 延迟队列：取出所有条目并派发。reason 为 "mention" 时 was_mentioned=True。"""
        state = self._delay_states.setdefault(key, DelayState())
        async with state.lock:
            if entry:
                state.entries.append(entry)
            current = asyncio.current_task()
            # 取消其他定时器（避免重复 flush）
            if state.timer and state.timer is not current:
                state.timer.cancel()
            state.timer = None
            entries = state.entries[:]
            state.entries.clear()
        if entries:
            await self._dispatch_entries(target_id, target_kind, entries, reason == "mention")

    async def _dispatch_entries(self, target_id: str, target_kind: str, entries: list[MochatBufferedEntry], was_mentioned: bool) -> None:
        """将缓冲条目合并为单条消息并派发到消息总线。"""
        if not entries:
            return
        last = entries[-1]
        is_group = bool(last.group_id)
        body = build_buffered_body(entries, is_group) or "[empty message]"
        await self._handle_message(
            sender_id=last.author, chat_id=target_id, content=body,
            metadata={
                "message_id": last.message_id, "timestamp": last.timestamp,
                "is_group": is_group, "group_id": last.group_id,
                "sender_name": last.sender_name, "sender_username": last.sender_username,
                "target_kind": target_kind, "was_mentioned": was_mentioned,
                "buffered_count": len(entries),
            },
        )

    async def _cancel_delay_timers(self) -> None:
        """取消所有延迟 flush 定时器。"""
        for state in self._delay_states.values():
            if state.timer:
                state.timer.cancel()
        self._delay_states.clear()

    # ---- notify 处理器 ----------------------------------------------------

    async def _handle_notify_chat_message(self, payload: Any) -> None:
        """处理 notify:chat.message.* 事件（panel 消息推送）。"""
        if not isinstance(payload, dict):
            return
        group_id = _str_field(payload, "groupId")
        panel_id = _str_field(payload, "converseId", "panelId")
        if not group_id or not panel_id:
            return
        # 仅处理已订阅的 panel
        if self._panel_set and panel_id not in self._panel_set:
            return

        evt = _make_synthetic_event(
            message_id=str(payload.get("_id") or payload.get("messageId") or ""),
            author=str(payload.get("author") or ""),
            content=payload.get("content"), meta=payload.get("meta"),
            group_id=group_id, converse_id=panel_id,
            timestamp=payload.get("createdAt"), author_info=payload.get("authorInfo"),
        )
        await self._process_inbound_event(panel_id, evt, "panel")

    async def _handle_notify_inbox_append(self, payload: Any) -> None:
        """处理 notify:chat.inbox.append 事件（session 收件箱新消息）。"""
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return
        detail = payload.get("payload")
        if not isinstance(detail, dict):
            return
        # 带 groupId 的是 panel 消息，由其他处理器处理
        if _str_field(detail, "groupId"):
            return
        converse_id = _str_field(detail, "converseId")
        if not converse_id:
            return

        # 通过 converseId 查找 session_id；未知则刷新目录
        session_id = self._session_by_converse.get(converse_id)
        if not session_id:
            await self._refresh_sessions_directory(self._ws_ready)
            session_id = self._session_by_converse.get(converse_id)
        if not session_id:
            return

        evt = _make_synthetic_event(
            message_id=str(detail.get("messageId") or payload.get("_id") or ""),
            author=str(detail.get("messageAuthor") or ""),
            content=str(detail.get("messagePlainContent") or detail.get("messageSnippet") or ""),
            meta={"source": "notify:chat.inbox.append", "converseId": converse_id},
            group_id="", converse_id=converse_id, timestamp=payload.get("createdAt"),
        )
        await self._process_inbound_event(session_id, evt, "session")

    # ---- 游标持久化 --------------------------------------------------------

    def _mark_session_cursor(self, session_id: str, cursor: int) -> None:
        """更新 session 游标（仅前进），并触发防抖保存。"""
        if cursor < 0 or cursor < self._session_cursor.get(session_id, 0):
            return
        self._session_cursor[session_id] = cursor
        if not self._cursor_save_task or self._cursor_save_task.done():
            self._cursor_save_task = asyncio.create_task(self._save_cursor_debounced())

    async def _save_cursor_debounced(self) -> None:
        """防抖保存游标到磁盘。"""
        await asyncio.sleep(CURSOR_SAVE_DEBOUNCE_S)
        await self._save_session_cursors()

    async def _load_session_cursors(self) -> None:
        """从磁盘加载游标。"""
        if not self._cursor_path.exists():
            return
        try:
            data = json.loads(self._cursor_path.read_text("utf-8"))
        except Exception as e:
            self.logger.warning("Failed to read cursor file: {}", e)
            return
        cursors = data.get("cursors") if isinstance(data, dict) else None
        if isinstance(cursors, dict):
            for sid, cur in cursors.items():
                if isinstance(sid, str) and isinstance(cur, int) and cur >= 0:
                    self._session_cursor[sid] = cur

    async def _save_session_cursors(self) -> None:
        """保存游标到磁盘。"""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._cursor_path.write_text(json.dumps({
                "schemaVersion": 1, "updatedAt": datetime.utcnow().isoformat(),
                "cursors": self._session_cursor,
            }, ensure_ascii=False, indent=2) + "\n", "utf-8")
        except Exception as e:
            self.logger.warning("Failed to save cursor file: {}", e)

    # ---- HTTP 辅助 --------------------------------------------------------

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """发送 POST JSON 请求，返回解析后的 data 字段。"""
        if not self._http:
            raise RuntimeError("Mochat HTTP client not initialized")
        url = f"{self.config.base_url.strip().rstrip('/')}{path}"
        response = await self._http.post(url, headers={
            "Content-Type": "application/json", "X-Claw-Token": self.config.claw_token,
        }, json=payload)
        if not response.is_success:
            raise RuntimeError(f"Mochat HTTP {response.status_code}: {response.text[:200]}")
        try:
            parsed = response.json()
        except Exception:
            parsed = response.text
        # Mochat API 返回 {code, message, data} 结构；code != 200 视为错误
        if isinstance(parsed, dict) and isinstance(parsed.get("code"), int):
            if parsed["code"] != 200:
                msg = str(parsed.get("message") or parsed.get("name") or "request failed")
                raise RuntimeError(f"Mochat API error: {msg} (code={parsed['code']})")
            data = parsed.get("data")
            return data if isinstance(data, dict) else {}
        return parsed if isinstance(parsed, dict) else {}

    async def _api_send(self, path: str, id_key: str, id_val: str,
                        content: str, reply_to: str | None, group_id: str | None = None) -> dict[str, Any]:
        """统一的 session/panel 发送辅助函数。"""
        body: dict[str, Any] = {id_key: id_val, "content": content}
        if reply_to:
            body["replyTo"] = reply_to
        if group_id:
            body["groupId"] = group_id
        return await self._post_json(path, body)

    @staticmethod
    def _read_group_id(metadata: dict[str, Any]) -> str | None:
        """从出站消息元数据中读取 group_id。"""
        if not isinstance(metadata, dict):
            return None
        value = metadata.get("group_id") or metadata.get("groupId")
        return value.strip() if isinstance(value, str) and value.strip() else None
