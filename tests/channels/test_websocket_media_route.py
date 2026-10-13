"""Tests for the signed ``/api/media/<sig>/<payload>`` route and its replay
integration on ``/api/sessions/<key>/messages``.

The route is the return path for images attached to persisted user turns:
:meth:`WebSocketChannel.gateway.media.sign_media_path` mints URLs during session reads,
and :meth:`GatewayHTTPHandler._handle_media_fetch` serves the bytes back.
These tests cover the two halves end-to-end plus the adversarial edges
(bad signatures, ``..`` traversal, non-existent files, non-image types).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import httpx
import pytest

from xianaibot.channels.websocket import WebSocketChannel, WebSocketConfig
from xianaibot.session.manager import Session, SessionManager
from xianaibot.webui.gateway_services import build_gateway_services
from xianaibot.webui.media_api import (
    b64url_decode,
    b64url_encode,
)

# PNG magic bytes + a couple of sentinel bytes so we can verify byte-for-byte
# round-trip of the served payload. Stays under mimetype + size limits.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x00\x00\x02\x00\x01"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _ch(
    bus: Any,
    *,
    session_manager: SessionManager | None = None,
    workspace_path: Path | None = None,
    port: int,
) -> WebSocketChannel:
    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    parsed = WebSocketConfig.model_validate(cfg)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=session_manager,
        static_dist_path=None,
        workspace_path=workspace_path or Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    return WebSocketChannel(cfg, bus, gateway=gateway)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


def _fake_media_dir(root: Path):
    def inner(channel: str | None = None) -> Path:
        path = root / channel if channel else root
        path.mkdir(parents=True, exist_ok=True)
        return path

    return inner


async def _http_get(
    url: str, headers: dict[str, str] | None = None
) -> httpx.Response:
    return await asyncio.to_thread(
        functools.partial(httpx.get, url, headers=headers or {}, timeout=5.0)
    )


# ---------------------------------------------------------------------------
# gateway.media.sign_media_path: the URL minter
# ---------------------------------------------------------------------------


def test_sign_media_path_rejects_paths_outside_media_root(
    bus: MagicMock, tmp_path: Path
) -> None:
    """Paths that resolve outside ``get_media_dir()`` must not be signed.

    This is the single most important invariant of the whole scheme:
    if the minter ever signed an arbitrary path, the HMAC would legitimise
    it for the fetch handler and we'd hand out a disk-read primitive.
    """
    outside = tmp_path / "secrets" / "cred.txt"
    outside.parent.mkdir()
    outside.write_text("nope")
    media = tmp_path / "media"
    media.mkdir()
    channel = _ch(bus, port=0)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        assert channel.gateway.media.sign_media_path(outside) is None
        # Traversal via the media root is also rejected — the resolve() step
        # normalises ``..`` out before the relative_to check.
        assert channel.gateway.media.sign_media_path(media / ".." / "secrets" / "cred.txt") is None


def test_sign_media_path_round_trips_via_hmac(
    bus: MagicMock, tmp_path: Path
) -> None:
    """The signature embeds exactly ``HMAC-SHA256(secret, payload)[:16]``."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.png").write_bytes(_PNG_BYTES)
    channel = _ch(bus, port=0)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        url = channel.gateway.media.sign_media_path(media / "a.png")
    assert url is not None
    assert url.startswith("/api/media/")
    sig, payload = url[len("/api/media/"):].split("/", 1)
    expected = hmac.new(
        channel.gateway.media.secret, payload.encode("ascii"), hashlib.sha256
    ).digest()[:16]
    assert b64url_decode(sig) == expected
    # The payload decodes back to the *relative* path — no absolute-path leaks.
    assert b64url_decode(payload).decode() == "a.png"


def test_local_markdown_image_is_staged_and_rewritten(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "demo_arch.png").write_bytes(_PNG_BYTES)
    media = tmp_path / "media"
    channel = _ch(bus, workspace_path=workspace, port=0)

    with patch("xianaibot.webui.media_gateway.get_media_dir", side_effect=_fake_media_dir(media)):
        rewritten = channel.gateway.media.rewrite_local_markdown_images(
            "The result:\n![Cloud Architecture Diagram](demo_arch.png)"
        )

    assert "![Cloud Architecture Diagram](/api/media/" in rewritten
    staged = list((media / "websocket").iterdir())
    assert len(staged) == 1
    assert staged[0].read_bytes() == _PNG_BYTES


