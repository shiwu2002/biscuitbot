"""WebUI 微信扫码登录会话管理。

所属模块与项目作用
==================
本文件位于 biscuitbot/webui 目录，是 WebUI 引导界面微信接入的轻量协调组件。
在项目架构中起到的作用：持有单个瞬时 :class:`WeixinChannel` 实例，暴露
非阻塞的「取二维码 → 逐次轮询 → 关闭」流程，供 HTTP 端点调用；避免把
阻塞式 ``login()`` 直接暴露给前端。

设计要点
--------
- 单会话：网关进程内同一时间只维护一个登录会话（一个 channel 实例），
  再次 ``fetch_qr`` 会复用同一实例并刷新二维码。
- 非阻塞：``fetch_qr`` 与 ``poll`` 各自只做一次 HTTP 调用，轮询节奏由
  前端控制，服务端不持有轮询循环。
"""
from __future__ import annotations

from typing import Any

from biscuitbot.channels.weixin import WeixinChannel, WeixinConfig


class WeixinLoginManager:
    """管理 WebUI 微信扫码登录的瞬时 channel 会话。"""

    def __init__(self) -> None:
        self._channel: WeixinChannel | None = None

    def _get_channel(self) -> WeixinChannel:
        """返回（必要时创建）当前登录会话的 channel 实例。"""
        if self._channel is None:
            self._channel = WeixinChannel(WeixinConfig(), bus=None)
        return self._channel

    async def fetch_qr(self) -> dict[str, str]:
        """获取一张登录二维码，返回 ``{"qrcode_id", "qr_content"}``。"""
        return await self._get_channel().fetch_login_qr()

    async def poll(self, qrcode_id: str) -> dict[str, Any]:
        """单次轮询 ``qrcode_id`` 的登录状态。"""
        return await self._get_channel().poll_login_qr(qrcode_id)

    async def close(self) -> None:
        """关闭并丢弃当前登录会话（确认/超时/错误后调用）。"""
        if self._channel is not None:
            await self._channel.close_login_client()
            self._channel = None
