"""文档文本抽取工具。

所属模块与项目作用
===================
本文件位于 xianaibot/utils 目录，是工具函数模块的文档抽取组件。
在项目架构中起到的作用：
将用户附件（PDF / DOCX / XLSX / PPTX / 纯文本 / 图片等）抽取为纯文本，
并把图片与文档分离，使下游 LLM 层只需处理文本块与视觉块。
为节省启动开销，各格式的解析库均采用惰性导入。
"""

import mimetypes  # 扩展名到 MIME 的回退嗅探
import re  # 截断尾标识别
from pathlib import Path

from loguru import logger  # 结构化日志记录

from xianaibot.utils.helpers import (  # 通用辅助函数
    audio_placeholder_text,  # 音频占位文本
    detect_image_mime,  # 基于魔数的图片 MIME 嗅探
    detect_video_mime,  # 基于魔数的视频 MIME 嗅探
    image_placeholder_text,  # 图片占位文本
    video_placeholder_text,  # 视频占位文本
)
from xianaibot.utils.media_decode import readable_media_name  # 落盘名的展示化

# 支持文本抽取的文件扩展名集合
SUPPORTED_EXTENSIONS: set[str] = {
    # Document formats
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    # Text formats
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    # Image formats (for future OCR support)
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
}

# 单文件抽取文本的最大字符数，超出将被截断
_MAX_TEXT_LENGTH = 200_000

# 音频扩展名白名单。判定**只看扩展名/MIME，不做魔数嗅探**——ISO BMFF
# （``.m4a`` 与 ``.mp4``）和 EBML（``.weba`` 与 ``.webm``）都是音视频共用
# 容器，魔数完全一样，嗅探只会把音频判成视频。见 :func:`is_audio_file`。
AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    ".aac", ".aif", ".aiff", ".amr", ".caf", ".flac", ".m4a", ".mp3", ".mpga",
    ".oga", ".ogg", ".opus", ".wav", ".weba", ".wma",
})


def extract_text(path: Path) -> str | None:
    """从文件中抽取文本。

    Args:
        path: 文件路径。

    Returns:
        抽取出的文本字符串；不支持的扩展名返回 None；
        抽取失败返回以 ``[error: ...]`` 开头的错误字符串。
    """
    if not isinstance(path, Path):
        path = Path(path)

    if not path.exists():
        return f"[error: file not found: {path}]"

    ext = path.suffix.lower()

    # Document formats -- each branch lazily imports its parser so that
    # startup does not pay the ~25 MB cost of loading openpyxl /
    # python-docx / python-pptx / pypdf up front (see issue #3422).
    if ext == ".pdf":
        return _extract_pdf(path)
    elif ext == ".docx":
        return _extract_docx(path)
    elif ext == ".xlsx":
        return _extract_xlsx(path)
    elif ext == ".pptx":
        return _extract_pptx(path)
    elif _is_text_extension(ext):
        return _extract_text_file(path)
    elif ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        # Image files - for future OCR support
        return f"[image: {path.name}]"
    else:
        # Unsupported extension
        return None


def _extract_pdf(path: Path) -> str:
    """使用 pypdf 从 PDF 中抽取文本。"""
    try:
        from pypdf import PdfReader  # 惰性导入 PDF 解析库
    except ImportError:
        return "[error: pypdf not installed]"
    try:
        reader = PdfReader(path)
        pages: list[str] = []
        for i, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            pages.append(f"--- Page {i} ---\n{text}")
        return _truncate("\n\n".join(pages), _MAX_TEXT_LENGTH)
    except Exception as e:
        logger.exception("Failed to extract PDF {}", path)
        return f"[error: failed to extract PDF: {e!s}]"


def _extract_docx(path: Path) -> str:
    """使用 python-docx 从 DOCX 中抽取文本。"""
    try:
        from docx import Document as DocxDocument  # 惰性导入 DOCX 解析库
    except ImportError:
        return "[error: python-docx not installed]"
    try:
        doc = DocxDocument(path)
        paragraphs: list[str] = [p.text for p in doc.paragraphs if p.text.strip()]
        return _truncate("\n\n".join(paragraphs), _MAX_TEXT_LENGTH)
    except Exception as e:
        logger.exception("Failed to extract DOCX {}", path)
        return f"[error: failed to extract DOCX: {e!s}]"


