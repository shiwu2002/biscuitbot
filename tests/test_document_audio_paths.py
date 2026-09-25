"""音频附件的类型判定与占位文本（``utils/document.py``）。

音频此前在上下文里**彻底消失**：扩展名不在 ``SUPPORTED_EXTENSIONS`` 里，
``extract_text`` 返回 ``None``，于是既不进正文也不进 ``forward_paths``。
现在音频与视频同享「只给路径」的通道，因此这里守三件事：

1. ``is_audio_file`` 只认扩展名/MIME，**不嗅探魔数**；
2. ``is_video_file`` 必须**否决**音频专用容器——``.m4a`` 与 ``.mp4``、
   ``.weba`` 与 ``.webm`` 的容器头完全相同，若让魔数优先，一段录音会被判成
   视频，模型于是拿到「用 ffmpeg 读画面」的错误说明；
3. ``extract_documents`` / ``media_placeholder_text`` 都要把音频当附件看待。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from xianaibot.utils.document import (
    extract_documents,
    is_audio_file,
    is_video_file,
    media_placeholder_text,
    reference_non_image_attachments,
)

# ISO BMFF 与 EBML 容器头：音频（m4a/weba）与视频（mp4/webm）共用，魔数无法区分
_ISO_BMFF_MAGIC = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 64
_EBML_MAGIC = b"\x1a\x45\xdf\xa3" + b"\x00" * 64


@pytest.mark.parametrize(
    "name",
    [
        "voice.mp3",
        "voice.MP3",
        "voice.m4a",
        "voice.wav",
        "voice.ogg",
        "voice.opus",
        "voice.flac",
        "voice.aac",
        "voice.weba",
        "voice.amr",
        "voice.wma",
        "voice.aiff",
    ],
)
def test_is_audio_file_accepts_audio_extensions(tmp_path: Path, name: str) -> None:
    p = tmp_path / name
    p.write_bytes(b"\x00" * 16)

    assert is_audio_file(str(p)) is True


@pytest.mark.parametrize("name", ["clip.mp4", "clip.webm", "clip.mov", "readme.txt", "chart.png"])
def test_is_audio_file_rejects_non_audio(tmp_path: Path, name: str) -> None:
    p = tmp_path / name
    p.write_bytes(b"\x00" * 16)

    assert is_audio_file(str(p)) is False


def test_is_audio_file_accepts_remote_url() -> None:
    """远程直链按扩展名判断，不做任何本地 IO。"""
    assert is_audio_file("https://cdn.example.com/a/voice.mp3") is True
    assert is_audio_file("https://cdn.example.com/a/clip.mp4") is False


@pytest.mark.parametrize("name", ["voice.m4a", "voice.weba"])
def test_is_video_file_vetoes_audio_containers(tmp_path: Path, name: str) -> None:
    """回归点：m4a/weba 的容器头与 mp4/webm 一致，绝不能被判成视频。"""
    p = tmp_path / name
    p.write_bytes(_ISO_BMFF_MAGIC if name.endswith("m4a") else _EBML_MAGIC)

    assert is_video_file(str(p)) is False


def test_is_video_file_still_accepts_video_containers(tmp_path: Path) -> None:
    """否决音频不得误伤真正的视频。"""
    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    webm = tmp_path / "clip.webm"
    webm.write_bytes(_EBML_MAGIC)

    assert is_video_file(str(mp4)) is True
    assert is_video_file(str(webm)) is True


def test_media_placeholder_marks_audio(tmp_path: Path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"ID3")

    assert media_placeholder_text(str(audio)) == f"[audio: {audio}]"


def test_extract_documents_keeps_audio_paths(tmp_path: Path) -> None:
    """音频必须留在附件列表里，否则模型不知道有文件可转写。"""
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"ID3" + b"\x00" * 32)

    text, forwarded = extract_documents("转成文字", [str(audio)])

    assert forwarded == [str(audio)]
    # 音频不能走文本抽取：既没有 [File: …] 段，正文也不该被改动
    assert "[File:" not in text
    assert text == "转成文字"


def test_extract_documents_keeps_remote_audio_url(tmp_path: Path) -> None:
    url = "https://cdn.example.com/a/voice.mp3"

    text, forwarded = extract_documents("转成文字", [url])

    assert forwarded == [url]
    assert text == "转成文字"


def test_reference_non_image_attachments_keeps_audio(tmp_path: Path) -> None:
    audio = tmp_path / "voice.m4a"
    audio.write_bytes(_ISO_BMFF_MAGIC)

    content, forwarded = reference_non_image_attachments("转成文字", [str(audio)])

    assert forwarded == [str(audio)]
    assert "[Attachment:" not in content