def test_local_markdown_video_is_staged_and_rewritten(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    video_bytes = b"fake mp4"
    (workspace / "xianaibot-intro.mp4").write_bytes(video_bytes)
    media = tmp_path / "media"
    channel = _ch(bus, workspace_path=workspace, port=0)

    with patch("xianaibot.webui.media_gateway.get_media_dir", side_effect=_fake_media_dir(media)):
        rewritten = channel.gateway.media.rewrite_local_markdown_images(
            "The result:\n![xianaibot-intro.mp4](xianaibot-intro.mp4)"
        )

    assert "![xianaibot-intro.mp4](/api/media/" in rewritten
    staged = list((media / "websocket").iterdir())
    assert len(staged) == 1
    assert staged[0].read_bytes() == video_bytes


def test_local_markdown_image_rejects_workspace_escape(
    bus: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG_BYTES)
    media = tmp_path / "media"
    channel = _ch(bus, workspace_path=workspace, port=0)
    text = "![nope](../outside.png)"

    with patch("xianaibot.webui.media_gateway.get_media_dir", side_effect=_fake_media_dir(media)):
        assert channel.gateway.media.rewrite_local_markdown_images(text) == text

    assert not (media / "websocket").exists()


# ---------------------------------------------------------------------------
# /api/media/<sig>/<payload>: the serving handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_route_serves_signed_file(
    bus: MagicMock, tmp_path: Path
) -> None:
    """Valid signature + existing file => 200 with correct bytes + MIME."""
    media = tmp_path / "media"
    media.mkdir()
    target = media / "round-trip.png"
    target.write_bytes(_PNG_BYTES)

    channel = _ch(bus, port=29920)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        url_path = channel.gateway.media.sign_media_path(target)
        assert url_path is not None
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            resp = await _http_get(f"http://127.0.0.1:29920{url_path}")
        finally:
            await channel.stop()
            await server_task

    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES
    assert resp.headers["content-type"].startswith("image/png")
    # Immutable cache header lets the browser skip round-trips on replay.
    assert "immutable" in resp.headers.get("cache-control", "")
    # Video players rely on byte ranges; images get the header for consistency.
    assert resp.headers.get("accept-ranges") == "bytes"
    # nosniff keeps the browser from second-guessing our Content-Type.
    assert resp.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.asyncio
async def test_media_route_serves_video_byte_ranges(
    bus: MagicMock, tmp_path: Path
) -> None:
    """MP4 playback needs HTTP Range support for mid-stream reads and seeking."""
    media = tmp_path / "media"
    media.mkdir()
    target = media / "clip.mp4"
    target.write_bytes(b"0123456789")

    channel = _ch(bus, port=29927)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        url_path = channel.gateway.media.sign_media_path(target)
        assert url_path is not None
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            resp = await _http_get(
                f"http://127.0.0.1:29927{url_path}",
                headers={"Range": "bytes=2-5"},
            )
        finally:
            await channel.stop()
            await server_task

    assert resp.status_code == 206
    assert resp.content == b"2345"
    assert resp.headers["content-type"].startswith("video/mp4")
    assert resp.headers.get("accept-ranges") == "bytes"
    assert resp.headers.get("content-range") == "bytes 2-5/10"
    assert resp.headers.get("content-length") == "4"


