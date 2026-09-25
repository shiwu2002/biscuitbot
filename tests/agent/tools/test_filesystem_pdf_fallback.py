"""``read_file`` 读 PDF 的双后端行为。

桌面端 sidecar 打包``--exclude-module pymupdf``，但 ``pypdf`` 是核心依赖、
一直服务于对话附件抽取。缺了 pymupdf 就报「PDF reading requires pymupdf」会
造成不一致：附件里的 PDF 读得出内容，工作区里的 PDF 读不出来。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from xianaibot.agent.tools.filesystem import ReadFileTool

pytest.importorskip("pypdf")

# pymupdf 只用来**生成**测试用 PDF（pypdf 没有便利的写入 API）。必须在模块
# 导入期拿到模块引用：``no_pymupdf`` fixture 会把 ``sys.modules["fitz"]`` 置成
# ``None`` 来模拟桌面端打包，之后任何 ``import fitz`` 都会失败。
try:
    import fitz as _fitz
except ImportError:  # pragma: no cover - 未安装 pymupdf 的开发环境
    _fitz = None


def _make_pdf(tmp_path: Path, pages: int = 2) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    pdf = tmp_path / "doc.pdf"
    with pdf.open("wb") as fh:
        writer.write(fh)
    return pdf


def _make_tool(tmp_path: Path) -> ReadFileTool:
    return ReadFileTool(workspace=tmp_path)


@pytest.fixture
def no_pymupdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 ``import fitz`` 抛 ImportError，模拟桌面端打包。"""
    monkeypatch.setitem(sys.modules, "fitz", None)


def test_falls_back_to_pypdf_when_pymupdf_missing(
    tmp_path: Path, no_pymupdf: None
) -> None:
    """F12 回归：pymupdf 缺失时不能再返回错误。"""
    pdf = _make_pdf(tmp_path)
    tool = _make_tool(tmp_path)

    result = tool._read_pdf(pdf, None)

    assert "requires pymupdf" not in result
    # 空白页没有可抽取文本，但必须是「读到了、只是没文本」，而不是失败
    assert result == f"(PDF has no extractable text: {pdf})"


def test_pymupdf_path_still_used_when_available(tmp_path: Path) -> None:
    """pymupdf 存在时行为不变。"""
    if _fitz is None:  # pragma: no cover - 未安装 pymupdf 的开发环境
        pytest.skip("pymupdf 未安装")
    pdf = _make_pdf(tmp_path)
    tool = _make_tool(tmp_path)

    result = tool._read_pdf(pdf, None)

    assert "requires pymupdf" not in result
    assert result == f"(PDF has no extractable text: {pdf})"


def test_out_of_range_pages_errors_in_both_backends(
    tmp_path: Path, no_pymupdf: None
) -> None:
    pdf = _make_pdf(tmp_path, pages=2)
    tool = _make_tool(tmp_path)

    result = tool._read_pdf(pdf, "1-9")

    # _parse_page_range 会把 9 夹到末页，因此这是合法范围而非错误
    assert result == f"(PDF has no extractable text: {pdf})"


def test_invalid_page_range_returns_error(tmp_path: Path, no_pymupdf: None) -> None:
    pdf = _make_pdf(tmp_path, pages=2)
    tool = _make_tool(tmp_path)

    result = tool._read_pdf(pdf, "abc")

    assert result.startswith("Error: Invalid page range")


def test_start_beyond_document_returns_out_of_bounds(
    tmp_path: Path, no_pymupdf: None
) -> None:
    pdf = _make_pdf(tmp_path, pages=2)
    tool = _make_tool(tmp_path)

    result = tool._read_pdf(pdf, "5-6")

    assert "out of bounds" in result


def test_missing_pypdf_reports_both_backends(
    tmp_path: Path, no_pymupdf: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个后端都没有时，错误文案要提到两个库，而不是只提 pymupdf。"""
    pdf = _make_pdf(tmp_path)
    tool = _make_tool(tmp_path)
    monkeypatch.setitem(sys.modules, "pypdf", None)

    result = tool._read_pdf(pdf, None)

    assert "pymupdf" in result and "pypdf" in result


def test_corrupt_pdf_returns_error_not_traceback(
    tmp_path: Path, no_pymupdf: None
) -> None:
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really a pdf")
    tool = _make_tool(tmp_path)

    result = tool._read_pdf(pdf, None)

    assert result.startswith("Error reading PDF:")


def test_real_text_pdf_is_read_via_pypdf(
    tmp_path: Path, no_pymupdf: None
) -> None:
    """真有文本的 PDF 在 pymupdf 缺失时仍能抽出内容并按页标记。

    PDF 本身用 fitz 生成（只有它有便利的写入 API），但读取在 ``no_pymupdf``
    下进行，因此走的是 pypdf 后端。
    """
    if _fitz is None:
        pytest.skip("pymupdf 未安装，无法生成带文本的测试 PDF")

    doc = _fitz.open()
    for _ in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), "hello from pypdf fallback")
    pdf = tmp_path / "text.pdf"
    doc.save(str(pdf))
    doc.close()

    tool = _make_tool(tmp_path)
    result = tool._read_pdf(pdf, "1-1")

    assert "--- Page 1 ---" in result
    assert "hello from pypdf fallback" in result
    assert "--- Page 2 ---" not in result  # 页范围被尊重
    # 续读提示与 pymupdf 路径格式一致
    assert "(Showing pages 1-1 of 2. Use pages='2-2' to continue.)" in result