def _extract_xlsx(path: Path) -> str:
    """使用 openpyxl 从 XLSX 中抽取文本。"""
    try:
        from openpyxl import load_workbook  # 惰性导入表格解析库
    except ImportError:
        return "[error: openpyxl not installed]"
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            sheets: list[str] = []
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows: list[str] = []
                for row in ws.iter_rows(values_only=True):
                    # 以制表符分隔单元格，空单元格留空
                    row_text = "\t".join(str(cell) if cell is not None else "" for cell in row)
                    if row_text.strip():
                        rows.append(row_text)
                if rows:
                    sheets.append(f"--- Sheet: {sheet_name} ---\n" + "\n".join(rows))
            return _truncate("\n\n".join(sheets), _MAX_TEXT_LENGTH)
        finally:
            wb.close()
    except Exception as e:
        logger.exception("Failed to extract XLSX {}", path)
        return f"[error: failed to extract XLSX: {e!s}]"


def _extract_pptx(path: Path) -> str:
    """使用 python-pptx 从 PPTX 中抽取文本。"""
    try:
        from pptx import Presentation as PptxPresentation  # 惰性导入 PPT 解析库
    except ImportError:
        return "[error: python-pptx not installed]"
    try:
        prs = PptxPresentation(path)
        slides: list[str] = []
        for i, slide in enumerate(prs.slides, 1):
            slide_text: list[str] = []
            for shape in slide.shapes:
                _collect_pptx_shape_text(shape, slide_text)
            if slide_text:
                slides.append(f"--- Slide {i} ---\n" + "\n".join(slide_text))
        return _truncate("\n\n".join(slides), _MAX_TEXT_LENGTH)
    except Exception as e:
        logger.exception("Failed to extract PPTX {}", path)
        return f"[error: failed to extract PPTX: {e!s}]"


def _collect_pptx_shape_text(shape, out: list[str]) -> None:
    """收集 PPTX 形状中的文本，递归处理分组与表格。

    分组形状 ``has_text_frame=False``，必须通过 ``.shapes`` 遍历；
    表格是 GraphicFrame 对象，其单元格文本位于 ``.table`` 之下。
    """
    sub_shapes = getattr(shape, "shapes", None)  # 分组形状：递归子形状
    if sub_shapes is not None:
        for sub in sub_shapes:
            _collect_pptx_shape_text(sub, out)
        return

    if getattr(shape, "has_table", False):  # 表格：按行收集单元格文本
        for row in shape.table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            line = "\t".join(cell for cell in cells if cell)
            if line:
                out.append(line)
        return

    text = getattr(shape, "text", "")  # 普通形状：直接取 text
    if text:
        out.append(text)


def _extract_text_file(path: Path) -> str:
    """从纯文本文件中抽取文本。"""
    try:
        # Try UTF-8 first, then latin-1 fallback
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = path.read_text(encoding="latin-1")  # 兜底编码，避免解码失败
        return _truncate(content, _MAX_TEXT_LENGTH)
    except Exception as e:
        logger.exception("Failed to read text file {}", path)
        return f"[error: failed to read file: {e!s}]"


def _truncate(text: str, max_length: int) -> str:
    """截断文本，并在末尾追加截断标记。"""
    if len(text) <= max_length:
        return text
    return text[:max_length] + f"... (truncated, {len(text)} chars total)"


# ``_truncate`` 的尾标。抽取结果是否被截断决定了注入块该怎么写：拿到节选却
# 以为拿到全文，与拿到全文却以为拿到节选，都是会让模型白干一轮的误判。
_TRUNCATION_MARKER_RE = re.compile(r"\.\.\. \(truncated, \d+ chars total\)\s*$")


def _is_truncated(extracted: str) -> bool:
    """抽取结果是否被 :func:`_truncate` 截断（尾标是同一文件内的契约）。"""
    return bool(_TRUNCATION_MARKER_RE.search(extracted))