@pytest.mark.asyncio
async def test_media_route_serves_suffix_video_byte_ranges(
    bus: MagicMock, tmp_path: Path
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    target = media / "clip.mp4"
    target.write_bytes(b"0123456789")

    channel = _ch(bus, port=29928)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        url_path = channel.gateway.media.sign_media_path(target)
        assert url_path is not None
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            resp = await _http_get(
                f"http://127.0.0.1:29928{url_path}",
                headers={"Range": "bytes=-3"},
            )
        finally:
            await channel.stop()
            await server_task

    assert resp.status_code == 206
    assert resp.content == b"789"
    assert resp.headers.get("content-range") == "bytes 7-9/10"


@pytest.mark.asyncio
async def test_media_route_rejects_unsatisfiable_byte_range(
    bus: MagicMock, tmp_path: Path
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    target = media / "clip.mp4"
    target.write_bytes(b"0123456789")

    channel = _ch(bus, port=29929)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        url_path = channel.gateway.media.sign_media_path(target)
        assert url_path is not None
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            resp = await _http_get(
                f"http://127.0.0.1:29929{url_path}",
                headers={"Range": "bytes=100-200"},
            )
        finally:
            await channel.stop()
            await server_task

    assert resp.status_code == 416
    assert resp.headers.get("accept-ranges") == "bytes"
    assert resp.headers.get("content-range") == "bytes */10"


@pytest.mark.asyncio
async def test_media_route_rejects_bad_signature(
    bus: MagicMock, tmp_path: Path
) -> None:
    """A payload re-signed with a different secret must 401.

    Protects against a restart: old URLs baked into a stale tab become
    un-forgeable once ``gateway.media.secret`` regenerates.
    """
    media = tmp_path / "media"
    media.mkdir()
    (media / "f.png").write_bytes(_PNG_BYTES)

    channel = _ch(bus, port=29921)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        good = channel.gateway.media.sign_media_path(media / "f.png")
        assert good is not None
        _, payload = good[len("/api/media/"):].split("/", 1)
        # Forge a sig with a *different* secret.
        forged_mac = hmac.new(
            b"\x00" * 32, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        forged = f"/api/media/{b64url_encode(forged_mac)}/{payload}"

        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            resp = await _http_get(f"http://127.0.0.1:29921{forged}")
        finally:
            await channel.stop()
            await server_task
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_media_route_rejects_path_traversal_payload(
    bus: MagicMock, tmp_path: Path
) -> None:
    """Even a validly-signed ``..`` payload must not escape the media root.

    The signer never *emits* such payloads, but an attacker who somehow
    obtained the secret (or the channel was misconfigured) must still be
    stopped by the resolve()+relative_to() guard in the serving path.
    """
    media = tmp_path / "media"
    media.mkdir()
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("classified")

    channel = _ch(bus, port=29922)
    # Hand-craft a traversal payload the legit signer would refuse to mint.
    payload = b64url_encode(b"../secret.txt")
    mac = hmac.new(
        channel.gateway.media.secret, payload.encode("ascii"), hashlib.sha256
    ).digest()[:16]
    url = f"/api/media/{b64url_encode(mac)}/{payload}"

    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            resp = await _http_get(f"http://127.0.0.1:29922{url}")
        finally:
            await channel.stop()
            await server_task
    assert resp.status_code == 404
    assert b"classified" not in resp.content


@pytest.mark.asyncio
async def test_media_route_404s_missing_file(
    bus: MagicMock, tmp_path: Path
) -> None:
    """A signed URL for a file that no longer exists degrades to 404 so the
    client can fall back to the placeholder tile instead of breaking."""
    media = tmp_path / "media"
    media.mkdir()
    target = media / "gone.png"
    target.write_bytes(_PNG_BYTES)

    channel = _ch(bus, port=29923)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        url_path = channel.gateway.media.sign_media_path(target)
        assert url_path is not None
        target.unlink()  # the file vanishes between signing and fetching
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            resp = await _http_get(f"http://127.0.0.1:29923{url_path}")
        finally:
            await channel.stop()
            await server_task
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_media_route_degrades_non_image_to_octet_stream(
    bus: MagicMock, tmp_path: Path
) -> None:
    """A non-image extension must not be served as its native MIME.

    Defence-in-depth: if media_dir ever contained (say) an HTML file, we
    do not want the browser to render it as HTML via the signed route.
    """
    media = tmp_path / "media"
    media.mkdir()
    (media / "scary.html").write_bytes(b"<script>alert(1)</script>")

    channel = _ch(bus, port=29924)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        payload = b64url_encode(b"scary.html")
        mac = hmac.new(
            channel.gateway.media.secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        url = f"/api/media/{b64url_encode(mac)}/{payload}"
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            resp = await _http_get(f"http://127.0.0.1:29924{url}")
        finally:
            await channel.stop()
            await server_task
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")
    # nosniff is the actual defence when we downgrade to octet-stream:
    # without it the browser might still sniff the bytes as HTML.
    assert resp.headers.get("x-content-type-options") == "nosniff"


@pytest.mark.asyncio
async def test_media_route_serves_svg_with_strict_csp(
    bus: MagicMock, tmp_path: Path
) -> None:
    """Generated SVG can preview as an image without becoming executable HTML."""
    media = tmp_path / "media"
    media.mkdir()
    target = media / "chart.svg"
    target.write_text("<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>")

    channel = _ch(bus, port=29928)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        url_path = channel.gateway.media.sign_media_path(target)
        assert url_path is not None
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            resp = await _http_get(f"http://127.0.0.1:29928{url_path}")
        finally:
            await channel.stop()
            await server_task

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "default-src 'none'" in resp.headers.get("content-security-policy", "")
    assert "sandbox" in resp.headers.get("content-security-policy", "")


# ---------------------------------------------------------------------------
# /api/sessions/<key>/messages: media_urls hydration on session read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_messages_exposes_signed_media_urls(
    bus: MagicMock, tmp_path: Path
) -> None:
    """The read path must map persisted ``media`` paths onto signed URLs
    and strip the raw path — the client never learns the server's layout."""
    media = tmp_path / "media"
    media.mkdir()
    img = media / "u.png"
    img.write_bytes(_PNG_BYTES)

    sm = SessionManager(tmp_path / "ws_state")
    sess = Session(key="websocket:media-hydrate")
    sess.add_message("user", "look at this", media=[str(img)])
    sess.add_message("assistant", "nice")
    sm.save(sess)

    channel = _ch(bus, session_manager=sm, port=29925)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            boot = await _http_get("http://127.0.0.1:29925/webui/bootstrap")
            token = boot.json()["token"]
            auth = {"Authorization": f"Bearer {token}"}
            resp = await _http_get(
                "http://127.0.0.1:29925/api/sessions/websocket:media-hydrate/messages",
                headers=auth,
            )
            body = resp.json()
            # The signed URL round-trips end-to-end: fetching it yields the same bytes.
            user_msg = next(m for m in body["messages"] if m["role"] == "user")
            urls = user_msg["media_urls"]
            assert isinstance(urls, list) and len(urls) == 1
            assert urls[0]["name"] == "u.png"
            assert urls[0]["url"].startswith("/api/media/")
            # Raw paths must not leak to the wire.
            assert "media" not in user_msg

            # And the URL actually works.
            fetched = await _http_get(f"http://127.0.0.1:29925{urls[0]['url']}")
            assert fetched.status_code == 200
            assert fetched.content == _PNG_BYTES
        finally:
            await channel.stop()
            await server_task


@pytest.mark.asyncio
async def test_session_messages_skips_vanished_media(
    bus: MagicMock, tmp_path: Path
) -> None:
    """Paths that no longer resolve inside the media root produce no URL —
    the message is still delivered, just without the preview."""
    media = tmp_path / "media"
    media.mkdir()

    sm = SessionManager(tmp_path / "ws_state")
    sess = Session(key="websocket:vanished")
    sess.add_message("user", "missing pic", media=[str(media / "absent.png")])
    sm.save(sess)

    channel = _ch(bus, session_manager=sm, port=29926)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            boot = await _http_get("http://127.0.0.1:29926/webui/bootstrap")
            token = boot.json()["token"]
            resp = await _http_get(
                "http://127.0.0.1:29926/api/sessions/websocket:vanished/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            user_msg = next(m for m in resp.json()["messages"] if m["role"] == "user")
            # absent.png lives inside the media root so it *does* get a signed
            # URL (we don't stat the file at signing time — that would slow
            # the listing). Fetching the URL is where the 404 surfaces.
            urls = user_msg.get("media_urls") or []
            assert len(urls) == 1
            fetched = await _http_get(f"http://127.0.0.1:29926{urls[0]['url']}")
            assert fetched.status_code == 404
            assert "media" not in user_msg
        finally:
            await channel.stop()
            await server_task


@pytest.mark.asyncio
async def test_session_messages_skips_legacy_remote_url_media(
    bus: MagicMock, tmp_path: Path
) -> None:
    """历史会话里残留的远程直链条目必须被**跳过**，而不是被当成本地路径。

    直链附件功能已下线（界面上不再有入口），但老会话的 ``media`` 里还留着
    ``https://…``。这类条目从未落过盘，没有可签名的文件；若把它交给
    ``Path()``/签名器，就会在会话读取路径上炸出 500。这里钉住：不 500、
    不回显那一条、本地条目照常。
    """
    media = tmp_path / "media"
    media.mkdir()
    img = media / "u.png"
    img.write_bytes(_PNG_BYTES)

    sm = SessionManager(tmp_path / "ws_state")
    sess = Session(key="websocket:legacy-link")
    sess.add_message(
        "user",
        "看下这个",
        media=[str(img), "https://cdn.example.com/clip.mp4"],
    )
    sm.save(sess)

    channel = _ch(bus, session_manager=sm, port=29927)
    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            boot = await _http_get("http://127.0.0.1:29927/webui/bootstrap")
            token = boot.json()["token"]
            resp = await _http_get(
                "http://127.0.0.1:29927/api/sessions/websocket:legacy-link/messages",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            body = resp.json()
            user_msg = next(m for m in body["messages"] if m["role"] == "user")
            urls = user_msg["media_urls"]
            # 只有本地那张图；远端那条既不回显、也不泄进整条响应。
            assert len(urls) == 1
            assert urls[0]["name"] == "u.png"
            assert "cdn.example.com" not in resp.text
        finally:
            await channel.stop()
            await server_task


# ---------------------------------------------------------------------------
# /api/sessions/<key>/workspace-media/<sig>/<payload>: 会话工作区签名直出
#
# 智能体的产出现在落在**会话工作区**（``<workspace>/generated`` 等），而工作区
# 不在 media 根内，/api/media 服务不了它。这条路由复用同一套 HMAC 签名，只把根
# 换成本会话的 ``scope.project_path``（由服务端从会话键派生，**不从负载里读**）。
# ---------------------------------------------------------------------------


def _bind_workspace(sm: SessionManager, chat_id: str, workspace: Path) -> str:
    """给会话绑定一个显式工作区作用域，返回会话键。"""
    sess = Session(key=f"websocket:{chat_id}")
    sess.metadata["webui"] = True
    sess.metadata["workspace_scope"] = {
        "project_path": str(workspace.resolve()),
        "access_mode": "full",
    }
    sm.save(sess)
    return f"websocket:{chat_id}"


def _media_url_prefix(session_key: str) -> str:
    return f"/api/sessions/{quote(session_key, safe='')}/workspace-media"


@pytest.mark.asyncio
async def test_workspace_media_round_trips_for_the_session_workspace(
    bus: MagicMock, tmp_path: Path
) -> None:
    """工作区里的产出文件必须能被签成 URL 并原样取回。

    这是整条资产链路的承重环：以前生成的图片/视频因为落在工作区而根本没 URL。
    """
    workspace = tmp_path / "proj"
    target = workspace / "generated" / "2026-10-12" / "img_abcd1234.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(_PNG_BYTES)

    sm = SessionManager(tmp_path / "state")
    key = _bind_workspace(sm, "ws-roundtrip", workspace)
    # 默认工作区故意指向别的目录：能服务成功就说明根取自**会话**作用域。
    channel = _ch(bus, session_manager=sm, workspace_path=tmp_path / "elsewhere", port=29940)

    prefix = _media_url_prefix(key)
    url_path = channel.gateway.media.sign_workspace_media_path(
        target, root=workspace, url_prefix=prefix
    )
    assert url_path is not None
    assert url_path.startswith(prefix + "/")

    # 负载是**带路由 tag 的相对路径** —— tag 进 MAC，两条签名路由因此互不通用。
    _, payload = url_path[len(prefix) + 1:].split("/", 1)
    assert b64url_decode(payload).decode() == "w:generated/2026-10-12/img_abcd1234.png"

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get(f"http://127.0.0.1:29940{url_path}")
    finally:
        await channel.stop()
        await server_task

    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES
    assert resp.headers["content-type"].startswith("image/png")
    assert "immutable" in resp.headers.get("cache-control", "")


@pytest.mark.asyncio
async def test_workspace_media_serves_video_byte_ranges(
    bus: MagicMock, tmp_path: Path
) -> None:
    """工作区视频同样拿到 Range 支持（复用同一套 serve，不额外实现）。"""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    target = workspace / "clip.mp4"
    target.write_bytes(b"0123456789")

    sm = SessionManager(tmp_path / "state")
    key = _bind_workspace(sm, "ws-range", workspace)
    channel = _ch(bus, session_manager=sm, workspace_path=workspace, port=29941)

    url_path = channel.gateway.media.sign_workspace_media_path(
        target, root=workspace, url_prefix=_media_url_prefix(key)
    )
    assert url_path is not None
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get(
            f"http://127.0.0.1:29941{url_path}", headers={"Range": "bytes=2-5"}
        )
    finally:
        await channel.stop()
        await server_task

    assert resp.status_code == 206
    assert resp.content == b"2345"
    assert resp.headers.get("content-range") == "bytes 2-5/10"


def test_workspace_media_signer_rejects_paths_outside_the_workspace(
    bus: MagicMock, tmp_path: Path
) -> None:
    """越根的工作区文件不能被签名（否则就是拿 HMAC 洗白一个任意读原语）。"""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    outside = tmp_path / "secrets" / "cred.png"
    outside.parent.mkdir()
    outside.write_bytes(_PNG_BYTES)

    channel = _ch(bus, port=0)
    prefix = _media_url_prefix("websocket:outside")
    assert (
        channel.gateway.media.sign_workspace_media_path(
            outside, root=workspace, url_prefix=prefix
        )
        is None
    )
    assert (
        channel.gateway.media.sign_workspace_media_path(
            workspace / ".." / "secrets" / "cred.png", root=workspace, url_prefix=prefix
        )
        is None
    )


@pytest.mark.asyncio
async def test_workspace_media_rejects_traversal_payload(
    bus: MagicMock, tmp_path: Path
) -> None:
    """即使部署者泄露了 secret，``..`` 负载也必须被 resolve+relative_to 挡下。"""
    workspace = tmp_path / "proj"
    workspace.mkdir()
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("classified")

    sm = SessionManager(tmp_path / "state")
    key = _bind_workspace(sm, "ws-traversal", workspace)
    channel = _ch(bus, session_manager=sm, workspace_path=workspace, port=29942)

    payload = b64url_encode(b"w:../secret.txt")
    mac = hmac.new(
        channel.gateway.media.secret, payload.encode("ascii"), hashlib.sha256
    ).digest()[:16]
    url = f"{_media_url_prefix(key)}/{b64url_encode(mac)}/{payload}"

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        resp = await _http_get(f"http://127.0.0.1:29942{url}")
    finally:
        await channel.stop()
        await server_task
    assert resp.status_code == 404
    assert b"classified" not in resp.content


@pytest.mark.asyncio
async def test_workspace_media_rejects_cross_route_signature_replay(
    bus: MagicMock, tmp_path: Path
) -> None:
    """两条签名路由共用一把 secret，URL 必须互不通用（否则一条 URL 能读两个根）。"""
    media = tmp_path / "media"
    media.mkdir()
    media_file = media / "a.png"
    media_file.write_bytes(_PNG_BYTES)
    workspace = tmp_path / "proj"
    workspace.mkdir()
    ws_file = workspace / "b.png"
    ws_file.write_bytes(_PNG_BYTES)

    sm = SessionManager(tmp_path / "state")
    key = _bind_workspace(sm, "ws-replay", workspace)
    channel = _ch(bus, session_manager=sm, workspace_path=workspace, port=29943)
    ws_prefix = _media_url_prefix(key)

    with patch("xianaibot.webui.media_gateway.get_media_dir", return_value=media):
        media_url = channel.gateway.media.sign_media_path(media_file)
        ws_url = channel.gateway.media.sign_workspace_media_path(
            ws_file, root=workspace, url_prefix=ws_prefix
        )
        assert media_url is not None and ws_url is not None

        media_sig, media_payload = media_url[len("/api/media/"):].split("/", 1)
        ws_sig, ws_payload = ws_url[len(ws_prefix) + 1:].split("/", 1)
        # media 路由的 URL 拿去工作区路由 → 负载没有 "w:" tag，直接拒
        replayed_into_ws = f"{ws_prefix}/{media_sig}/{media_payload}"
        # 工作区路由的 URL 拿去 media 路由 → 解析成 media 根下的 "w:..." → 不存在
        replayed_into_media = f"/api/media/{ws_sig}/{ws_payload}"

        server_task = asyncio.create_task(channel.start())
        await asyncio.sleep(0.3)
        try:
            a = await _http_get(f"http://127.0.0.1:29943{replayed_into_ws}")
            b = await _http_get(f"http://127.0.0.1:29943{replayed_into_media}")
            # 各自的合法 URL 仍然可用（排除「全都 404」这种假绿）
            ok = await _http_get(f"http://127.0.0.1:29943{ws_url}")
        finally:
            await channel.stop()
            await server_task

    assert a.status_code == 404
    assert b.status_code == 404
    assert ok.status_code == 200 and ok.content == _PNG_BYTES


@pytest.mark.asyncio
async def test_workspace_media_root_comes_from_the_session_not_the_payload(
    bus: MagicMock, tmp_path: Path
) -> None:
    """负载只带**相对**路径，根一律由 URL 里的会话键决定。

    两个会话各有一个同名文件；同一段 sig/payload 换到另一个会话的键下，取到的是
    **那个会话**自己的文件，而不是签发它的会话的。关键是负载里根本没有绝对路径
    可读 —— 服务端不可能被负载指使去读会话工作区以外的地方（越界那部分由上一条
    traversal 用例覆盖）。
    """
    workspaces = {}
    for chat_id in ("ws-left", "ws-right"):
        ws = tmp_path / chat_id
        ws.mkdir()
        (ws / "same.png").write_bytes(_PNG_BYTES + chat_id.encode())
        workspaces[chat_id] = ws

    sm = SessionManager(tmp_path / "state")
    keys = {cid: _bind_workspace(sm, cid, ws) for cid, ws in workspaces.items()}
    channel = _ch(bus, session_manager=sm, workspace_path=tmp_path, port=29944)

    left_url = channel.gateway.media.sign_workspace_media_path(
        workspaces["ws-left"] / "same.png",
        root=workspaces["ws-left"],
        url_prefix=_media_url_prefix(keys["ws-left"]),
    )
    assert left_url is not None
    sig, payload = left_url[len(_media_url_prefix(keys["ws-left"])) + 1:].split("/", 1)

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        same_payload_other_session = await _http_get(
            f"http://127.0.0.1:29944{_media_url_prefix(keys['ws-right'])}/{sig}/{payload}"
        )
        absent_payload = b64url_encode(b"w:absent.png")
        absent_mac = hmac.new(
            channel.gateway.media.secret, absent_payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        absent = await _http_get(
            "http://127.0.0.1:29944"
            f"{_media_url_prefix(keys['ws-right'])}"
            f"/{b64url_encode(absent_mac)}/{absent_payload}"
        )
    finally:
        await channel.stop()
        await server_task

    assert same_payload_other_session.status_code == 200
    assert same_payload_other_session.content == _PNG_BYTES + b"ws-right"
    assert absent.status_code == 404


# ---------------------------------------------------------------------------
# /api/sessions/<key>/assets: 会话资产列表 / 预览 / 删除
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_assets_lists_and_serves_workspace_outputs(
    bus: MagicMock, tmp_path: Path
) -> None:
    """端到端回归：``?scope=workspace`` 下列出工作区里的产出，且 URL 立刻可用。"""
    workspace = tmp_path / "proj"
    img = workspace / "generated" / "2026-10-12" / "img_abcd1234.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(_PNG_BYTES)
    doc = workspace / "报告.html"
    doc.write_text("<h1>hi</h1>", encoding="utf-8")

    sm = SessionManager(tmp_path / "state")
    key = _bind_workspace(sm, "assets", workspace)
    channel = _ch(bus, session_manager=sm, workspace_path=tmp_path / "elsewhere", port=29945)
    base = f"http://127.0.0.1:29945/api/sessions/{quote(key, safe='')}"

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        token = (await _http_get("http://127.0.0.1:29945/webui/bootstrap")).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        # 缺省口径是「本会话」：这个会话没有台账（没有会话记录），列表必须是空的。
        assert (await _http_get(f"{base}/assets", headers=auth)).json()["assets"] == []

        resp = await _http_get(f"{base}/assets?scope=workspace", headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["truncated"] is False
        names = sorted(a["name"] for a in body["assets"])
        assert names == ["img_abcd1234.png", "报告.html"]
        assert {a["kind"] for a in body["assets"]} == {"image", "document"}

        image = next(a for a in body["assets"] if a["kind"] == "image")
        assert image["media_url"].startswith(_media_url_prefix(key) + "/")
        fetched = await _http_get(f"http://127.0.0.1:29945{image['media_url']}")
        assert fetched.status_code == 200
        assert fetched.content == _PNG_BYTES

        # 未认证的列表请求必须被拒（工作区内容属于会话数据）。
        assert (await _http_get(f"{base}/assets")).status_code == 401

        deleted = await _http_get(
            f"{base}/assets/delete?id={image['id']}&scope=workspace", headers=auth
        )
        assert deleted.status_code == 200
        assert [a["name"] for a in deleted.json()["assets"]] == ["报告.html"]
        assert not img.exists()
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_session_assets_default_view_isolates_by_conversation(
    bus: MagicMock, tmp_path: Path
) -> None:
    """两个会话共用同一个工作区时，缺省口径只看各自的台账。

    工作区口径下每个会话看到的清单一模一样（这正是用户报的问题）；缺省口径按会话
    记录里的 ``file_edit``（写过的）和出站 ``media``（交付过的）隔离。
    """
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "a.html").write_text("<h1>a</h1>", encoding="utf-8")
    (workspace / "b.html").write_text("<h1>b</h1>", encoding="utf-8")

    sm = SessionManager(tmp_path / "state")
    key_a = _bind_workspace(sm, "iso-a", workspace)
    key_b = _bind_workspace(sm, "iso-b", workspace)
    channel = _ch(bus, session_manager=sm, workspace_path=tmp_path / "elsewhere", port=29948)
    base_a = f"http://127.0.0.1:29948/api/sessions/{quote(key_a, safe='')}"
    base_b = f"http://127.0.0.1:29948/api/sessions/{quote(key_b, safe='')}"

    webui_dir = tmp_path / "webui"
    webui_dir.mkdir()
    (webui_dir / f"{SessionManager.safe_key(key_a)}.jsonl").write_text(
        json.dumps(
            {
                "event": "file_edit",
                "edits": [{"path": "a.html", "absolute_path": str(workspace / "a.html")}],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        token = (await _http_get("http://127.0.0.1:29948/webui/bootstrap")).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        with patch("xianaibot.webui.transcript.get_webui_dir", lambda: webui_dir):
            mine = (await _http_get(f"{base_a}/assets", headers=auth)).json()
            other = (await _http_get(f"{base_b}/assets", headers=auth)).json()
            both = (await _http_get(f"{base_a}/assets?scope=workspace", headers=auth)).json()
    finally:
        await channel.stop()
        await server_task

    # 本会话：只有自己写过的那个；b.html 是同工作区的别人的产出，不出现。
    assert [a["name"] for a in mine["assets"]] == ["a.html"]
    # 没有台账的会话看到的是空列表 —— 这才是「会话资产」。
    assert other["assets"] == []
    # 工作区口径仍是原来的能力（两个都在）。
    assert sorted(a["name"] for a in both["assets"]) == ["a.html", "b.html"]


@pytest.mark.asyncio
async def test_session_assets_include_delivered_files_anywhere(
    bus: MagicMock, tmp_path: Path
) -> None:
    """端到端硬保证：会话里**交付给用户**的文件必须出现在资产里。

    这个文件在工作区外、扩展名（``.zip``）也不在产出白名单里 —— 两条都会让它被
    普通扫描漏掉。交付台账（会话记录里的出站 ``media``）把它捞回来：列表里带
    ``delivered`` 标记，地址是 media 根的签名 URL，能看、能删。
    """
    workspace = tmp_path / "proj"
    workspace.mkdir()
    outside = tmp_path / "elsewhere" / "报告打包.zip"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"PK\x03\x04zip")

    sm = SessionManager(tmp_path / "state")
    key = _bind_workspace(sm, "delivered", workspace)
    channel = _ch(bus, session_manager=sm, workspace_path=tmp_path / "elsewhere", port=29947)
    base = f"http://127.0.0.1:29947/api/sessions/{quote(key, safe='')}"

    webui_dir = tmp_path / "webui"
    media = tmp_path / "media"
    transcript = webui_dir / f"{SessionManager.safe_key(key)}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "event": "message",
                "chat_id": "delivered",
                "text": "打包好了",
                "media": [str(outside)],
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        token = (await _http_get("http://127.0.0.1:29947/webui/bootstrap")).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        with (
            patch(
                "xianaibot.webui.transcript.get_webui_dir",
                lambda: webui_dir,
            ),
            patch(
                "xianaibot.webui.media_gateway.get_media_dir",
                side_effect=_fake_media_dir(media),
            ),
        ):
            resp = await _http_get(f"{base}/assets", headers=auth)
            assert resp.status_code == 200
            (asset,) = resp.json()["assets"]
            assert asset["name"] == "报告打包.zip"
            assert asset["delivered"] is True
            assert asset["media_url"].startswith("/api/media/")

            fetched = await _http_get(f"http://127.0.0.1:29947{asset['media_url']}")
            assert fetched.status_code == 200
            assert fetched.content == outside.read_bytes()

            deleted = await _http_get(f"{base}/assets/delete?id={asset['id']}", headers=auth)
    finally:
        await channel.stop()
        await server_task

    assert deleted.status_code == 200
    assert deleted.json()["assets"] == []
    assert not outside.exists()


@pytest.mark.asyncio
async def test_session_assets_404s_for_non_websocket_session_keys(
    bus: MagicMock, tmp_path: Path
) -> None:
    """非 websocket 会话键没有工作区作用域可言，按「会话不存在」处理。"""
    channel = _ch(bus, workspace_path=tmp_path, port=29946)
    server_task = asyncio.create_task(channel.start())
    await asyncio.sleep(0.3)
    try:
        token = (await _http_get("http://127.0.0.1:29946/webui/bootstrap")).json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            "http://127.0.0.1:29946/api/sessions/telegram:123/assets", headers=auth
        )
    finally:
        await channel.stop()
        await server_task
    assert resp.status_code == 404
