"""Tests for WS envelope media handling (client image upload path).

Exercises ``WebSocketChannel._dispatch_envelope`` for the ``message`` branch:
decoding base64 data URLs, rejecting malformed / oversized / non-whitelisted
payloads, preserving backward compatibility with media-less frames, and
forwarding saved paths to ``_handle_message``.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xianaibot.channels.websocket import (
    WebSocketChannel,
    WebSocketConfig,
    _extract_data_url_mime,
)
from xianaibot.webui.gateway_services import build_gateway_services


def _tiny_png_data_url() -> str:
    """A 1-pixel PNG prefixed as a data URL — just enough for magic-bytes sniffing."""
    # 1x1 transparent PNG
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
        b"\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx"
        b"\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01\x00\x18\xdd\x8d\xb4\x00"
        b"\x00\x00\x00IEND\xaeB`\x82"
    )
    return f"data:image/png;base64,{base64.b64encode(png).decode()}"


def _data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def _make_channel() -> WebSocketChannel:
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    cfg = {"enabled": True, "allowFrom": ["*"], "websocketRequiresToken": False}
    parsed = WebSocketConfig.model_validate(cfg)
    gateway = build_gateway_services(
        config=parsed,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=Path.cwd(),
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )
    channel = WebSocketChannel(cfg, bus, gateway=gateway)
    channel._handle_message = AsyncMock()  # type: ignore[method-assign]
    return channel


# -- Pure helpers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("data:image/png;base64,AAAA", "image/png"),
        ("data:image/jpeg;base64,AAAA", "image/jpeg"),
        ("data:audio/webm;codecs=opus;base64,AAAA", "audio/webm"),
        ("data:IMAGE/PNG;base64,AAAA", "image/png"),
        ("data:image/svg+xml;base64,AAAA", "image/svg+xml"),
        ("data:text/plain;base64,AAAA", "text/plain"),
        ("http://evil.example/x.png", None),
        ("data:image/png,AAAA", None),  # missing `;base64`
        ("", None),
        (None, None),
    ],
)
def test_extract_data_url_mime(url: Any, expected: str | None) -> None:
    assert _extract_data_url_mime(url) == expected


# -- max_message_bytes bump ----------------------------------------------------


def test_max_message_bytes_default_supports_multi_image_frame() -> None:
    """Default 36 MB must hold the largest legal batch the whitelist allows.

    Per-item limits alone are not sufficient: 1 × 18 MB video + 4 × 6 MB images
    is individually legal, yet base64 inflation (×1.374) would push it past the
    frame cap, and ``max_message_bytes`` is enforced by the ``websockets``
    library *before* our handler runs — the client would just see a protocol
    close with no localizable error. ``_MAX_TOTAL_UPLOAD_BYTES`` is what keeps
    the legal combinations inside one frame.
    """
    from xianaibot.channels.websocket import (
        _MAX_TOTAL_UPLOAD_BYTES,
        WebSocketConfig,
    )

    default = WebSocketConfig().max_message_bytes
    # Worst accepted batch: total budget raw bytes → base64.
    assert default >= _MAX_TOTAL_UPLOAD_BYTES * 4 // 3
    # Upper bound 40 MB matches plan
    with pytest.raises(Exception):
        WebSocketConfig(max_message_bytes=41_943_040 + 1)


# -- _dispatch_envelope message branch + media --------------------------------


@pytest.mark.asyncio
async def test_message_without_media_backward_compatible() -> None:
    """Existing clients that don't send ``media`` keep working unchanged."""
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {"type": "message", "chat_id": "abc123", "content": "hello"}

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    call = channel._handle_message.call_args
    assert call.kwargs["chat_id"] == "abc123"
    assert call.kwargs["content"] == "hello"
    # When no media, we pass ``media=None`` so downstream treats it as absent.
    assert call.kwargs["media"] is None


@pytest.mark.asyncio
async def test_message_forwards_normalized_cli_app_attachments() -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "please use @drawio",
        "webui": True,
        "cli_apps": [
            {
                "name": "DrawIO",
                "display_name": "Draw.io",
                "category": "diagram",
                "entry_point": "cli-anything-drawio",
                "logo_url": "https://example.invalid/drawio.svg",
                "brand_color": "#F08705",
            },
            {"name": "bad name", "entry_point": "nope"},
        ],
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    metadata = channel._handle_message.call_args.kwargs["metadata"]
    assert metadata["webui"] is True
    assert metadata["cli_apps"] == [{
        "name": "drawio",
        "display_name": "Draw.io",
        "category": "diagram",
        "entry_point": "cli-anything-drawio",
        "logo_url": "https://example.invalid/drawio.svg",
        "brand_color": "#F08705",
    }]


