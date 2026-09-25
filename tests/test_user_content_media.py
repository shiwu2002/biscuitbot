"""``_build_user_content`` 的附件分派：图片进块，音视频只给路径。

视频**永不**构造内容块（也不读字节）是产品决策——逐帧理解按帧计费，成本
不可控。音频同理，但多一步：模型可以按需调用 ``transcribe_media`` 把语音
转成文字（按音频秒数计费，画面与音频字节都不进上下文）。所以这里守两条
不变量：

1. 请求里绝不出现音视频像素/音频字节和 base64，只有一句带路径的说明；
2. 那句说明必须发出去。视频-only 的消息没有任何内容块，若沿用「无块即返回
   原文」的早退，模型会完全不知道有这个附件——比「看得到但没内容」更糟。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xianaibot.agent.context import ContextBuilder

# 最小可识别的 ISO BMFF 头（``ftyp`` box 位于偏移 4）
_MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
_WEBM_MAGIC = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
# 塞进视频字节里的哨兵：它若出现在请求里，就说明我们读了并编码了视频
_SENTINEL = b"SECRETFRAME"


def _make_builder(tmp_path: Path) -> ContextBuilder:
    return ContextBuilder(workspace=tmp_path, timezone="UTC")


def _blocks(result: object) -> list[dict]:
    assert isinstance(result, list), f"expected content blocks, got {type(result)}"
    return result


def _text_of(result: object) -> str:
    if isinstance(result, str):
        return result
    return "\n".join(b.get("text", "") for b in result if b.get("type") == "text")


def test_video_produces_no_content_block(tmp_path: Path) -> None:
    builder = _make_builder(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)

    result = builder._build_user_content("描述画面", [str(video)])

    # 只有视频 → 连内容块列表都不需要，直接是文本
    assert isinstance(result, str)
    assert "clip.mp4" in result


def test_video_note_says_the_content_was_not_sent(tmp_path: Path) -> None:
    """说明必须写明「没送」——否则模型会顺着用户的话假装看过画面。"""
    builder = _make_builder(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)

    text = _text_of(builder._build_user_content("描述画面", [str(video)]))

    assert "[用户附加视频：" in text
    assert "未发送给模型" in text


def test_video_note_points_at_the_video_understanding_skill(tmp_path: Path) -> None:
    """说明要点名技能，并把关键几步内联。

    「界面上的视频直链入口」已下线，模型对视频的唯一指引就是这段说明。点名技能
    是为了让它去读完整的成本阶梯；把三步内联是因为技能可能被 ``disabled_skills``
    或员工的 capability allowlist 过滤掉，那样光写技能名就悬空了。
    """
    builder = _make_builder(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)

    text = _text_of(builder._build_user_content("里面说了什么", [str(video)]))

    assert "video-understanding" in text
    assert "transcribe_media" in text  # 第一步：转写拿文字稿
    assert "逐帧" in text  # 红线：不许整片逐帧扫描


def test_video_bytes_never_reach_the_request(tmp_path: Path) -> None:
    """哨兵字节不得出现在请求里：视频既不编码也不读。"""
    builder = _make_builder(tmp_path)
    video = tmp_path / "clip.webm"
    video.write_bytes(_WEBM_MAGIC + _SENTINEL)

    payload = json.dumps(builder._build_user_content("看这个", [str(video)]), ensure_ascii=False)

    assert _SENTINEL.decode() not in payload
    assert "base64" not in payload
    assert "data:video/" not in payload


def test_remote_video_url_reaches_the_note_verbatim(tmp_path: Path) -> None:
    """远程直链照样不下载、不编码，原样写进说明。"""
    builder = _make_builder(tmp_path)
    url = "https://cdn.example.com/a/clip.mp4"

    text = _text_of(builder._build_user_content("描述画面", [url]))

    assert url in text
    assert "base64" not in text


def test_remote_non_video_url_is_ignored(tmp_path: Path) -> None:
    builder = _make_builder(tmp_path)

    assert builder._build_user_content("hi", ["https://example.com/readme.txt"]) == "hi"


def test_image_still_produces_an_image_url_block(tmp_path: Path) -> None:
    """视频通道的收窄不得影响图片。"""
    builder = _make_builder(tmp_path)
    png = tmp_path / "chart.png"
    png.write_bytes(_PNG_MAGIC)

    result = _blocks(builder._build_user_content("看", [str(png)]))

    image_blocks = [b for b in result if b["type"] == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    # ``_meta.path`` 是降级与历史占位的唯一依据，必须存在
    assert image_blocks[0]["_meta"]["path"] == str(png)


def test_text_note_groups_images_and_videos_separately(tmp_path: Path) -> None:
    builder = _make_builder(tmp_path)
    png = tmp_path / "chart.png"
    png.write_bytes(_PNG_MAGIC)
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)

    result = _blocks(builder._build_user_content("分析", [str(png), str(video)]))

    text = _text_of(result)
    assert "[用户附加图片：" in text and "[用户附加视频：" in text
    assert "chart.png" in text
    assert "clip.mp4" in text
    # 图片仍以内容块形式送上，视频不在其中
    assert [b["type"] for b in result if b["type"] != "text"] == ["image_url"]


def test_video_only_message_keeps_the_note_without_a_leading_newline(tmp_path: Path) -> None:
    """空文本 + 视频：说明不能丢，也不要以换行开头。"""
    builder = _make_builder(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)

    text = _text_of(builder._build_user_content("", [str(video)]))

    assert text.startswith("[用户附加视频：")
    assert not text.startswith("\n")


def test_missing_video_file_is_skipped(tmp_path: Path) -> None:
    """路径不存在时不应崩，也不应产出空块。"""
    builder = _make_builder(tmp_path)

    assert builder._build_user_content("hi", [str(tmp_path / "gone.mp4")]) == "hi"


# -- 音频：与视频同一通道，但说明里点名 transcribe_media -------------------------

_AUDIO_BYTES = b"ID3" + b"\x00" * 64


def test_audio_produces_no_content_block(tmp_path: Path) -> None:
    builder = _make_builder(tmp_path)
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(_AUDIO_BYTES + _SENTINEL)

    result = builder._build_user_content("转成文字", [str(audio)])

    # 只有音频 → 不需要内容块列表，说明带上路径即可
    assert isinstance(result, str)
    assert "voice.mp3" in result


def test_audio_note_says_the_content_was_not_sent(tmp_path: Path) -> None:
    builder = _make_builder(tmp_path)
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(_AUDIO_BYTES)

    text = _text_of(builder._build_user_content("转成文字", [str(audio)]))

    assert "[用户附加音频：" in text
    assert "未随消息发送" in text


def test_audio_note_points_at_transcribe_media(tmp_path: Path) -> None:
    """没点名工具，模型就只能靠猜——这是「按需转写」链条的第一环。"""
    builder = _make_builder(tmp_path)
    audio = tmp_path / "voice.m4a"
    audio.write_bytes(_AUDIO_BYTES)

    text = _text_of(builder._build_user_content("说了什么", [str(audio)]))

    assert "transcribe_media" in text
    # 正文里已有转写文本时不该再调（渠道语音会内联转写结果）
    assert "正文" in text


def test_audio_bytes_never_reach_the_request(tmp_path: Path) -> None:
    builder = _make_builder(tmp_path)
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(_AUDIO_BYTES + _SENTINEL)

    payload = json.dumps(builder._build_user_content("听一下", [str(audio)]), ensure_ascii=False)

    assert _SENTINEL.decode() not in payload
    assert "base64" not in payload
    assert "data:audio/" not in payload


def test_audio_is_not_read_into_memory(
    tmp_path: Path, monkeypatch: "pytest.MonkeyPatch"
) -> None:
    """音频必须在 ``read_bytes()`` 之前分流：否则 20MB 音频会被整个读进内存再丢掉。"""
    builder = _make_builder(tmp_path)
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(_AUDIO_BYTES)
    m4a = tmp_path / "voice.m4a"
    m4a.write_bytes(_AUDIO_BYTES)
    reads: list[Path] = []
    original = Path.read_bytes

    def recording_read_bytes(self: Path) -> bytes:
        reads.append(self)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", recording_read_bytes)

    builder._build_user_content("转成文字", [str(audio), str(m4a)])

    assert reads == []


def test_remote_audio_url_reaches_the_note_verbatim(tmp_path: Path) -> None:
    builder = _make_builder(tmp_path)
    url = "https://cdn.example.com/a/voice.mp3"

    text = _text_of(builder._build_user_content("转成文字", [url]))

    assert url in text
    assert "base64" not in text


def test_video_note_mentions_transcription(tmp_path: Path) -> None:
    """视频里的信息常常在语音里——说明要给出这条路，而不是让模型去猜。"""
    builder = _make_builder(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)

    text = _text_of(builder._build_user_content("讲的是什么", [str(video)]))

    assert "transcribe_media" in text


def test_text_note_groups_image_audio_and_video(tmp_path: Path) -> None:
    builder = _make_builder(tmp_path)
    png = tmp_path / "chart.png"
    png.write_bytes(_PNG_MAGIC)
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(_AUDIO_BYTES)
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)

    result = _blocks(builder._build_user_content("分析", [str(png), str(audio), str(video)]))

    text = _text_of(result)
    assert "[用户附加图片：" in text
    assert "[用户附加音频：" in text
    assert "[用户附加视频：" in text
    # 顺序固定：图片 → 音频 → 视频
    assert text.index("[用户附加图片：") < text.index("[用户附加音频：")
    assert text.index("[用户附加音频：") < text.index("[用户附加视频：")
    # 只有图片是内容块
    assert [b["type"] for b in result if b["type"] != "text"] == ["image_url"]
