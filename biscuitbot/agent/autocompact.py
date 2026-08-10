"""自动压缩：对空闲会话进行主动压缩归档，以降低 token 开销与延迟。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块中负责会话自动压缩的组件。
在项目架构中起到的作用：
- 监控会话的空闲时长，当会话超过 TTL 仍未活跃时，主动调用 Consolidator
  将其压缩为摘要，避免历史过长导致 LLM 调用成本与延迟上升；
- 在用户下次唤醒该会话时，将压缩摘要作为上下文摘要注入，保证对话连续性；
- 区分热路径（进程未重启，内存中存有摘要）与冷路径（进程重启，从会话元数据
  读取摘要），在保证正确性的同时尽量降低读取开销。
"""

from __future__ import annotations

from collections.abc import Collection  # 仅用于类型注解：集合型入参
from datetime import datetime  # 用于会话时间戳的解析与过期判断
from typing import TYPE_CHECKING, Callable, Coroutine  # 类型注解支持

from loguru import logger  # 日志记录，用于压缩过程中的异常与状态输出

from biscuitbot.session.manager import Session, SessionManager  # 会话管理器与 Session 对象

if TYPE_CHECKING:
    from biscuitbot.agent.memory import Consolidator  # 仅类型检查时导入，避免运行时循环依赖


class AutoCompact:
    """空闲会话自动压缩器。

    职责与项目角色：
    - 周期性扫描所有会话，识别超过 TTL 仍处于空闲状态的会话；
    - 调用 ``Consolidator.compact_idle_session`` 将旧消息压缩为摘要并持久化；
    - 在会话被重新激活时，返回对应的摘要供上下文构建器注入。

    典型用法：由 AgentLoop/调度器在每次轮询时调用 ``check_expired``，
    并在准备会话时调用 ``prepare_session`` 获取摘要。
    """

    _RECENT_SUFFIX_MESSAGES = 8  # 压缩时保留的最近消息条数（不参与压缩的尾部窗口）
    _INTERNAL_SESSION_PREFIXES = ("dream:",)  # 内部会话前缀，这些会话不参与自动压缩

    def __init__(self, sessions: SessionManager, consolidator: Consolidator,
                 session_ttl_minutes: int = 0):
        self.sessions = sessions  # 会话管理器，用于列出/获取/创建会话
        self.consolidator = consolidator  # 压缩器，执行实际的摘要生成
        self._ttl = session_ttl_minutes  # 会话空闲过期阈值（分钟），<=0 表示禁用
        self._archiving: set[str] = set()  # 正在归档中的会话 key 集合，避免重复归档
        self._summaries: dict[str, tuple[str, datetime]] = {}  # 内存中的摘要缓存：key -> (摘要文本, 最后活跃时间)

    def _is_expired(self, ts: datetime | str | None,
                    now: datetime | None = None) -> bool:
        """判断给定时间戳是否已超过 TTL。

        参数:
            ts: 会话最后更新时间，可为 datetime、ISO 字符串或 None；
            now: 当前时间，未提供则取 ``datetime.now()``。

        返回:
            若 TTL 未启用或 ts 为空返回 False；否则返回是否超过阈值。
        """
        if self._ttl <= 0 or not ts:
            return False
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)  # 字符串形式的时间戳需先解析
        return ((now or datetime.now()) - ts).total_seconds() >= self._ttl * 60

    @staticmethod
    def _format_summary(text: str, last_active: datetime) -> str:
        """将摘要文本格式化为可注入提示的字符串。

        参数:
            text: 压缩后的会话摘要文本；
            last_active: 会话最后活跃时间。

        返回:
            带有“上次活跃时间”前缀的摘要字符串。
        """
        return f"Previous conversation summary (last active {last_active.isoformat()}):\n{text}"

    @classmethod
    def _is_internal_session(cls, key: str) -> bool:
        """判断会话 key 是否属于内部会话（如 dream: 前缀），内部会话不参与压缩。"""
        return key.startswith(cls._INTERNAL_SESSION_PREFIXES)

    def check_expired(self, schedule_background: Callable[[Coroutine], None],
                      active_session_keys: Collection[str] = ()) -> None:
        """扫描所有会话，为已过期且空闲的会话调度后台归档任务。

        参数:
            schedule_background: 用于将归档协程投递到后台执行的回调；
            active_session_keys: 当前正在处理任务的会话 key 集合，这些会话将被跳过，
                避免与进行中的 Agent 任务冲突。
        """
        now = datetime.now()
        for info in self.sessions.list_sessions():
            key = info.get("key", "")
            # 跳过无 key、内部会话、正在归档中的会话
            if not key or self._is_internal_session(key) or key in self._archiving:
                continue
            # 跳过仍有 Agent 任务在执行的会话
            if key in active_session_keys:
                continue
            if self._is_expired(info.get("updated_at"), now):
                self._archiving.add(key)  # 标记为归档中，防止重复调度
                schedule_background(self._archive(key))

    async def _archive(self, key: str) -> None:
        """对单个会话执行压缩归档。

        参数:
            key: 目标会话的 key。
        """
        # 内部会话不压缩，清理标记后直接返回
        if self._is_internal_session(key):
            self._archiving.discard(key)
            return
        try:
            summary = await self.consolidator.compact_idle_session(
                key, self._RECENT_SUFFIX_MESSAGES,
            )
            # 仅在生成了有效摘要时缓存（"(nothing)" 视为无内容）
            if summary and summary != "(nothing)":
                session = self.sessions.get_or_create(key)
                meta = session.metadata.get("_last_summary")
                if isinstance(meta, dict):
                    self._summaries[key] = (
                        meta["text"],
                        datetime.fromisoformat(meta["last_active"]),
                    )
        except Exception:
            logger.exception("Auto-compact: failed for {}", key)
        finally:
            self._archiving.discard(key)  # 无论成功与否都清理归档标记

    def prepare_session(self, session: Session, key: str) -> tuple[Session, str | None]:
        """在会话被激活前准备会话对象与其压缩摘要。

        参数:
            session: 当前持有的 Session 对象；
            key: 会话 key。

        返回:
            元组 (会话对象, 摘要字符串或 None)。若会话处于归档中或已过期，
            会重新加载会话；并优先从内存缓存返回摘要，其次从元数据读取。
        """
        # 内部会话：清理相关状态后直接返回，不提供摘要
        if self._is_internal_session(key):
            self._archiving.discard(key)
            self._summaries.pop(key, None)
            return session, None
        # 会话正在归档或已过期：重新从存储加载最新状态
        if key in self._archiving or self._is_expired(session.updated_at):
            logger.info("Auto-compact: reloading session {} (archiving={})", key, key in self._archiving)
            session = self.sessions.get_or_create(key)
        # 热路径：进程未重启，摘要仍驻留在内存字典中。
        entry = self._summaries.pop(key, None)
        if entry:
            return session, self._format_summary(entry[0], entry[1])
        # 冷路径：进程已重启，从会话元数据中读取持久化的摘要。
        meta = session.metadata.get("_last_summary")
        if isinstance(meta, dict):
            return session, self._format_summary(meta["text"], datetime.fromisoformat(meta["last_active"]))
        return session, None
