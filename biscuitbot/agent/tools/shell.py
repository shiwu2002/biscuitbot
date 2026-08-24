"""Shell 命令执行工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统中的 shell 执行组件。
``ExecTool``（exec）允许 agent 执行 shell 命令（如构建、测试、git、包管理），
并提供多层安全防护：

- **deny-list**：按严重程度分两级，灾难性命令（rm -rf、mkfs 等）在 standard
  和 minimal 级别均拦截；摩擦型命令（download-and-execute、内部状态文件写入）
  仅在 standard 级别拦截。
- **SSRF 防护**：检测并拦截内网/私有 URL。
- **工作区边界**：当 restrict_to_workspace 启用时，拦截路径穿越与工作区外
  的绝对路径。
- **沙箱包装**：可选通过 bubblewrap 沙箱限制文件系统访问。
- **会话模式**：通过 yield_time_ms 支持长时间运行命令的异步会话。
"""

from __future__ import annotations

import asyncio  # 异步 IO，用于子进程管理
import hashlib  # sha256 生成冻结环境 shim 目录指纹
import os  # 操作系统接口
import re  # 正则表达式，用于 deny-list 匹配
import shlex  # shim 内容里的路径安全引用
import shutil  # shell 工具查找（which）
import sys  # 系统相关（平台判断 / 冻结环境检测）
import tempfile  # 冻结环境 shim 目录
from contextlib import suppress  # 上下文管理器，忽略异常
from dataclasses import dataclass  # 数据类装饰器
from pathlib import Path  # 路径处理
from typing import Any  # 类型注解

