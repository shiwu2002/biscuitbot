"""Media gateway services shared by WebUI HTTP routes and WebSocket frames."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from xianaibot.config.paths import get_media_dir
from xianaibot.webui.media_api import (
    WORKSPACE_MEDIA_PAYLOAD_TAG,
    attach_signed_media_urls,
    serve_signed_media,
    sign_media_path,
    sign_or_stage_media_path,
    signed_media_attachments,
)
from xianaibot.webui.transcript import rewrite_local_markdown_images


class WebUIMediaGateway:
    """Own media URL signing and WebUI markdown/media augmentation."""

    def __init__(
        self,
        *,
        workspace_path: Path,
        logger: Any,
        media_dir: Callable[[str | None], Path] | None = None,
        secret: bytes | None = None,
    ) -> None:
        self.workspace_path = workspace_path
        self.logger = logger
        self._media_dir = media_dir or (lambda channel=None: get_media_dir(channel))
        self.secret = secret or secrets.token_bytes(32)

    def serve_signed_media(
        self,
        sig: str,
        payload: str,
        *,
        request: WsRequest | None = None,
    ) -> Response:
        return serve_signed_media(
            sig,
            payload,
            secret=self.secret,
            request=request,
            media_dir=self._media_dir,
        )

    def sign_media_path(self, abs_path: Path) -> str | None:
        return sign_media_path(
            abs_path,
            secret=self.secret,
            media_dir=self._media_dir,
        )

    def sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None:
        return sign_or_stage_media_path(
            path,
            secret=self.secret,
            media_dir=self._media_dir,
            logger=self.logger,
        )

    def sign_workspace_media_path(
        self,
        abs_path: Path,
        *,
        root: Path,
        url_prefix: str,
    ) -> str | None:
        """给**会话工作区**内的文件签名，直接签原文件（不 staging、不复制）。

        工作区根随会话而变，所以按调用逐个传入；负载带
        :data:`WORKSPACE_MEDIA_PAYLOAD_TAG` 前缀，与 media 根路由互不通用。
        """
        return sign_media_path(
            abs_path,
            secret=self.secret,
            media_dir=lambda _channel=None: root,
            payload_tag=WORKSPACE_MEDIA_PAYLOAD_TAG,
            url_prefix=url_prefix,
        )

    def sign_or_stage_media_url(self, abs_path: Path) -> str | None:
        """media 根内直接签名，根外文件先稳定 staging 再签；只要 URL（丢掉名字/路径）。

        交付台账里的工作区外文件用它取展示地址 —— 与发送那一刻走的是同一条路，
        稳定命名意味着不会再复制一份副本。
        """
        signed = self.sign_or_stage_media_path(abs_path)
        return signed["url"] if signed else None

    def serve_signed_workspace_media(
        self,
        sig: str,
        payload: str,
        *,
        root: Path,
        request: WsRequest | None = None,
    ) -> Response:
        return serve_signed_media(
            sig,
            payload,
            secret=self.secret,
            request=request,
            media_dir=lambda _channel=None: root,
            payload_tag=WORKSPACE_MEDIA_PAYLOAD_TAG,
        )

    def rewrite_local_markdown_images(
        self,
        text: str,
        *,
        workspace_path: Path | None = None,
    ) -> str:
        return rewrite_local_markdown_images(
            text,
            workspace_path=workspace_path or self.workspace_path,
            sign_path=self.sign_or_stage_media_path,
        )

    def augment_media_urls(self, payload: dict[str, Any]) -> None:
        attach_signed_media_urls(payload, sign_path=self.sign_media_path)

    def augment_transcript_media(self, paths: list[str]) -> list[dict[str, Any]]:
        return signed_media_attachments(
            paths,
            sign_path=self.sign_or_stage_media_path,
        )
