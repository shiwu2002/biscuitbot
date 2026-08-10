"""WhatsApp 渠道实现，通过 Node.js 桥接进程接入。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 WhatsApp 平台组件。
在项目架构中起到的作用：通过 Node.js 桥接进程将 WhatsApp 的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：通过 Node.js 桥接进程（基于 @whiskeysockets/baileys 库）处理 WhatsApp Web 协议，
  Python 与 Node.js 之间通过 WebSocket 通信。
- 鉴权：首次使用需扫描二维码登录，桥接令牌持久化到本地文件（权限 0600）。
- 消息格式：支持文本和媒体（图片/文件/视频）的收发，语音消息自动转写。
- 群聊策略：支持 open（全部响应）和 mention（仅 @时响应）两种模式。
- 地址格式：支持旧版手机号（@s.whatsapp.net）和新版 LID（@lid.whatsapp.net）两种 JID 格式。
- 桥接管理：自动检测桥接源码变更并重新构建，基于哈希戳文件实现增量构建。
"""

import asyncio  # 异步事件循环与 WebSocket 通信
import hashlib  # 桥接源码哈希计算（用于增量构建判断）
import json  # JSON 序列化/反序列化（桥接消息协议）
import mimetypes  # MIME 类型猜测（媒体文件分类）
import os  # 环境变量传递
import secrets  # 安全随机令牌生成
import shutil  # 文件/目录复制与删除（桥接部署）
import subprocess  # 子进程管理（npm install/build）
from collections import OrderedDict  # 有序去重缓存（消息 ID 去重）
from contextlib import suppress  # 上下文管理器，抑制指定异常
from pathlib import Path  # 路径处理
from typing import Any, Literal  # 类型注解支持

from loguru import logger  # 日志记录
from pydantic import Field  # Pydantic 模型字段定义

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.schema import Base  # 配置模型基类


class WhatsAppConfig(Base):
    """WhatsApp 渠道配置。"""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"  # Node.js 桥接服务的 WebSocket 地址
    bridge_token: str = ""  # 桥接认证令牌（为空时自动生成并持久化）
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单
    group_policy: Literal["open", "mention"] = "open"  # 群聊策略：open=全部响应，mention=仅@时响应


def _bridge_token_path() -> Path:
    """返回桥接令牌持久化文件的路径。"""
    from biscuitbot.config.paths import get_runtime_subdir

    return get_runtime_subdir("whatsapp-auth") / "bridge-token"


def _load_or_create_bridge_token(path: Path) -> str:
    """加载已持久化的桥接令牌，或首次使用时创建一个。"""
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    with suppress(OSError):
        path.chmod(0o600)
    return token


