"""通过结构化编辑指令批量应用文件修改的工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的文件编辑组件。
在项目架构中起到的作用：提供 ``apply_patch`` 工具，允许 agent 在单次调用中
对多个文件执行 replace（替换文本片段）或 add（追加文本/新建文件）操作，
并通过 dry_run 模式进行预校验，是 agent 进行代码修改的主要入口之一。
"""

from __future__ import annotations

import difflib  # 用于计算文件修改前后的行级增删统计
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from biscuitbot.agent.tools.base import tool_parameters  # 工具参数装饰器
from biscuitbot.agent.tools.filesystem import _FsTool  # 文件系统工具基类
from biscuitbot.agent.tools.schema import (  # JSON Schema 构造器
    ArraySchema,
    BooleanSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)


@dataclass(slots=True)
class _PatchSummary:
    """单次补丁操作的摘要信息。

    用于在工具返回结果中汇总每个编辑条目的执行情况。
    """

    action: str  # 实际执行的动作名称（add/update）
    path: str  # 目标文件相对路径
    added: int = 0  # 新增行数
    deleted: int = 0  # 删除行数


class _PatchError(ValueError):
    """补丁处理过程中的错误，用于区分业务错误与其他异常。"""
    pass


# 匹配 Windows 绝对路径（如 C:\ 或 D:/）
_ABSOLUTE_WINDOWS_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _validate_relative_path(path: str) -> str:
    """校验并规范化补丁路径，确保其为相对路径且不含目录穿越。

    参数:
        path: 待校验的路径字符串。

    返回:
        去除首尾空白后的路径。

    抛出:
        _PatchError: 当路径为空、含空字节、为绝对路径或包含 ``..`` 时。
    """
    normalized = path.strip()
    if not normalized:
        raise _PatchError("patch path cannot be empty")
    if "\0" in normalized:
        raise _PatchError(f"patch path contains a null byte: {path!r}")
    # 禁止绝对路径与家目录符号，防止越权访问
    if normalized.startswith(("~", "/", "\\")) or _ABSOLUTE_WINDOWS_RE.match(normalized):
        raise _PatchError(f"patch path must be relative: {path}")
    # 禁止 .. 目录穿越
    if any(part == ".." for part in re.split(r"[\\/]+", normalized)):
        raise _PatchError(f"patch path must not contain '..': {path}")
    return normalized


def _lines_to_text(lines: list[str]) -> str:
    """将行列表拼接为带结尾换行的文本。"""
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _text_line_count(text: str) -> int:
    """统计文本的行数。"""
    if not text:
        return 0
    return len(text.splitlines())


def _line_diff_stats(before: str, after: str) -> tuple[int, int]:
    """计算修改前后的行级增删数量。

    参数:
        before: 修改前文本。
        after: 修改后文本。

    返回:
        ``(added, deleted)`` 元组，分别表示新增行数与删除行数。
    """
    # 统一换行符后按行切分，便于行级 diff
    before_lines = before.replace("\r\n", "\n").splitlines()
    after_lines = after.replace("\r\n", "\n").splitlines()
    added = 0
    deleted = 0
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            deleted += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, deleted


def _append_text(content: str, addition: str) -> str:
    """将文本追加到已有内容末尾，避免合并到未结束的最后一行。

    参数:
        content: 已有文件内容。
        addition: 待追加的文本。

    返回:
        合并后的文本，保证以换行符结尾。
    """
    base = content.replace("\r\n", "\n")
    extra = addition.replace("\r\n", "\n")
    # 若原内容末尾无换行且追加内容不以换行开头，则补一个换行，避免两段拼接在同一行
    if base and extra and not base.endswith("\n") and not extra.startswith("\n"):
        base += "\n"
    combined = base + extra
    if combined and not combined.endswith("\n"):
        combined += "\n"
    return combined


