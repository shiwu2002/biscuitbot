"""搜索工具：文件查找与内容搜索。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统中的文件发现与内容搜索
组件。提供两个工具：

- ``FindFilesTool``（find_files）：按路径片段、glob 模式或文件类型查找文件，
  返回工作区相对路径。
- ``GrepTool``（grep）：使用 regex 模式搜索文件内容，返回匹配的文件路径或
  带上下文的匹配行。

这两个工具是 agent 进行代码导航与分析的基础能力。
"""

from __future__ import annotations

import fnmatch  # Unix shell 风格的通配符匹配
import os  # 操作系统接口，用于遍历目录树
import re  # 正则表达式，用于 grep 内容匹配
from contextlib import suppress  # 上下文管理器，忽略指定异常
from pathlib import Path, PurePosixPath  # 路径处理，PurePosixPath 用于 glob 匹配
from typing import Any, Iterable, TypeVar  # 类型注解

from biscuitbot.agent.tools.filesystem import ListDirTool, _FsTool  # 文件系统工具基类与列表工具

_DEFAULT_HEAD_LIMIT = 250  # grep 默认返回结果上限
_DEFAULT_FILE_HEAD_LIMIT = 200  # find_files 默认返回路径上限
T = TypeVar("T")  # 泛型类型变量，用于分页工具函数
# 文件类型简写到 glob 模式的映射表，支持常见编程语言与配置文件类型
_TYPE_GLOB_MAP = {
    "py": ("*.py", "*.pyi"),
    "python": ("*.py", "*.pyi"),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "ts": ("*.ts", "*.tsx", "*.mts", "*.cts"),
    "tsx": ("*.tsx",),
    "jsx": ("*.jsx",),
    "json": ("*.json",),
    "md": ("*.md", "*.mdx"),
    "markdown": ("*.md", "*.mdx"),
    "go": ("*.go",),
    "rs": ("*.rs",),
    "rust": ("*.rs",),
    "java": ("*.java",),
    "sh": ("*.sh", "*.bash"),
    "yaml": ("*.yaml", "*.yml"),
    "yml": ("*.yaml", "*.yml"),
    "toml": ("*.toml",),
    "sql": ("*.sql",),
    "html": ("*.html", "*.htm"),
    "css": ("*.css", "*.scss", "*.sass"),
}


def _normalize_pattern(pattern: str) -> str:
    """规范化 glob 模式：去除首尾空白并将反斜杠转为正斜杠。"""
    return pattern.strip().replace("\\", "/")


def _match_glob(rel_path: str, name: str, pattern: str) -> bool:
    """判断文件是否匹配给定的 glob 模式。

    若模式包含路径分隔符或以 ``**`` 开头，则按完整相对路径匹配；
    否则仅按文件名匹配。
    """
    normalized = _normalize_pattern(pattern)
    if not normalized:
        return False
    if "/" in normalized or normalized.startswith("**"):
        return PurePosixPath(rel_path).match(normalized)
    return fnmatch.fnmatch(name, normalized)


def _is_binary(raw: bytes) -> bool:
    """启发式判断字节内容是否为二进制（非文本）。

    判定规则：含 NUL 字节即为二进制；否则取前 4096 字节采样，
    若控制字符占比超过 20% 则视为二进制。
    """
    if b"\x00" in raw:
        return True
    sample = raw[:4096]
    if not sample:
        return False
    non_text = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return (non_text / len(sample)) > 0.2


def _paginate(items: list[T], limit: int | None, offset: int) -> tuple[list[T], bool]:
    """对结果列表进行分页，返回切片结果与是否被截断的标志。

    参数:
        items: 完整结果列表。
        limit: 每页最大条数；None 表示不限。
        offset: 跳过的条数。

    返回:
        (分页后的列表, 是否还有更多结果被截断)。
    """
    if limit is None:
        return items[offset:], False
    sliced = items[offset : offset + limit]
    truncated = len(items) > offset + limit
    return sliced, truncated


def _pagination_note(limit: int | None, offset: int, truncated: bool) -> str | None:
    """生成分页提示文本，仅在结果被截断或存在偏移时返回。"""
    if truncated:
        if limit is None:
            return f"(pagination: offset={offset})"
        return f"(pagination: limit={limit}, offset={offset})"
    if offset > 0:
        return f"(pagination: offset={offset})"
    return None


