"""WebSocket 服务端渠道：biscuitbot 作为 WebSocket 服务端，为已连接的客户端提供服务。

所属模块与项目作用
==================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 WebSocket 平台组件。
在项目架构中起到的作用：作为 WebSocket 服务端运行，将 Web UI 及其他 WebSocket 客户端的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：biscuitbot 自身作为 WebSocket 服务端，客户端通过 ws:// 或 wss:// 连接。
- 鉴权：支持静态 token、动态签发令牌（token_issue_path）以及 client_id 白名单三种方式。
- 监听方式：支持 TCP 监听（host:port）和 Unix 域套接字（unix_socket_path）两种模式。
- TLS 支持：可选配置 SSL 证书启用 WSS（安全 WebSocket）。
- 多会话管理：每个连接拥有独立会话（chat_id），支持订阅/取消订阅多个会话。
- 流式响应：支持渐进式消息推送（delta、stream_end、reasoning_delta 等）。
- 媒体处理：支持 base64 data URL 解码保存、图片/视频上传限制与 MIME 白名单校验。
- 信封协议：支持新式 JSON 信封（new_chat/attach/message/fork_chat 等）与旧式纯文本帧。
- 工作区作用域：支持多工作区隔离，按会话维度管理工作区配置。
- 转录与追踪：支持对话转录、全链路追踪事件推送。
"""

from __future__ import annotations  # 延迟注解求值，允许类型注解引用尚未定义的类型

import asyncio  # 异步事件循环与并发原语
import hmac  # HMAC 安全比较（token 校验）
import json  # JSON 序列化/反序列化（消息帧解析）
import re  # 正则表达式（chat_id 格式校验、data URL 解析）
import ssl  # SSL/TLS 上下文（WSS 安全连接）
import uuid  # UUID 生成（会话 ID、匿名客户端 ID）
from collections.abc import Callable  # 可调用对象类型
from contextlib import suppress  # 上下文管理器：忽略指定异常
from pathlib import Path  # 路径处理（Unix 套接字、媒体文件）
from typing import Any, Self, TypeGuard  # 类型注解支持

from pydantic import Field, field_validator, model_validator  # Pydantic 模型字段与校验器
from websockets.asyncio.server import ServerConnection, serve, unix_serve  # WebSocket 服务端（异步）
from websockets.exceptions import ConnectionClosed  # 连接关闭异常
from websockets.http11 import Request as WsRequest  # HTTP/1.1 请求（WS 升级检测）

from biscuitbot.bus.events import OUTBOUND_META_AGENT_UI, OutboundMessage  # 出站消息事件及 Agent UI 元数据键
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_media_dir  # 媒体目录获取
from biscuitbot.config.schema import Base  # 配置模型基类
from biscuitbot.security.workspace_access import (  # 工作区访问控制
    WORKSPACE_SCOPE_METADATA_KEY,  # 工作区作用域元数据键
    WorkspaceScopeError,  # 工作区作用域错误
)
from biscuitbot.session.goal_state import goal_state_ws_blob  # 目标状态 WebSocket 数据块
from biscuitbot.session.webui_turns import websocket_turn_wall_started_at  # WebUI 轮次启动时间
from biscuitbot.utils.media_decode import (  # 媒体解码工具
    FileSizeExceeded,  # 文件大小超限异常
    save_base64_data_url,  # 保存 base64 data URL 为文件
)
from biscuitbot.webui.cli_apps_api import normalize_cli_app_mentions  # CLI 应用提及归一化
from biscuitbot.webui.forking import handle_webui_fork_chat  # 会话分叉处理
from biscuitbot.webui.gateway_services import GatewayServices  # 网关服务集合
from biscuitbot.webui.http_utils import (  # HTTP 工具函数
    normalize_config_path as _normalize_config_path,  # 配置路径归一化
)
from biscuitbot.webui.http_utils import (
    parse_request_path as _parse_request_path,  # 请求路径解析
)
from biscuitbot.webui.http_utils import (
    query_first as _query_first,  # 查询参数取首值
)
from biscuitbot.webui.mcp_presets_api import normalize_mcp_preset_mentions  # MCP 预设提及归一化
from biscuitbot.webui.transcription_ws import webui_transcription_event  # 转录 WebSocket 事件
from biscuitbot.webui.websocket_logging import websockets_server_logger  # WebSocket 服务端日志器