def _human_size(size: int) -> str:
    """字节数的人类可读写法（注入块里给模型看的）。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _doc_label(disk_name: str, size: int, extracted: str) -> str:
    """文档注入块的抬头：文件名 + 大小 + **提取完整性**。

    完整性那几个字是必需的，不是装饰。抬头只写 ``[File: x.xlsx]`` 时模型无从
    判断手里是全文还是节选，于是倾向于自己再解析一遍原文件——实测这会在一
    次本可秒回的问答上多花 70 秒、6 次失败的工具调用（Windows 命令引号、
    找不到 python、``del /f /q`` 被 exec 安全策略拦下）。明确告知「已完整
    提取」即可省掉这一轮；真被截断时也必须说出来，并给出后续内容的上限。

    *disk_name* 是落盘名，含入口加的随机唯一性前缀；抬头里要剥掉它——
    ``0b96bcf8acf5_学员备注表.xlsx`` 对模型仍是噪音，它要认得的是
    ``学员备注表.xlsx``。
    """
    name = readable_media_name(disk_name)
    size_label = _human_size(size)
    if _is_truncated(extracted):
        return f"{name} ({size_label}，内容过长已截断，仅含前 {_MAX_TEXT_LENGTH} 字符)"
    return f"{name} ({size_label}，已完整提取)"


def _is_text_extension(ext: str) -> bool:
    """判断扩展名是否属于纯文本格式。"""
    return ext in {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".xml",
        ".html",
        ".htm",
        ".log",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
    }


# ---------------------------------------------------------------------------
# High-level helper: split media into images + extracted document text
# ---------------------------------------------------------------------------

# 抽取单文件的最大字节数（50 MB），超出则跳过以限制内存/CPU
_MAX_EXTRACT_FILE_SIZE = 50 * 1024 * 1024  # 50 MB


def is_remote_url(value: str) -> bool:
    """判断 *value* 是否为 http(s) 直链。

    远程媒体**不做本地文件读取**：``Path("https://…").is_file()`` 恒为
    ``False``，若下游把它当本地路径去 ``read_bytes()`` 会直接抛异常。
    所有面对 ``media`` 列表的函数都必须先过这一关。
    """
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def is_image_file(path: str) -> bool:
    """判断 *path* 是否为图片文件。

    优先用魔数检测（读取前 16 字节），失败时回退到 ``mimetypes``
    基于扩展名的判断。
    """
    p = Path(path)
    mime: str | None = None
    if p.is_file():
        try:
            with p.open("rb") as f:
                mime = detect_image_mime(f.read(16))  # 读取前 16 字节做魔数嗅探
        except OSError:
            mime = None
    if not mime:
        mime = mimetypes.guess_type(path)[0]  # 回退到扩展名判断
    return bool(mime and mime.startswith("image/"))


def is_audio_file(path: str) -> bool:
    """判断 *path* 是否为音频（本地文件或 http(s) 直链）。

    与 :func:`is_image_file` / :func:`is_video_file` 不同，这里**不嗅探魔数**：
    ``.m4a`` 与 ``.mp4``、``.weba`` 与 ``.webm`` 的容器头完全一样，嗅探无法
    区分，只会把录音误判成视频。所以扩展名白名单优先，``mimetypes`` 兜底。
    """
    if Path(path).suffix.lower() in AUDIO_EXTENSIONS:
        return True
    mime, _ = mimetypes.guess_type(path)
    return bool(mime and mime.startswith("audio/"))


def is_video_file(path: str) -> bool:
    """判断 *path* 是否为视频（本地文件或 http(s) 直链）。

    与 :func:`is_image_file` 对称：魔数优先，扩展名/MIME 兜底。远程直链不做
    本地读取，只按 URL 路径的扩展名判断。

    先按扩展名**否决**音频专用容器：``.m4a``（``ftypM4A``）与 ``.mp4``、
    ``.weba`` 与 ``.webm`` 的魔数完全相同，若让魔数优先，一段录音会被判成
    视频，于是模型拿到「用 ffmpeg 读画面」的错误说明——它要的是音频。
    """
    if Path(path).suffix.lower() in AUDIO_EXTENSIONS:
        return False

    if is_remote_url(path):
        mime, _ = mimetypes.guess_type(path)
        return bool(mime and mime.startswith("video/"))

    p = Path(path)
    mime: str | None = None
    if p.is_file():
        try:
            with p.open("rb") as f:
                mime = detect_video_mime(f.read(16))
        except OSError:
            mime = None
    if not mime:
        mime = mimetypes.guess_type(path)[0]
    return bool(mime and mime.startswith("video/"))


def media_placeholder_text(path: str | None, *, empty: str = "[media]") -> str:
    """按类型给出 kind-aware 的附件占位文本。

    仅看扩展名/MIME，不读文件内容——这是会话回放的热路径，且文件可能已被
    清理（此时 ``is_image_file``/``is_video_file`` 会自动回退到扩展名判断）。
    命名与 :func:`image_placeholder_text` 保持一致的 ``[kind: path]`` 形状。
    """
    if not path:
        return empty
    if is_audio_file(path):
        return audio_placeholder_text(path)
    if is_video_file(path):
        return video_placeholder_text(path)
    if is_image_file(path):
        return image_placeholder_text(path)
    return f"[file: {path}]"


def reference_non_image_attachments(
    content: str, media: list[str],
) -> tuple[str, list[str]]:
    """在不读取文件内容的前提下，将视觉/音频附件与其他附件分离。

    图片、视频与音频路径保留，交给下游 ``_build_user_content`` 处理（图片转
    内容块，视频与音频转路径说明）；远程直链同样保留，因为它也是「下游还得管」
    的附件。其余路径以 ``[Attachment: path]`` 形式追加到内容末尾。
    """
    forward_paths: list[str] = []
    attachment_refs: list[str] = []
    for path in media:
        if (
            is_remote_url(path)
            or is_image_file(path)
            or is_audio_file(path)
            or is_video_file(path)
        ):
            forward_paths.append(path)
        else:
            attachment_refs.append(f"[Attachment: {path}]")
    if attachment_refs:
        suffix = "\n".join(attachment_refs)
        content = f"{content}\n\n{suffix}" if content else suffix
    return content, forward_paths


def extract_documents(
    text: str,
    media_paths: list[str],
    *,
    max_file_size: int = _MAX_EXTRACT_FILE_SIZE,
) -> tuple[str, list[str]]:
    """将 *media_paths* 中的文档与视觉/音频附件分离。

    文档（PDF、DOCX、XLSX、PPTX、纯文本等）会被抽取为文本并追加到
    *text* 中；返回列表保留**仍需下游处理的路径**——图片、音频、视频与
    http(s) 直链。下游 ``_build_user_content`` 把图片转成 ``image_url`` 块，
    音频与视频只取路径写进文本说明（内容都不送模型，音频由模型按需调用
    ``transcribe_media`` 转写）。

    每个文档块以 ``[File: <文件名> (<大小>，已完整提取)]`` 开头（被截断时
    写明「仅含前 N 字符」），见 :func:`_doc_label`——这行抬头是模型判断
    「还需要自己读原文件吗」的唯一依据。

    注意：视频与音频**不能**走文本抽取分支。它们的扩展名不在
    :data:`SUPPORTED_EXTENSIONS` 中，``extract_text`` 会返回 ``None``，
    若在此处按「非图片即文档」处理，它们会被静默丢弃、模型完全不知道
    这个附件的存在。

    超过 *max_file_size* 字节的文件会被跳过并记录警告，以避免
    无限制的内存 / CPU 占用。
    """
    forward_paths: list[str] = []
    doc_texts: list[str] = []

    for path_str in media_paths:
        if is_remote_url(path_str):
            # 远程直链不做本地读取与文本抽取，原样交给下游
            forward_paths.append(path_str)
            continue

        p = Path(path_str)
        if not p.is_file():
            continue

        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > max_file_size:  # 超限文件跳过
            logger.warning(
                "Skipping oversized file for extraction: {} ({:.1f} MB > {} MB limit)",
                p.name, size / (1024 * 1024), max_file_size // (1024 * 1024),
            )
            continue

        if (
            is_image_file(path_str)
            or is_audio_file(path_str)
            or is_video_file(path_str)
        ):
            forward_paths.append(path_str)
        else:
            extracted = extract_text(p)
            if extracted and not extracted.startswith("[error:"):
                # 抬头带上文件名（落盘名里的可读 slug）与提取完整性：模型据此
                # 判断「不必再自己去读原文件」，见 ``_doc_label``。
                label = _doc_label(p.name, size, extracted)
                doc_texts.append(f"[File: {label}]\n{extracted}")

    if doc_texts:  # 将抽取的文档文本拼接到原文本后
        text = text + "\n\n" + "\n\n".join(doc_texts)

    return text, forward_paths
