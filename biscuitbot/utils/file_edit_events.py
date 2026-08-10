"""文件编辑活动辅助工具，用于生成 WebUI 进度事件。

所属模块与项目作用
===================
本文件位于 biscuitbot/utils 目录，是工具函数模块的文件编辑事件组件。
在项目架构中起到的作用：
跟踪文件编辑工具（write_file / edit_file / apply_patch）执行前后
的文件快照与行级差异，构造 WebUI 所需的进度事件（start / live /
end / error / pending），使前端能实时展示编辑增量。
"""

from __future__ import annotations

import difflib  # 行级差异计算
import re  # 流式 JSON 字段扫描
import time  # 单调时钟节流
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger  # 结构化日志记录

# 需要跟踪编辑活动的文件编辑工具集合
TRACKED_FILE_EDIT_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})
# 读取文件快照的最大字节数，超出标记为 oversized
_MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
# 流式 live 事件的最小发射间隔（秒），用于节流
_LIVE_EMIT_INTERVAL_S = 0.18
# 行数变化达到该阈值时立即发射 live 事件
_LIVE_EMIT_LINE_STEP = 24


@dataclass(slots=True)
class FileSnapshot:
    """文件在某一时刻的快照，记录内容与可读性状态。"""

    path: Path
    exists: bool
    text: str | None
    unreadable: bool = False
    binary: bool = False
    oversized: bool = False

    @property
    def countable(self) -> bool:
        """是否可用于行级差异统计（非二进制、未超限、可读）。"""
        return (
            self.text is not None
            and not self.binary
            and not self.oversized
            and not self.unreadable
        )


@dataclass(slots=True)
class FileEditTracker:
    """单次文件编辑工具调用的跟踪记录。"""

    call_id: str
    tool: str
    path: Path
    display_path: str
    before: FileSnapshot


def is_file_edit_tool(tool_name: str | None) -> bool:
    """判断工具名是否属于需跟踪的文件编辑工具。"""
    return bool(tool_name) and tool_name in TRACKED_FILE_EDIT_TOOLS


def resolve_file_edit_path(
    tool: Any,
    workspace: Path | None,
    params: dict[str, Any] | None,
) -> Path | None:
    """在工具参数准备完成后，解析目标文件的绝对路径。"""
    if not isinstance(params, dict):
        return None
    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    resolver = getattr(tool, "_resolve", None)  # 优先调用工具自带的路径解析器
    if callable(resolver):
        try:
            resolved = resolver(raw_path)
            if isinstance(resolved, Path):
                return resolved
            if resolved:
                return Path(resolved)
        except Exception:
            logger.debug("file_edit_events: tool path resolver failed for {}", raw_path, exc_info=True)
            return None
    if workspace is None:
        return Path(raw_path).expanduser().resolve()  # 无工作区时按绝对路径处理
    return (workspace / raw_path).expanduser().resolve()


def display_file_edit_path(path: Path, workspace: Path | None) -> str:
    """生成相对工作区的展示路径；无法相对化时回退为 POSIX 路径。"""
    if workspace is not None:
        try:
            return path.resolve().relative_to(workspace.resolve()).as_posix()
        except Exception:
            logger.debug("file_edit_events: relative path conversion failed", exc_info=True)
    return path.as_posix()


def read_file_snapshot(path: Path, *, max_bytes: int = _MAX_SNAPSHOT_BYTES) -> FileSnapshot:
    """读取文件快照，区分不存在、超限、二进制与可读文本等状态。"""
    try:
        if not path.exists() or not path.is_file():
            return FileSnapshot(path=path, exists=False, text="")
        size = path.stat().st_size
        if size > max_bytes:  # 超限：标记 oversized 并不读内容
            return FileSnapshot(path=path, exists=True, text=None, oversized=True)
        raw = path.read_bytes()
    except OSError:
        return FileSnapshot(path=path, exists=path.exists(), text=None, unreadable=True)
    if b"\x00" in raw:  # 含 NUL 字节视为二进制
        return FileSnapshot(path=path, exists=True, text=None, binary=True)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return FileSnapshot(path=path, exists=True, text=None, binary=True)
    # 统一换行为 LF，便于后续行级比较
    return FileSnapshot(path=path, exists=True, text=text.replace("\r\n", "\n"))


