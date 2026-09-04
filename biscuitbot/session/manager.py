"""对话历史的会话管理。

所属模块与项目作用
===================
本文件位于 biscuitbot/session 目录，提供会话（Session）与会话管理器（SessionManager）
的核心实现。
在项目架构中起到的作用：以 JSONL 文件持久化对话历史，支持会话的加载、保存、归档、
分叉与列表，同时负责历史回放时的清洗、token 预算裁剪、媒体附件占位等处理，是智能体
跨 turn 维持上下文的基础设施。
"""

import json  # JSONL 文件读写
import os  # 文件描述符级 fsync 持久化
import re  # 正则清洗历史文本
import shutil  # 遗留会话文件迁移
from contextlib import suppress  # 忽略特定异常
from copy import deepcopy  # 深拷贝用于会话分叉
from dataclasses import dataclass, field  # Session 数据类定义
from datetime import datetime  # 时间戳处理
from pathlib import Path  # 跨平台路径
from typing import Any  # 任意类型标注

from loguru import logger  # 日志输出

from biscuitbot.config.paths import get_legacy_sessions_dir  # 遗留会话目录
from biscuitbot.utils.helpers import (  # 通用辅助函数
    anchor_to_first_user_turn,
    ensure_dir,
    estimate_message_tokens,
    find_legal_message_start,
    image_placeholder_text,
    safe_filename,
    strip_think,
)
from biscuitbot.utils.subagent_channel_display import (
    scrub_subagent_announce_body,  # 子智能体公告文本清洗
)

FILE_MAX_MESSAGES = 2000  # 单个会话文件的最大消息数
_MESSAGE_TIME_PREFIX_RE = re.compile(r"^\[Message Time: [^\]]+\]\n?")  # 消息时间戳前缀
_LOCAL_IMAGE_BREADCRUMB_RE = re.compile(r"^\[image: (?:/|~)[^\]]+\]\s*$")  # 本地图片占位面包屑
_TOOL_CALL_ECHO_RE = re.compile(r'^\s*(?:generate_image|message)\([^)]*\)\s*$')  # 工具调用回显行
_SESSION_PREVIEW_MAX_CHARS = 120  # 会话列表预览文本最大字符数
_SESSION_LIST_PREVIEW_MAX_RECORDS = 200  # 会话列表预览扫描的最大记录数
_SESSION_LIST_PREVIEW_MAX_CHARS = 1_000_000  # 会话列表预览扫描的最大字符数
# 会话分叉时需要丢弃的易失性元数据键
_FORK_VOLATILE_METADATA_KEYS = {
    "goal_state",
    "pending_user_turn",
    "runtime_checkpoint",
    "thread_goal",
    "title",
    "title_user_edited",
}


def _sanitize_assistant_replay_text(content: str) -> str:
    """移除模型可能此前复制的内部回放工件。

    这些字符串作为运行时/会话元数据有用，但出现在助手示例中会成为模型重复的示范。
    """
    content = _MESSAGE_TIME_PREFIX_RE.sub("", content, count=1)
    lines = [
        line
        for line in content.splitlines()
        if not _LOCAL_IMAGE_BREADCRUMB_RE.match(line)
        and not _TOOL_CALL_ECHO_RE.match(line)
    ]
    return "\n".join(lines).strip()