class WebSocketConfig(Base):
    """WebSocket 服务端渠道配置。

    客户端通过形如 ``ws://{host}:{port}{path}?client_id=...&token=...`` 的 URL 连接。
    - ``client_id``：用于 ``allow_from`` 授权；若省略，将生成一个值并记录到日志。
    - ``token``：若非空，``token`` 查询参数可匹配此静态密钥；来自 ``token_issue_path`` 的短期令牌也被接受。
    - ``token_issue_path``：若非空，对该路径的 **GET**（HTTP/1.1）请求返回 JSON
      ``{"token": "...", "expires_in": <秒>}``；打开 WebSocket 时使用 ``?token=...``。
      必须与 ``path``（WS 升级路径）不同。若客户端与 biscuitbot 运行在 **同一进程**
      且共享 asyncio 循环，请使用线程或异步 HTTP 客户端执行 GET——不要在协程中
      调用阻塞的 ``urllib`` 或同步 ``httpx``。
    - ``token_issue_secret``：若非空，令牌请求必须发送 ``Authorization: Bearer <secret>`` 或
      ``X-Biscuitbot-Auth: <secret>``。
    - ``websocket_requires_token``：若为 True，握手必须包含有效令牌（静态或已签发且未过期）。
    - 每个连接拥有自己的会话：唯一的 ``chat_id`` 内部映射到代理会话。
    - 出站消息中的 ``media`` 字段包含本地文件系统路径；远程客户端需要
      共享文件系统或 HTTP 文件服务器才能访问这些文件。
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    unix_socket_path: str = ""
    path: str = "/"
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    # 默认 36 MB，上限 40 MB：支持最多 4 张约 6 MB 的图片（经客户端
    # Worker 归一化后，见 webui Composer）。4 × 6 MB × 1.37（base64 开销）
    # + 信封帧仍低于 36 MB；40 MB 上限为发送方留出少量余量，且不会引入 DoS 风险。
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    ping_interval_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @field_validator("unix_socket_path")
    @classmethod
    def unix_socket_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if "\x00" in value:
            raise ValueError("unix_socket_path must not contain NUL bytes")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("unix_socket_path must be an absolute path")
        return str(path)

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('path must start with "/"')
        return _normalize_config_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError('token_issue_path must start with "/"')
        return _normalize_config_path(value)

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if not self.token_issue_path:
            return self
        if _normalize_config_path(self.token_issue_path) == _normalize_config_path(self.path):
            raise ValueError("token_issue_path must differ from path (the WebSocket upgrade path)")
        return self

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> Self:
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.token.strip() or self.token_issue_secret.strip():
            return self
        raise ValueError(
            "host is 0.0.0.0 (all interfaces) but neither token nor "
            "token_issue_secret is set — set one to prevent unauthenticated access"
        )


def publish_runtime_model_update(
    bus: MessageBus,
    model: str,
    model_preset: str | None,
) -> None:
    """将运行时模型快照入队，供 WebSocket 订阅者接收（渠道内扇出）。"""
    bus.outbound.put_nowait(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={
            "_runtime_model_updated": True,
            "model": model,
            "model_preset": model_preset,
        },
    ))


def _parse_inbound_payload(raw: str) -> str | None:
    """将客户端帧解析为文本；对空内容或无法识别的内容返回 None。"""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


# 接受 UUID 和短作用域键（如 "unified:default"）。保持能力命名空间足够小，
# 以排除路径遍历/引号注入攻击。
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _is_valid_chat_id(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """若帧为新式 JSON 信封则返回类型化字典，否则返回 None。

    当帧解析为带有字符串 ``type`` 字段的 JSON 对象时，视为合格信封。
    旧式帧（纯文本，或不含 ``type`` 的 ``{"content": ...}``）返回 None；
    调用方应回退到 :func:`_parse_inbound_payload` 处理这些帧。
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    if not isinstance(t, str):
        return None
    return data


# 单消息媒体限制。服务端限制略宽于客户端 ``Worker`` 归一化目标（6 MB）——
# 容忍客户端冗余，但仍将总入口量限制在 ``_MAX_IMAGES_PER_MESSAGE * _MAX_IMAGE_BYTES``，
# 该值完全在 ``max_message_bytes`` 范围内。
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024