def _matches_type(name: str, file_type: str | None) -> bool:
    """判断文件名是否匹配指定的文件类型简写。

    支持的类型见 ``_TYPE_GLOB_MAP``；未映射的类型按 ``*.<type>`` 处理。
    """
    if not file_type:
        return True
    lowered = file_type.strip().lower()
    if not lowered:
        return True
    patterns = _TYPE_GLOB_MAP.get(lowered, (f"*.{lowered}",))
    return any(fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in patterns)


def _matches_query(rel_path: str, query: str | None) -> bool:
    """判断路径是否包含查询的所有术语（空格分隔，大小写不敏感）。"""
    if not query:
        return True
    haystack = rel_path.lower()
    terms = [part for part in query.lower().split() if part]
    return all(term in haystack for term in terms)


class _SearchTool(_FsTool):
    """搜索工具的公共基类，提供路径展示与文件遍历能力。"""

    _IGNORE_DIRS = set(ListDirTool._IGNORE_DIRS)  # 复用 ListDirTool 的忽略目录集合

    def _display_path(self, target: Path, root: Path) -> str:
        """将目标路径转换为展示用的相对路径。

        优先相对工作区根；若不在工作区内则相对搜索根。
        """
        workspace = self._display_workspace()
        if workspace:
            with suppress(ValueError):
                return target.relative_to(workspace).as_posix()
        return target.relative_to(root).as_posix()

    def _iter_files(self, root: Path) -> Iterable[Path]:
        """递归遍历目录树，跳过忽略目录，按字典序返回所有文件。"""
        if root.is_file():
            yield root
            return

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in self._IGNORE_DIRS)
            current = Path(dirpath)
            for filename in sorted(filenames):
                yield current / filename