def _text_preview(content: Any) -> str:
    """返回用于会话列表的紧凑展示文本。"""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # content 为 block 列表时，提取所有文本 block 拼接
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        text = " ".join(parts)
    else:
        return ""
    text = _sanitize_assistant_replay_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    # 超长预览截断
    if len(text) > _SESSION_PREVIEW_MAX_CHARS:
        text = text[: _SESSION_PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return text


def _message_preview_text(message: dict[str, Any]) -> str:
    """会话列表预览文本；子智能体注入的 blob 会被缩短以便展示。"""
    content: Any = message.get("content")
    if message.get("injected_event") == "subagent_result" and isinstance(content, str):
        content = scrub_subagent_announce_body(content)
    return _text_preview(content)


def _metadata_title(metadata: Any) -> str:
    """从会话元数据中提取标题；用户编辑过的标题原样返回，否则去除 think 标签。"""
    if not isinstance(metadata, dict):
        return ""
    title = metadata.get("title")
    if not isinstance(title, str):
        return ""
    if metadata.get("title_user_edited") is True:
        return title
    return strip_think(title)


@dataclass
class Session:
    """一个对话会话。

    封装单个会话的消息列表、时间戳、元数据与已整合偏移量，提供历史回放、
    清理、保留最近合法后缀与文件容量限制等能力。
    """

    key: str  # 会话键，通常为 channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # 已整合到文件的消息数量（偏移量）

    def __post_init__(self) -> None:
        # 越界偏移量（元数据损坏）会隐藏全部历史，因此重置为 0。
        if (
            isinstance(self.last_consolidated, bool)
            or not isinstance(self.last_consolidated, int)
            or not 0 <= self.last_consolidated <= len(self.messages)
        ):
            self.last_consolidated = 0

    @staticmethod
    def _annotate_message_time(message: dict[str, Any], content: Any) -> Any:
        """向模型暴露持久化的 turn 时间戳，用于相对日期推理。

        为每个助手 turn 标注会通过 in-context 示范训练模型以同样的
        ``[Message Time: ...]`` 前缀开始回复，从而向用户泄露元数据。
        因此仅标注用户 turn。用户侧时间戳足以固定相邻助手回复的相对时间，
        包括用户稍后回复的主动消息。
        """
        timestamp = message.get("timestamp")
        if not timestamp or not isinstance(content, str):
            return content
        role = message.get("role")
        if role != "user":
            return content
        return f"[Message Time: {timestamp}]\n{content}"

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """向会话追加一条消息。"""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(
        self,
        max_messages: int = 120,
        *,
        max_tokens: int = 0,
        include_timestamps: bool = False,
    ) -> list[dict[str, Any]]:
        """返回未整合的消息作为 LLM 输入。

        历史先按消息数（``max_messages``）切片，再按 token 预算从尾部
        （``max_tokens``）裁剪（若提供）。
        """
        unconsolidated = self.messages[self.last_consolidated:]
        max_messages = max_messages if max_messages > 0 else 120
        sliced = unconsolidated[-max_messages:]

        # 尽量避免从 turn 中间开始，除非是用户可能正在回复的主动助手投递。
        sliced = anchor_to_first_user_turn(sliced)

        # 丢弃开头的孤立 tool 结果。
        start = find_legal_message_start(sliced)
        if start:
            sliced = sliced[start:]

        out: list[dict[str, Any]] = []
        for message in sliced:
            if message.get("_command"):
                continue
            content = message.get("content", "")
            role = message.get("role")
            if role == "assistant" and isinstance(content, str):
                content = _sanitize_assistant_replay_text(content)
            # 从持久化的 ``media`` kwarg 合成 ``[image: path]`` 面包屑，
            # 使 LLM 回放时仍能看到图片原来的位置。否则仅含图片的用户 turn
            # 会回放为空用户消息——助手回复看起来像在回应虚无。
            media = message.get("media")
            if role == "user" and isinstance(media, list) and media and isinstance(content, str):
                breadcrumbs = "\n".join(
                    image_placeholder_text(p) for p in media if isinstance(p, str) and p
                )
                content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            cli_apps = message.get("cli_apps")
            if role == "user" and isinstance(cli_apps, list) and cli_apps and isinstance(content, str):
                cli_lines: list[str] = []
                for item in cli_apps[:8]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip().lower()
                    if not name:
                        continue
                    entry = str(item.get("entry_point") or "unknown").strip() or "unknown"
                    cli_lines.append(
                        f"[CLI App Attachment: @{name}; tool=run_cli_app; entry_point={entry}; "
                        f"skill=skills/cli-app-{name}/SKILL.md]"
                    )
                if cli_lines:
                    breadcrumbs = "\n".join(cli_lines)
                    content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            mcp_presets = message.get("mcp_presets")
            if (
                role == "user"
                and isinstance(mcp_presets, list)
                and mcp_presets
                and isinstance(content, str)
            ):
                mcp_lines: list[str] = []
                for item in mcp_presets[:8]:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip().lower()
                    if not name:
                        continue
                    transport = str(item.get("transport") or "mcp").strip() or "mcp"
                    mcp_lines.append(
                        f"[MCP Preset Attachment: @{name}; tool_prefix=mcp_{name}_; "
                        f"transport={transport}]"
                    )
                if mcp_lines:
                    breadcrumbs = "\n".join(mcp_lines)
                    content = f"{content}\n{breadcrumbs}" if content else breadcrumbs
            if include_timestamps:
                content = self._annotate_message_time(message, content)
            # 跳过内容为空且无工具调用/推理的助手消息
            if role == "assistant" and isinstance(content, str) and not content.strip():
                if not any(key in message for key in ("tool_calls", "reasoning_content", "thinking_blocks")):
                    continue
            entry: dict[str, Any] = {"role": message["role"], "content": content}
            for key in ("tool_calls", "tool_call_id", "name", "reasoning_content", "thinking_blocks"):
                if key in message:
                    entry[key] = message[key]
            out.append(entry)

        # 按 token 预算从尾部裁剪
        if max_tokens > 0 and out:
            kept: list[dict[str, Any]] = []
            used = 0
            for message in reversed(out):
                tokens = estimate_message_tokens(message)
                if kept and used + tokens > max_tokens:
                    break
                kept.append(message)
                used += tokens
            kept.reverse()

            # 保持历史与首个可见用户 turn 对齐。
            first_user = next((i for i, m in enumerate(kept) if m.get("role") == "user"), None)
            if first_user is not None:
                kept = kept[first_user:]
            else:
                # 紧张的 token 预算可能留下仅含助手的尾部。
                # 若未切片输出中存在用户 turn，即使略微超出预算也恢复最近的一个。
                recovered_user = next(
                    (i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"),
                    None,
                )
                if recovered_user is not None:
                    kept = out[recovered_user:]

            # 在开头保持合法的 tool-call 边界。
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
            out = kept
        return out

    def clear(self) -> None:
        """清空所有消息并将会话重置为初始状态。"""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()
        self.metadata.pop("_last_summary", None)

    def retain_recent_legal_suffix(
        self,
        max_messages: int,
        *,
        extend_to_user: bool = False,
    ) -> tuple[list[dict], int]:
        """保留合法的最近后缀，可选地向前扩展到某个用户 turn。

        返回 ``(dropped, already_consolidated_count)``，其中 *dropped* 是按原始
        顺序移除的消息列表，*already_consolidated_count* 是其中位于既有
        ``last_consolidated`` 前缀内、因此无需原始归档的消息数。
        """
        if max_messages <= 0:
            dropped = list(self.messages)
            lc = self.last_consolidated
            self.clear()
            return dropped, min(lc, len(dropped))
        if len(self.messages) <= max_messages:
            return [], 0

        original = list(self.messages)
        before_lc = self.last_consolidated

        start_idx = max(0, len(self.messages) - max_messages)
        # 可选地向前扩展到用户 turn
        if extend_to_user:
            start_idx = next(
                (i for i in range(start_idx, -1, -1) if self.messages[i].get("role") == "user"),
                start_idx,
            )

        retained = self.messages[start_idx:]

        # 保留窗口内存在用户 turn 时优先从用户 turn 开始。
        first_user = next((i for i, m in enumerate(retained) if m.get("role") == "user"), None)
        if first_user is not None:
            retained = retained[first_user:]
        elif not extend_to_user:
            # 硬截断尾部仅含助手/工具时，锚定到整个会话中最近的用户 turn，
            # 并取一个受限的向前窗口。
            latest_user = next(
                (i for i in range(len(self.messages) - 1, -1, -1)
                 if self.messages[i].get("role") == "user"),
                None,
            )
            if latest_user is not None:
                retained = self.messages[latest_user: latest_user + max_messages]

        # 与 get_history() 保持一致：避免持久化开头的孤立 tool 结果。
        start = find_legal_message_start(retained)
        if start:
            retained = retained[start:]

        # 除非调用方要求扩展到用户 turn，否则硬上限保证。
        if not extend_to_user and len(retained) > max_messages:
            retained = retained[-max_messages:]
            start = find_legal_message_start(retained)
            if start:
                retained = retained[start:]

        # 使用身份比较计算实际丢弃的消息，确保即使 retained 是 original 的
        # 非连续切片（上述 else 分支）也不会重复或丢失消息。
        retained_ids = set(id(m) for m in retained)
        dropped = [m for m in original if id(m) not in retained_ids]

        # 统计丢弃消息中有多少位于原始列表的已整合前缀内。
        # 不能简单用 min()，因为 dropped 可能包含来自已整合前缀之后的消息
        # （例如 else 分支）。
        already_consolidated = sum(
            1 for i, m in enumerate(original)
            if i < before_lc and id(m) not in retained_ids
        )

        # 新 last_consolidated = retained 中位于旧已整合前缀内的消息数。
        new_lc = sum(
            1 for i, m in enumerate(original)
            if i < before_lc and id(m) in retained_ids
        )

        self.messages = retained
        self.last_consolidated = new_lc
        self.updated_at = datetime.now()
        return dropped, already_consolidated

    def enforce_file_cap(
        self,
        on_archive: Any = None,
        limit: int = FILE_MAX_MESSAGES,
    ) -> None:
        """通过归档并裁剪旧前缀来限制会话消息增长。"""
        if limit <= 0 or len(self.messages) <= limit:
            return

        dropped, already_consolidated = self.retain_recent_legal_suffix(limit)
        if not dropped:
            return

        # 仅归档未整合部分
        archive_chunk = dropped[already_consolidated:]
        if archive_chunk and on_archive:
            on_archive(archive_chunk)
        logger.info(
            "Session file cap hit for {}: dropped {}, raw-archived {}, kept {}",
            self.key,
            len(dropped),
            len(archive_chunk),
            len(self.messages),
        )


class SessionManager:
    """会话管理器。

    会话以 JSONL 文件形式存储在 sessions 目录中，支持加载、保存、归档、
    分叉、列表及损坏修复等操作。
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")  # 会话存储目录
        self.legacy_sessions_dir = get_legacy_sessions_dir()  # 遗留全局会话目录
        self._cache: dict[str, Session] = {}  # 内存中的会话缓存

    @staticmethod
    def safe_key(key: str) -> str:
        """公共辅助：将任意 key 映射为稳定的文件名 stem，供 HTTP 处理器使用。

        safe_filename 对纯符号 key 会收敛为空串，返回空 stem 会让会话文件退化成
        隐藏的 ``.jsonl`` 且此类 key 互相碰撞；回退为 ``"session"`` 保持可见。
        """
        return safe_filename(key.replace(":", "_")) or "session"

    def _get_session_path(self, key: str) -> Path:
        """获取会话对应的文件路径。"""
        return self.sessions_dir / f"{self.safe_key(key)}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """遗留全局会话路径（~/.biscuitbot/sessions/）。"""
        return self.legacy_sessions_dir / f"{self.safe_key(key)}.jsonl"

    def get_or_create(self, key: str) -> Session:
        """获取已存在的会话，不存在则创建新会话。

        Args:
            key: Session key (usually channel:chat_id).

        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Session | None:
        """从磁盘加载会话。"""
        path = self._get_session_path(key)
        # 当前路径不存在时尝试从遗留路径迁移
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session {} from legacy path", key)
                except Exception:
                    logger.exception("Failed to migrate session {}", key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            updated_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    # 首行为元数据记录
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        updated_at = datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session {}: {}", key, e)
            # 加载失败时尝试修复
            repaired = self._repair(key)
            if repaired is not None:
                logger.info("Recovered session {} from corrupt file ({} messages)", key, len(repaired.messages))
            return repaired

    def _repair(self, key: str) -> Session | None:
        """尝试从损坏的 JSONL 文件中恢复会话。"""
        path = self._get_session_path(key)
        if not path.exists():
            return None

        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: datetime | None = None
            updated_at: datetime | None = None
            last_consolidated = 0
            skipped = 0  # 跳过的损坏行数

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        # 跳过无法解析的行
                        skipped += 1
                        continue

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        if data.get("created_at"):
                            with suppress(ValueError, TypeError):
                                created_at = datetime.fromisoformat(data["created_at"])
                        if data.get("updated_at"):
                            with suppress(ValueError, TypeError):
                                updated_at = datetime.fromisoformat(data["updated_at"])
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            if skipped:
                logger.warning("Skipped {} corrupt lines in session {}", skipped, key)

            if not messages and not metadata:
                return None

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Repair failed for session {}: {}", key, e)
            return None

    @staticmethod
    def _session_payload(session: Session) -> dict[str, Any]:
        """将会话转换为可序列化的字典负载。"""
        return {
            "key": session.key,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "metadata": session.metadata,
            "messages": session.messages,
        }

    def save(self, session: Session, *, fsync: bool = False) -> None:
        """原子地将会话保存到磁盘。

        当 *fsync* 为 ``True`` 时，最终文件及其父目录会显式刷新到持久存储。
        默认关闭（常规操作下 OS 页缓存足够），但在优雅停机时应启用，以免使用
        写回缓存（如 rclone VFS、NFS、FUSE 挂载）的文件系统丢失最近写入。
        """
        path = self._get_session_path(session.key)
        tmp_path = path.with_suffix(".jsonl.tmp")

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                # 首行写入元数据记录
                metadata_line = {
                    "_type": "metadata",
                    "key": session.key,
                    "created_at": session.created_at.isoformat(),
                    "updated_at": session.updated_at.isoformat(),
                    "metadata": session.metadata,
                    "last_consolidated": session.last_consolidated
                }
                f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
                for msg in session.messages:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())

            # 原子替换
            os.replace(tmp_path, path)

            if fsync:
                # fsync 目录使重命名持久化。
                # Windows 上以 O_RDONLY 打开目录会抛 PermissionError——
                # 跳过目录同步（NTFS 同步记录元数据）。
                with suppress(PermissionError):
                    fd = os.open(str(path.parent), os.O_RDONLY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        except BaseException:
            # 出错时清理临时文件
            tmp_path.unlink(missing_ok=True)
            raise

        self._cache[session.key] = session

    def flush_all(self) -> int:
        """以 fsync 重新保存所有缓存会话，用于持久化停机。

        返回已刷新会话数。单个会话出错会记录日志但不会阻止其他会话刷新。
        """
        flushed = 0
        for key, session in list(self._cache.items()):
            try:
                self.save(session, fsync=True)
                flushed += 1
            except Exception:
                logger.warning("Failed to flush session {}", key, exc_info=True)
        return flushed

    def invalidate(self, key: str) -> None:
        """从内存缓存中移除会话。"""
        self._cache.pop(key, None)

    def delete_session(self, key: str) -> bool:
        """从磁盘与内存缓存中移除会话。

        找到并删除 JSONL 文件时返回 True。
        """
        path = self._get_session_path(key)
        self.invalidate(key)
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.warning("Failed to delete session file {}: {}", path, e)
            return False

    def fork_session_before_user_index(
        self,
        source_key: str,
        target_key: str,
        before_user_index: int,
    ) -> Session | None:
        """在全局用户消息索引之前从 *source_key* 创建 *target_key*。

        ``before_user_index`` 基于完整会话中的用户消息从零开始计数：
        ``0`` 表示"在第一条用户消息之前"，``1`` 表示"在第二条用户消息之前"，
        以此类推。值等于用户消息总数时复制完整会话前缀。WebUI 助手回复分叉
        传入下一个用户索引，使选中的已完成助手 turn 被包含。
        """
        if before_user_index < 0:
            return None
        source = self._cache.get(source_key) or self._load(source_key)
        if source is None:
            return None

        copied: list[dict[str, Any]] = []
        user_index = 0
        found_target = False
        # 遍历到目标用户消息索引，复制之前的所有消息
        for message in source.messages:
            if message.get("role") == "user":
                if user_index == before_user_index:
                    found_target = True
                    break
                user_index += 1
            copied.append(deepcopy(message))
        if user_index == before_user_index:
            found_target = True
        if not found_target:
            return None

        # 复制元数据并丢弃易失性键
        metadata = deepcopy(source.metadata)
        for key in _FORK_VOLATILE_METADATA_KEYS:
            metadata.pop(key, None)

        last_consolidated = min(source.last_consolidated, len(copied))
        # 若源已整合前缀超出复制范围，清除摘要并重置偏移
        if source.last_consolidated > len(copied):
            metadata.pop("_last_summary", None)
            last_consolidated = 0

        now = datetime.now()
        target = Session(
            key=target_key,
            messages=copied,
            created_at=now,
            updated_at=now,
            metadata=metadata,
            last_consolidated=last_consolidated,
        )
        self.save(target, fsync=True)
        return target

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        """从磁盘加载会话但不缓存；用于只读 HTTP 端点。

        返回 ``{"key", "created_at", "updated_at", "metadata", "messages"}``，
        会话文件不存在或解析失败时返回 ``None``。
        """
        path = self._get_session_path(key)
        if not path.exists():
            return None
        try:
            messages: list[dict[str, Any]] = []
            metadata: dict[str, Any] = {}
            created_at: str | None = None
            updated_at: str | None = None
            stored_key: str | None = None
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = data.get("created_at")
                        updated_at = data.get("updated_at")
                        stored_key = data.get("key")
                    else:
                        messages.append(data)
            return {
                "key": stored_key or key,
                "created_at": created_at,
                "updated_at": updated_at,
                "metadata": metadata,
                "messages": messages,
            }
        except Exception as e:
            logger.warning("Failed to read session {}: {}", key, e)
            # 解析失败时尝试修复
            repaired = self._repair(key)
            if repaired is not None:
                logger.info("Recovered read-only session view {} from corrupt file", key)
                return self._session_payload(repaired)
            return None

    def read_session_metadata(self, key: str) -> dict[str, Any] | None:
        """仅从会话文件加载元数据记录。

        供只需要会话级元数据而不需要完整对话记录的 WebUI 路由使用。
        """
        path = self._get_session_path(key)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("_type") != "metadata":
                        return None
                    metadata = data.get("metadata", {})
                    return {
                        "key": data.get("key") or key,
                        "created_at": data.get("created_at"),
                        "updated_at": data.get("updated_at"),
                        "metadata": metadata if isinstance(metadata, dict) else {},
                    }
            return None
        except Exception as e:
            logger.warning("Failed to read session metadata {}: {}", key, e)
            repaired = self._repair(key)
            if repaired is not None:
                logger.info("Recovered read-only session metadata {} from corrupt file", key)
                return {
                    "key": repaired.key,
                    "created_at": repaired.created_at.isoformat(),
                    "updated_at": repaired.updated_at.isoformat(),
                    "metadata": repaired.metadata,
                }
            return None

    def list_sessions(self) -> list[dict[str, Any]]:
        """列出所有会话。

        Returns:
            List of session info dicts.
        """
        sessions = []

        for path in self.sessions_dir.glob("*.jsonl"):
            fallback_key = path.stem.replace("_", ":", 1)
            try:
                # 读取元数据行与少量预览用于会话列表。
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            metadata = data.get("metadata", {})
                            title = _metadata_title(metadata)
                            preview = ""
                            fallback_preview = ""
                            scanned_records = 0
                            scanned_chars = 0
                            # 扫描消息行生成预览（优先用户消息，回退助手消息）
                            for line in f:
                                if not line.strip():
                                    continue
                                scanned_records += 1
                                scanned_chars += len(line)
                                # 超过扫描上限则停止
                                if (
                                    scanned_records > _SESSION_LIST_PREVIEW_MAX_RECORDS
                                    or scanned_chars > _SESSION_LIST_PREVIEW_MAX_CHARS
                                ):
                                    break
                                item = json.loads(line)
                                if item.get("_type") == "metadata":
                                    continue
                                text = _message_preview_text(item)
                                if not text:
                                    continue
                                if item.get("role") == "user":
                                    preview = text
                                    break
                                if not fallback_preview and item.get("role") == "assistant":
                                    fallback_preview = text
                            preview = preview or fallback_preview
                            sessions.append(
                                {
                                    "key": key,
                                    "created_at": data.get("created_at"),
                                    "updated_at": data.get("updated_at"),
                                    "title": title,
                                    "preview": preview,
                                    "path": str(path),
                                }
                            )
            except Exception:
                # 解析失败时尝试修复并补充会话条目
                repaired = self._repair(fallback_key)
                if repaired is not None:
                    sessions.append(
                        {
                            "key": repaired.key,
                            "created_at": repaired.created_at.isoformat(),
                            "updated_at": repaired.updated_at.isoformat(),
                            "title": _metadata_title(repaired.metadata),
                            "preview": next(
                                (
                                    text
                                    for msg in repaired.messages
                                    if (text := _message_preview_text(msg))
                                ),
                                "",
                            ),
                            "path": str(path),
                        }
                    )
                continue
        # 按 updated_at 倒序排序
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
