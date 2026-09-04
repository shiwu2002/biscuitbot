"""跟踪文件读取状态，用于编辑前重读提醒与读取去重。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的文件状态跟踪组件。
在项目架构中起到的作用：记录每个会话中文件的读取与写入状态，支撑
"编辑前需先读取"的提醒机制与"文件未变更则跳过重复读取"的去重机制，
状态按会话隔离，避免跨会话泄漏。
"""

from __future__ import annotations

import hashlib
import os
from contextvars import ContextVar, Token  # 上下文变量与令牌，用于异步任务传递
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ReadState:
    """单次文件读取的状态记录。"""

    mtime: float  # 读取时的文件修改时间
    offset: int  # 读取的起始行偏移
    limit: int | None  # 读取的行数限制
    content_hash: str | None  # 文件内容哈希，用于检测内容变更
    can_dedup: bool  # 是否可去重（写入后不可去重）


def _hash_file(p: str) -> str | None:
    """计算文件内容的 SHA256 哈希，失败时返回 None。"""
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return None


class FileStates:
    """Per-session read/write tracker.

    Owns its own state dict so read-dedup ("File unchanged since last read")
    and read-before-edit warnings stay scoped to one agent session and do
    not leak across sessions sharing this process.

    中文说明：单会话的文件读写状态跟踪器，状态按会话隔离，避免跨会话泄漏。
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        """初始化空的状态字典。"""
        self._state: dict[str, ReadState] = {}

    def record_read(
        self,
        path: str | Path,
        offset: int = 1,
        limit: int | None = None,
        content_hash: str | None = None,
    ) -> None:
        """记录文件已读取（在成功读取后调用）。

        参数:
            path: 文件路径。
            offset: 读取的起始行偏移。
            limit: 读取的行数限制。
            content_hash: 调用方已计算好的内容哈希；缺省时内部重新读盘计算，
                传入可避免一次重复的全文件读取与哈希。
        """
        p = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            return
        self._state[p] = ReadState(
            mtime=mtime,
            offset=offset,
            limit=limit,
            content_hash=content_hash if content_hash is not None else _hash_file(p),
            can_dedup=True,
        )

    def record_write(self, path: str | Path) -> None:
        """记录文件已写入（更新状态中的 mtime）。

        参数:
            path: 文件路径。
        """
        p = str(Path(path).resolve())
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            self._state.pop(p, None)
            return
        self._state[p] = ReadState(
            mtime=mtime,
            offset=1,
            limit=None,
            content_hash=_hash_file(p),
            can_dedup=False,  # 写入后不可去重
        )

    def check_read(self, path: str | Path) -> str | None:
        """检查文件是否已被读取且内容新鲜。

        参数:
            path: 文件路径。

        返回:
            正常返回 None，否则返回警告字符串。
            当 mtime 变化但内容相同时（如 touch、编辑器保存），检查通过以避免误报过期。
        """
        p = str(Path(path).resolve())
        entry = self._state.get(p)
        if entry is None:
            return "Warning: file has not been read yet. Read it first to verify content before editing."
        try:
            current_mtime = os.path.getmtime(p)
        except OSError:
            return None
        if current_mtime != entry.mtime:
            # mtime 变化时比对内容哈希，避免误报
            if entry.content_hash and _hash_file(p) == entry.content_hash:
                entry.mtime = current_mtime
                return None
            return "Warning: file has been modified since last read. Re-read to verify content before editing."
        # mtime 未变 - 仍检查内容哈希以检测快速修改
        if entry.content_hash and _hash_file(p) != entry.content_hash:
            return "Warning: file has been modified since last read. Re-read to verify content before editing."
        return None

    def is_unchanged(self, path: str | Path, offset: int = 1, limit: int | None = None) -> bool:
        """判断文件是否以相同参数读取过且内容未变更。

        参数:
            path: 文件路径。
            offset: 读取的起始行偏移。
            limit: 读取的行数限制。

        返回:
            文件以相同参数读取过且内容未变更返回 True，否则 False。
        """
        p = str(Path(path).resolve())
        entry = self._state.get(p)
        if entry is None:
            return False
        if not entry.can_dedup:
            return False
        if entry.offset != offset or entry.limit != limit:
            return False
        try:
            current_mtime = os.path.getmtime(p)
        except OSError:
            return False
        if current_mtime != entry.mtime:
            # mtime 变化 - 检查内容是否也变化
            current_hash = _hash_file(p)
            if current_hash != entry.content_hash:
                # 内容确实变化 - 不可去重
                entry.can_dedup = False
                return False
            # mtime 变化但内容相同（如 touch）- 标记为不可去重，强制下次完整读取
            entry.can_dedup = False
            return True
        # mtime 未变 - 内容必然相同
        return True

    def get(self, path: str | Path) -> ReadState | None:
        """返回指定路径的原始 ReadState，无记录时返回 None。"""
        return self._state.get(str(Path(path).resolve()))

    def clear(self) -> None:
        """清空所有跟踪状态（用于测试）。"""
        self._state.clear()


class FileStateStore:
    """按会话键查找的文件读写状态存储表。"""

    __slots__ = ("_states_by_key",)

    def __init__(self) -> None:
        """初始化空的状态字典。"""
        self._states_by_key: dict[str, FileStates] = {}

    def for_session(self, session_key: str | None) -> FileStates:
        """获取指定会话的 FileStates，不存在则创建。

        参数:
            session_key: 会话键，为空时使用默认键。

        返回:
            该会话对应的 FileStates 实例。
        """
        key = session_key or "__default__"
        states = self._states_by_key.get(key)
        if states is None:
            states = FileStates()
            self._states_by_key[key] = states
        return states

    def clear(self) -> None:
        """清空所有会话的状态。"""
        self._states_by_key.clear()


# 当前异步任务的文件状态上下文变量，默认为 None
_current_file_states: ContextVar[FileStates | None] = ContextVar(
    "biscuitbot_file_states",
    default=None,
)


def current_file_states(default: FileStates) -> FileStates:
    """返回绑定到当前 agent 任务的 FileStates，无绑定时返回 fallback。

    参数:
        default: 默认的 FileStates 实例。

    返回:
        当前任务绑定的或默认的 FileStates。
    """
    return _current_file_states.get() or default


def bind_file_states(file_states: FileStates) -> Token[FileStates | None]:
    """为当前异步任务绑定文件读写状态。

    参数:
        file_states: 待绑定的 FileStates。

    返回:
        ContextVar 令牌，用于后续重置。
    """
    return _current_file_states.set(file_states)


def reset_file_states(token: Token[FileStates | None]) -> None:
    """重置文件状态到绑定前。

    参数:
        token: bind_file_states 返回的令牌。
    """
    _current_file_states.reset(token)


# 模块级默认实例，保留以兼容直接访问的测试与调用方。
# 按会话使用的调用方应持有自己的 FileStates 实例，而非直接使用此实例。
_default = FileStates()


def record_read(path: str | Path, offset: int = 1, limit: int | None = None) -> None:
    """在默认实例上记录文件读取（兼容旧调用方）。"""
    _default.record_read(path, offset=offset, limit=limit)


def record_write(path: str | Path) -> None:
    """在默认实例上记录文件写入（兼容旧调用方）。"""
    _default.record_write(path)


def check_read(path: str | Path) -> str | None:
    """在默认实例上检查文件读取状态（兼容旧调用方）。"""
    return _default.check_read(path)


def is_unchanged(path: str | Path, offset: int = 1, limit: int | None = None) -> bool:
    """在默认实例上判断文件是否未变更（兼容旧调用方）。"""
    return _default.is_unchanged(path, offset=offset, limit=limit)


def clear() -> None:
    """清空默认实例的状态（兼容旧调用方）。"""
    _default.clear()


# 旧版属性访问支持：供直接访问模块级字典的调用方使用（filesystem.py 曾这样做）。
# 保留为属性式访问器以维持既有导入可用。
def __getattr__(name: str):
    if name == "_state":
        return _default._state
    raise AttributeError(name)
