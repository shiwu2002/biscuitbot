"""``extract_documents`` / ``reference_non_image_attachments`` 的视频与 URL 处理。

回归点是 F1：视频扩展名不在 ``SUPPORTED_EXTENSIONS`` 中，``extract_text`` 返回
``None``，若把「非图片」一律当成文档处理，视频会被静默丢弃——落盘了，但模型
完全不知道有这个附件。
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from xianaibot.utils.document import (
    extract_documents,
    is_remote_url,
    is_video_file,
    media_placeholder_text,
    reference_non_image_attachments,
)

_MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


# -- is_remote_url / is_video_file --------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://cdn.example.com/a.mp4", True),
        ("http://cdn.example.com/a.mp4", True),
        ("/tmp/media/a.mp4", False),
        ("file:///tmp/a.mp4", False),
        ("", False),
    ],
)
def test_is_remote_url(value: str, expected: bool) -> None:
    assert is_remote_url(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://cdn.example.com/a.mp4", True),
        ("https://cdn.example.com/a.webm", True),
        ("https://cdn.example.com/a.m3u8", False),
        ("https://cdn.example.com/a.txt", False),
    ],
)
def test_is_video_file_for_remote_urls(value: str, expected: bool) -> None:
    """远程 URL 只按扩展名判断——绝不 ``Path(url).is_file()``。"""
    assert is_video_file(value) is expected


def test_is_video_file_uses_magic_bytes_for_local_files(tmp_path: Path) -> None:
    # 扩展名撒谎也要靠魔数識別出来
    lying = tmp_path / "clip.bin"
    lying.write_bytes(_MP4_MAGIC)
    assert is_video_file(str(lying)) is True

    png = tmp_path / "shot.png"
    png.write_bytes(_PNG_MAGIC)
    assert is_video_file(str(png)) is False


# -- extract_documents ---------------------------------------------------------


def test_extract_documents_keeps_local_video(tmp_path: Path) -> None:
    """F1 回归：视频必须出现在返回列表中，而不是被静默吞掉。"""
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)

    text, visual = extract_documents("看这个", [str(video)])

    assert visual == [str(video)]
    # 视频不能被文本抽取：既没有 [File: …] 段，也没有报错文本
    assert "[File:" not in text
    assert text == "看这个"


def test_extract_documents_keeps_remote_video_url_without_reading_it(
    tmp_path: Path,
) -> None:
    """URL 条目不做任何本地 IO——``Path(url).read_bytes()`` 会直接崩。"""
    url = "https://cdn.example.com/a/clip.mp4"

    text, visual = extract_documents("描述", [url])

    assert visual == [url]
    assert text == "描述"


def test_extract_documents_still_extracts_documents(tmp_path: Path) -> None:
    """新增视频分支不得影响既有文档抽取。"""
    doc = tmp_path / "notes.md"
    doc.write_text("# 标题\n正文内容", encoding="utf-8")

    text, visual = extract_documents("总结", [str(doc)])

    assert visual == []
    assert "[File: notes.md" in text
    assert "正文内容" in text


def test_extract_documents_mixed_batch(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text("正文内容", encoding="utf-8")
    png = tmp_path / "shot.png"
    png.write_bytes(_PNG_MAGIC)
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)
    url = "https://cdn.example.com/remote.mp4"

    text, visual = extract_documents("混合", [str(doc), str(png), str(video), url])

    assert visual == [str(png), str(video), url]
    assert "[File: notes.md" in text


# -- 注入块的抬头（文件名 + 提取完整性） ----------------------------------------


def test_doc_label_states_extraction_is_complete(tmp_path: Path) -> None:
    """抬头必须明说「已完整提取」。

    实测动机：抬头只有 ``[File: b92da4cc4d26.xlsx]`` 时，模型无从判断手里
    是全文还是节选，于是自己重新解析原文件——一次问答白花 70 秒与 6 次失败
    的工具调用。这行字就是让它不必再读一遍的依据。
    """
    doc = tmp_path / "学生备注表.md"
    doc.write_text("正文", encoding="utf-8")

    text, _ = extract_documents("看看", [str(doc)])

    assert "[File: 学生备注表.md (" in text
    assert "已完整提取" in text
    assert "已截断" not in text


def test_doc_label_hides_the_generated_upload_prefix(tmp_path: Path) -> None:
    """WebSocket 上传落盘的 ``<uuid12>_名字`` 在抬头里要还原成名字。

    前缀只是入口为了唯一性加的，对模型是纯噪音——它要认得的是用户的文件名。
    """
    doc = tmp_path / "0b96bcf8acf5_学员备注表.md"
    doc.write_text("正文", encoding="utf-8")

    text, _ = extract_documents("看看", [str(doc)])

    assert "[File: 学员备注表.md (" in text
    assert "0b96bcf8acf5" not in text


def test_doc_label_reports_truncation(tmp_path: Path) -> None:
    """真被截断时必须说出来，并给出内容上限——否则模型会以为拿到了全文。"""
    doc = tmp_path / "huge.txt"
    doc.write_text("x" * 300_000, encoding="utf-8")

    text, _ = extract_documents("看看", [str(doc)])

    assert "已截断" in text
    assert "已完整提取" not in text
    # 上限来自 ``_MAX_TEXT_LENGTH``，写清具体数字模型才知道缺了多少
    assert "200000" in text


def test_doc_label_reports_human_readable_size(tmp_path: Path) -> None:
    doc = tmp_path / "small.md"
    doc.write_text("hello", encoding="utf-8")

    text, _ = extract_documents("看看", [str(doc)])

    assert "(5 B，" in text


def test_extract_documents_skips_missing_path(tmp_path: Path) -> None:
    text, visual = extract_documents("hi", [str(tmp_path / "gone.mp4")])
    assert visual == []
    assert text == "hi"


def test_extract_documents_returns_two_tuple(tmp_path: Path) -> None:
    """签名保持 2 元组——既有测试与 monkeypatch 依赖这一点。"""
    doc = tmp_path / "a.md"
    doc.write_text("x", encoding="utf-8")
    result = extract_documents("t", [str(doc)])
    assert isinstance(result, tuple) and len(result) == 2


def test_extract_documents_pdf_actually_extracts(tmp_path: Path) -> None:
    """PDF 现在能通过 WS 白名单抵达抽取器，这条覆盖抽取本身仍可用。"""
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    pdf = tmp_path / "blank.pdf"
    with pdf.open("wb") as fh:
        writer.write(fh)
    assert isinstance(pypdf, object)

    text, visual = extract_documents("读一下", [str(pdf)])

    assert visual == []
    assert "[File: blank.pdf" in text


# -- reference_non_image_attachments ------------------------------------------


def test_reference_non_image_attachments_keeps_video(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(_MP4_MAGIC)

    content, visual = reference_non_image_attachments("hi", [str(video)])

    assert visual == [str(video)]
    assert "Attachment:" not in content


def test_reference_non_image_attachments_keeps_remote_url() -> None:
    url = "https://cdn.example.com/a.mp4"

    content, visual = reference_non_image_attachments("hi", [url])

    assert visual == [url]
    assert "Attachment:" not in content


def test_reference_non_image_attachments_refs_real_documents(tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text("x", encoding="utf-8")

    content, visual = reference_non_image_attachments("hi", [str(doc)])

    assert visual == []
    assert f"[Attachment: {doc}]" in content


# -- media_placeholder_text ---------------------------------------------------


@pytest.mark.parametrize(
    ("path", "prefix"),
    [
        ("/tmp/media/a.mp4", "[video: "),
        ("https://cdn.example.com/a.mp4", "[video: "),
        ("/tmp/media/a.png", "[image: "),
        ("https://cdn.example.com/a.png", "[image: "),
        ("/tmp/media/a.pdf", "[file: "),
    ],
)
def test_media_placeholder_text_is_kind_aware(path: str, prefix: str) -> None:
    assert media_placeholder_text(path).startswith(prefix)


def test_media_placeholder_text_empty() -> None:
    assert media_placeholder_text(None) == "[media]"
    assert media_placeholder_text(None, empty="[media omitted]") == "[media omitted]"


def test_video_placeholder_never_looks_like_an_image() -> None:
    """会话回放面包屑不能把 mp4 显示成 ``[image: x.mp4]``。"""
    placeholder = media_placeholder_text("/tmp/media/ab12.mp4")
    assert placeholder == "[video: /tmp/media/ab12.mp4]"


def test_png_magic_and_base64_sanity() -> None:
    """守卫：上面用到的魔数常量本身是可识别、可编码的。"""
    assert base64.b64encode(_PNG_MAGIC).decode()