class WhatsAppChannel(BaseChannel):
    """WhatsApp 渠道，通过 Node.js 桥接进程连接。

    桥接使用 @whiskeysockets/baileys 库处理 WhatsApp Web 协议。
    Python 与 Node.js 之间通过 WebSocket 通信。
    """

    name = "whatsapp"
    display_name = "WhatsApp"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return WhatsAppConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WhatsAppConfig.model_validate(config)
        super().__init__(config, bus)
        self._ws = None  # WebSocket 连接实例
        self._connected = False  # 桥接连接状态
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()  # 有序去重缓存（消息 ID）
        self._lid_to_phone: dict[str, str] = {}  # LID 到手机号的映射缓存
        self._bridge_token: str | None = None  # 桥接认证令牌（延迟初始化）

    def _effective_bridge_token(self) -> str:
        """解析桥接令牌，必要时生成本地密钥。"""
        if self._bridge_token is not None:
            return self._bridge_token
        configured = self.config.bridge_token.strip()
        if configured:
            self._bridge_token = configured
        else:
            self._bridge_token = _load_or_create_bridge_token(_bridge_token_path())
        return self._bridge_token

    async def login(self, force: bool = False) -> bool:
        """
        Set up and run the WhatsApp bridge for QR code login.

        This spawns the Node.js bridge process which handles the WhatsApp
        authentication flow. The process blocks until the user scans the QR code
        or interrupts with Ctrl+C.
        """
        try:
            bridge_dir = _ensure_bridge_setup()
        except RuntimeError:
            self.logger.exception("bridge setup failed")
            return False

        env = {**os.environ}
        env["BRIDGE_TOKEN"] = self._effective_bridge_token()
        env["AUTH_DIR"] = str(_bridge_token_path().parent)

        self.logger.info("Starting WhatsApp bridge for QR login...")
        try:
            subprocess.run(
                [shutil.which("npm"), "start"], cwd=bridge_dir, check=True, env=env
            )
        except subprocess.CalledProcessError:
            return False

        return True

    async def start(self) -> None:
        """通过连接桥接服务启动 WhatsApp 渠道。"""
        import websockets

        bridge_url = self.config.bridge_url

        self.logger.info("Connecting to WhatsApp bridge at {}...", bridge_url)

        self._running = True

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    await ws.send(
                        json.dumps({"type": "auth", "token": self._effective_bridge_token()})
                    )
                    self._connected = True
                    self.logger.info("Connected to WhatsApp bridge")

                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception:
                            self.logger.exception("Error handling bridge message")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                self.logger.warning("WhatsApp bridge connection error: {}", e)

                if self._running:
                    self.logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """停止 WhatsApp 渠道。"""
        self._running = False
        self._connected = False

        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage) -> None:
        """通过 WhatsApp 发送消息。"""
        if not self._ws or not self._connected:
            self.logger.warning("WhatsApp bridge not connected")
            return

        chat_id = msg.chat_id

        if msg.content:
            try:
                payload = {"type": "send", "to": chat_id, "text": msg.content}
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self.logger.exception("Error sending message")
                raise

        for media_path in msg.media or []:
            try:
                mime, _ = mimetypes.guess_type(media_path)
                payload = {
                    "type": "send_media",
                    "to": chat_id,
                    "filePath": media_path,
                    "mimetype": mime or "application/octet-stream",
                    "fileName": media_path.rsplit("/", 1)[-1],
                }
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self.logger.exception("Error sending media {}", media_path)
                raise

    async def _handle_bridge_message(self, raw: str) -> None:
        """处理来自桥接的消息。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return

        msg_type = data.get("type")

        if msg_type == "message":
            # Incoming message from WhatsApp
            # Deprecated by whatsapp: old phone number style typically: <phone>@s.whatspp.net
            pn = data.get("pn", "")
            # New LID sytle typically:
            sender = data.get("sender", "")
            content = data.get("content", "")
            message_id = data.get("id", "")

            # Extract just the phone number or lid as chat_id
            is_group = data.get("isGroup", False)
            was_mentioned = bool(data.get("wasMentioned", False) or data.get("isReplyToBot", False))

            if is_group and getattr(self.config, "group_policy", "open") == "mention":
                if not was_mentioned:
                    return

            # Classify by JID suffix: @s.whatsapp.net = phone, @lid.whatsapp.net = LID
            # The bridge's pn/sender fields don't consistently map to phone/LID across versions.
            raw_a = pn or ""
            participant = data.get("participant", "")
            raw_b = participant or sender or ""
            id_a = raw_a.split("@")[0] if "@" in raw_a else raw_a
            id_b = raw_b.split("@")[0] if "@" in raw_b else raw_b

            phone_id = ""
            lid_id = ""
            for raw, extracted in [(raw_a, id_a), (raw_b, id_b)]:
                if "@s.whatsapp.net" in raw:
                    phone_id = extracted
                elif "@lid.whatsapp.net" in raw:
                    lid_id = extracted
                elif extracted and not phone_id:
                    phone_id = extracted  # best guess for bare values

            sender_id = phone_id or self._lid_to_phone.get(lid_id, "") or lid_id or id_a or id_b
            if not self.is_allowed(sender_id):
                return

            if message_id:
                if message_id in self._processed_message_ids:
                    return
                self._processed_message_ids[message_id] = None
                while len(self._processed_message_ids) > 1000:
                    self._processed_message_ids.popitem(last=False)

            if phone_id and lid_id:
                self._lid_to_phone[lid_id] = phone_id

            self.logger.info("Sender phone={} lid={} → sender_id={}", phone_id or "(empty)", lid_id or "(empty)", sender_id)

            # Extract media paths (images/documents/videos downloaded by the bridge)
            media_paths = data.get("media") or []

            # Handle voice transcription if it's a voice message
            if content == "[Voice Message]":
                if media_paths:
                    self.logger.info("Transcribing voice message from {}...", sender_id)
                    transcription = await self.transcribe_audio(media_paths[0])
                    if transcription:
                        content = transcription
                        media_paths = []
                        self.logger.info("Transcribed voice from {}: {}...", sender_id, transcription[:50])
                    else:
                        content = "[Voice Message: Transcription failed]"
                else:
                    content = "[Voice Message: Audio not available]"

            # Build content tags matching Telegram's pattern: [image: /path] or [file: /path]
            if media_paths:
                for p in media_paths:
                    mime, _ = mimetypes.guess_type(p)
                    media_type = "image" if mime and mime.startswith("image/") else "file"
                    media_tag = f"[{media_type}: {p}]"
                    content = f"{content}\n{media_tag}" if content else media_tag

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False),
                    "is_forwarded": bool(data.get("isForwarded", False)),
                    "participant": participant or None,
                    "is_reply_to_bot": data.get("isReplyToBot", False),
                },
            )

        elif msg_type == "status":
            # Connection status update
            status = data.get("status")
            self.logger.info("Status: {}", status)

            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False

        elif msg_type == "qr":
            # QR code for authentication
            self.logger.info("Scan QR code in the bridge terminal to connect WhatsApp")

        elif msg_type == "error":
            self.logger.error("Bridge error: {}", data.get("error"))


def _ensure_bridge_setup() -> Path:
    """确保 WhatsApp 桥接已设置并构建完成。

    返回桥接目录。如果未找到 npm 或无法构建桥接，则抛出 RuntimeError。
    """
    from biscuitbot.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()
    stamp_file = user_bridge / ".biscuitbot-bridge-source-hash"

    # Find source bridge
    current_file = Path(__file__)
    pkg_bridge = current_file.parent.parent / "bridge"
    src_bridge = current_file.parent.parent.parent / "bridge"

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        raise RuntimeError(
            "WhatsApp bridge source not found. "
            "Try reinstalling: pip install --force-reinstall biscuitbot"
        )

    def source_hash(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if rel.parts and rel.parts[0] in {"node_modules", "dist"}:
                continue
            digest.update(rel.as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    expected_hash = source_hash(source)
    current_hash = stamp_file.read_text().strip() if stamp_file.exists() else None

    if (user_bridge / "dist" / "index.js").exists() and current_hash == expected_hash:
        return user_bridge

    if (user_bridge / "dist" / "index.js").exists() and current_hash != expected_hash:
        logger.info("WhatsApp bridge source changed; rebuilding bridge...")

    npm_path = shutil.which("npm")
    if not npm_path:
        raise RuntimeError("npm not found. Please install Node.js >= 18.")

    logger.info("Setting up WhatsApp bridge...")
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    logger.info("  Installing dependencies...")
    subprocess.run([npm_path, "install"], cwd=user_bridge, check=True, capture_output=True)

    logger.info("  Building...")
    subprocess.run([npm_path, "run", "build"], cwd=user_bridge, check=True, capture_output=True)
    stamp_file.write_text(expected_hash + "\n")

    logger.info("Bridge ready")
    return user_bridge
