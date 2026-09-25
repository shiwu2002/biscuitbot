"""Tests for ``xianaibot.utils.media_decode``."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from xianaibot.utils.media_decode import (
    DEFAULT_MAX_BYTES,
    MAX_FILE_SIZE,
    FileSizeExceeded,
    estimate_decoded_size,
    readable_media_name,
    save_base64_data_url,
)


def _data_url(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode()}"


def test_saves_png_with_correct_extension(tmp_path) -> None:
    result = save_base64_data_url(_data_url(b"fake png"), tmp_path)
    assert result is not None
    assert result.endswith(".png")
    assert (tmp_path / result.split("/")[-1]).read_bytes() == b"fake png"


def test_saves_data_url_with_mime_parameters(tmp_path) -> None:
    result = save_base64_data_url(_data_url(b"voice", mime="audio/webm;codecs=opus"), tmp_path)
    assert result is not None
    assert result.endswith(".webm")
    assert (tmp_path / result.split("/")[-1]).read_bytes() == b"voice"


@pytest.mark.parametrize(
    ("mime", "suffix"),
    [
        ("audio/webm", ".webm"),
        ("video/webm", ".webm"),
        ("audio/ogg", ".ogg"),
        ("audio/wav", ".wav"),
        ("audio/mpga", ".mpga"),
    ],
)
def test_saves_common_audio_with_api_friendly_extension(
    tmp_path, mime: str, suffix: str
) -> None:
    result = save_base64_data_url(_data_url(b"voice", mime=mime), tmp_path)
    assert result is not None
    assert result.endswith(suffix)


def test_returns_none_for_malformed_data_url(tmp_path) -> None:
    assert save_base64_data_url("not-a-data-url", tmp_path) is None


def test_returns_none_for_broken_base64(tmp_path) -> None:
    # Python's b64decode strips non-alphabet chars by default, so we need a
    # payload whose alphabet-filtered length breaks padding.
    assert save_base64_data_url("data:image/png;base64,not-valid-base64!!!", tmp_path) is None


def test_unknown_mime_falls_back_to_bin(tmp_path) -> None:
    result = save_base64_data_url(_data_url(b"xyz", mime="unknown/type"), tmp_path)
    assert result is not None
    assert result.endswith(".bin")


def test_default_limit_is_10mb(tmp_path) -> None:
    """Backwards-compatible default — the API path depends on this."""
    assert DEFAULT_MAX_BYTES == 10 * 1024 * 1024
    assert MAX_FILE_SIZE == 10 * 1024 * 1024

    oversized = b"x" * (11 * 1024 * 1024)
    with pytest.raises(FileSizeExceeded, match="10MB limit"):
        save_base64_data_url(_data_url(oversized), tmp_path)


def test_explicit_max_bytes_overrides_default(tmp_path) -> None:
    """WS channel passes 8 MB; a 9 MB payload should be rejected there even
    though it would pass the 10 MB API limit."""
    payload = b"y" * (9 * 1024 * 1024)
    with pytest.raises(FileSizeExceeded, match="8MB limit"):
        save_base64_data_url(_data_url(payload), tmp_path, max_bytes=8 * 1024 * 1024)


def test_saved_file_lives_under_media_dir(tmp_path) -> None:
    result = save_base64_data_url(_data_url(b"ok"), tmp_path)
    assert result is not None
    assert result.startswith(str(tmp_path))


def test_legacy_symbols_reexported_from_api_server() -> None:
    """Existing tests import ``_save_base64_data_url`` / ``_FileSizeExceeded``
    from ``xianaibot.api.server`` — keep the aliases working."""
    from xianaibot.api import server

    assert server._save_base64_data_url is save_base64_data_url
    assert server._FileSizeExceeded is FileSizeExceeded
    assert server.MAX_FILE_SIZE == MAX_FILE_SIZE


# -- decode-front size precheck ------------------------------------------------


def test_estimate_decoded_size_matches_base64_inflation() -> None:
    for n in (0, 1, 2, 3, 100, 4096):
        payload = base64.b64encode(b"x" * n).decode()
        assert estimate_decoded_size(payload) >= n
        # 估算值不会离谱地超过真实长度（base64 膨胀上限 + 取整余量）
        assert estimate_decoded_size(payload) <= n + 4


def test_oversized_payload_is_rejected_without_decoding(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7 回归：超限载荷必须在 ``b64decode`` **之前**被拒。

    先 decode 再比长度会让几十 MB 的 str + bytes 同时在内存里展开一次，
    是一个廉价的 DoS 放大点。
    """
    decoded_calls = []
    real_b64decode = base64.b64decode

    def _spy(data, *args, **kwargs):
        decoded_calls.append(len(data))
        return real_b64decode(data, *args, **kwargs)

    monkeypatch.setattr("xianaibot.utils.media_decode.base64.b64decode", _spy)

    oversized = b"x" * (11 * 1024 * 1024)
    with pytest.raises(FileSizeExceeded, match="10MB limit"):
        save_base64_data_url(_data_url(oversized), tmp_path)

    assert decoded_calls == [], "payload must be rejected before base64 decoding"


