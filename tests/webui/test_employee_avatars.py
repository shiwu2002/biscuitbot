"""员工头像：文件名解析与 /api/avatars 服务路由。"""

from __future__ import annotations

from pathlib import Path

from biscuitbot.webui.ws_http import (
    GatewayHTTPHandler,
    _resolve_avatar_path,
)

_KNOWN_AVATAR = "img_dccb1217c046.jpg"  # 内置员工 阿伟（视频总监）


def test_resolve_avatar_path_finds_source_image() -> None:
    """源码运行模式下，头像文件定位到仓库 images/bot/ 下的实体文件。"""
    path = _resolve_avatar_path(_KNOWN_AVATAR)
    assert path is not None
    assert path.is_file()
    assert path.name == _KNOWN_AVATAR


def test_resolve_avatar_path_rejects_traversal() -> None:
    """路径穿越 / 分隔符 / 非法字符一律拒绝。"""
    for bad in ("../secret.jpg", "a/b.jpg", "..", ".", "", "\\etc", ".hidden"):
        assert _resolve_avatar_path(bad) is None


def test_resolve_avatar_path_unknown_file_returns_none() -> None:
    """存在但非文件 / 不存在的头像返回 None。"""
    assert _resolve_avatar_path("does-not-exist.jpg") is None
    assert _resolve_avatar_path("img_dccb1217c046.jpg/..") is None


def test_resolve_avatar_path_finds_user_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """人才市场下载的头像落在实例数据目录 <config>/avatars/，路由也能命中。"""
    from biscuitbot.config import paths as config_paths

    user_avatar_dir = tmp_path / "avatars"
    user_avatar_dir.mkdir()
    (user_avatar_dir / "market-bot.png").write_bytes(b"\x89PNGfake")

    monkeypatch.setattr(
        config_paths, "get_runtime_subdir", lambda name: tmp_path / name
    )
    path = _resolve_avatar_path("market-bot.png")
    assert path is not None
    assert path == user_avatar_dir / "market-bot.png"


def test_handle_avatar_returns_image_bytes() -> None:
    """GET /api/avatars/<file> 返回图片字节与正确 content-type。"""
    handler = GatewayHTTPHandler.__new__(GatewayHTTPHandler)
    resp = handler._handle_avatar(_KNOWN_AVATAR)
    assert resp.status_code == 200
    body = resp.body
    assert isinstance(body, bytes) and body
    # JPEG 魔数
    assert body.startswith(b"\xff\xd8\xff")
    content_type = dict(resp.headers.raw_items()).get("Content-Type", "")
    assert content_type == "image/jpeg"
    # 静态资源缓存
    headers = dict(resp.headers.raw_items())
    assert headers.get("Cache-Control") == "public, max-age=86400"


def test_handle_avatar_unknown_returns_404() -> None:
    handler = GatewayHTTPHandler.__new__(GatewayHTTPHandler)
    for bad in ("nope.jpg", "../etc/passwd"):
        resp = handler._handle_avatar(bad)
        assert resp.status_code == 404