class FindFilesTool(_SearchTool):
    """按路径片段、glob 模式或文件类型查找文件。"""
    _scopes = {"core", "subagent"}  # 工具可用作用域：核心与子 agent

    _capability = (
        "Find files by path fragment, glob, or file type; returns workspace-relative paths."
    )
    _usage_md = "docs/find_files.md"  # 使用说明文档路径

    @property
    def name(self) -> str:
        return "find_files"

    @property
    def description(self) -> str:
        return (
            "Find files by path fragment, glob, or file type. "
            "Use this before read_file when you need to locate files, and "
            "prefer it over shell find/ls for ordinary workspace discovery. "
            "Returns workspace-relative paths and skips common dependency/build "
            "directories."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory or file to search in (default '.')",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive path fragment search. "
                        "Whitespace-separated terms must all be present."
                    ),
                },
                "glob": {
                    "type": "string",
                    "description": "Optional file filter, e.g. '*.py' or 'tests/**/test_*.py'",
                },
                "type": {
                    "type": "string",
                    "description": "Optional file type shorthand, e.g. 'py', 'ts', 'md', 'json'",
                },
                "include_dirs": {
                    "type": "boolean",
                    "description": "Include matching directories as well as files (default false)",
                },
                "sort": {
                    "type": "string",
                    "enum": ["path", "modified"],
                    "description": "Sort by path or most recently modified first (default path)",
                },
                "head_limit": {
                    "type": "integer",
                    "description": "Maximum number of paths to return (default 200, 0 for all, max 1000)",
                    "minimum": 0,
                    "maximum": 1000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip the first N results before applying head_limit",
                    "minimum": 0,
                    "maximum": 100000,
                },
            },
        }

    def _iter_paths(self, root: Path, *, include_dirs: bool) -> Iterable[Path]:
        """递归遍历路径，可选择包含目录。

        与 ``_iter_files`` 不同，本方法在 ``include_dirs=True`` 时也会产出目录。
        """
        if root.is_file():
            yield root
            return
        if include_dirs:
            yield root
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in self._IGNORE_DIRS)
            current = Path(dirpath)
            if include_dirs and current != root:
                yield current
            for filename in sorted(filenames):
                yield current / filename

    async def execute(
        self,
        path: str = ".",
        query: str | None = None,
        glob: str | None = None,
        type: str | None = None,
        include_dirs: bool = False,
        sort: str = "path",
        head_limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> str:
        """执行文件查找。

        参数:
            path: 搜索的目录或文件（默认 '.'）。
            query: 可选的不区分大小写的路径片段搜索，空格分隔的术语必须全部存在。
            glob: 可选的文件过滤器，例如 '*.py' 或 'tests/**/test_*.py'。
            type: 可选的文件类型简写，例如 'py', 'ts', 'md', 'json'。
            include_dirs: 是否包含匹配的目录（默认 false）。
            sort: 排序方式，'path'（路径）或 'modified'（最近修改），默认 'path'。
            head_limit: 返回的最大路径数（默认 200，0 表示全部，最大 1000）。
            offset: 应用 head_limit 前跳过的结果数。

        返回:
            匹配的工作区相对路径列表；若未找到则返回 "No files found"。
        """
        try:
            target = self._resolve(path or ".")
            if not target.exists():
                return f"Error: Path not found: {path}"
            if not (target.is_dir() or target.is_file()):
                return f"Error: Unsupported path: {path}"

            if sort not in {"path", "modified"}:
                return "Error: sort must be 'path' or 'modified'"

            limit = (
                _DEFAULT_FILE_HEAD_LIMIT
                if head_limit is None
                else None if head_limit == 0 else head_limit
            )
            root = target if target.is_dir() else target.parent
            matches: list[tuple[str, float]] = []

            for candidate in self._iter_paths(target, include_dirs=include_dirs):
                if candidate.is_dir() and not include_dirs:
                    continue
                rel_path = candidate.relative_to(root).as_posix()
                display_path = self._display_path(candidate, root)
                name = candidate.name

                if glob and not _match_glob(rel_path, name, glob):
                    continue
                if candidate.is_file() and not _matches_type(name, type):
                    continue
                if candidate.is_dir() and type:
                    continue
                if not _matches_query(display_path, query):
                    continue
                try:
                    mtime = candidate.stat().st_mtime
                except OSError:
                    mtime = 0.0
                suffix = "/" if candidate.is_dir() else ""
                matches.append((display_path + suffix, mtime))

            if sort == "modified":
                matches.sort(key=lambda item: (-item[1], item[0]))
            else:
                matches.sort(key=lambda item: item[0])

            paths = [item[0] for item in matches]
            paged, truncated = _paginate(paths, limit, offset)
            if not paged:
                return "No files found"

            result = "\n".join(paged)
            note = _pagination_note(limit, offset, truncated)
            if note:
                result += "\n\n" + note
            return result
        except PermissionError as e:
            return f"Error: 无权限访问目录/文件：{e}，请确认可读权限"
        except Exception as e:
            return f"Error: 查找文件失败：{e}"


class GrepTool(_SearchTool):
    """使用类 regex 模式搜索文件内容。"""
    _scopes = {"core", "subagent"}  # 工具可用作用域：核心与子 agent

    _capability = (
        "Search file contents by regex pattern; returns matching paths or lines with context."
    )
    _usage_md = "docs/grep.md"  # 使用说明文档路径

    _MAX_RESULT_CHARS = 128_000  # 单次结果最大字符数
    _MAX_FILE_BYTES = 2_000_000  # 跳过超过 2MB 的文件

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents with a regex pattern. "
            "Default output_mode is files_with_matches (file paths only); "
            "use content mode for matching lines with context. Prefer this "
            "over shell grep for ordinary workspace searches. "
            "Skips binary and files >2 MB. Supports glob/type filtering."
        )

    @property
    def read_only(self) -> bool:
        return True

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex or plain text pattern to search for",
                    "minLength": 1,
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default '.')",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional file filter, e.g. '*.py' or 'tests/**/test_*.py'",
                },
                "type": {
                    "type": "string",
                    "description": "Optional file type shorthand, e.g. 'py', 'ts', 'md', 'json'",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search (default false)",
                },
                "fixed_strings": {
                    "type": "boolean",
                    "description": "Treat pattern as plain text instead of regex (default false)",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": (
                        "content: matching lines with optional context; "
                        "files_with_matches: only matching file paths; "
                        "count: matching line counts per file. "
                        "Default: files_with_matches"
                    ),
                },
                "context_before": {
                    "type": "integer",
                    "description": "Number of lines of context before each match",
                    "minimum": 0,
                    "maximum": 20,
                },
                "context_after": {
                    "type": "integer",
                    "description": "Number of lines of context after each match",
                    "minimum": 0,
                    "maximum": 20,
                },
                "max_matches": {
                    "type": "integer",
                    "description": (
                        "Legacy alias for head_limit in content mode"
                    ),
                    "minimum": 1,
                    "maximum": 1000,
                },
                "max_results": {
                    "type": "integer",
                    "description": (
                        "Legacy alias for head_limit in files_with_matches or count mode"
                    ),
                    "minimum": 1,
                    "maximum": 1000,
                },
                "head_limit": {
                    "type": "integer",
                    "description": (
                        "Maximum number of results to return. In content mode this limits "
                        "matching line blocks; in other modes it limits file entries. "
                        "Default 250"
                    ),
                    "minimum": 0,
                    "maximum": 1000,
                },
                "offset": {
                    "type": "integer",
                    "description": "Skip the first N results before applying head_limit",
                    "minimum": 0,
                    "maximum": 100000,
                },
            },
            "required": ["pattern"],
        }

    @staticmethod
    def _format_block(
        display_path: str,
        lines: list[str],
        match_line: int,
        before: int,
        after: int,
    ) -> str:
        """格式化一个匹配块：包含文件路径、行号与上下文行。

        匹配行用 ``>`` 标记，上下文行用空格标记。
        """
        start = max(1, match_line - before)
        end = min(len(lines), match_line + after)
        block = [f"{display_path}:{match_line}"]
        for line_no in range(start, end + 1):
            marker = ">" if line_no == match_line else " "
            block.append(f"{marker} {line_no}| {lines[line_no - 1]}")
        return "\n".join(block)

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        type: str | None = None,
        case_insensitive: bool = False,
        fixed_strings: bool = False,
        output_mode: str = "files_with_matches",
        context_before: int = 0,
        context_after: int = 0,
        max_matches: int | None = None,
        max_results: int | None = None,
        head_limit: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> str:
        """执行内容搜索。

        参数:
            pattern: 要搜索的 regex 或纯文本模式。
            path: 搜索的文件或目录（默认 '.'）。
            glob: 可选的文件过滤器。
            type: 可选的文件类型简写。
            case_insensitive: 是否大小写不敏感搜索（默认 false）。
            fixed_strings: 将模式视为纯文本而非 regex（默认 false）。
            output_mode: 输出模式：'content'（带上下文的匹配行）、
                'files_with_matches'（仅文件路径）、'count'（每文件匹配数）。
            context_before: 匹配行前的上下文行数。
            context_after: 匹配行后的上下文行数。
            max_matches: content 模式下 head_limit 的旧别名。
            max_results: files_with_matches/count 模式下 head_limit 的旧别名。
            head_limit: 返回的最大结果数。
            offset: 应用 head_limit 前跳过的结果数。

        返回:
            匹配结果；若未找到则返回 "No matches found"。
        """
        try:
            target = self._resolve(path or ".")
            if not target.exists():
                return f"Error: Path not found: {path}"
            if not (target.is_dir() or target.is_file()):
                return f"Error: Unsupported path: {path}"

            flags = re.IGNORECASE if case_insensitive else 0
            try:
                needle = re.escape(pattern) if fixed_strings else pattern
                regex = re.compile(needle, flags)
            except re.error as e:
                return f"Error: invalid regex pattern: {e}"

            if head_limit is not None:
                limit = None if head_limit == 0 else head_limit
            elif output_mode == "content" and max_matches is not None:
                limit = max_matches
            elif output_mode != "content" and max_results is not None:
                limit = max_results
            else:
                limit = _DEFAULT_HEAD_LIMIT
            blocks: list[str] = []
            result_chars = 0
            seen_content_matches = 0
            truncated = False
            size_truncated = False
            skipped_binary = 0
            skipped_large = 0
            matching_files: list[str] = []
            counts: dict[str, int] = {}
            file_mtimes: dict[str, float] = {}
            root = target if target.is_dir() else target.parent

            for file_path in self._iter_files(target):
                rel_path = file_path.relative_to(root).as_posix()
                if glob and not _match_glob(rel_path, file_path.name, glob):
                    continue
                if not _matches_type(file_path.name, type):
                    continue

                raw = file_path.read_bytes()
                if len(raw) > self._MAX_FILE_BYTES:
                    skipped_large += 1
                    continue
                if _is_binary(raw):
                    skipped_binary += 1
                    continue
                try:
                    mtime = file_path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    skipped_binary += 1
                    continue

                lines = content.splitlines()
                display_path = self._display_path(file_path, root)
                file_had_match = False
                for idx, line in enumerate(lines, start=1):
                    if not regex.search(line):
                        continue
                    file_had_match = True

                    if output_mode == "count":
                        counts[display_path] = counts.get(display_path, 0) + 1
                        continue
                    if output_mode == "files_with_matches":
                        if display_path not in matching_files:
                            matching_files.append(display_path)
                            file_mtimes[display_path] = mtime
                        break

                    seen_content_matches += 1
                    if seen_content_matches <= offset:
                        continue
                    if limit is not None and len(blocks) >= limit:
                        truncated = True
                        break
                    block = self._format_block(
                        display_path,
                        lines,
                        idx,
                        context_before,
                        context_after,
                    )
                    extra_sep = 2 if blocks else 0
                    if result_chars + extra_sep + len(block) > self._MAX_RESULT_CHARS:
                        size_truncated = True
                        break
                    blocks.append(block)
                    result_chars += extra_sep + len(block)
                if output_mode == "count" and file_had_match:
                    if display_path not in matching_files:
                        matching_files.append(display_path)
                        file_mtimes[display_path] = mtime
                if output_mode in {"count", "files_with_matches"} and file_had_match:
                    continue
                if truncated or size_truncated:
                    break

            if output_mode == "files_with_matches":
                if not matching_files:
                    result = f"No matches found for pattern '{pattern}' in {path}"
                else:
                    ordered_files = sorted(
                        matching_files,
                        key=lambda name: (-file_mtimes.get(name, 0.0), name),
                    )
                    paged, truncated = _paginate(ordered_files, limit, offset)
                    result = "\n".join(paged)
            elif output_mode == "count":
                if not counts:
                    result = f"No matches found for pattern '{pattern}' in {path}"
                else:
                    ordered_files = sorted(
                        matching_files,
                        key=lambda name: (-file_mtimes.get(name, 0.0), name),
                    )
                    ordered, truncated = _paginate(ordered_files, limit, offset)
                    lines = [f"{name}: {counts[name]}" for name in ordered]
                    result = "\n".join(lines)
            else:
                if not blocks:
                    result = f"No matches found for pattern '{pattern}' in {path}"
                else:
                    result = "\n\n".join(blocks)

            notes: list[str] = []
            if output_mode == "content" and truncated:
                notes.append(
                    f"(pagination: limit={limit}, offset={offset})"
                )
            elif output_mode == "content" and size_truncated:
                notes.append("(output truncated due to size)")
            elif truncated and output_mode in {"count", "files_with_matches"}:
                notes.append(
                    f"(pagination: limit={limit}, offset={offset})"
                )
            elif output_mode in {"count", "files_with_matches"} and offset > 0:
                notes.append(f"(pagination: offset={offset})")
            elif output_mode == "content" and offset > 0 and blocks:
                notes.append(f"(pagination: offset={offset})")
            if skipped_binary:
                notes.append(f"(skipped {skipped_binary} binary/unreadable files)")
            if skipped_large:
                notes.append(f"(skipped {skipped_large} large files)")
            if output_mode == "count" and counts:
                notes.append(
                    f"(total matches: {sum(counts.values())} in {len(counts)} files)"
                )
            if notes:
                result += "\n\n" + "\n".join(notes)
            return result
        except PermissionError as e:
            return f"Error: 无权限访问目录/文件：{e}，请确认可读权限"
        except Exception as e:
            return f"Error: 搜索文件失败：{e}"
