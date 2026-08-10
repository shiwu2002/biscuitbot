"""长时运行 exec 工作流的会话支持。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的执行会话组件。
在项目架构中起到的作用：为长时运行的命令提供会话管理能力，支持启动
后台进程、向其 stdin 写入数据、轮询输出、等待特定输出出现、关闭 stdin、
终止进程以及列出活跃会话。使得 agent 能与交互式命令（如开发服务器、
测试监听器）进行增量式交互。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.context import current_request_session_key  # 当前请求会话键
from biscuitbot.agent.tools.schema import (  # Schema 构造器
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)

DEFAULT_YIELD_MS = 1000  # 默认轮询等待毫秒数
MAX_YIELD_MS = 30_000  # 最大轮询等待毫秒数
DEFAULT_WAIT_FOR_MS = 10_000  # 默认等待目标输出毫秒数
MAX_WAIT_FOR_MS = 120_000  # 最大等待目标输出毫秒数
DEFAULT_MAX_OUTPUT_CHARS = 10_000  # 默认返回输出字符上限
MAX_OUTPUT_CHARS = 50_000  # 最大返回输出字符上限
OUTPUT_DRAIN_GRACE_S = 0.1  # 输出排空宽限秒数


@dataclass(slots=True)
class _SessionPoll:
    """单次会话轮询的结果。"""

    output: str  # 本次轮询获取的输出
    done: bool  # 进程是否已退出
    exit_code: int | None  # 退出码，未退出时为 None
    elapsed_s: float = 0.0  # 已运行秒数
    timed_out: bool = False  # 是否因超时终止
    terminated: bool = False  # 是否被手动终止
    stdin_closed: bool = False  # stdin 是否已关闭
    truncated_chars: int = 0  # 被截断的字符数


@dataclass(slots=True)
class ExecSessionInfo:
    """执行会话的概要信息，用于列表展示。"""

    session_id: str  # 会话 ID
    command: str  # 执行的命令
    cwd: str  # 工作目录
    elapsed_s: float  # 已运行秒数
    idle_s: float  # 空闲秒数
    remaining_s: float  # 剩余超时秒数
    returncode: int | None  # 退出码
    owner_session_key: str | None = None  # 所属会话键


class _ExecSession:
    """单个执行会话的内部实现，封装子进程与输出缓冲。

    职责：管理一个异步子进程，持续读取 stdout/stderr 到缓冲区，
    提供 stdin 写入、stdin 关闭、输出轮询与进程终止能力。
    """

    def __init__(
        self,
        *,
        session_id: str,
        process: asyncio.subprocess.Process,
        command: str,
        cwd: str,
        timeout: int | None,
        owner_session_key: str | None = None,
    ) -> None:
        """初始化执行会话。

        参数:
            session_id: 会话唯一标识。
            process: 异步子进程对象。
            command: 执行的命令字符串。
            cwd: 工作目录。
            timeout: 超时秒数，None/0 表示无限制。
            owner_session_key: 所属会话键，用于归属隔离。
        """
        self.session_id = session_id
        self.process = process
        self.command = command
        self.cwd = cwd
        self.owner_session_key = owner_session_key
        self.started_at = time.monotonic()
        # timeout 为 None/0 时无限制，使用无限大截止时间
        self.deadline = time.monotonic() + timeout if timeout else float("inf")
        self.last_access = time.monotonic()
        self._chunks: list[str] = []  # 输出缓冲区
        self._lock = asyncio.Lock()  # 保护缓冲区的异步锁
        self._timed_out = False
        # 启动后台任务持续读取 stdout 与 stderr
        self._stdout_task = asyncio.create_task(self._read_stream(process.stdout, ""))
        self._stderr_task = asyncio.create_task(self._read_stream(process.stderr, "STDERR:\n"))

    async def _read_stream(
        self,
        stream: asyncio.StreamReader | None,
        prefix: str,
    ) -> None:
        """持续读取流并追加到缓冲区。

        参数:
            stream: 异步流读取器。
            prefix: 首个数据块的前缀（如 stderr 的 "STDERR:\\n"）。
        """
        if stream is None:
            return
        first = True
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            if prefix and first:
                text = prefix + text  # 仅首块加前缀
                first = False
            async with self._lock:
                self._chunks.append(text)

    async def write(self, chars: str) -> str | None:
        """向进程 stdin 写入字符。

        参数:
            chars: 待写入的字符串。

        返回:
            成功返回 None，失败返回错误描述。
        """
        if self.process.returncode is not None:
            return "session has already exited"
        if self.process.stdin is None:
            return "session stdin is not available"
        try:
            self.process.stdin.write(chars.encode("utf-8"))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return "session stdin is closed"
        return None

    async def close_stdin(self) -> str | None:
        """关闭进程 stdin（发送 EOF）。

        返回:
            成功返回 None，失败返回错误描述。
        """
        if self.process.returncode is not None:
            return "session has already exited"
        if self.process.stdin is None:
            return "session stdin is not available"
        self.process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await self.process.stdin.wait_closed()
        return None

    async def poll(
        self,
        yield_time_ms: int,
        max_output_chars: int,
        *,
        terminated: bool = False,
        stdin_closed: bool = False,
    ) -> _SessionPoll:
        """轮询会话输出。

        参数:
            yield_time_ms: 等待输出的毫秒数。
            max_output_chars: 返回输出的字符上限。
            terminated: 是否已终止进程。
            stdin_closed: 是否已关闭 stdin。

        返回:
            本次轮询的结果对象。
        """
        self.last_access = time.monotonic()
        # 进程未退出时等待指定时间以收集输出
        if yield_time_ms > 0 and self.process.returncode is None:
            await asyncio.sleep(min(yield_time_ms, MAX_YIELD_MS) / 1000)

        # 超时检查
        if self.process.returncode is None and time.monotonic() >= self.deadline:
            self._timed_out = True
            await self.kill()

        if self.process.returncode is not None:
            # 进程已退出：等待读取任务完成以排空剩余输出
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(self._stdout_task, self._stderr_task),
                    timeout=2.0,
                )
        elif yield_time_ms > 0:
            await self._wait_for_buffered_output()

        async with self._lock:
            output = "".join(self._chunks)
            self._chunks.clear()

        output, truncated = _truncate_output(output, max_output_chars)
        return _SessionPoll(
            output=output,
            done=self.process.returncode is not None,
            exit_code=self.process.returncode,
            elapsed_s=max(0.0, time.monotonic() - self.started_at),
            timed_out=self._timed_out,
            terminated=terminated,
            stdin_closed=stdin_closed,
            truncated_chars=truncated,
        )

    async def kill(self) -> None:
        """强制终止进程。"""
        if self.process.returncode is not None:
            return
        self.process.kill()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.process.wait(), timeout=5.0)

    async def _wait_for_buffered_output(self) -> None:
        """在宽限期内等待缓冲区出现数据，避免丢失快速产生的输出。"""
        deadline = time.monotonic() + OUTPUT_DRAIN_GRACE_S
        while time.monotonic() < deadline:
            async with self._lock:
                if self._chunks:
                    return
            await asyncio.sleep(0.01)


class ExecSessionManager:
    """执行会话管理器，负责会话的创建、查找、写入与清理。

    职责：维护一组活跃的 _ExecSession，提供并发安全的会话生命周期管理，
    包括空闲会话自动清理与归属会话键隔离。
    """

    def __init__(self, *, max_sessions: int = 8, idle_timeout: int = 1800) -> None:
        """初始化会话管理器。

        参数:
            max_sessions: 最大并发会话数。
            idle_timeout: 空闲超时秒数，超过后自动清理。
        """
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self._sessions: dict[str, _ExecSession] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        command: str,
        cwd: str,
        env: dict[str, str],
        timeout: int | None,
        shell_program: str | None,
        login: bool,
        yield_time_ms: int,
        max_output_chars: int,
        owner_session_key: str | None = None,
    ) -> tuple[str, _SessionPoll]:
        """启动新的执行会话。

        参数:
            command: 命令字符串。
            cwd: 工作目录。
            env: 环境变量。
            timeout: 超时秒数。
            shell_program: 自定义 shell 程序。
            login: 是否使用登录 shell。
            yield_time_ms: 初始轮询等待毫秒数。
            max_output_chars: 输出字符上限。
            owner_session_key: 所属会话键。

        返回:
            (session_id, 首次轮询结果) 元组。
        """
        async with self._lock:
            await self._cleanup_locked()
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError(f"maximum exec sessions reached ({self.max_sessions})")
            process = await self._spawn(command, cwd, env, shell_program, login)
            session_id = uuid.uuid4().hex[:12]
            session = _ExecSession(
                session_id=session_id,
                process=process,
                command=command,
                cwd=cwd,
                timeout=timeout,
                owner_session_key=owner_session_key,
            )
            self._sessions[session_id] = session

        poll = await session.poll(yield_time_ms, max_output_chars)
        if poll.done:
            # 进程立即退出则移除会话
            async with self._lock:
                self._sessions.pop(session_id, None)
        return session_id, poll

    async def write(
        self,
        *,
        session_id: str,
        chars: str | None,
        close_stdin: bool,
        terminate: bool,
        yield_time_ms: int,
        max_output_chars: int,
        owner_session_key: str | None = None,
    ) -> _SessionPoll:
        """向会话写入 stdin 或轮询输出。

        参数:
            session_id: 会话 ID。
            chars: 待写入的字符。
            close_stdin: 是否关闭 stdin。
            terminate: 是否终止进程。
            yield_time_ms: 轮询等待毫秒数。
            max_output_chars: 输出字符上限。
            owner_session_key: 所属会话键，用于归属校验。

        返回:
            轮询结果对象。
        """
        async with self._lock:
            await self._cleanup_locked()
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        # 归属会话键校验，防止跨会话访问
        if (
            owner_session_key
            and session.owner_session_key
            and session.owner_session_key != owner_session_key
        ):
            raise KeyError(session_id)

        if chars:
            error = await session.write(chars)
            if error:
                raise RuntimeError(error)
        stdin_closed = False
        if close_stdin:
            error = await session.close_stdin()
            if error:
                raise RuntimeError(error)
            stdin_closed = True
        if terminate:
            await session.kill()
        poll = await session.poll(
            yield_time_ms,
            max_output_chars,
            terminated=terminate,
            stdin_closed=stdin_closed,
        )
        if poll.done:
            async with self._lock:
                self._sessions.pop(session_id, None)
        return poll

    async def list(self, *, owner_session_key: str | None = None) -> list[ExecSessionInfo]:
        """列出活跃会话。

        参数:
            owner_session_key: 所属会话键，用于过滤。

        返回:
            会话概要信息列表。
        """
        async with self._lock:
            await self._cleanup_locked()
            now = time.monotonic()
            return [
                ExecSessionInfo(
                    session_id=session_id,
                    command=session.command,
                    cwd=session.cwd,
                    elapsed_s=max(0.0, now - session.started_at),
                    idle_s=max(0.0, now - session.last_access),
                    remaining_s=max(0.0, session.deadline - now),
                    returncode=session.process.returncode,
                    owner_session_key=session.owner_session_key,
                )
                for session_id, session in sorted(self._sessions.items())
                if not owner_session_key
                or not session.owner_session_key
                or session.owner_session_key == owner_session_key
            ]

    async def _cleanup_locked(self) -> None:
        """清理空闲超时的会话（调用方需持有锁）。"""
        now = time.monotonic()
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_access > self.idle_timeout
        ]
        for session_id in stale:
            session = self._sessions.pop(session_id)
            await session.kill()

    async def _spawn(
        self,
        command: str,
        cwd: str,
        env: dict[str, str],
        shell_program: str | None,
        login: bool,
    ) -> asyncio.subprocess.Process:
        """创建子进程，委托给 ExecTool 的 _spawn 方法。"""
        from biscuitbot.agent.tools.shell import ExecTool

        return await ExecTool._spawn(
            command, cwd, env, shell_program, login,
            stdin=asyncio.subprocess.PIPE,
        )


# 模块级默认会话管理器实例
DEFAULT_EXEC_SESSION_MANAGER = ExecSessionManager()


def clamp_session_int(value: int | None, default: int, minimum: int, maximum: int) -> int:
    """将整数值限制在 [minimum, maximum] 范围内，None 时返回默认值。

    参数:
        value: 原始值。
        default: 默认值。
        minimum: 最小值。
        maximum: 最大值。

    返回:
        限制后的整数值。
    """
    if value is None:
        return default
    return min(max(value, minimum), maximum)


def _truncate_output(output: str, max_output_chars: int) -> tuple[str, int]:
    """截断输出到指定字符数，保留首尾各一半。

    参数:
        output: 原始输出。
        max_output_chars: 最大字符数。

    返回:
        (截断后的输出, 被截断的字符数) 元组。
    """
    if len(output) <= max_output_chars:
        return output, 0
    half = max_output_chars // 2
    omitted = len(output) - max_output_chars
    return (
        output[:half]
        + f"\n\n... ({omitted:,} chars truncated) ...\n\n"
        + output[-half:],
        omitted,
    )


def format_session_poll(session_id: str, poll: _SessionPoll) -> str:
    """将轮询结果格式化为可读字符串。

    参数:
        session_id: 会话 ID。
        poll: 轮询结果对象。

    返回:
        格式化后的字符串。
    """
    parts = [poll.output] if poll.output else []
    if poll.truncated_chars:
        parts.append(f"(output truncated by {poll.truncated_chars:,} chars)")
    if poll.timed_out:
        parts.append("Error: Command timed out; session was terminated.")
    if poll.terminated and not poll.timed_out:
        parts.append("Session terminated.")
    if poll.stdin_closed:
        parts.append("Stdin closed.")
    if poll.done:
        parts.append(f"Exit code: {poll.exit_code}")
    else:
        parts.append(f"Process running. session_id: {session_id}")
    parts.append(f"Elapsed: {poll.elapsed_s:.1f}s")
    return "\n".join(parts) if parts else "(no output yet)"


@tool_parameters(
    tool_parameters_schema(
        session_id=StringSchema("Session id returned by exec when yield_time_ms is used."),
        chars=StringSchema(
            "Bytes/text to write to stdin. Omit or pass an empty string to only poll recent output.",
            nullable=True,
        ),
        close_stdin=BooleanSchema(
            description="Close stdin after writing chars. Useful for commands waiting for EOF.",
            default=False,
        ),
        terminate=BooleanSchema(
            description="Terminate the running exec session.",
            default=False,
        ),
        yield_time_ms=IntegerSchema(
            DEFAULT_YIELD_MS,
            description="Milliseconds to wait before returning recent output (default 1000, max 30000).",
            minimum=0,
            maximum=MAX_YIELD_MS,
        ),
        wait_for=StringSchema(
            "Optional text to wait for in output before returning. "
            "Useful for interactive commands and dev servers.",
            nullable=True,
        ),
        wait_timeout_ms=IntegerSchema(
            DEFAULT_WAIT_FOR_MS,
            description="Maximum milliseconds to wait for wait_for text (default 10000, max 120000).",
            minimum=0,
            maximum=MAX_WAIT_FOR_MS,
            nullable=True,
        ),
        max_output_chars=IntegerSchema(
            DEFAULT_MAX_OUTPUT_CHARS,
            description="Maximum output characters to return from this poll (default 10000, max 50000).",
            minimum=1000,
            maximum=MAX_OUTPUT_CHARS,
        ),
        max_output_tokens=IntegerSchema(
            DEFAULT_MAX_OUTPUT_CHARS,
            description="Compatibility alias for max_output_chars. The current runtime uses a character budget.",
            minimum=1000,
            maximum=MAX_OUTPUT_CHARS,
            nullable=True,
        ),
        required=["session_id"],
    )
)
class WriteStdinTool(Tool):
    """向运行中的 exec 会话写入 stdin 或轮询其输出。

    职责：与通过 exec（yield_time_ms）创建的运行中会话交互，支持写入
    stdin、关闭 stdin、终止进程、轮询输出，以及等待特定输出出现。

    用法：由 agent 调用，传入 session_id 及相应操作参数。
    """

    _capability = (
        "Send stdin to a running exec session and poll its output incrementally."
    )
    _usage_md = "docs/write_stdin.md"  # 工具使用说明文档路径

    _scopes = {"core", "subagent"}  # 工具可用作用域
    config_key = "exec"  # 配置键名

    @classmethod
    def config_cls(cls):
        """返回该工具对应的配置类。"""
        from biscuitbot.agent.tools.shell import ExecToolConfig

        return ExecToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """根据配置判断工具是否启用。"""
        return ctx.config.exec.enable

    def __init__(
        self,
        *,
        manager: ExecSessionManager | None = None,
    ) -> None:
        """初始化工具，注入会话管理器。

        参数:
            manager: 执行会话管理器，为空时使用默认实例。
        """
        self._manager = manager or DEFAULT_EXEC_SESSION_MANAGER

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """工厂方法：创建工具实例。"""
        return cls()

    @property
    def exclusive(self) -> bool:
        """该工具需独占执行，避免并发写入冲突。"""
        return True

    @property
    def name(self) -> str:
        """工具名称。"""
        return "write_stdin"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "Interact with a running exec session created by exec with "
            "yield_time_ms. Use chars='' to poll without writing, chars to send "
            "stdin, close_stdin=true to send EOF, or terminate=true to stop the "
            "process. Use wait_for with wait_timeout_ms for dev servers, test "
            "watchers, and prompts where you need to wait for expected output. "
            "Do not use this to start new commands; start them with exec."
        )

    async def execute(
        self,
        session_id: str,
        chars: str | None = None,
        close_stdin: bool = False,
        terminate: bool = False,
        yield_time_ms: int | None = None,
        wait_for: str | None = None,
        wait_timeout_ms: int | None = None,
        max_output_chars: int | None = None,
        max_output_tokens: int | None = None,
        **kwargs: Any,
    ) -> str:
        """执行写入或轮询操作。

        参数:
            session_id: 会话 ID。
            chars: 待写入 stdin 的字符。
            close_stdin: 是否关闭 stdin。
            terminate: 是否终止进程。
            yield_time_ms: 轮询等待毫秒数。
            wait_for: 等待出现的输出文本。
            wait_timeout_ms: 等待超时毫秒数。
            max_output_chars: 输出字符上限。
            max_output_tokens: max_output_chars 的兼容别名。

        返回:
            操作结果字符串。
        """
        try:
            # 兼容旧参数名 max_output_tokens
            if max_output_chars is None:
                max_output_chars = max_output_tokens
            output_limit = clamp_session_int(
                max_output_chars,
                DEFAULT_MAX_OUTPUT_CHARS,
                1000,
                MAX_OUTPUT_CHARS,
            )
            if wait_for:
                # 等待特定输出模式
                return await self._wait_for_output(
                    session_id=session_id,
                    chars=chars,
                    close_stdin=close_stdin,
                    terminate=terminate,
                    wait_for=wait_for,
                    wait_timeout_ms=clamp_session_int(
                        wait_timeout_ms,
                        DEFAULT_WAIT_FOR_MS,
                        0,
                        MAX_WAIT_FOR_MS,
                    ),
                    max_output_chars=output_limit,
                )
            poll = await self._manager.write(
                session_id=session_id,
                chars=chars,
                close_stdin=close_stdin,
                terminate=terminate,
                yield_time_ms=clamp_session_int(yield_time_ms, DEFAULT_YIELD_MS, 0, MAX_YIELD_MS),
                max_output_chars=output_limit,
                owner_session_key=current_request_session_key(),
            )
            return format_session_poll(session_id, poll)
        except KeyError:
            return f"Error: exec session not found: {session_id}"
        except Exception as exc:
            return f"Error writing to exec session: {exc}"

    async def _wait_for_output(
        self,
        *,
        session_id: str,
        chars: str | None,
        close_stdin: bool,
        terminate: bool,
        wait_for: str,
        wait_timeout_ms: int,
        max_output_chars: int,
    ) -> str:
        """循环轮询直到输出中出现目标文本或超时。

        参数:
            session_id: 会话 ID。
            chars: 首次写入的字符。
            close_stdin: 是否首次关闭 stdin。
            terminate: 是否首次终止进程。
            wait_for: 等待的目标文本。
            wait_timeout_ms: 等待超时毫秒数。
            max_output_chars: 输出字符上限。

        返回:
            聚合输出与状态信息字符串。
        """
        deadline = time.monotonic() + (wait_timeout_ms / 1000)
        aggregate: list[str] = []
        first = True
        poll: _SessionPoll | None = None

        while True:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            step_ms = min(500, remaining_ms)
            poll = await self._manager.write(
                session_id=session_id,
                chars=chars if first else None,
                close_stdin=close_stdin if first else False,
                terminate=terminate if first else False,
                yield_time_ms=step_ms,
                max_output_chars=max_output_chars,
                owner_session_key=current_request_session_key(),
            )
            first = False
            if poll.output:
                aggregate.append(poll.output)
                joined = "".join(aggregate)
                # 检测目标文本是否出现
                if wait_for in joined:
                    poll.output = joined
                    return format_session_poll(session_id, poll)
            if poll.done or remaining_ms <= 0:
                poll.output = "".join(aggregate)
                result = format_session_poll(session_id, poll)
                if wait_for not in poll.output:
                    result += f"\nWait target not observed: {wait_for!r}"
                return result


@tool_parameters(tool_parameters_schema())
class ListExecSessionsTool(Tool):
    """列出活跃的 exec 会话。

    职责：返回当前运行中的执行会话列表，包含会话 ID、工作目录、运行时长、
    空闲时间、剩余超时与命令预览，便于 agent 在上下文切换后恢复 session_id。

    用法：由 agent 调用，无需参数。
    """

    _capability = "List currently running exec sessions with their IDs and status."
    _usage_md = "docs/list_exec_sessions.md"  # 工具使用说明文档路径

    _scopes = {"core", "subagent"}  # 工具可用作用域
    config_key = "exec"  # 配置键名

    @classmethod
    def config_cls(cls):
        """返回该工具对应的配置类。"""
        from biscuitbot.agent.tools.shell import ExecToolConfig

        return ExecToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """根据配置判断工具是否启用。"""
        return ctx.config.exec.enable

    def __init__(
        self,
        *,
        manager: ExecSessionManager | None = None,
    ) -> None:
        """初始化工具，注入会话管理器。

        参数:
            manager: 执行会话管理器，为空时使用默认实例。
        """
        self._manager = manager or DEFAULT_EXEC_SESSION_MANAGER

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """工厂方法：创建工具实例。"""
        return cls()

    @property
    def name(self) -> str:
        """工具名称。"""
        return "list_exec_sessions"

    @property
    def description(self) -> str:
        """工具描述。"""
        return (
            "List active long-running exec sessions, including session_id, cwd, "
            "elapsed time, idle time, remaining timeout, and command preview. "
            "Use this to recover a session_id after context shifts before "
            "polling, writing stdin, or terminating with write_stdin."
        )

    @property
    def read_only(self) -> bool:
        """该工具只读，无副作用，可安全并行。"""
        return True

    async def execute(self, **kwargs: Any) -> str:
        """执行会话列表查询。

        返回:
            活跃会话列表字符串；无会话时返回提示。
        """
        try:
            sessions = await self._manager.list(
                owner_session_key=current_request_session_key(),
            )
            if not sessions:
                return "No active exec sessions."
            lines = []
            for info in sessions:
                command = " ".join(info.command.split())
                # 截断过长的命令预览
                if len(command) > 120:
                    command = command[:119] + "..."
                status = "exited" if info.returncode is not None else "running"
                lines.append(
                    f"{info.session_id} | {status} | elapsed={info.elapsed_s:.1f}s "
                    f"| idle={info.idle_s:.1f}s | remaining={info.remaining_s:.1f}s "
                    f"| cwd={info.cwd} | {command}"
                )
            return "\n".join(lines)
        except Exception as exc:
            return f"Error listing exec sessions: {exc}"
