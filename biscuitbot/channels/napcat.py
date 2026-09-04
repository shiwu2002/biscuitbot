"""Napcat（OneBot v11）QQ 渠道实现，通过 WebSocket 接入。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的 QQ（Napcat）平台组件。
在项目架构中起到的作用：通过 OneBot v11 协议将 QQ 的消息收发能力接入 biscuitbot 消息总线。

平台特点与接入方式
------------------
- 接入方式：通过 WebSocket 连接 Napcat（OneBot v11 实现）服务端，接收 QQ 消息事件。
- 鉴权：通过 Bearer Token 进行 WebSocket 连接认证（可选）。
- 消息格式：支持文本、图片、@提及、回复、表情等消息段（segment）。
- 群聊策略：支持 mention（仅@/回复时响应）、open（全部响应）、概率响应三种模式，
  支持按群 ID 覆盖全局策略。
- 媒体处理：入站图片通过 HTTP 下载（支持大小限制），出站图片支持 URL 和 base64。
- 自动重连：连接断开后自动重连，退避间隔 5s → 10s → 30s。
- 请求-响应：通过 echo 字段实现 WebSocket 上的请求-响应模式。
"""

from __future__ import annotations

import asyncio  # 异步事件循环与 WebSocket 通信
import base64  # base64 编码（本地图片上传）
import json  # JSON 序列化/反序列化（OneBot 协议）
import os  # 文件路径处理
import random  # 概率群聊策略的随机数生成
import time  # 时间戳生成（图片文件名）
import uuid  # 生成请求 echo 标识
from collections import deque  # 固定长度去重队列（消息 ID）
from pathlib import Path  # 路径处理
from typing import Annotated, Any, Literal  # 类型注解支持

import aiohttp  # 异步 HTTP 客户端（图片下载）
from loguru import logger  # 日志记录
from pydantic import Field  # Pydantic 模型字段定义
from websockets.asyncio.client import ClientConnection  # WebSocket 客户端连接类型
from websockets.asyncio.client import connect as ws_connect  # WebSocket 连接函数

from biscuitbot.bus.events import OutboundMessage  # 出站消息事件
from biscuitbot.bus.queue import MessageBus  # 消息总线
from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类
from biscuitbot.config.paths import get_media_dir  # 媒体文件目录
from biscuitbot.config.schema import Base  # 配置模型基类
from biscuitbot.security.network import validate_url_target  # URL 安全校验
from biscuitbot.utils.helpers import MessageIdDedup, safe_filename  # 消息去重与文件名安全化共享工具

_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=60)  # 图片下载超时时间（60秒）
_ACTION_TIMEOUT = 20.0  # OneBot action 请求超时时间（20秒）


# `"mention"`（仅@/回复时响应）| `"open"`（每条消息都响应）| [0, 1] 浮点数：
# @/回复始终响应；其他消息以概率 p 响应。0.0 等同于 "mention"，1.0 等同于 "open"。
GroupPolicy = Literal["mention", "open"] | Annotated[float, Field(ge=0.0, le=1.0)]


class NapcatConfig(Base):
    """Napcat（OneBot v11）渠道配置。"""

    enabled: bool = False
    ws_url: str = "ws://127.0.0.1:3001"  # Napcat WebSocket 服务地址
    access_token: str = ""  # WebSocket 连接认证令牌
    allow_from: list[str] = Field(default_factory=list)  # 允许的用户白名单
    allow_all: bool = False  # 静默放行：为 True 时所有用户可直接私聊，无需白名单或配对码
    group_policy: GroupPolicy = "mention"  # 群聊响应策略
    # 按群 ID（字符串形式）覆盖的群聊策略，如 {"123456": "open"}。
    # 群 ID 未列出时回退到 group_policy。
    group_policy_overrides: dict[str, GroupPolicy] = Field(default_factory=dict)
    welcome_new_members: bool = True  # 是否欢迎新入群成员
    # 入站图片下载的硬上限。超过此大小的图片将被丢弃。
    max_image_bytes: int = Field(default=20 * 1024 * 1024, ge=1)