def line_diff_stats(before: str | None, after: str | None) -> tuple[int, int]:
    """对 UTF-8 文本做行级差异，返回 ``(新增行数, 删除行数)``。"""
    if before is None or after is None:
        return 0, 0
    if before == "":
        return _text_line_count(after), 0  # 全新增
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


def _text_line_count(text: str) -> int:
    """统计文本行数，正确处理 CR / LF / CRLF 换行。"""
    if not text:
        return 0
    line_count = 0
    last_was_newline = False
    last_was_cr = False
    for ch in text:
        if ch == "\r":  # 单独 CR 计为一行
            line_count += 1
            last_was_newline = True
            last_was_cr = True
        elif ch == "\n":  # LF 计行，但 CRLF 不重复计数
            if not last_was_cr:
                line_count += 1
            last_was_newline = True
            last_was_cr = False
        else:
            last_was_newline = False
            last_was_cr = False
    # 末尾无换行时补计最后一行
    return line_count if last_was_newline else line_count + 1


def prepare_file_edit_tracker(
    *,
    call_id: str,
    tool_name: str,
    tool: Any,
    workspace: Path | None,
    params: dict[str, Any] | None,
) -> FileEditTracker | None:
    """准备单个文件编辑跟踪器，无目标路径时返回 None。"""
    trackers = prepare_file_edit_trackers(
        call_id=call_id,
        tool_name=tool_name,
        tool=tool,
        workspace=workspace,
        params=params,
    )
    return trackers[0] if trackers else None


def prepare_file_edit_trackers(
    *,
    call_id: str,
    tool_name: str,
    tool: Any,
    workspace: Path | None,
    params: dict[str, Any] | None,
) -> list[FileEditTracker]:
    """为一次文件编辑工具调用准备跟踪器列表（apply_patch 可含多文件）。"""
    if not is_file_edit_tool(tool_name):
        return []
    paths = resolve_file_edit_paths(tool_name, tool, workspace, params)
    trackers: list[FileEditTracker] = []
    seen: set[Path] = set()  # 去重已处理的路径
    for path in paths:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        before = read_file_snapshot(path)  # 记录编辑前快照
        trackers.append(FileEditTracker(
            call_id=str(call_id or ""),
            tool=tool_name,
            path=path,
            display_path=display_file_edit_path(path, workspace),
            before=before,
        ))
    return trackers


def resolve_file_edit_paths(
    tool_name: str,
    tool: Any,
    workspace: Path | None,
    params: dict[str, Any] | None,
) -> list[Path]:
    """解析一次工具调用涉及的全部目标路径。"""
    if tool_name == "apply_patch":
        return _resolve_apply_patch_paths(tool, workspace, params)
    path = resolve_file_edit_path(tool, workspace, params)
    if path is None:
        return []
    return [path]


def _resolve_apply_patch_paths(
    tool: Any,
    workspace: Path | None,
    params: dict[str, Any] | None,
) -> list[Path]:
    """从 apply_patch 的 edits 数组中解析全部目标路径。"""
    if not isinstance(params, dict):
        return []
    edits = params.get("edits")
    if not isinstance(edits, list) or not edits:
        return []
    if params.get("dry_run") is True:  # 干跑模式不实际写入，无需跟踪
        return []

    resolved: list[Path] = []
    seen: set[Path] = set()  # 同一路径只保留一次
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        raw_path = edit.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        path = _resolve_raw_file_edit_path(tool, workspace, raw_path)
        if path is not None and path not in seen:
            seen.add(path)
            resolved.append(path)
    return resolved


def _resolve_raw_file_edit_path(
    tool: Any,
    workspace: Path | None,
    raw_path: str,
) -> Path | None:
    """解析单个原始路径字符串为绝对路径（支持工具自带解析器）。"""
    resolver = getattr(tool, "_resolve", None)
    if callable(resolver):
        try:
            resolved = resolver(raw_path)
            if isinstance(resolved, Path):
                return resolved
            if resolved:
                return Path(resolved)
        except Exception:
            logger.debug("file_edit_events: tool path resolver failed for {}", raw_path, exc_info=True)
            return None
    if workspace is None:
        return Path(raw_path).expanduser().resolve()
    return (workspace / raw_path).expanduser().resolve()