from loguru import logger  # 日志记录
from pydantic import Field  # Pydantic 字段

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.context import current_request_session_key  # 当前请求会话键
from biscuitbot.agent.tools.exec_session import (  # 执行会话管理
    DEFAULT_EXEC_SESSION_MANAGER,
    DEFAULT_MAX_OUTPUT_CHARS,
    DEFAULT_YIELD_MS,
    MAX_OUTPUT_CHARS,
    MAX_YIELD_MS,
    clamp_session_int,
    format_session_poll,
)
from biscuitbot.agent.tools.sandbox import wrap_command  # 沙箱命令包装
from biscuitbot.agent.tools.schema import (  # JSON Schema 类型
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from biscuitbot.config.paths import get_media_dir  # 媒体目录路径
from biscuitbot.config_base import Base  # 配置基类
from biscuitbot.security.guard_level import GuardPolicy  # 守卫策略
from biscuitbot.security.workspace_access import current_scope_allows_loopback, current_tool_workspace  # 工作区访问控制
from biscuitbot.security.workspace_policy import is_path_within  # 路径在工作区内判断

_IS_WINDOWS = sys.platform == "win32"  # 是否为 Windows 平台


# 追加到可恢复的工作区边界守卫错误后的策略说明
_WORKSPACE_BOUNDARY_NOTE = (
    "\n\nNote: this is a hard policy boundary, not a transient failure. "
    "Do NOT retry with shell tricks (symlinks, base64 piping, alternative "
    "tools, working_dir overrides). If the user genuinely needs this "
    "resource, tell them you cannot reach it under the current "
    "restrict_to_workspace policy and ask how to proceed."
)

# 硬编码的 shell 拒绝列表，按严重程度分级以配合 guard_level 门控。
# 灾难性命令在 guard_level standard + minimal 级别均被拦截。
# 摩擦型模式（download-and-execute、内部状态文件写入）仅在 standard 级别拦截；
# 它们对合法开发流程（rustup/homebrew 安装脚本）会造成实际摩擦，因此高级用户
# 可通过 minimal/off 级别放宽。
_CATASTROPHIC_DENY_PATTERNS: list[str] = [
    r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
    r"\bdel\s+/[fq]\b",              # del /f, del /q
    r"\brmdir\s+/s\b",               # rmdir /s
    r"(?:^|[;&|]\s*)format(?!=)\b",   # format（仅作为独立命令）
    r"\b(mkfs|diskpart)\b",          # 磁盘操作
    r"\bdd\s+if=",                   # dd
    r">\s*/dev/sd",                  # 写入磁盘
    r"\b(shutdown|reboot|poweroff)\b",  # 系统电源
    r":\(\)\s*\{.*\};\s*:",          # fork 炸弹
]

# 摩擦级模式：对间接提示注入高信号，但常被合法开发流程触发。仅在 standard 级别生效。
_FRICTION_DENY_PATTERNS: list[str] = [
    # 拦截 "download-and-execute" 模式，该模式常用于间接提示注入，将良性 exec
    # 转为远程代码执行。正常开发流程很少将远程获取管道到解释器 shell，
    # 因此这些是高信号拒绝规则。
    r"\b(?:curl|wget|fetch)\b[^|;&]*\|\s*(?:sh|bash|zsh|dash|ksh)\b",   # curl … | sh
    r"\b(?:curl|wget|fetch)\b[^|;&]*\|\s*(?:sh|bash|zsh|dash|ksh)\s",  # curl … | sh -
    r"\bbase64\s+-d\b[^|]*\|\s*(?:sh|bash|zsh|dash|ksh)\b",            # base64 -d … | sh
    r"\beval\s+[\"'$]?\(?\s*\$?\(\s*(?:curl|wget|fetch)\b",            # eval "$(curl …)"
    # 拦截对 biscuitbot 内部状态文件的写入（#2989）。
    # history.jsonl / .dream_cursor 由 append_history() 管理；
    # 直接写入会破坏游标格式并导致 /dream 崩溃。
    r">>?\s*\S*(?:history\.jsonl|\.dream_cursor)",            # > / >> 重定向
    r"\btee\b[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",     # tee / tee -a
    r"\b(?:cp|mv)\b(?:\s+[^\s|;&<>]+)+\s+\S*(?:history\.jsonl|\.dream_cursor)",  # cp/mv 目标
    r"\bdd\b[^|;&<>]*\bof=\S*(?:history\.jsonl|\.dream_cursor)",  # dd of=
    r"\bsed\s+-i[^|;&<>]*(?:history\.jsonl|\.dream_cursor)",  # sed -i
]


class ExecToolConfig(Base):
    """Shell exec 工具配置。"""
    enable: bool = True
    timeout: int = Field(default=60, ge=0)  # 硬超时（秒）；0 = 无限制。不受单次调用上限约束。
    path_prepend: str = ""  # 前置 PATH
    path_append: str = ""  # 后置 PATH
    sandbox: str = ""  # 沙箱后端名称（如 "bwrap"）
    prefer_venv_python: bool = True  # exec 命令的 python3/pip3 优先解析到当前 venv
    allowed_env_keys: list[str] = Field(default_factory=list)  # 允许透传的环境变量键
    allow_patterns: list[str] = Field(default_factory=list)  # 用户自定义允许列表
    deny_patterns: list[str] = Field(default_factory=list)  # 用户自定义拒绝列表


@dataclass(slots=True)
class _PreparedCommand:
    """已准备好的命令：经过守卫检查与沙箱包装后的执行参数。"""
    command: str  # 命令字符串
    cwd: str  # 工作目录
    env: dict[str, str]  # 环境变量
    timeout: int | None  # 超时（秒），None 表示不限
    shell_program: str | None  # 指定的 shell 程序路径
    login: bool  # 是否以 login shell 方式运行


@tool_parameters(
    tool_parameters_schema(
        command=StringSchema("The shell command to execute"),
        cmd=StringSchema("Compatibility alias for command"),
        working_dir=StringSchema("Optional working directory for the command"),
        workdir=StringSchema("Compatibility alias for working_dir"),
        timeout=IntegerSchema(
            60,
            description=(
                "Timeout in seconds. Increase for long-running commands "
                "like compilation or installation (default 60, max 600)."
            ),
            minimum=1,
            maximum=600,
        ),
        shell=StringSchema(
            "Optional shell binary to launch. On Unix, supports sh, bash, or zsh.",
            nullable=True,
        ),
        login=BooleanSchema(
            description="Whether to run bash/zsh with login shell semantics (default true).",
            default=True,
            nullable=True,
        ),
        yield_time_ms=IntegerSchema(
            description=(
                "Optional milliseconds to wait before returning output. "
                "When set, a still-running command returns a session_id that "
                "can be polled or written to with write_stdin. Omit this field "
                "to keep one-shot exec behavior."
            ),
            minimum=0,
            maximum=MAX_YIELD_MS,
            nullable=True,
        ),
        max_output_chars=IntegerSchema(
            description=(
                "Maximum output characters to return when yield_time_ms is used "
                "(default 10000, max 50000)."
            ),
            minimum=1000,
            maximum=MAX_OUTPUT_CHARS,
            nullable=True,
        ),
        max_output_tokens=IntegerSchema(
            description=(
                "Compatibility alias for max_output_chars. The current runtime "
                "uses a character budget."
            ),
            minimum=1000,
            maximum=MAX_OUTPUT_CHARS,
            nullable=True,
        ),
    )
)
class ExecTool(Tool):
    """执行 shell 命令的工具。"""
    _scopes = {"core", "subagent"}  # 工具可用作用域：核心与子 agent

    _capability = (
        "Execute shell commands (build, test, git, package managers) with "
        "timeout, sandbox, and deny-list guards."
    )
    _always_include = True  # 该工具的完整 schema 始终发送给模型
    _usage_md = "docs/exec.md"  # 使用说明文档路径

    config_key = "exec"  # 配置键名

    @classmethod
    def config_cls(cls):
        return ExecToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return ctx.config.exec.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        cfg = ctx.config.exec
        return cls(
            working_dir=ctx.workspace,
            timeout=cfg.timeout,
            restrict_to_workspace=ctx.config.restrict_to_workspace,
            webui_allow_local_service_access=ctx.config.webui_allow_local_service_access,
            sandbox=cfg.sandbox,
            path_prepend=cfg.path_prepend,
            path_append=cfg.path_append,
            prefer_venv_python=cfg.prefer_venv_python,
            allowed_env_keys=cfg.allowed_env_keys,
            allow_patterns=cfg.allow_patterns,
            deny_patterns=cfg.deny_patterns,
            guard_level=ctx.config.guard_level,
        )

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        webui_allow_local_service_access: bool = True,
        allow_local_preview_access: bool | None = None,
        sandbox: str = "",
        path_prepend: str = "",
        path_append: str = "",
        prefer_venv_python: bool = True,
        allowed_env_keys: list[str] | None = None,
        guard_level: str = "standard",
        session_manager: Any | None = None,
    ):
        self.timeout = timeout  # 配置级默认超时
        self.working_dir = working_dir  # 工作目录
        self.sandbox = sandbox  # 沙箱后端
        self.guard_level = guard_level  # 守卫等级
        # 根据 guard_level 构建硬编码拒绝列表。用户自定义 deny_patterns 始终生效
        # （用户自己的规则）。应用的硬编码模式由 GuardPolicy 门控：
        #   standard → 灾难性 + 摩擦型
        #   minimal  → 仅灾难性
        #   off      → 无
        # _guard_command 中的 SSRF 和工作区边界检查始终运行，不受等级影响。
        policy = GuardPolicy(guard_level)
        hardcoded: list[str] = []
        if policy.catastrophic_shell_blocks:
            hardcoded.extend(_CATASTROPHIC_DENY_PATTERNS)
        if policy.shell_denylist:
            hardcoded.extend(_FRICTION_DENY_PATTERNS)
        self.deny_patterns = (deny_patterns or []) + hardcoded
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        if allow_local_preview_access is not None:
            webui_allow_local_service_access = allow_local_preview_access
        self.webui_allow_local_service_access = webui_allow_local_service_access
        self.path_prepend = path_prepend
        self.path_append = path_append
        self.prefer_venv_python = prefer_venv_python
        self.allowed_env_keys = allowed_env_keys or []
        self._session_manager = session_manager or DEFAULT_EXEC_SESSION_MANAGER

    @property
    def name(self) -> str:
        return "exec"

    _MAX_TIMEOUT = 600  # 单次调用最大超时（秒）
    _MAX_OUTPUT = 10_000  # 默认输出最大字符数

    # 可安全作为 stdio 重定向目标的内核设备文件（#3599）
    _BENIGN_DEVICE_PATHS: frozenset[str] = frozenset({
        "/dev/null",
        "/dev/zero",
        "/dev/full",
        "/dev/random",
        "/dev/urandom",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/tty",
    })

    @property
    def description(self) -> str:
        return (
            "Execute a shell command and return its output. "
            "Use this for tests, builds, package commands, git commands, and "
            "other process execution. Prefer read_file/find_files/grep for "
            "inspection and apply_patch/write_file/edit_file for file changes "
            "instead of cat, shell find/grep, echo, or sed. "
            "Use -y or --yes flags to avoid interactive prompts. "
            "For long-running or interactive commands, pass yield_time_ms; "
            "if the command keeps running, exec returns a session_id that can "
            "be polled or written to with write_stdin. Output is truncated at "
            "10 000 chars; timeout defaults to 60s."
        )

    @property
    def exclusive(self) -> bool:
        return True

    async def execute(
        self, command: str | None = None, cmd: str | None = None,
        working_dir: str | None = None, workdir: str | None = None,
        timeout: int | None = None, shell: str | None = None,
        login: bool | None = None, yield_time_ms: int | None = None,
        max_output_chars: int | None = None,
        max_output_tokens: int | None = None,
        **kwargs: Any,
    ) -> str:
        """执行 shell 命令并返回输出。

        参数:
            command: 要执行的 shell 命令（cmd 为兼容别名）。
            cmd: command 的兼容别名。
            working_dir: 可选的工作目录（workdir 为兼容别名）。
            workdir: working_dir 的兼容别名。
            timeout: 超时秒数（默认 60，最大 600）。
            shell: 可选的 shell 程序（Unix 支持 sh/bash/zsh）。
            login: 是否以 login shell 方式运行 bash/zsh（默认 true）。
            yield_time_ms: 等待毫秒数；设置后仍在运行的命令返回 session_id。
            max_output_chars: 返回的最大输出字符数（默认 10000，最大 50000）。
            max_output_tokens: max_output_chars 的兼容别名。

        返回:
            命令输出（stdout + stderr + 退出码）；超时或错误时返回错误信息。
        """
        command = command or cmd
        working_dir = working_dir or workdir
        if not command:
            return "Error: Missing command. Provide command or cmd."
        if max_output_chars is None:
            max_output_chars = max_output_tokens

        prepared = self._prepare_command(command, working_dir, timeout, shell, login)
        if isinstance(prepared, str):
            return prepared

        if yield_time_ms is not None:
            return await self._execute_session(prepared, yield_time_ms, max_output_chars)

        try:
            process = await self._spawn(
                prepared.command,
                prepared.cwd,
                prepared.env,
                prepared.shell_program,
                prepared.login,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=prepared.timeout,
                )
            except asyncio.TimeoutError:
                await self._kill_process(process)
                return f"Error: Command timed out after {prepared.timeout} seconds"
            except asyncio.CancelledError:
                await self._kill_process(process)
                raise

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            output_parts.append(f"\nExit code: {process.returncode}")

            result = "\n".join(output_parts) if output_parts else "(no output)"

            max_len = clamp_session_int(max_output_chars, self._MAX_OUTPUT, 1000, MAX_OUTPUT_CHARS)
            if len(result) > max_len:
                half = max_len // 2
                result = (
                    result[:half]
                    + f"\n\n... ({len(result) - max_len:,} chars truncated) ...\n\n"
                    + result[-half:]
                )

            return result

        except Exception as e:
            return (
                f"Error: 命令启动失败（{type(e).__name__}: {e}），"
                "请检查命令与 working_dir 是否正确"
            )

    async def _execute_session(
        self,
        prepared: _PreparedCommand,
        yield_time_ms: int | None,
        max_output_chars: int | None,
    ) -> str:
        """以会话模式启动命令，返回可轮询的 session_id。"""
        try:
            session_id, poll = await self._session_manager.start(
                command=prepared.command,
                cwd=prepared.cwd,
                env=prepared.env,
                timeout=prepared.timeout,
                shell_program=prepared.shell_program,
                login=prepared.login,
                yield_time_ms=clamp_session_int(yield_time_ms, DEFAULT_YIELD_MS, 0, MAX_YIELD_MS),
                owner_session_key=current_request_session_key(),
                max_output_chars=clamp_session_int(
                    max_output_chars,
                    DEFAULT_MAX_OUTPUT_CHARS,
                    1000,
                    MAX_OUTPUT_CHARS,
                ),
            )
            return format_session_poll(session_id, poll)
        except Exception as exc:
            return (
                f"Error: 会话命令启动失败（{type(exc).__name__}: {exc}），"
                "请检查 working_dir 是否存在及命令是否有效"
            )

    def _resolve_timeout(self, timeout: int | None) -> int | None:
        """解析有效的硬超时（秒），None 表示无限制。

        模型提供的单次调用超时始终被 _MAX_TIMEOUT 封顶，防止 LLM 请求无限制
        执行。配置级默认值（self.timeout）可超过该上限，0 表示完全禁用限制
        用于可信的长时间运行任务（#3595）。
        """
        if timeout:
            return min(timeout, self._MAX_TIMEOUT)
        if self.timeout and self.timeout > 0:
            return self.timeout
        return None

    def _prepare_command(
        self,
        command: str,
        working_dir: str | None = None,
        timeout: int | None = None,
        shell: str | None = None,
        login: bool | None = None,
    ) -> _PreparedCommand | str:
        """准备命令：解析工作区、守卫检查、沙箱包装、构建环境变量。

        返回 ``_PreparedCommand`` 或错误字符串。
        """
        access = current_tool_workspace(
            self.working_dir,
            restrict_to_workspace=self.restrict_to_workspace,
            sandbox_restricts_workspace=bool(self.sandbox),
        )
        workspace_root = str(access.project_path) if access.project_path is not None else self.working_dir
        cwd = working_dir or workspace_root or os.getcwd()

        # Prevent an LLM-supplied working_dir from escaping the configured
        # workspace when restrict_to_workspace is enabled (#2826). Without
        # this, a caller can pass working_dir="/etc" and then all absolute
        # paths under /etc would pass the _guard_command check that anchors
        # on cwd.
        if access.restrict_to_workspace and workspace_root:
            try:
                requested = Path(cwd).expanduser().resolve()
                resolved_root = Path(workspace_root).expanduser().resolve()
            except Exception:
                return (
                    f"Error: 无法解析 working_dir（{working_dir!r}）：路径无效或含非法字符"
                    + _WORKSPACE_BOUNDARY_NOTE
                )
            if not is_path_within(requested, resolved_root):
                return (
                    "Error: working_dir is outside the configured workspace"
                    + _WORKSPACE_BOUNDARY_NOTE
                )

        guard_error = self._guard_command(
            command,
            cwd,
            restrict_to_workspace=access.restrict_to_workspace,
        )
        if guard_error:
            return guard_error

        if self.sandbox:
            if _IS_WINDOWS:
                logger.warning(
                    "Sandbox '{}' is not supported on Windows; running unsandboxed",
                    self.sandbox,
                )
            else:
                workspace = workspace_root or cwd
                try:
                    command = wrap_command(self.sandbox, command, workspace, cwd)
                except ValueError as exc:
                    return f"Error: {exc}"
                cwd = str(Path(workspace).resolve())

        effective_timeout = self._resolve_timeout(timeout)
        env = self._build_env()

        # 把「项目解释器」（venv 网关 → venv bin；PyInstaller 冻结 → sidecar
        # 解释器模式 shim）的可执行目录排到 PATH 最前，使 exec 里的 python3/pip3
        # 默认解析到打包/项目的解释器，核心依赖开箱即用，避免 agent 落入系统
        # python + pip 调试泥潭。
        venv_bin = self._prefer_python_bin() if self.prefer_venv_python else None
        if venv_bin or self.path_prepend or self.path_append:
            if _IS_WINDOWS:
                env["PATH"] = self._compose_path(env.get("PATH", ""), venv_bin)
            else:
                command = self._wrap_path_export(command, env, venv_bin)

        shell_program, shell_error = self._resolve_shell(shell)
        if shell_error:
            return shell_error

        return _PreparedCommand(
            command=command,
            cwd=cwd,
            env=env,
            timeout=effective_timeout,
            shell_program=shell_program,
            login=True if login is None else login,
        )

    def _compose_path(self, current_path: str, venv_bin: str | None = None) -> str:
        parts = []
        if venv_bin:
            parts.append(venv_bin)
        if self.path_prepend:
            parts.append(self.path_prepend)
        if current_path:
            parts.append(current_path)
        if self.path_append:
            parts.append(self.path_append)
        return os.pathsep.join(parts)

    def _wrap_path_export(
        self, command: str, env: dict[str, str], venv_bin: str | None = None
    ) -> str:
        segments = []
        if venv_bin:
            env["BISCUITBOT_VENV_BIN"] = venv_bin
            segments.append("$BISCUITBOT_VENV_BIN")
        if self.path_prepend:
            env["BISCUITBOT_PATH_PREPEND"] = self.path_prepend
            segments.append("$BISCUITBOT_PATH_PREPEND")
        segments.append("$PATH")
        if self.path_append:
            env["BISCUITBOT_PATH_APPEND"] = self.path_append
            segments.append("$BISCUITBOT_PATH_APPEND")
        path_expr = os.pathsep.join(segments)
        return f'export PATH="{path_expr}"; {command}'

    @staticmethod
    async def _spawn(
        command: str, cwd: str, env: dict[str, str],
        shell_program: str | None = None,
        login: bool = True,
        *,
        stdin: int = asyncio.subprocess.DEVNULL,
    ) -> asyncio.subprocess.Process:
        """Launch *command* in a platform-appropriate shell."""
        if _IS_WINDOWS:
            if "\n" in command:
                return await asyncio.create_subprocess_exec(
                    "powershell", "-NoProfile", "-Command", command,
                    stdin=stdin,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            return await asyncio.create_subprocess_shell(
                command,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        shell_program = shell_program or shutil.which("bash") or "/bin/bash"
        args = [shell_program]
        shell_name = Path(shell_program).name.lower()
        if login and shell_name in {"bash", "bash.exe", "zsh", "zsh.exe"}:
            args.append("-l")
        args.extend(["-c", command])
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

    @staticmethod
    def _resolve_shell(shell: str | None) -> tuple[str | None, str | None]:
        if not shell:
            return None, None
        if _IS_WINDOWS:
            return None, "Error: shell parameter is not supported on Windows"
        if "\0" in shell or "\n" in shell or "\r" in shell:
            return None, "Error: shell contains invalid characters"
        allowed = {"sh", "bash", "zsh"}
        path = Path(shell).expanduser()
        if path.is_absolute():
            if path.name not in allowed:
                return None, f"Error: unsupported shell {shell!r}. Allowed: bash, sh, zsh"
            if not path.is_file() or not os.access(path, os.X_OK):
                return None, f"Error: shell is not executable: {shell}"
            return str(path), None
        if "/" in shell or "\\" in shell:
            return None, "Error: shell must be a shell name or absolute path"
        if shell not in allowed:
            return None, f"Error: unsupported shell {shell!r}. Allowed: bash, sh, zsh"
        resolved = shutil.which(shell)
        if not resolved:
            return None, f"Error: shell not found: {shell}"
        return resolved, None

    @staticmethod
    async def _kill_process(process: asyncio.subprocess.Process) -> None:
        """Kill a subprocess and reap it to prevent zombies."""
        process.kill()
        try:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5.0)
        finally:
            if not _IS_WINDOWS:
                try:
                    os.waitpid(process.pid, os.WNOHANG)
                except (ProcessLookupError, ChildProcessError) as e:
                    logger.debug("Process already reaped or not found: {}", e)

    @staticmethod
    def _venv_bin_dir() -> str | None:
        """返回当前进程所在 venv 的可执行目录（Unix: bin / Windows: Scripts）。

        网关以 ``.venv/bin/python`` 启动时，``sys.prefix`` 指向 venv 根，
        可执行目录即 ``sys.prefix/bin``（或 Scripts）。以系统解释器 /
        PyInstaller 冻结环境运行（``sys.prefix == sys.base_prefix``）时
        返回 None，exec 命令解析行为保持原样。
        """
        if sys.prefix == sys.base_prefix:
            return None
        bindir = Path(sys.prefix) / ("Scripts" if _IS_WINDOWS else "bin")
        if not bindir.is_dir():
            return None
        # 防御：确认 sys.executable 确实位于该目录，排除前缀异常等情况
        if not str(Path(sys.executable).parent).startswith(str(bindir)):
            return None
        return str(bindir)

    def _prefer_python_bin(self) -> str | None:
        """返回应前置到 PATH 的 Python 可执行目录，None 表示不注入：

        - venv 网关（开发）→ 项目 venv bin（:meth:`_venv_bin_dir`）
        - PyInstaller 冻结（桌面 sidecar）→ 解释器 shim 目录（:meth:`_bundled_python_bin`）
        """
        return self._venv_bin_dir() or self._bundled_python_bin()

    def _bundled_python_bin(self) -> str | None:
        """PyInstaller 冻结（桌面 sidecar）：生成指向 sidecar 解释器模式的 shim。

        bundle 内没有独立 python 可执行文件（唯一可执行是 sidecar 本体，跑网关
        主程序），因此在临时目录生成 python/python3/pip/pip3 shim，exec 时以
        ``<sidecar> __biscuitbot_python__ ...`` 把请求转给 sidecar 的「解释器
        模式」（``biscuitbot.desktop.sidecar``），复用打包进 bundle 的标准库与
        第三方依赖。目录以可执行路径指纹命名，sidecar 重装后自动失效重建。
        """
        if not getattr(sys, "frozen", False):
            return None
        exe = Path(sys.executable).resolve()
        if not exe.is_file():
            return None
        key = hashlib.sha256(str(exe).encode()).hexdigest()[:12]
        bindir = Path(tempfile.gettempdir()) / f"biscuitbot-py-{key}"
        if bindir.is_dir() and (bindir / "python3").exists():
            return str(bindir)
        try:
            bindir.mkdir(parents=True, exist_ok=True)
            if _IS_WINDOWS:
                for name in ("python.cmd", "python3.cmd"):
                    (bindir / name).write_text(
                        f'@echo off\r\n"{exe}" __biscuitbot_python__ %*\r\n',
                        encoding="utf-8",
                    )
                for name in ("pip.cmd", "pip3.cmd"):
                    (bindir / name).write_text(
                        f'@echo off\r\n"{exe}" __biscuitbot_python__ -m pip %*\r\n',
                        encoding="utf-8",
                    )
            else:
                exe_q = shlex.quote(str(exe))
                for name in ("python", "python3"):
                    (bindir / name).write_text(
                        f'#!/bin/sh\nexec {exe_q} __biscuitbot_python__ "$@"\n',
                        encoding="utf-8",
                    )
                for name in ("pip", "pip3"):
                    (bindir / name).write_text(
                        f'#!/bin/sh\nexec {exe_q} __biscuitbot_python__ -m pip "$@"\n',
                        encoding="utf-8",
                    )
            for shim in bindir.iterdir():
                shim.chmod(0o755)
        except OSError:
            logger.warning("无法创建 bundled python shim 目录：{}", bindir)
            return None
        return str(bindir)

    def _inject_venv_env(self, env: dict[str, str]) -> None:
        """开启 prefer_venv_python 且运行在 venv 时，注入 VIRTUAL_ENV 指向 venv 根。"""
        if not self.prefer_venv_python:
            return
        venv_bin = self._venv_bin_dir()
        if venv_bin:
            env["VIRTUAL_ENV"] = str(Path(venv_bin).parent)

    def _build_env(self) -> dict[str, str]:
        """为子进程构建最小环境变量集合。

        Unix 上仅传递 HOME/LANG/TERM；``bash -l`` 会 source 用户 profile 来
        设置 PATH 及其他必需变量。开启 ``prefer_venv_python`` 时额外注入
        VIRTUAL_ENV，配合 PATH 前置的 venv bin 目录让 python/pip 落到项目 venv。

        Windows 上 ``cmd.exe`` 没有 login-profile 机制，因此转发一组精选的
        系统变量（含 PATH）。API key 和其他密钥始终被排除。
        """
        if _IS_WINDOWS:
            sr = os.environ.get("SYSTEMROOT", r"C:\Windows")
            env = {
                "SYSTEMROOT": sr,
                "COMSPEC": os.environ.get("COMSPEC", f"{sr}\\system32\\cmd.exe"),
                "USERPROFILE": os.environ.get("USERPROFILE", ""),
                "HOMEDRIVE": os.environ.get("HOMEDRIVE", "C:"),
                "HOMEPATH": os.environ.get("HOMEPATH", "\\"),
                "TEMP": os.environ.get("TEMP", f"{sr}\\Temp"),
                "TMP": os.environ.get("TMP", f"{sr}\\Temp"),
                "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
                "PATH": os.environ.get("PATH", f"{sr}\\system32;{sr}"),
                "PYTHONUNBUFFERED": "1",
                "APPDATA": os.environ.get("APPDATA", ""),
                "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
                "ProgramData": os.environ.get("ProgramData", ""),
                "ProgramFiles": os.environ.get("ProgramFiles", ""),
                "ProgramFiles(x86)": os.environ.get("ProgramFiles(x86)", ""),
                "ProgramW6432": os.environ.get("ProgramW6432", ""),
            }
            self._inject_venv_env(env)
            for key in self.allowed_env_keys:
                val = os.environ.get(key)
                if val is not None:
                    env[key] = val
            return env
        home = os.environ.get("HOME") or os.path.expanduser("~")
        env = {
            "HOME": home,
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": os.environ.get("TERM", "dumb"),
            "PYTHONUNBUFFERED": "1",
        }
        self._inject_venv_env(env)
        for key in self.allowed_env_keys:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        return env

    def _guard_command(
        self,
        command: str,
        cwd: str,
        *,
        restrict_to_workspace: bool | None = None,
    ) -> str | None:
        """对潜在破坏性命令进行尽力而为的安全守卫检查。

        检查顺序：allow_patterns 优先 → deny_patterns → SSRF 内网 URL →
        工作区边界（路径穿越与绝对路径）。返回错误字符串或 None（通过）。
        """
        cmd = command.strip()
        lower = cmd.lower()

        # allow_patterns take priority over deny_patterns so that users can
        # exempt specific commands (e.g. "rm -rf" inside a build directory)
        # from the hardcoded deny list via configuration.
        explicitly_allowed = bool(self.allow_patterns) and any(
            re.search(p, lower) for p in self.allow_patterns
        )
        if not explicitly_allowed:
            for pattern in self.deny_patterns:
                if re.search(pattern, lower):
                    return f"Error: 命令被安全策略拦截（命中规则：{pattern}）。请改写命令避开该模式，或在配置 allow_patterns 中显式放行"

            if self.allow_patterns:
                return f"Error: 命令不在 allowlist 白名单内。当前白名单：{self.allow_patterns}。请调整命令以匹配白名单，或联系管理员修改配置"

        from biscuitbot.security.network import contains_internal_url
        if contains_internal_url(
            cmd,
            allow_loopback=current_scope_allows_loopback(
                enabled=self.webui_allow_local_service_access,
            ),
        ):
            # The runner turns this marker into a non-retryable security hint.
            return "Error: Command blocked by safety guard (internal/private URL detected)"

        should_restrict = self.restrict_to_workspace if restrict_to_workspace is None else restrict_to_workspace
        if should_restrict:
            if "..\\" in cmd or "../" in cmd:
                return (
                    "Error: Command blocked by safety guard (path traversal detected)"
                    + _WORKSPACE_BOUNDARY_NOTE
                )

            cwd_path = Path(cwd).resolve()

            for raw in self._extract_absolute_paths(cmd):
                try:
                    expanded = os.path.expandvars(raw.strip())
                    # Match against the un-resolved path first.  On Linux,
                    # /dev/stderr is a symlink to /proc/self/fd/2 and
                    # ``Path.resolve()`` would mask the device-file intent.
                    if self._is_benign_device_path(expanded):
                        continue
                    p = Path(expanded).expanduser().resolve()
                except Exception:
                    continue

                if self._is_benign_device_path(str(p)):
                    continue

                media_path = get_media_dir().resolve()
                if p.is_absolute() and not (
                    is_path_within(p, cwd_path)
                    or is_path_within(p, media_path)
                ):
                    return (
                        "Error: Command blocked by safety guard (path outside working dir)"
                        + _WORKSPACE_BOUNDARY_NOTE
                    )

        return None

    @classmethod
    def _is_benign_device_path(cls, path: str) -> bool:
        """判断是否为不应被工作区边界拦截的内核设备文件。"""
        if path in cls._BENIGN_DEVICE_PATHS:
            return True
        return path.startswith("/dev/fd/")

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        # Windows: match drive-root paths like `C:\` as well as `C:\path\to\file`, and UNC paths like `\\server\share`
        # NOTE: `*` is required so `C:\` (nothing after the slash) is still extracted.
        win_paths = re.findall(
            r"(?<![A-Za-z])(?:[A-Za-z]:[^\s\"'|><;]*|\\\\[^\s\"'|><;]+(?:\\[^\s\"'|><;]+)*)",
            command
        )
        posix_paths = re.findall(r"(?:^|[\s|>'\"])(/[^\s\"'>;|<]+)", command) # POSIX: /absolute only
        home_paths = re.findall(r"(?:^|[\s>'\"])(~[^\s\"'>;|<]*)", command) # POSIX/Windows home shortcut: ~
        return win_paths + posix_paths + home_paths