class NapcatChannel(BaseChannel):
    """Napcat / OneBot v11 渠道。"""

    name = "napcat"
    display_name = "NapCat（QQ）"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        """返回默认配置字典。"""
        return NapcatConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = NapcatConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: NapcatConfig = config

        self._ws: ClientConnection | None = None  # WebSocket 连接实例
        self._http: aiohttp.ClientSession | None = None  # HTTP 会话（图片下载）
        self._media_root: Path = get_media_dir("channels/napcat")  # 媒体文件根目录
        self._self_id: int | None = None  # 机器人自身的 QQ 号
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}  # 待处理的 action 响应（echo → Future）
        self._processed_ids = MessageIdDedup(maxlen=2000)  # 已处理消息 ID 去重（LRU）
        self._bot_outbound_ids: deque[int] = deque(maxlen=2000)  # 机器人发送的消息 ID 队列（用于回复检测）
        self._background_tasks: set[asyncio.Task[None]] = set()  # 后台任务集合

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 Napcat 渠道，建立 WebSocket 连接并自动重连。"""
        if not self.config.ws_url:
            logger.error("napcat: ws_url not configured")
            return

        self._running = True
        self._http = aiohttp.ClientSession(timeout=_DOWNLOAD_TIMEOUT)

        backoff = iter((5, 10))  # then 30s forever
        while self._running:
            try:
                await self._run_once()
                backoff = iter((5, 10))  # reset after a clean session
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("napcat: connection lost: {}", e)
            if self._running:
                await asyncio.sleep(next(backoff, 30))

    async def _run_once(self) -> None:
        """执行一次 WebSocket 连接会话（含登录验证和消息分发）。"""
        headers = []
        if self.config.access_token:
            headers.append(("Authorization", f"Bearer {self.config.access_token}"))

        logger.info("napcat: connecting to {}", self.config.ws_url)
        async with ws_connect(self.config.ws_url, additional_headers=headers) as ws:
            self._ws = ws
            logger.info("napcat: connected")
            try:
                # Validate the connection before entering the dispatch loop.
                # Napcat may interleave meta_event frames before our echo
                # response, so dispatch any non-matching frames as we go.
                echo = uuid.uuid4().hex
                await ws.send(
                    json.dumps(
                        {"action": "get_login_info", "params": {}, "echo": echo},
                        ensure_ascii=False,
                    )
                )
                deadline = asyncio.get_running_loop().time() + _ACTION_TIMEOUT
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError("get_login_info timed out")
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict) and payload.get("echo") == echo:
                        data = payload.get("data") or {}
                        logger.info(
                            "napcat: logged in as {} (user_id={})",
                            data.get("nickname"),
                            data.get("user_id"),
                        )
                        break
                    await self._dispatch_frame(raw)

                async for raw in ws:
                    await self._dispatch_frame(raw)
            finally:
                self._ws = None
                self._fail_pending(RuntimeError("napcat: websocket disconnected"))

    async def stop(self) -> None:
        """停止 Napcat 渠道，关闭连接并清理资源。"""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                logger.debug("napcat: ws.close() failed during stop", exc_info=True)
            self._ws = None
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                logger.debug("napcat: http.close() failed during stop", exc_info=True)
            self._http = None
        self._fail_pending(RuntimeError("napcat: stopped"))
        tasks = list(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()

    def _fail_pending(self, err: BaseException) -> None:
        """将所有待处理的 Future 标记为异常（连接断开时调用）。"""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    # ------------------------------------------------------------------
    # 帧分发
    # ------------------------------------------------------------------

    async def _dispatch_frame(self, raw: str | bytes) -> None:
        """分发 WebSocket 帧：action 响应或事件推送。"""
        # logger.debug("dispatch frame {}", raw)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("napcat: dropping non-JSON frame")
            return
        if not isinstance(payload, dict):
            return

        # Action response: identified by `echo` and absence of post_type.
        if "echo" in payload and payload.get("post_type") is None:
            echo = payload.get("echo")
            fut = self._pending.pop(echo, None) if isinstance(echo, str) else None
            if fut and not fut.done():
                fut.set_result(payload)
            return

        if (sid := payload.get("self_id")) is not None:
            try:
                self._self_id = int(sid)
            except (TypeError, ValueError):
                pass

        post_type = payload.get("post_type")
        if post_type == "message":
            self._create_background_task(self._on_message(payload), "message")
        elif post_type == "notice":
            self._create_background_task(self._on_notice(payload), "notice")

    def _create_background_task(self, coro: Any, kind: str) -> None:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _done(done: asyncio.Task[None]) -> None:
            self._background_tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("napcat: {} handler failed: {}", kind, e)

        task.add_done_callback(_done)

    # ------------------------------------------------------------------
    # 入站：消息处理
    # ------------------------------------------------------------------

    async def _on_message(self, ev: dict[str, Any]) -> None:
        """处理入站消息事件。"""
        msg_id = ev.get("message_id")
        if isinstance(msg_id, int):
            if not self._processed_ids.check_and_mark(str(msg_id)):
                return

        message_type = ev.get("message_type")
        user_id = ev.get("user_id")
        if user_id is None or message_type not in ("group", "private"):
            return

        segments = self._normalize_segments(ev.get("message"))
        text, images, mentioned_self, reply_to_id = self._parse_segments(segments)

        media_paths: list[str] = []
        for info in images:
            if local := await self._download_image(info):
                media_paths.append(local)

        sender = ev.get("sender") or {}
        nickname = sender.get("card") or sender.get("nickname")

        if message_type == "group":
            group_id = ev.get("group_id")
            if group_id is None:
                return

            replying_to_bot = (
                isinstance(reply_to_id, int) and reply_to_id in self._bot_outbound_ids
            )
            if not self._should_reply_in_group(
                group_id=group_id,
                mentioned_self=mentioned_self,
                replying_to_bot=replying_to_bot,
            ):
                return

            chat_id = f"group:{group_id}"
            content = self._format_group_content(
                text=text,
                nickname=nickname,
                user_id=user_id,
            )
        else:
            chat_id = f"private:{user_id}"
            content = text

        if not content and not media_paths:
            return

        await self._handle_message(
            sender_id=str(user_id),
            chat_id=chat_id,
            content=content,
            media=media_paths or None,
            metadata={
                "message_id": msg_id,
                "is_group": message_type == "group",
                "nickname": nickname,
                "reply_to": reply_to_id,
            },
            is_dm=message_type == "private",  # 私聊传 True，便于未授权用户收到配对码
        )

    @staticmethod
    def _normalize_segments(message: Any) -> list[dict[str, Any]]:
        """将消息归一化为消息段列表。"""
        # Napcat defaults to array format. Treat raw strings as a single text
        # segment rather than parsing CQ codes — that path is fragile and
        # users can configure napcat to emit arrays.
        if isinstance(message, list):
            return [seg for seg in message if isinstance(seg, dict)]
        if isinstance(message, str) and message:
            return [{"type": "text", "data": {"text": message}}]
        return []

    def _parse_segments(
        self, segments: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]], bool, int | None]:
        """解析消息段列表，提取文本、图片、@标记和回复目标。"""
        parts: list[str] = []
        images: list[dict[str, Any]] = []
        mentioned_self = False
        reply_to: int | None = None
        self_id_str = str(self._self_id) if self._self_id is not None else None

        for seg in segments:
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text":
                if txt := data.get("text"):
                    parts.append(str(txt))
            elif stype == "image":
                # OneBot exposes the downloadable image at `url`. Napcat
                # additionally provides `file` (e.g. <md5>.png) and
                # `file_size` (bytes, sometimes a string).
                url = data.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    images.append(
                        {
                            "url": url,
                            "file": data.get("file"),
                            "file_size": data.get("file_size"),
                        }
                    )
                else:
                    logger.warning("napcat: received invalid image url: {}", url)
            elif stype == "at":
                qq = str(data.get("qq", ""))
                if self_id_str and qq == self_id_str:
                    mentioned_self = True
                else:
                    parts.append(f"@{qq}")
            elif stype == "reply":
                rid = data.get("id")
                try:
                    reply_to = int(rid) if rid is not None else None
                except (TypeError, ValueError):
                    pass
            elif stype == "face":
                parts.append(f"[face:{data.get('id', '')}]")

        text = " ".join(p.strip() for p in parts if p.strip()).strip()
        return text, images, mentioned_self, reply_to

    def _should_reply_in_group(
        self, *, group_id: Any, mentioned_self: bool, replying_to_bot: bool
    ) -> bool:
        """根据群聊策略判断是否应响应该群消息。"""
        if mentioned_self or replying_to_bot:
            return True
        policy = self.config.group_policy_overrides.get(str(group_id), self.config.group_policy)
        if policy == "open":
            return True
        if policy == "mention":
            return False
        # Probability case: float in [0.0, 1.0].
        return random.random() < float(policy)

    @staticmethod
    def _format_group_content(
        *,
        text: str,
        nickname: str,
        user_id: Any,
    ) -> str:
        """格式化群消息内容，添加发送者标签。"""
        label = nickname or str(user_id)
        return f"{label}: {text}"

    # ------------------------------------------------------------------
    # 入站：通知事件（成员入群等）
    # ------------------------------------------------------------------

    async def _on_notice(self, ev: dict[str, Any]) -> None:
        """处理通知事件（如群成员增加）。"""
        if ev.get("notice_type") != "group_increase" or not self.config.welcome_new_members:
            return

        group_id = ev.get("group_id")
        user_id = ev.get("user_id")
        if group_id is None or user_id is None:
            return

        try:
            group_id_int = int(group_id)
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            logger.warning("napcat: invalid group_increase ids group_id={} user_id={}", group_id, user_id)
            return

        nickname = await self._lookup_member_name(group_id_int, user_id_int)

        # Note: this routes through is_allowed(). For group bots set
        # `allow_from: ["*"]` (or include the joining user's id) for welcomes
        # to fire — same trust model as a regular inbound message.
        await self._handle_message(
            sender_id=str(user_id),
            chat_id=f"group:{group_id}",
            content=f"[group event] new member {nickname} joined group {group_id}",
            metadata={
                "is_group": True,
                "event": "group_increase",
            },
        )

    async def _lookup_member_name(self, group_id: int, user_id: int) -> str:
        """查询群成员昵称，失败时回退到用户 ID。"""
        try:
            resp = await self._call_action(
                "get_group_member_info",
                {"group_id": group_id, "user_id": user_id, "no_cache": True},
            )
            data = resp.get("data", {})
            # logger.debug("get_group_member_info: {}", resp)
            return data.get("card") or data.get("nickname") or str(user_id)
        except Exception as e:
            logger.warning("napcat: get_group_member_info failed: {}", e)
            return str(user_id)

    # ------------------------------------------------------------------
    # 出站消息
    # ------------------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        """通过 Napcat 发送消息。"""
        if self._ws is None:
            logger.warning("napcat: not connected, dropping outbound message")
            return

        kind, _, target = msg.chat_id.partition(":")
        if kind not in ("private", "group") or not target:
            logger.error("napcat: invalid chat_id '{}'", msg.chat_id)
            return

        segments: list[dict[str, Any]] = []
        for ref in msg.media or []:
            if seg := await self._build_image_segment(ref):
                segments.append(seg)
        if text := (msg.content or "").strip():
            segments.append({"type": "text", "data": {"text": text}})
        if not segments:
            return

        params: dict[str, Any] = {"message": segments}
        if kind == "group":
            params["message_type"] = "group"
            params["group_id"] = int(target)
        else:
            params["message_type"] = "private"
            params["user_id"] = int(target)

        resp = await self._call_action("send_msg", params)
        data = resp.get("data") or {}
        if (mid := data.get("message_id")) is not None:
            self._bot_outbound_ids.append(int(mid))

    async def _build_image_segment(self, ref: str) -> dict[str, Any] | None:
        """构建图片消息段，支持 URL 和本地路径（base64 编码）。"""
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref.startswith(("http://", "https://")):
            ok, err = validate_url_target(ref)
            if not ok:
                logger.warning("napcat: rejected remote image '{}': {}", ref, err)
                return None
            return {"type": "image", "data": {"file": ref}}
        # Local path → base64 so it works even when napcat runs on a
        # different host/container than biscuitbot.
        path = Path(os.path.expanduser(ref)).resolve()
        if not path.is_file():
            logger.warning("napcat: local image not found: {}", path)
            return None
        data = await asyncio.to_thread(path.read_bytes)
        return {"type": "image", "data": {"file": "base64://" + base64.b64encode(data).decode()}}

    async def _call_action(
        self,
        action: str,
        params: dict[str, Any],
        timeout: float = _ACTION_TIMEOUT,
    ) -> dict[str, Any]:
        """通过 echo 机制调用 OneBot action 并等待响应。"""
        if self._ws is None:
            raise RuntimeError("napcat: not connected")
        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send(
                json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            )
            resp = await asyncio.wait_for(fut, timeout=timeout)
            status = resp.get("status")
            retcode = resp.get("retcode")
            if (status and status != "ok") or (retcode not in (None, 0)):
                raise RuntimeError(
                    f"napcat: action {action} failed status={status!r} retcode={retcode!r}"
                )
            return resp
        finally:
            self._pending.pop(echo, None)

    # ------------------------------------------------------------------
    # 图片下载
    # ------------------------------------------------------------------

    async def _download_image(self, info: dict[str, Any]) -> str | None:
        """下载入站图片到本地，支持大小限制和流式读取。"""
        url = info.get("url")
        if not isinstance(url, str):
            return None
        # logger.debug("napcat: downloading image from {}", url)
        if self._http is None:
            return None
        ok, err = validate_url_target(url)
        if not ok:
            logger.warning("napcat: skip image '{}': {}", url, err)
            return None
        max_bytes = self.config.max_image_bytes

        # Reject upfront when napcat tells us the size and it's too big.
        try:
            declared_size = int(info["file_size"])
            if declared_size > max_bytes:
                logger.warning(
                    "napcat: image declared size={} exceeds max_image_bytes={} url={}",
                    declared_size,
                    max_bytes,
                    url,
                )
                return None
        except (TypeError, KeyError):
            pass

        try:
            async with self._http.get(url, allow_redirects=False) as resp:
                if 300 <= resp.status < 400:
                    logger.warning("napcat: image download redirect rejected url={}", url)
                    return None
                if resp.status >= 400:
                    logger.warning("napcat: image download status={} url={}", resp.status, url)
                    return None
                # Stream until EOF, capping memory at max_bytes. Don't use
                # content.read(max_bytes+1) — it returns only what's currently
                # buffered, which truncates chunked responses mid-image.
                buf = bytearray()
                truncated = False
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        truncated = True
                        break
                if truncated:
                    logger.warning(
                        "napcat: image exceeds max_image_bytes={} url={}", max_bytes, url
                    )
                    return None
                data = bytes(buf)
        except Exception as e:
            logger.warning("napcat: image download error url={} err={}", url, e)
            return None

        filename_hint = info.get("file")
        # safe_filename 对纯符号文件名会收敛为空串；空时回退时间戳名，避免写到目录上
        name = safe_filename(filename_hint or "") or f"{int(time.time() * 1000)}.jpg"
        path = self._media_root / name
        try:
            await asyncio.to_thread(path.write_bytes, data)
        except OSError as e:
            logger.warning("napcat: failed to save image: {}", e)
            return None
        return str(path)