def _format_summary(summary: _PatchSummary) -> str:
    """将补丁摘要格式化为可读的字符串行。

    参数:
        summary: 补丁摘要对象。

    返回:
        形如 ``- update foo.py (+3/-1)`` 的字符串。
    """
    stats = ""
    if summary.added or summary.deleted:
        stats = f" (+{summary.added}/-{summary.deleted})"
    return f"- {summary.action} {summary.path}{stats}"


@tool_parameters(
    tool_parameters_schema(
        edits=ArraySchema(
            items=ObjectSchema(
                path=StringSchema("Relative path to the file to edit."),
                action=StringSchema(
                    "Operation type: replace or add.",
                    enum=["replace", "add"],
                ),
                old_text=StringSchema(
                    "Exact text to search for in the file. Required for replace.",
                    nullable=True,
                ),
                new_text=StringSchema(
                    "Text to replace with or append. Required for replace and add.",
                    nullable=True,
                ),
                required=["path", "action"],
            ),
            description="List of edits to apply. Each edit specifies a file and the change to make.",
            min_items=1,
            max_items=20,
        ),
        dry_run=BooleanSchema(
            description="Validate and summarize the patch without writing files.",
            default=False,
        ),
        required=["edits"],
    )
)
class ApplyPatchTool(_FsTool):
    """通过结构化编辑指令应用文件修改的工具。

    职责：接收一组编辑指令（replace/add），在单次调用中完成多文件修改，
    支持 dry_run 预校验、CRLF 换行保留、写入失败回滚以及变更摘要输出。

    用法：由 agent 调用，传入 ``edits`` 列表与可选的 ``dry_run`` 标志。
    """

    _scopes = {"core", "subagent"}  # 工具可在 core 与 subagent 作用域使用

    _capability = (
        "Apply multi-file code edits with replace/add actions in a single call."
    )
    _usage_md = "docs/apply_patch.md"  # 工具使用说明文档路径

    @property
    def name(self) -> str:
        """工具名称。"""
        return "apply_patch"

    @property
    def description(self) -> str:
        """工具描述，供模型理解工具能力。"""
        return (
            "Default tool for code edits. Supports multi-file changes in a single call. "
            "Provide a list of structured edits, each specifying a file path, action "
            "(replace/add), and the exact text to change. "
            "Paths must be relative. Set dry_run=true to validate and preview without writing files. "
            "Use edit_file only for small exact replacements on a single file."
        )

    async def execute(
        self,
        edits: list[dict] | None = None,
        dry_run: bool = False,
        **kwargs: Any,
    ) -> str:
        """执行补丁应用。

        参数:
            edits: 编辑指令列表，每项含 ``path``、``action``、``old_text``、``new_text``。
            dry_run: 为 True 时仅校验并返回摘要，不实际写盘。

        返回:
            执行结果字符串，包含每个编辑条目的摘要；失败时返回错误信息。
        """
        try:
            if not edits:
                raise _PatchError("must provide edits")

            writes: dict[Path, str] = {}  # 待写入文件路径到新内容的映射
            summaries: list[_PatchSummary] = []  # 各编辑条目的摘要

            for edit in edits:
                if not isinstance(edit, dict):
                    raise _PatchError("each edit must be an object")
                raw_path = edit.get("path")
                if not isinstance(raw_path, str):
                    raise _PatchError("path required for edit")
                path = _validate_relative_path(raw_path)
                action = edit.get("action")
                if not isinstance(action, str):
                    raise _PatchError(f"action required for edit: {path}")
                source = self._resolve(path)

                if action == "add":
                    # add 动作：追加文本或新建文件
                    new_text = edit.get("new_text")
                    if new_text is None:
                        raise _PatchError(f"new_text required for add: {path}")

                    pending = writes.get(source)
                    if pending is not None:
                        content = pending
                        exists = True
                    elif source.exists():
                        raw = source.read_bytes()
                        try:
                            content = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            raise _PatchError(f"file is not UTF-8 text: {path}")
                        exists = True
                    else:
                        content = ""
                        exists = False

                    if exists:
                        uses_crlf = "\r\n" in content  # 记录是否使用 CRLF 换行
                        new_norm = _append_text(content, new_text)
                        if uses_crlf:
                            new_norm = new_norm.replace("\n", "\r\n")  # 还原 CRLF
                        writes[source] = new_norm
                        added, deleted = _line_diff_stats(content, new_norm)
                        action_name = "update"
                    else:
                        # 新建文件场景
                        new_norm = new_text.replace("\r\n", "\n")
                        if new_norm and not new_norm.endswith("\n"):
                            new_norm += "\n"
                        writes[source] = new_norm
                        added = _text_line_count(new_norm)
                        deleted = 0
                        action_name = "add"

                    summaries.append(
                        _PatchSummary(
                            action=action_name, path=path, added=added, deleted=deleted
                        )
                    )

                elif action == "replace":
                    # replace 动作：精确替换文件中的文本片段
                    old_text = edit.get("old_text") or ""
                    if not old_text:
                        raise _PatchError(f"old_text required for replace: {path}")
                    new_text = edit.get("new_text")
                    if new_text is None:
                        raise _PatchError(f"new_text required for replace: {path}")

                    pending = writes.get(source)
                    if pending is not None:
                        content = pending
                    elif source.exists():
                        raw = source.read_bytes()
                        try:
                            content = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            raise _PatchError(f"file is not UTF-8 text: {path}")
                    else:
                        raise _PatchError(f"file to update does not exist: {path}")

                    if pending is None and not source.is_file():
                        raise _PatchError(f"path to update is not a file: {path}")

                    uses_crlf = "\r\n" in content
                    # 统一换行符后再做查找替换，避免 CRLF 不匹配
                    norm_content = content.replace("\r\n", "\n")
                    norm_old = old_text.replace("\r\n", "\n")

                    pos = norm_content.find(norm_old)
                    if pos < 0:
                        raise _PatchError(f"old_text 未在 {path} 中找到，请先 read_file 重新读取该文件，复制精确的原文片段后重试")
                    # 禁止 old_text 出现多次，避免歧义替换
                    if norm_content.find(norm_old, pos + 1) >= 0:
                        raise _PatchError(f"old_text appears multiple times in {path}")

                    new_norm = (
                        norm_content[:pos]
                        + new_text.replace("\r\n", "\n")
                        + norm_content[pos + len(norm_old) :]
                    )
                    if new_norm and not new_norm.endswith("\n"):
                        new_norm += "\n"
                    if uses_crlf:
                        new_norm = new_norm.replace("\n", "\r\n")

                    writes[source] = new_norm
                    added, deleted = _line_diff_stats(content, new_norm)
                    summaries.append(
                        _PatchSummary(
                            action="update", path=path, added=added, deleted=deleted
                        )
                    )

                else:
                    raise _PatchError(f"unknown action: {action}")

            if dry_run:
                # 干跑模式：仅返回校验摘要，不写盘
                return "Patch dry-run succeeded:\n" + "\n".join(
                    _format_summary(summary) for summary in summaries
                )

            # 写盘前备份，便于失败回滚
            backups: dict[Path, bytes | None] = {}
            for path in writes:
                backups[path] = path.read_bytes() if path.exists() else None

            try:
                for path, content in writes.items():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8", newline="")
            except Exception:
                # 写入失败时回滚到备份状态
                for path, data in backups.items():
                    if data is None:
                        if path.exists():
                            path.unlink()
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                raise

            for path in writes:
                self._file_states.record_write(path)
            return "Patch applied:\n" + "\n".join(
                _format_summary(summary) for summary in summaries
            )
        except PermissionError:
            return "Error: 没有权限写入补丁目标文件，请检查文件/目录权限"
        except _PatchError as exc:
            return f"Error applying patch: {exc}"
        except Exception as exc:
            return f"Error: 写入补丁失败（{exc}），已回滚本次所有修改。请检查目标路径是否为目录、磁盘空间是否充足"