# 图片 MIME 白名单——与 Composer 的 ``accept`` 列表一致。显式排除 SVG
# 以避免嵌入脚本的 XSS 攻击面。
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

_VIDEO_MIME_ALLOWED: frozenset[str] = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
})

_UPLOAD_MIME_ALLOWED: frozenset[str] = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED

_DATA_URL_MIME_RE = re.compile(r"^data:([^;,]+)(?:;[^,]*)*;base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """返回 ``data:<mime>;base64,...`` URL 的 MIME 类型，否则返回 ``None``。"""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


def _is_websocket_upgrade(request: WsRequest) -> bool:
    """检测真正的 WS 升级请求；对同一路径的普通 HTTP GET 应直接放行。"""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


class WebSocketChannel(BaseChannel):
    """运行本地 WebSocket 服务端；将文本/JSON 消息转发到消息总线。"""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        gateway: GatewayServices,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> 订阅该会话的连接集合（扇出目标）
        self._subs: dict[str, set[Any]] = {}
        # connection -> 该连接订阅的 chat_id 集合（断开时 O(1) 清理）
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> 旧式帧（省略路由）使用的默认 chat_id
        self._conn_default: dict[Any, str] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None

        self.gateway = gateway
        self._http_router = gateway.http
        self._tokens = gateway.tokens
        self._media = gateway.media
        self._transcripts = gateway.transcripts
        self._workspaces = gateway.workspaces

        self._stream_text_buffers: dict[tuple[str, str], list[str]] = {}

    # -- 订阅簿记 -----------------------------------------------------------

    def _workspace_controls_available(self, connection: Any) -> bool:
        """检查当前连接是否允许使用工作区控制功能（如切换工作区）。"""
        return self._http_router.workspace_controls_available(connection)

    def _attach(self, connection: Any, chat_id: str) -> None:
        """幂等地将 *connection* 订阅到 *chat_id*。"""
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    def _cleanup_connection(self, connection: Any) -> None:
        """从所有订阅集合中移除 *connection*；可安全多次调用。"""
        chat_ids = self._conn_chats.pop(connection, set())
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
        self._conn_default.pop(connection, None)

    async def _maybe_push_active_goal_state(self, chat_id: str) -> None:
        """在 *chat_id* 被订阅后，从会话元数据重放活动的持续目标。

        目标元数据存储在会话 JSONL 中，在网关重启后依然存在，但
        已连接的客户端通常通过 ``goal_state`` / ``turn_end`` 帧看到它。
        在此处推送使刷新+重连能恢复进度条，无需新的模型轮次。
        """
        if self.gateway.session_manager is None:
            return
        row = self.gateway.session_manager.read_session_file(f"websocket:{chat_id}")
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        blob = goal_state_ws_blob(meta)
        if not blob.get("active"):
            return
        await self.send_goal_state(chat_id, blob)

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """当轮次仍在活动时重放 ``goal_status: running``（同进程刷新）。"""
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """订阅后重放目标/运行进度条状态（同进程刷新）。"""
        await self._maybe_push_active_goal_state(chat_id)
        await self._maybe_push_turn_run_wall_clock(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """向单个连接发送控制事件（attached、error 等）。"""
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典，用于渠道注册时生成默认配置。"""
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        """返回归一化后的 WebSocket 升级路径，用于路由匹配。"""
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        """构建 SSL 上下文以启用 WSS（安全 WebSocket）；未配置证书时返回 None。"""
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    # -- HTTP 分发 ----------------------------------------------------------

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """将入站 HTTP 请求路由到 HTTP 处理器或 WS 升级。"""
        got, query = _parse_request_path(request.path)

        # WebSocket 升级——由渠道自身处理
        expected_ws = self._expected_path()
        if got == expected_ws and _is_websocket_upgrade(request):
            client_id = _query_first(query, "client_id") or ""
            if len(client_id) > 128:
                client_id = client_id[:128]
            if not self.is_allowed(client_id):
                return connection.respond(403, "Forbidden")
            return self._authorize_websocket_handshake(connection, query)

        # 其余请求交给 HTTP 处理器
        return await self._http_router.dispatch(connection, request)

    def _authorize_websocket_handshake(self, connection: Any, query: dict[str, list[str]]) -> Any:
        """校验 WebSocket 握手请求中的 token。

        鉴权优先级：
        1. 若配置了静态 ``token``，则查询参数 ``token`` 必须匹配静态密钥或有效的已签发令牌。
        2. 若 ``websocket_requires_token`` 为 True（无静态 token 时），必须提供有效的已签发令牌。
        3. 其他情况放行（但仍会消费已签发令牌以实现一次性使用）。
        校验失败时返回 401 响应。
        """
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return None
            if supplied and self._tokens.take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if self.config.websocket_requires_token:
            if supplied and self._tokens.take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            self._tokens.take_issued_token_if_valid(supplied)
        return None

    # -- 服务端生命周期与连接入口 -------------------------------------------

    async def start(self) -> None:
        """启动 WebSocket 服务端。

        支持 TCP（host:port）和 Unix 域套接字两种监听模式。
        服务端运行直到 ``stop()`` 被调用；退出时关闭服务端并清理 Unix 套接字文件。
        """
        from biscuitbot.utils.logging_bridge import redirect_lib_logging

        redirect_lib_logging("websockets", level="WARNING")
        ws_logger = websockets_server_logger()

        self._running = True
        self._stop_event = asyncio.Event()

        ssl_context = self._build_ssl_context()
        scheme = "wss" if ssl_context else "ws"

        async def process_request(
            connection: ServerConnection,
            request: WsRequest,
        ) -> Any:
            return await self._dispatch_http(connection, request)

        async def handler(connection: ServerConnection) -> None:
            await self._connection_loop(connection)

        self.logger.info(
            "WebSocket server listening on {}",
            (
                f"unix:{self.config.unix_socket_path}{self.config.path}"
                if self.config.unix_socket_path
                else f"{scheme}://{self.config.host}:{self.config.port}{self.config.path}"
            ),
        )
        if self.config.token_issue_path:
            self.logger.info(
                "WebSocket token issue route: {}",
                (
                    f"unix:{self.config.unix_socket_path}{_normalize_config_path(self.config.token_issue_path)}"
                    if self.config.unix_socket_path
                    else (
                        f"{scheme}://{self.config.host}:{self.config.port}"
                        f"{_normalize_config_path(self.config.token_issue_path)}"
                    )
                ),
            )

        async def runner() -> None:
            socket_path = self.config.unix_socket_path
            if socket_path:
                path_obj = Path(socket_path)
                path_obj.parent.mkdir(parents=True, exist_ok=True)
                with suppress(FileNotFoundError):
                    path_obj.unlink()
                server = await unix_serve(
                    handler,
                    socket_path,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    logger=ws_logger,
                )
                with suppress(OSError):
                    path_obj.chmod(0o600)
            else:
                server = await serve(
                    handler,
                    self.config.host,
                    self.config.port,
                    process_request=process_request,
                    max_size=self.config.max_message_bytes,
                    ping_interval=self.config.ping_interval_s,
                    ping_timeout=self.config.ping_timeout_s,
                    ssl=ssl_context,
                    logger=ws_logger,
                )
            try:
                assert self._stop_event is not None
                await self._stop_event.wait()
            finally:
                server.close()
                await server.wait_closed()
                if socket_path:
                    with suppress(FileNotFoundError):
                        Path(socket_path).unlink()

        self._server_task = asyncio.create_task(runner())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        """处理单个 WebSocket 连接的完整生命周期。

        流程：发送 ``ready`` 事件 → 注册默认会话 → 循环接收帧 →
        解析信封或纯文本 → 路由到消息总线。连接断开时清理订阅。
        """
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        client_id_raw = _query_first(query, "client_id")
        client_id = client_id_raw.strip() if client_id_raw else ""
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        default_chat_id = str(uuid.uuid4())

        try:
            await connection.send(
                json.dumps(
                    {
                        "event": "ready",
                        "chat_id": default_chat_id,
                        "client_id": client_id,
                    },
                    ensure_ascii=False,
                )
            )
            # 仅在 ready 成功发送后注册，避免乱序发送
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                # WebSocket 在握手时已完成鉴权（token），
                # 因此配对不适用。视为非 DM 以避免
                # 向已鉴权客户端发送配对码。
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata={"remote": getattr(connection, "remote_address", None)},
                    is_dm=False,
                )
        except Exception as e:
            self.logger.debug("connection ended: {}", e)
        finally:
            self._cleanup_connection(connection)

    # -- 入站 WebSocket 信封 -----------------------------------------------

    def _save_envelope_media(
        self,
        media: list[Any],
    ) -> tuple[list[str], str | None]:
        """解码并持久化 ``message`` 信封中的 ``media`` 项。

        成功时返回 ``(paths, None)``，首次失败时返回 ``([], reason)``——
        调用方应将 ``reason`` 反馈给客户端并跳过发布，确保不会有
        半成形的消息到达代理。失败时，同一调用中先前已写入磁盘的
        文件将被删除，避免部分入口泄露为孤儿文件。
        ``reason`` 是适合 UI 本地化的简短稳定令牌。

        数据结构：``list[{"data_url": str, "name"?: str | None}]``。
        """
        image_count = 0
        video_count = 0
        for item in media:
            mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = get_media_dir("websocket")
        paths: list[str] = []

        def _abort(reason: str) -> tuple[list[str], str]:
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(
                        "failed to unlink partial media {}: {}", p, exc
                    )
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            max_bytes = _MAX_VIDEO_BYTES if is_video else _MAX_IMAGE_BYTES
            try:
                saved = save_base64_data_url(
                    data_url, media_dir, max_bytes=max_bytes,
                )
            except FileSizeExceeded:
                return _abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """路由一个类型化入站信封（``new_chat`` / ``attach`` / ``message``）。"""
        t = envelope.get("type")
        if t == "new_chat":
            new_id = str(uuid.uuid4())
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._workspaces.scope_for_new_chat(
                    envelope,
                    controls_available=self._workspace_controls_available(connection),
                ),
            )
            if scope is None:
                return
            employee_id = self._employee_id_from_envelope(envelope)
            self._workspaces.persist_scope(new_id, scope)
            if employee_id:
                self._workspaces.persist_employee(new_id, employee_id)
            self._attach(connection, new_id)
            await self._send_event(connection, "attached", chat_id=new_id)
            updated_fields: dict[str, Any] = {
                "chat_id": new_id,
                "scope": "metadata",
                "workspace_scope": scope.payload(),
            }
            if employee_id:
                updated_fields["employee"] = employee_id
            await self._send_event(connection, "session_updated", **updated_fields)
            await self._hydrate_after_subscribe(new_id)
            return
        if t == "fork_chat":
            await handle_webui_fork_chat(self, connection, envelope)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            await self._hydrate_after_subscribe(cid)
            return
        if t == "set_workspace_scope":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._workspaces.scope_for_set_request(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._workspace_controls_available(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return
            self._workspaces.persist_scope(cid, scope)
            await self._send_event(
                connection,
                "session_updated",
                chat_id=cid,
                scope="metadata",
                workspace_scope=scope.payload(),
            )
            return
        if t == "set_employee":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            # employee 字段为空/缺失 → 解除绑定；显式传了非空员工 id 但未通过
            # 校验（不存在/已禁用）时报错，避免把拼错 id 悄悄当成解除绑定。
            raw_employee = envelope.get("employee")
            explicit = isinstance(raw_employee, str) and bool(raw_employee.strip())
            employee_id = self._employee_id_from_envelope(envelope)
            if explicit and employee_id is None:
                await self._send_event(
                    connection,
                    "error",
                    detail="unknown_employee",
                    reason=raw_employee.strip(),
                )
                return
            self._workspaces.persist_employee(cid, employee_id)
            await self._send_event(
                connection,
                "session_updated",
                chat_id=cid,
                scope="metadata",
                employee=employee_id or "",
            )
            return
        if t == "transcribe_audio":
            event, payload = await webui_transcription_event(envelope)
            await self._send_event(connection, event, **payload)
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not isinstance(content, str):
                await self._send_event(connection, "error", detail="missing content")
                return

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason="malformed",
                    )
                    return
                media_paths, reason = self._save_envelope_media(raw_media)
                if reason is not None:
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason=reason,
                    )
                    return

            # 允许纯图片轮次（附带媒体时 content 可能为空）
            if not content.strip() and not media_paths:
                await self._send_event(connection, "error", detail="missing content")
                return
            scope = await self._workspace_scope_or_error(
                connection,
                lambda: self._workspaces.scope_for_message(
                    envelope,
                    chat_id=cid,
                    chat_running=websocket_turn_wall_started_at(cid) is not None,
                    controls_available=self._workspace_controls_available(connection),
                ),
                chat_id=cid,
            )
            if scope is None:
                return

            # 首次使用时自动附加，使客户端无需单独 attach 即可一步发送
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)
            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            if envelope.get("webui") is True:
                metadata["webui"] = True
                metadata.update(self._transcripts.client_turn_metadata(envelope.get("turn_id")))
            cli_apps = normalize_cli_app_mentions(envelope.get("cli_apps"))
            if cli_apps:
                metadata["cli_apps"] = cli_apps
            mcp_presets = normalize_mcp_preset_mentions(envelope.get("mcp_presets"))
            if mcp_presets:
                metadata["mcp_presets"] = mcp_presets
            metadata[WORKSPACE_SCOPE_METADATA_KEY] = scope.metadata()
            self._workspaces.persist_scope(cid, scope)
            image_generation = envelope.get("image_generation")
            if isinstance(image_generation, dict) and image_generation.get("enabled") is True:
                aspect_ratio = image_generation.get("aspect_ratio")
                metadata["image_generation"] = {
                    "enabled": True,
                    "aspect_ratio": aspect_ratio if isinstance(aspect_ratio, str) else None,
                }
            if metadata.get("webui") is True and self.is_allowed(client_id):
                self._transcripts.append_user_message(
                    cid,
                    content,
                    metadata=metadata,
                    media_paths=media_paths or None,
                    cli_apps=cli_apps or None,
                    mcp_presets=mcp_presets or None,
                )
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
                is_dm=False,
            )
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    async def _workspace_scope_or_error(
        self,
        connection: Any,
        resolver: Callable[[], Any],
        *,
        chat_id: str | None = None,
    ) -> Any | None:
        """执行工作区作用域解析器，捕获 ``WorkspaceScopeError`` 并向客户端返回错误事件。"""
        try:
            return resolver()
        except WorkspaceScopeError as exc:
            await self._send_event(
                connection,
                "error",
                detail="workspace_scope_rejected",
                reason=exc.message,
                **({"chat_id": chat_id} if chat_id else {}),
            )
            return None

    def _employee_id_from_envelope(self, envelope: dict[str, Any]) -> str | None:
        """从信封中取出已启用员工 id；非法/未启用/不存在时返回 None（回退全局行为）。

        员工仅在 ``new_chat`` 时绑定；无法解析时静默降级，不阻断建会话。
        """
        raw = envelope.get("employee")
        if not isinstance(raw, str) or not raw.strip():
            return None
        employee_id = raw.strip()
        employees = getattr(getattr(self, "gateway", None), "employees", None)
        if employees is not None:
            existing = employees.get_employee(employee_id)
            if existing is None or not existing.get("enabled", True):
                return None
        return employee_id

    # -- 出站 WebSocket 事件 -----------------------------------------------

    async def stop(self) -> None:
        """停止 WebSocket 服务端，清理连接和令牌状态。"""
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._server_task:
            try:
                await self._server_task
            except Exception as e:
                self.logger.warning("server task error during shutdown: {}", e)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._tokens.clear()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """向单个连接发送原始帧，在 ConnectionClosed 时清理。"""
        try:
            await connection.send(raw)
        except ConnectionClosed:
            self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    async def send(self, msg: OutboundMessage) -> None:
        """将出站消息推送给订阅了对应 ``chat_id`` 的所有 WebSocket 连接。

        根据消息元数据分发不同事件类型：
        - ``_runtime_model_updated``：广播运行时模型变更
        - ``_agent_trace``：推送全链路追踪事件
        - ``_goal_state_sync`` / ``_goal_status``：推送目标状态
        - ``_turn_end``：推送轮次结束信号
        - ``_session_updated``：推送会话更新通知
        - ``_file_edit_events``：推送文件编辑事件
        - 普通消息：推送 ``message`` 事件（含文本、媒体、工具事件等）
        """
        if msg.metadata.get("_runtime_model_updated"):
            await self.send_runtime_model_updated(
                model_name=msg.metadata.get("model"),
                model_preset=msg.metadata.get("model_preset"),
            )
            return

        # 快照订阅者集合，确保迭代中 ConnectionClosed 清理是安全的
        conns = list(self._subs.get(msg.chat_id, ()))

        # 全链路追踪事件：直接推送给前端，不写入 transcript
        if msg.metadata.get("_agent_trace"):
            if conns:
                payload = {
                    "event": "agent_trace",
                    "chat_id": msg.chat_id,
                    "turn_id": msg.metadata.get("turn_id", ""),
                    "phase": msg.metadata.get("phase", ""),
                    "step": msg.metadata.get("step", ""),
                    "status": msg.metadata.get("status", ""),
                    "duration_ms": msg.metadata.get("duration_ms"),
                    "detail": msg.metadata.get("detail", {}),
                }
                raw = json.dumps(payload, ensure_ascii=False)
                for connection in conns:
                    await self._safe_send_to(connection, raw, label=" trace")
            return

        if not conns:
            if (
                msg.metadata.get("_progress")
                or msg.metadata.get("_file_edit_events")
                or msg.metadata.get("_turn_end")
                or msg.metadata.get("_session_updated")
                or msg.metadata.get("_goal_status")
                or msg.metadata.get("_goal_state_sync")
            ):
                self.logger.debug("no active subscribers for chat_id={}", msg.chat_id)
            else:
                self.logger.warning("no active subscribers for chat_id={}", msg.chat_id)
        if msg.metadata.get("_goal_state_sync"):
            if conns:
                blob = msg.metadata.get("goal_state")
                await self.send_goal_state(msg.chat_id, blob if isinstance(blob, dict) else {"active": False})
            return
        if msg.metadata.get("_goal_status"):
            if conns:
                status = msg.metadata.get("goal_status")
                if status in ("running", "idle"):
                    started_raw = msg.metadata.get("started_at", msg.metadata.get("goal_started_at"))
                    await self.send_goal_status(
                        msg.chat_id,
                        status,
                        started_at=float(started_raw) if isinstance(started_raw, int | float) else None,
                    )
            return
        # 信号：代理已完全完成当前轮次的处理
        if msg.metadata.get("_turn_end"):
            lat = msg.metadata.get("latency_ms")
            lat_i = int(lat) if isinstance(lat, (int, float)) else None
            gs = msg.metadata.get("goal_state")
            gs_blob = gs if isinstance(gs, dict) else None
            await self.send_turn_end(
                msg.chat_id,
                latency_ms=lat_i,
                goal_state=gs_blob,
                metadata=msg.metadata,
            )
            await self.send_session_updated(msg.chat_id, scope="thread")
            return
        if msg.metadata.get("_session_updated"):
            if conns:
                scope = msg.metadata.get("_session_update_scope")
                await self.send_session_updated(
                    msg.chat_id,
                    scope=scope if isinstance(scope, str) else None,
                )
            return
        if msg.metadata.get("_file_edit_events"):
            edits = msg.metadata.get("_file_edit_events")
            await self.send_file_edit_events(
                msg.chat_id,
                edits if isinstance(edits, list) else [],
                msg.metadata,
            )
            return
        text = msg.content
        wire_text = self._media.rewrite_local_markdown_images(text)
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": wire_text,
        }
        if msg.media:
            payload["media"] = msg.media
            urls: list[dict[str, str]] = []
            for entry in msg.media:
                signed = self._media.sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    urls.append(signed)
            if urls:
                payload["media_urls"] = urls
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        lat = msg.metadata.get("latency_ms")
        if isinstance(lat, (int, float)):
            payload["latency_ms"] = int(lat)
        if msg.metadata.get("_tool_events"):
            payload["tool_events"] = msg.metadata["_tool_events"]
        agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
        if agent_ui is not None:
            payload["agent_ui"] = agent_ui
        # 标记中间代理面包屑（工具调用提示、通用进度字符串），
        # 使 WS 客户端将其渲染为从属追踪行，而非对话回复。
        if msg.metadata.get("_tool_hint"):
            payload["kind"] = "tool_hint"
        elif msg.metadata.get("_progress"):
            payload["kind"] = "progress"
        phase = "activity" if payload.get("kind") in ("tool_hint", "progress") else "answer"
        self._transcripts.prepare_and_append(
            msg.chat_id,
            payload,
            metadata=msg.metadata,
            phase=phase,
            include_source=True,
            transcript_overrides={"text": text},
        )
        raw = json.dumps(payload, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """推送一块模型推理内容。镜像 ``send_delta`` 结构，使客户端
        接收到一个打开、原地更新、关闭的流——
        在活动助手气泡上方渲染，带闪烁标题，
        直到匹配的 ``reasoning_end`` 到达。
        """
        conns = list(self._subs.get(chat_id, ()))
        if not delta:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_delta",
            "chat_id": chat_id,
            "text": delta,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="reasoning",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning ")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """为原地渲染器关闭当前推理流片段。"""
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_end",
            "chat_id": chat_id,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="reasoning",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning_end ")

    async def send_file_edit_events(
        self,
        chat_id: str,
        edits: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """推送文件编辑事件给订阅客户端，用于实时显示文件变更。"""
        conns = list(self._subs.get(chat_id, ()))
        payload: dict[str, Any] = {
            "event": "file_edit",
            "chat_id": chat_id,
            "edits": edits,
        }
        self._transcripts.prepare_and_append(
            chat_id,
            payload,
            metadata=metadata,
            phase="activity",
        )
        raw = json.dumps(payload, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" file_edit ")

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """推送流式增量文本（``delta``）或流结束信号（``stream_end``）。

        流结束时，合并缓冲区中的完整文本，并重写本地 Markdown 图片链接，
        确保远程客户端可访问媒体。
        """
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        stream_key = (chat_id, str(meta.get("_stream_id") or ""))
        if meta.get("_stream_end"):
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
            buffered = self._stream_text_buffers.pop(stream_key, [])
            if delta:
                buffered.append(delta)
            full_text = "".join(buffered)
            rewritten = self._media.rewrite_local_markdown_images(full_text)
            if delta or rewritten != full_text:
                body["text"] = rewritten
        else:
            body = {
                "event": "delta",
                "chat_id": chat_id,
                "text": delta,
            }
            self._stream_text_buffers.setdefault(stream_key, []).append(delta)
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=meta,
            phase="answer",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" stream ")

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        goal_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """信号：代理已完全完成当前轮次的处理。"""
        conns = list(self._subs.get(chat_id, ()))
        body: dict[str, Any] = {"event": "turn_end", "chat_id": chat_id}
        if latency_ms is not None:
            body["latency_ms"] = int(latency_ms)
        if goal_state is not None:
            body["goal_state"] = goal_state
        self._transcripts.prepare_and_append(
            chat_id,
            body,
            metadata=metadata,
            phase="complete",
        )
        raw = json.dumps(body, ensure_ascii=False)
        if not conns:
            return
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" turn_end ")

    async def send_goal_state(self, chat_id: str, blob: dict[str, Any]) -> None:
        """为 *chat_id* 推送已持久化的目标状态快照（多会话隔离）。"""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body = {"event": "goal_state", "chat_id": chat_id, "goal_state": blob}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_state ")

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """通知已订阅客户端轮次已开始或结束（挂钟时间提示）。"""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "goal_status",
            "chat_id": chat_id,
            "status": status,
        }
        if status == "running" and started_at is not None:
            body["started_at"] = started_at
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_status ")

    async def send_session_updated(self, chat_id: str, *, scope: str | None = None) -> None:
        """通知 WebUI 客户端某会话行需要刷新。"""
        conns = list(self._conn_chats)
        if not conns:
            return
        body: dict[str, Any] = {"event": "session_updated", "chat_id": chat_id}
        if scope:
            body["scope"] = scope
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" session_updated ")

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        """向每个已打开的 WebSocket 连接广播运行时模型变更。"""
        conns = list(self._conn_chats)
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        body: dict[str, Any] = {
            "event": "runtime_model_updated",
            "model_name": model_name.strip(),
        }
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" runtime_model_updated ")