@pytest.mark.asyncio
async def test_message_forwards_normalized_skill_attachments() -> None:
    """对话界面选中的技能随 envelope 进来后落到 metadata（非法名字被清洗掉）。"""
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "帮我做一版活动方案",
        "webui": True,
        "skills": [
            {"name": "Demo-Skill", "display_name": "演示技能", "source": "workspace"},
            {"name": "bad name"},
            {"name": "../../etc/passwd"},
            {"name": "demo-skill"},  # 与首条重复（大小写不同），只留一个
        ],
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    metadata = channel._handle_message.call_args.kwargs["metadata"]
    assert metadata["skills"] == [
        {"name": "demo-skill", "display_name": "演示技能", "source": "workspace"}
    ]


@pytest.mark.asyncio
async def test_message_without_skills_leaves_metadata_clean() -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {"type": "message", "chat_id": "abc123", "content": "hello", "webui": True}

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    metadata = channel._handle_message.call_args.kwargs["metadata"]
    assert "skills" not in metadata


@pytest.mark.asyncio
async def test_message_with_single_image_forwards_saved_path(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "look at this",
        "media": [{"data_url": _tiny_png_data_url(), "name": "shot.png"}],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    paths = channel._handle_message.call_args.kwargs["media"]
    assert isinstance(paths, list) and len(paths) == 1
    saved = Path(paths[0])
    assert saved.exists()
    assert saved.suffix == ".png"
    assert saved.is_relative_to(tmp_path)


@pytest.mark.asyncio
async def test_message_with_multiple_images(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "a couple",
        "media": [
            {"data_url": _tiny_png_data_url()},
            {"data_url": _tiny_png_data_url()},
            {"data_url": _tiny_png_data_url()},
        ],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    paths = channel._handle_message.call_args.kwargs["media"]
    assert len(paths) == 3
    # Saved filenames must be unique.
    assert len({Path(p).name for p in paths}) == 3


@pytest.mark.asyncio
async def test_image_only_message_allows_empty_text(tmp_path) -> None:
    """When media is attached, empty text is acceptable."""
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "",
        "media": [{"data_url": _tiny_png_data_url()}],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    # Error event NOT sent.
    mock_conn.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_rejected_when_more_than_four_images(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "hi",
        "media": [{"data_url": _tiny_png_data_url()}] * 5,
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    mock_conn.send.assert_awaited_once()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["event"] == "error"
    assert err["detail"] == "media_rejected"
    assert err["reason"] == "too_many_images"


@pytest.mark.asyncio
async def test_message_rejected_on_oversize_payload(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    oversized = b"x" * (7 * 1024 * 1024)  # > 6 MB per-image limit
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "big",
        "media": [{"data_url": _data_url("image/png", oversized)}],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["detail"] == "media_rejected"
    assert err["reason"] == "size"


@pytest.mark.asyncio
async def test_message_rejected_on_non_whitelisted_mime(tmp_path) -> None:
    """Whitelisted documents pass, but an executable-ish MIME still does not."""
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "exe?",
        "media": [{"data_url": _data_url("application/x-msdownload", b"MZ")}],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["detail"] == "media_rejected"
    assert err["reason"] == "mime"


@pytest.mark.asyncio
async def test_message_rejected_on_svg_mime(tmp_path) -> None:
    """SVG is explicitly rejected — XSS surface inside embedded scripts."""
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "svg",
        "media": [{"data_url": _data_url("image/svg+xml", b"<svg/>")}],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["reason"] == "mime"


@pytest.mark.asyncio
async def test_message_rejected_on_malformed_data_url(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "nope",
        "media": [{"data_url": "http://evil.example/image.png"}],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["reason"] == "decode"


@pytest.mark.asyncio
async def test_message_rejected_on_broken_base64(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "nope",
        "media": [{"data_url": "data:image/png;base64,not-valid-base64!!!"}],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["reason"] == "decode"


@pytest.mark.asyncio
async def test_message_rejected_when_media_item_shape_wrong(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "huh",
        # Not a dict — plain string at the top level.
        "media": ["data:image/png;base64,XXXX"],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["reason"] == "malformed"


@pytest.mark.asyncio
async def test_message_rejected_when_media_field_is_not_list() -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "huh",
        "media": "not-a-list",
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["detail"] == "media_rejected"
    assert err["reason"] == "malformed"


@pytest.mark.asyncio
async def test_failed_media_does_not_partially_persist(tmp_path) -> None:
    """If the second image is invalid, the first must not be forwarded.

    Also: images already written in this call are cleaned up on failure, so
    a mixed-valid/invalid batch never leaves orphan files in the media dir.
    """
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "mixed",
        "media": [
            {"data_url": _tiny_png_data_url()},
            {"data_url": _data_url("application/x-msdownload", b"MZ")},
        ],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["reason"] == "mime"
    # Partial-batch failures must not leak files to disk.
    leftover = [p for p in tmp_path.iterdir() if p.is_file()]
    assert leftover == [], f"orphan media after rejected batch: {leftover}"


# -- Documents / audio (previously rejected at the whitelist) -------------------


@pytest.mark.parametrize(
    ("mime", "expected_ext"),
    [
        ("application/pdf", ".pdf"),
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".docx",
        ),
        (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
        ),
        (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".pptx",
        ),
        ("text/markdown", ".md"),
        ("audio/mpeg", ".mp3"),
    ],
)
@pytest.mark.asyncio
async def test_document_and_audio_upload_persists(
    tmp_path, mime: str, expected_ext: str
) -> None:
    """Documents/audio reach disk — and therefore the text extractor — at all.

    Before this whitelist existed, a PDF was rejected with ``reason="mime"``
    even though ``utils.document.extract_text`` could already parse it.
    """
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "read this",
        "media": [{"data_url": _data_url(mime, b"payload"), "name": f"a{expected_ext}"}],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    paths = channel._handle_message.call_args.kwargs["media"]
    assert len(paths) == 1
    saved = Path(paths[0])
    assert saved.exists()
    assert saved.suffix == expected_ext


@pytest.mark.asyncio
async def test_too_many_documents_rejected(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "many",
        "media": [{"data_url": _data_url("application/pdf", b"%PDF-1.4")}] * 4,
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["reason"] == "too_many_documents"
    assert [p for p in tmp_path.iterdir() if p.is_file()] == []


@pytest.mark.asyncio
async def test_total_upload_budget_rejected_before_writing(tmp_path) -> None:
    """Per-item limits are all respected, yet the batch is still refused.

    This is the regression guard for the frame-size contradiction: the batch
    below is 3 × 7 MB documents (each under ``_MAX_DOC_BYTES``) plus 1 × 4 MB
    image (under ``_MAX_IMAGE_BYTES``) — 25 MB raw, ~34 MB once base64-encoded,
    which exceeds the total budget and would blow past ``max_message_bytes``,
    surfacing to the user as a bare protocol-level disconnect instead of a
    localizable error.
    """
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "huge",
        "media": [
            {"data_url": _data_url("application/pdf", b"x" * (7 * 1024 * 1024))},
            {"data_url": _data_url("application/pdf", b"x" * (7 * 1024 * 1024))},
            {"data_url": _data_url("application/pdf", b"x" * (7 * 1024 * 1024))},
            {"data_url": _data_url("image/png", b"x" * (4 * 1024 * 1024))},
        ],
    }

    with patch(
        "xianaibot.channels.websocket.get_media_dir", return_value=tmp_path
    ):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["detail"] == "media_rejected"
    assert err["reason"] == "too_large"
    # The budget check runs before any decode/write.
    assert [p for p in tmp_path.iterdir() if p.is_file()] == []


# -- Legacy direct-link entries are ignored -----------------------------------
#
# 「视频直链」附件已下线（界面上不再有入口）。旧客户端仍可能发 ``{"kind": "url"}``：
# 服务端整条忽略——不落盘、不进 media、不做任何地址校验。链接写在消息正文里即可，
# 模型会自己读并按 ``video-understanding`` 技能处理。


@pytest.mark.asyncio
async def test_legacy_video_url_entry_is_ignored(tmp_path) -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "describe this",
        "media": [
            {"kind": "url", "url": "https://cdn.example.com/clip.mp4"},
            {"data_url": _tiny_png_data_url()},
        ],
    }

    with patch("xianaibot.channels.websocket.get_media_dir", return_value=tmp_path):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    paths = channel._handle_message.call_args.kwargs["media"]
    # 只有那张图：URL 条目被丢弃，也不会被当作路径回传。
    assert len(paths) == 1
    assert "https://" not in paths[0]
    assert len([p for p in tmp_path.iterdir() if p.is_file()]) == 1


@pytest.mark.asyncio
async def test_legacy_video_url_only_entry_delivers_message_without_media(tmp_path) -> None:
    """只有 URL 条目时消息照常送达，只是不带任何附件（不报错、不半途丢弃）。"""
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "see https://cdn.example.com/clip.mp4",
        "media": [{"kind": "url", "url": "https://cdn.example.com/clip.mp4"}],
    }

    with patch("xianaibot.channels.websocket.get_media_dir", return_value=tmp_path):
        await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_awaited_once()
    assert channel._handle_message.call_args.kwargs["media"] is None  # 空列表 → None
    assert [p for p in tmp_path.iterdir() if p.is_file()] == []
    mock_conn.send.assert_not_called()  # 成功路径不发任何帧（尤其不是拒绝帧）


@pytest.mark.asyncio
async def test_rejects_empty_text_without_media() -> None:
    """When no media is attached, whitespace-only content is still rejected
    (matches the existing behavior for backward compat)."""
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": "   ",
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["detail"] == "missing content"


@pytest.mark.asyncio
async def test_non_string_content_still_rejected() -> None:
    channel = _make_channel()
    mock_conn = AsyncMock()
    envelope = {
        "type": "message",
        "chat_id": "abc123",
        "content": 42,
    }

    await channel._dispatch_envelope(mock_conn, "client-1", envelope)

    channel._handle_message.assert_not_awaited()
    err = json.loads(mock_conn.send.call_args[0][0])
    assert err["detail"] == "missing content"