def build_file_edit_start_event(
    tracker: FileEditTracker,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """构造编辑开始事件，基于预测的编辑后文本估算行差。"""
    predicted_after = _predict_after_text(tracker.tool, params or {}, tracker.before)
    if tracker.before.countable and predicted_after is not None:
        added, deleted = line_diff_stats(tracker.before.text, predicted_after)
    else:
        added, deleted = 0, 0
    return _event_payload(
        tracker,
        phase="start",
        status="editing",
        added=added,
        deleted=deleted,
        approximate=True,  # 开始阶段为预估值
    )


def build_file_edit_end_event(
    tracker: FileEditTracker,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造编辑结束事件，优先用实际编辑后快照计算精确行差。"""
    after = read_file_snapshot(tracker.path)  # 读取编辑后真实快照
    counted = False
    if tracker.before.countable and after.countable:
        # 编辑前后均可统计：使用精确行差
        added, deleted = line_diff_stats(tracker.before.text, after.text)
        counted = True
    else:
        # 无法精确统计时回退到预测值
        predicted_after = _predict_after_text(tracker.tool, params or {}, tracker.before)
        if tracker.before.countable and predicted_after is not None:
            added, deleted = line_diff_stats(tracker.before.text, predicted_after)
            counted = True
        else:
            added, deleted = 0, 0
    return _event_payload(
        tracker,
        phase="end",
        status="done",
        added=added,
        deleted=deleted,
        approximate=False,
        binary=(after.binary or after.oversized or after.unreadable) and not counted,
        operation="delete" if tracker.before.exists and not after.exists else None,
    )


def build_file_edit_error_event(
    tracker: FileEditTracker,
    error: str | None = None,
) -> dict[str, Any]:
    """构造编辑失败事件，并附带截断后的错误信息。"""
    payload = _event_payload(
        tracker,
        phase="error",
        status="error",
        added=0,
        deleted=0,
        approximate=False,
    )
    if error:
        payload["error"] = error.strip()[:240]  # 截断错误信息长度
    return payload


def build_file_edit_live_event(
    tracker: FileEditTracker,
    *,
    added: int,
    deleted: int = 0,
    operation: str | None = None,
) -> dict[str, Any]:
    """在工具调用参数仍流式传输时，构造近似的进行中事件。"""
    return _event_payload(
        tracker,
        phase="start",
        status="editing",
        added=added,
        deleted=deleted,
        approximate=True,
        operation=operation,
    )


def build_file_edit_pending_event(
    *,
    call_id: str,
    tool_name: str,
    added: int = 0,
    deleted: int = 0,
) -> dict[str, Any]:
    """在流式 JSON 路径尚未可用前，构造早期占位事件。"""
    return {
        "version": 1,
        "call_id": str(call_id or ""),
        "tool": tool_name,
        "path": "",  # 路径未知，留空
        "phase": "start",
        "added": max(0, int(added)),
        "deleted": max(0, int(deleted)),
        "approximate": True,
        "status": "editing",
        "pending": True,
    }


class StreamingFileEditTracker:
    """在模型仍在流式输出工具参数时，跟踪文件编辑工具的参数增量。

    工具执行事件只有在 provider 完成整个 function call 后才开始。
    对于大型 ``write_file`` 调用，长等待通常是模型生成 JSON ``content``
    参数的过程；大型 ``edit_file`` 调用则在 ``old_text`` / ``new_text``
    流入时也有同样的等待。本跟踪器在这些参数增量到达时，把它们转换为
    近似的 WebUI 文件编辑事件，直到最终的精确差异可用。
    """

    def __init__(
        self,
        *,
        workspace: Path | None,
        tools: Any,
        emit: Callable[[list[dict[str, Any]]], Awaitable[None]],
    ) -> None:
        self._workspace = workspace
        self._tools = tools
        self._emit = emit  # 事件发射回调
        self._states: dict[str, _StreamingFileEditState] = {}  # 按 key 维护各流式状态

    async def update(self, payload: dict[str, Any]) -> None:
        """处理一条流式增量，必要时发射近似 live 事件。"""
        key = _stream_key(payload)
        if not key:
            return
        state = self._states.get(key)
        if state is None:
            state = _StreamingFileEditState(key=key)
            self._states[key] = state

        state.apply_delta(payload)
        if state.name == "apply_patch":
            await self._update_apply_patch(state)
            return
        if state.name not in {"write_file", "edit_file"}:
            return
        # 路径未就绪前只能发 pending 事件
        if state.path is None:
            state.path = _extract_complete_json_string(state.arguments, "path")
        if state.path is None:
            added, deleted = state.live_diff_counts()
            now = time.monotonic()
            if state.should_emit_pending(added, deleted, now):
                state.mark_pending_emitted(added, deleted, now)
                await self._emit([build_file_edit_pending_event(
                    call_id=state.call_id or state.key,
                    tool_name=state.name,
                    added=added,
                    deleted=deleted,
                )])
            return
        # 路径就绪后构建正式 tracker
        if state.tracker is None:
            tool = self._tools.get(state.name) if hasattr(self._tools, "get") else None
            state.tracker = prepare_file_edit_tracker(
                call_id=state.call_id or state.key,
                tool_name=state.name,
                tool=tool,
                workspace=self._workspace,
                params={"path": state.path},
            )
            if state.tracker is None:
                return

        added, deleted = state.live_diff_counts()
        now = time.monotonic()
        if not state.should_emit(added, deleted, now):  # 节流：不满足条件则不发射
            return
        state.mark_emitted(added, deleted, now)
        await self._emit([build_file_edit_live_event(
            state.tracker,
            added=added,
            deleted=deleted,
        )])

    async def _update_apply_patch(self, state: _StreamingFileEditState) -> None:
        """处理 apply_patch 的流式增量，按每条 edit 解析路径与行差。"""
        if _json_bool_true(state.arguments, "dry_run"):
            return  # 干跑不写入，跳过
        tool = self._tools.get("apply_patch") if hasattr(self._tools, "get") else None
        events: list[dict[str, Any]] = []
        now = time.monotonic()

        # 用正则扫描所有 "path":"..." 出现位置，按段切分每条 edit
        path_matches = list(re.finditer(r'"path"\s*:\s*"([^"]+)"', state.arguments))
        if not path_matches:
            return

        for i, m in enumerate(path_matches):
            raw_path = m.group(1)
            path = _resolve_raw_file_edit_path(tool, self._workspace, raw_path)
            if path is None:
                continue

            # 当前 edit 段落范围：从本 path 到下一个 path（或串尾）
            segment_start = m.start()
            segment_end = path_matches[i + 1].start() if i + 1 < len(path_matches) else len(state.arguments)
            segment = state.arguments[segment_start:segment_end]

            action_match = re.search(r'"action"\s*:\s*"(replace|add)"', segment)
            action = action_match.group(1) if action_match else "replace"

            old_text = _extract_json_string_prefix(segment, "old_text") or ""
            new_text = _extract_json_string_prefix(segment, "new_text") or ""

            added = _text_line_count(new_text) if action in ("replace", "add") else 0
            deleted = _text_line_count(old_text) if action == "replace" else 0

            file_state = state.patch_files.get(raw_path)
            if file_state is None:
                # 首次见到该路径：构造 tracker 并记录编辑前快照
                tracker = FileEditTracker(
                    call_id=state.call_id or state.key,
                    tool="apply_patch",
                    path=path,
                    display_path=display_file_edit_path(path, self._workspace),
                    before=read_file_snapshot(path),
                )
                file_state = _StreamingPatchFileState(tracker=tracker)
                state.patch_files[raw_path] = file_state
            if not file_state.should_emit(added, deleted, now):  # 节流
                continue
            file_state.mark_emitted(added, deleted, now)
            events.append(build_file_edit_live_event(
                file_state.tracker,
                added=added,
                deleted=deleted,
            ))
        if events:
            await self._emit(events)

    async def flush(self) -> None:
        """流结束后补发尚未同步的最终增量事件。"""
        events: list[dict[str, Any]] = []
        now = time.monotonic()
        for state in self._states.values():
            # 先处理 apply_patch 的各文件状态
            for file_state in state.patch_files.values():
                added, deleted = file_state.last_added, file_state.last_deleted
                if not file_state.emitted_once:
                    continue
                if (
                    file_state.last_emitted_added == added
                    and file_state.last_emitted_deleted == deleted
                ):
                    continue  # 与上次发射一致，跳过
                file_state.mark_emitted(added, deleted, now)
                events.append(build_file_edit_live_event(
                    file_state.tracker,
                    added=added,
                    deleted=deleted,
                ))
            if state.tracker is None:
                continue
            # 再处理 write_file / edit_file 的单文件状态
            added, deleted = state.live_diff_counts()
            if (
                state.last_emitted_added == added
                and state.last_emitted_deleted == deleted
                and state.emitted_once
            ):
                continue
            state.mark_emitted(added, deleted, now)
            events.append(build_file_edit_live_event(
                state.tracker,
                added=added,
                deleted=deleted,
            ))
        if events:
            await self._emit(events)

    def apply_final_call_ids(self, final_tool_calls: list[Any]) -> None:
        """把最终 start/end 事件与早期流式占位事件通过相同 call_id 关联。"""
        used_canonicals: set[str] = set()
        for tool_call in final_tool_calls:
            canonical = self.canonical_call_id_for(tool_call)
            if canonical and canonical not in used_canonicals:
                try:
                    tool_call.id = canonical  # 复用流式阶段生成的 call_id
                    used_canonicals.add(canonical)
                except (AttributeError, TypeError):
                    pass

    def canonical_call_id_for(self, tool_call: Any) -> str | None:
        """返回与最终工具调用匹配的规范化 call_id。"""
        for state in self._states.values():
            if state.matches_final_tool_call(tool_call):
                return state.call_id or (state.tracker.call_id if state.tracker else None) or state.key
        return None

    async def error_unmatched(
        self,
        final_tool_calls: list[Any],
        error: str,
    ) -> None:
        """当没有最终工具调用会执行时，把已流式跟踪的编辑标记为失败。"""
        events: list[dict[str, Any]] = []
        for state in self._states.values():
            # apply_patch：逐文件标记失败
            for file_state in state.patch_files.values():
                if any(state.matches_final_tool_call(tool_call) for tool_call in final_tool_calls):
                    continue
                events.append(build_file_edit_error_event(file_state.tracker, error))
            if state.tracker is None:
                continue
            # write_file / edit_file：标记单文件失败
            if any(state.matches_final_tool_call(tool_call) for tool_call in final_tool_calls):
                continue
            events.append(build_file_edit_error_event(state.tracker, error))
        if events:
            await self._emit(events)


@dataclass(slots=True)
class _StreamingJsonStringField:
    """流式扫描 JSON 中某个字符串字段，逐字符统计行数。

    用于在不完整 JSON 上增量计算 content / old_text / new_text 的
    行数，避免等待完整 JSON 解析。
    """

    key: str
    scan_pos: int | None = None  # 下次扫描起点
    closed: bool = False  # 字符串是否已闭合
    escape: bool = False  # 上一字符是否为转义符
    unicode_remaining: int = 0  # \uXXXX 剩余待读字符数
    unicode_buffer: str = ""
    newline_count: int = 0
    has_chars: bool = False
    last_char_newline: bool = False
    last_char_cr: bool = False

    @property
    def line_count(self) -> int:
        """根据已扫描字符估算行数。"""
        if not self.has_chars:
            return 0
        return self.newline_count + (0 if self.last_char_newline else 1)

    def reset(self) -> None:
        """重置扫描状态，供参数整体覆盖时重新扫描。"""
        self.scan_pos = None
        self.closed = False
        self.escape = False
        self.unicode_remaining = 0
        self.unicode_buffer = ""
        self.newline_count = 0
        self.has_chars = False
        self.last_char_newline = False
        self.last_char_cr = False

    def scan(self, source: str) -> None:
        """从上次位置继续扫描 source，更新行数统计。"""
        if self.closed:
            return
        if self.scan_pos is None:
            # 首次扫描：定位字段起始的引号位置
            match = re.search(rf'"{re.escape(self.key)}"\s*:\s*"', source)
            if match is None:
                return
            self.scan_pos = match.end()
        i = self.scan_pos
        while i < len(source):
            ch = source[i]
            if self.unicode_remaining > 0:  # 处理 \u 转义的剩余字符
                self.unicode_buffer += ch
                self.unicode_remaining -= 1
                if self.unicode_remaining == 0:
                    try:
                        decoded = chr(int(self.unicode_buffer, 16))
                    except ValueError:
                        decoded = "x"
                    self.unicode_buffer = ""
                    self._mark_char(decoded)
                i += 1
                continue
            if self.escape:  # 处理转义字符
                self.escape = False
                if ch == "u":
                    self.unicode_remaining = 4
                    self.unicode_buffer = ""
                elif ch == "n":
                    self._mark_char("\n")
                elif ch == "r":
                    self._mark_char("\r")
                else:
                    self._mark_char(ch)
                i += 1
                continue
            if ch == "\\":  # 进入转义
                self.escape = True
                i += 1
                continue
            if ch == '"':  # 字符串闭合
                self.closed = True
                i += 1
                break
            self._mark_char(ch)
            i += 1
        self.scan_pos = i

    def _mark_char(self, ch: str) -> None:
        """记录一个字符并更新换行统计（处理 CR / LF / CRLF）。"""
        self.has_chars = True
        if ch == "\r":
            self.newline_count += 1
            self.last_char_newline = True
            self.last_char_cr = True
        elif ch == "\n":
            if not self.last_char_cr:  # CRLF 不重复计数
                self.newline_count += 1
            self.last_char_newline = True
            self.last_char_cr = False
        else:
            self.last_char_newline = False
            self.last_char_cr = False


@dataclass(slots=True)
class _StreamingPatchFileState:
    """apply_patch 中单个文件的流式发射状态与节流记录。"""

    tracker: FileEditTracker
    emitted_once: bool = False
    last_emitted_added: int = -1
    last_emitted_deleted: int = -1
    last_emit_at: float = 0.0
    last_added: int = 0
    last_deleted: int = 0

    def should_emit(self, added: int, deleted: int, now: float) -> bool:
        """根据是否首次、变化量与时间间隔决定是否发射。"""
        self.last_added = added
        self.last_deleted = deleted
        if not self.emitted_once:
            return True  # 首次必发
        if added == self.last_emitted_added and deleted == self.last_emitted_deleted:
            return False  # 与上次相同则不发
        if max(
            abs(added - self.last_emitted_added),
            abs(deleted - self.last_emitted_deleted),
        ) >= _LIVE_EMIT_LINE_STEP:
            return True  # 行数变化超阈值立即发
        return now - self.last_emit_at >= _LIVE_EMIT_INTERVAL_S  # 否则按时间节流

    def mark_emitted(self, added: int, deleted: int, now: float) -> None:
        """记录本次发射的行差与时间。"""
        self.emitted_once = True
        self.last_added = added
        self.last_deleted = deleted
        self.last_emitted_added = added
        self.last_emitted_deleted = deleted
        self.last_emit_at = now


@dataclass(slots=True)
class _StreamingFileEditState:
    """单次流式工具调用的聚合状态，含路径、tracker 与各字段扫描器。"""

    key: str
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    path: str | None = None
    tracker: FileEditTracker | None = None
    content: _StreamingJsonStringField = field(
        default_factory=lambda: _StreamingJsonStringField("content")  # write_file 的内容字段
    )
    old_text: _StreamingJsonStringField = field(
        default_factory=lambda: _StreamingJsonStringField("old_text")  # edit_file 的旧文本字段
    )
    new_text: _StreamingJsonStringField = field(
        default_factory=lambda: _StreamingJsonStringField("new_text")  # edit_file 的新文本字段
    )
    patch_files: dict[str, _StreamingPatchFileState] = field(default_factory=dict)
    emitted_once: bool = False
    last_emitted_added: int = -1
    last_emitted_deleted: int = -1
    last_emit_at: float = 0.0
    pending_emitted: bool = False
    last_pending_added: int = -1
    last_pending_deleted: int = -1
    last_pending_at: float = 0.0

    def apply_delta(self, payload: dict[str, Any]) -> None:
        """应用一条流式增量，更新 call_id / name / arguments。"""
        call_id = payload.get("call_id")
        if isinstance(call_id, str) and call_id:
            self.call_id = call_id
        name = payload.get("name")
        if isinstance(name, str) and name:
            self.name = name
        args = payload.get("arguments")
        if isinstance(args, str):
            # 完整参数覆盖：重置所有扫描器
            self.arguments = args
            self.content.reset()
            self.old_text.reset()
            self.new_text.reset()
            self.patch_files.clear()
            return
        delta = payload.get("arguments_delta")
        if isinstance(delta, str) and delta:
            self.arguments += delta  # 增量拼接

    def live_diff_counts(self) -> tuple[int, int]:
        """根据工具类型扫描对应字段，返回 (新增行数, 删除行数)。"""
        if self.name == "write_file":
            self.content.scan(self.arguments)
            return self.content.line_count, 0
        if self.name == "edit_file":
            self.old_text.scan(self.arguments)
            self.new_text.scan(self.arguments)
            return self.new_text.line_count, self.old_text.line_count
        return 0, 0

    def should_emit(self, added: int, deleted: int, now: float) -> bool:
        """判断是否应发射 live 事件（首次/变化量/时间节流）。"""
        if not self.emitted_once:
            return True
        if added == self.last_emitted_added and deleted == self.last_emitted_deleted:
            return False
        if max(
            abs(added - self.last_emitted_added),
            abs(deleted - self.last_emitted_deleted),
        ) >= _LIVE_EMIT_LINE_STEP:
            return True
        return now - self.last_emit_at >= _LIVE_EMIT_INTERVAL_S

    def mark_emitted(self, added: int, deleted: int, now: float) -> None:
        """记录 live 事件的发射状态。"""
        self.emitted_once = True
        self.last_emitted_added = added
        self.last_emitted_deleted = deleted
        self.last_emit_at = now

    def should_emit_pending(self, added: int, deleted: int, now: float) -> bool:
        """判断是否应发射 pending 事件（与 should_emit 同策略但独立计数）。"""
        if not self.pending_emitted:
            return True
        if added == self.last_pending_added and deleted == self.last_pending_deleted:
            return False
        if max(
            abs(added - self.last_pending_added),
            abs(deleted - self.last_pending_deleted),
        ) >= _LIVE_EMIT_LINE_STEP:
            return True
        return now - self.last_pending_at >= _LIVE_EMIT_INTERVAL_S

    def mark_pending_emitted(self, added: int, deleted: int, now: float) -> None:
        """记录 pending 事件的发射状态。"""
        self.pending_emitted = True
        self.last_pending_added = added
        self.last_pending_deleted = deleted
        self.last_pending_at = now

    def matches_final_tool_call(self, tool_call: Any) -> bool:
        """判断最终工具调用是否对应本流式状态（按 call_id / name / path 匹配）。"""
        call_id = getattr(tool_call, "id", None)
        canonical = self.call_id or (self.tracker.call_id if self.tracker else "")
        if isinstance(call_id, str) and call_id and canonical and call_id == canonical:
            return True  # call_id 完全匹配
        name = getattr(tool_call, "name", None)
        if name != self.name:
            return False
        if self.name == "apply_patch":
            arguments = getattr(tool_call, "arguments", None)
            if not isinstance(arguments, dict):
                return False
            edits = arguments.get("edits")
            if not isinstance(edits, list):
                return False
            return '"edits"' in self.arguments  # 流式串中含 edits 即视为匹配
        arguments = getattr(tool_call, "arguments", None)
        if not isinstance(arguments, dict):
            return False
        path = arguments.get("path")
        if self.path is None and isinstance(path, str) and path:
            # 流式阶段未取到 path，此处补全并视为匹配
            self.path = path
            return True
        return isinstance(path, str) and path == self.path


def _stream_key(payload: dict[str, Any]) -> str:
    """从增量 payload 中提取稳定 key（优先 index，其次 call_id）。"""
    index = payload.get("index")
    if isinstance(index, int):
        return f"idx:{index}"
    if isinstance(index, str) and index:
        return f"idx:{index}"
    call_id = payload.get("call_id")
    if isinstance(call_id, str) and call_id:
        return f"id:{call_id}"
    return ""


def _json_bool_true(source: str, key: str) -> bool:
    """在不完整 JSON 中检测某布尔字段是否为 true。"""
    return re.search(rf'"{re.escape(key)}"\s*:\s*true\b', source) is not None


def _extract_json_string_prefix(source: str, key: str) -> str | None:
    """提取 JSON 中某字符串字段的前缀内容（字段未闭合也返回已读部分）。"""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"', source)
    if match is None:
        return None
    out: list[str] = []
    i = match.end()
    escape = False
    while i < len(source):
        ch = source[i]
        if escape:  # 处理转义字符
            escape = False
            if ch == "n":
                out.append("\n")
            elif ch == "r":
                out.append("\r")
            elif ch == "t":
                out.append("\t")
            elif ch == "u":
                digits = source[i + 1:i + 5]
                if len(digits) < 4:
                    break  # \u 不完整，停止
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    break
                i += 4
            else:
                out.append(ch)
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue
        if ch == '"':
            return "".join(out)  # 字段闭合
        out.append(ch)
        i += 1
    return "".join(out)


def _extract_complete_json_string(source: str, key: str) -> str | None:
    """提取 JSON 中已完整闭合的字符串字段，未闭合返回 None。"""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"', source)
    if match is None:
        return None
    out: list[str] = []
    i = match.end()
    escape = False
    while i < len(source):
        ch = source[i]
        if escape:  # 处理转义字符
            escape = False
            if ch == "n":
                out.append("\n")
            elif ch == "r":
                out.append("\r")
            elif ch == "t":
                out.append("\t")
            elif ch == "u":
                digits = source[i + 1:i + 5]
                if len(digits) < 4:
                    return None  # \u 不完整视为未闭合
                try:
                    out.append(chr(int(digits, 16)))
                except ValueError:
                    return None
                i += 4
            else:
                out.append(ch)
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue
        if ch == '"':
            return "".join(out)  # 字段闭合才返回
        out.append(ch)
        i += 1
    return None  # 未闭合


def _event_payload(
    tracker: FileEditTracker,
    *,
    phase: str,
    status: str,
    added: int,
    deleted: int,
    approximate: bool,
    binary: bool = False,
    operation: str | None = None,
) -> dict[str, Any]:
    """构造统一的文件编辑事件负载字典。"""
    payload: dict[str, Any] = {
        "version": 1,
        "call_id": tracker.call_id,
        "tool": tracker.tool,
        "path": tracker.display_path,
        "absolute_path": tracker.path.as_posix(),
        "phase": phase,
        "added": max(0, int(added)),  # 钳制为非负
        "deleted": max(0, int(deleted)),
        "approximate": bool(approximate),
        "status": status,
    }
    if binary:
        payload["binary"] = True
    if operation:
        payload["operation"] = operation
    return payload


def _predict_after_text(
    tool_name: str,
    params: dict[str, Any],
    before: FileSnapshot,
) -> str | None:
    """根据工具类型与参数预测编辑后的文本，用于估算行差。"""
    if not before.countable:
        return None
    before_text = before.text or ""
    if tool_name == "write_file":
        content = params.get("content")
        return content if isinstance(content, str) else ""
    if tool_name == "edit_file":
        old_text = params.get("old_text")
        new_text = params.get("new_text")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            return None
        replace_all = bool(params.get("replace_all"))
        if old_text == "":
            return new_text if not before.exists else before_text  # 空匹配：新增或保持
        if old_text in before_text:
            if replace_all:
                return before_text.replace(old_text, new_text)  # 全部替换
            return before_text.replace(old_text, new_text, 1)  # 仅替换首个
        return None  # 匹配失败，无法预测
    return None