def test_payload_exactly_at_limit_is_accepted(tmp_path) -> None:
    """估算余量不能把恰好等于上限的文件误杀。"""
    limit = 1024
    payload = b"x" * limit

    result = save_base64_data_url(_data_url(payload), tmp_path, max_bytes=limit)

    assert result is not None
    assert (tmp_path / result.split("/")[-1]).stat().st_size == limit


def test_payload_one_byte_over_limit_is_rejected(tmp_path) -> None:
    limit = 1024
    with pytest.raises(FileSizeExceeded):
        save_base64_data_url(_data_url(b"x" * (limit + 1)), tmp_path, max_bytes=limit)


# -- client filename only contributes an extension -----------------------------


@pytest.mark.parametrize(
    ("name", "mime", "suffix"),
    [
        # 客户端扩展名在白名单内 → 采用它（浏览器常把 docx 报成 octet-stream）
        (
            "季度报告.docx",
            "application/octet-stream",
            ".docx",
        ),
        ("clip.mov", "video/quicktime", ".mov"),
        ("clip.mp4", "application/octet-stream", ".mp4"),
        # 不在白名单 → 回退 MIME 映射，客户端说了不算
        ("evil.exe", "image/png", ".png"),
        ("weird.zzz", "video/mp4", ".mp4"),
    ],
)
def test_whitelisted_client_extension_wins_over_mime(
    tmp_path, name: str, mime: str, suffix: str
) -> None:
    result = save_base64_data_url(_data_url(b"data", mime=mime), tmp_path, name=name)
    assert result is not None
    assert result.endswith(suffix)


def test_hostile_client_name_cannot_escape_media_dir(tmp_path) -> None:
    """路径遍历名不能把文件写到 media_dir 之外。

    落盘名以随机 uuid 开头、随后只保留 ``safe_filename`` 白名单内的字符，
    所以恶意名既逃不出目录，也顶多污染自己文件名的后半段。
    """
    result = save_base64_data_url(
        _data_url(b"data"), tmp_path, name="../../../../etc/passwd.mp4"
    )
    assert result is not None
    name = Path(result).name
    assert Path(result).parent == tmp_path
    assert name.endswith("_passwd.mp4")
    # 名字里可以有 "passwd"，但不能有目录成分
    assert "/" not in name and "\\" not in name and ".." not in name
    assert Path(result).is_file()
    assert not (tmp_path.parent / "etc").exists()


def test_readable_client_name_is_kept_for_display(tmp_path) -> None:
    """原始文件名保留在落盘名里 —— 模型与 WebUI 才看得见「这是什么文件」。

    回归的是实测问题：抬头显示 ``b92da4cc4d26.xlsx`` 时，模型怀疑摘要不完整
    而自己重新解析原文件，一次问答白花 70 秒与 6 次失败的工具调用。
    """
    result = save_base64_data_url(
        _data_url(b"data", mime="application/octet-stream"),
        tmp_path,
        name="季度报告.docx",
    )
    assert result is not None
    # 随机前缀仍在最前（保证唯一），可读名跟在后面
    assert re.fullmatch(r"[0-9a-f]{12}_季度报告\.docx", Path(result).name)


def test_unsafe_characters_in_client_name_are_replaced(tmp_path) -> None:
    result = save_base64_data_url(
        _data_url(b"data"), tmp_path, name="a*b?c:d.pdf"
    )
    assert result is not None
    name = Path(result).name
    assert name.endswith("_a_b_c_d.pdf")
    for ch in "*?:":
        assert ch not in name


def test_overlong_client_name_is_truncated(tmp_path) -> None:
    """超长文件名必须截断：Windows 路径上限 260 字符，越界会变成
    「附件解码失败」这种用户无法理解的错误。"""
    result = save_base64_data_url(
        _data_url(b"data"), tmp_path, name="x" * 500 + ".pdf"
    )
    assert result is not None
    assert len(Path(result).name) < 80
    assert Path(result).is_file()


def test_missing_or_useless_client_name_falls_back_to_uuid(tmp_path) -> None:
    """没有可读部分时退回纯 uuid 命名（与改动前的形状一致）。"""
    for name in (None, "", "...", "  "):
        result = save_base64_data_url(_data_url(b"data"), tmp_path, name=name)
        assert result is not None
        assert re.fullmatch(r"[0-9a-f]{12}\.png", Path(result).name), name


def test_same_client_name_twice_does_not_collide(tmp_path) -> None:
    """可读名不能变成覆盖风险：两次上传同名文件必须是两个文件。"""
    first = save_base64_data_url(_data_url(b"one"), tmp_path, name="同名.pdf")
    second = save_base64_data_url(_data_url(b"two"), tmp_path, name="同名.pdf")
    assert first is not None and second is not None
    assert first != second
    assert Path(first).read_bytes() == b"one"
    assert Path(second).read_bytes() == b"two"


# -- readable_media_name（展示用名字） -----------------------------------------


def test_readable_media_name_strips_the_generated_prefix() -> None:
    """落盘名 → 展示名：随机前缀是给唯一性用的，不该出现在 UI 标签里。"""
    assert readable_media_name("b92da4cc4d26_学生备注表.xlsx") == "学生备注表.xlsx"
    assert readable_media_name("0f1e2d3c4b5a_clip.mp4") == "clip.mp4"


@pytest.mark.parametrize(
    "filename",
    [
        "b92da4cc4d26.xlsx",  # 无 slug，纯 uuid 命名（改动前落下的附件）
        "报告.xlsx",  # 模型自建 / 用户 workspace 里的普通文件名
        "deadbeef1234",  # 恰好 12 位十六进制但没有下划线
        "",  # 空
        "notahash_报告.xlsx",  # 前缀不是十六进制
    ],
)
def test_readable_media_name_leaves_other_names_alone(filename: str) -> None:
    """只剥自己的前缀；其余名字原样返回，所以对任意文件名都可安全调用。"""
    assert readable_media_name(filename) == filename


def test_video_mime_gets_canonical_extension(tmp_path) -> None:
    """``mimetypes`` 对视频容器的映射平台相关，必须显式钉住。"""
    for mime, suffix in [
        ("video/mp4", ".mp4"),
        ("video/quicktime", ".mov"),
        ("video/x-m4v", ".m4v"),
        ("video/webm", ".webm"),
    ]:
        result = save_base64_data_url(_data_url(b"v"), tmp_path, max_bytes=1024)
        assert result is not None
        direct = save_base64_data_url(_data_url(b"v", mime=mime), tmp_path)
        assert direct is not None and direct.endswith(suffix)
