"""CLI 命令模块。

所属模块与项目作用
===================
本文件位于 biscuitbot/cli 目录，是 CLI 模块的核心命令实现文件。
在项目架构中起到的作用：
- 定义 biscuitbot 命令行工具的所有子命令（onboard、agent、gateway、
  serve、desktop、status、channels、plugins 等）；
- 通过 typer 框架注册命令，提供帮助文本与参数解析；
- 承担交互式聊天、流式渲染、配置初始化、网关启动、定时任务注册等
  核心运行时逻辑；
- 统一管理日志格式、终端编码、输入历史与信号处理。
"""

import asyncio  # 异步事件循环与协程支持
import os  # 操作系统接口，用于环境变量与文件描述符判断
import select  # I/O 多路复用，用于清空待读 TTY 输入
import signal  # 信号处理，注册 SIGINT/SIGTERM/SIGHUP 等
import sys  # 系统相关接口，访问 stdin/stdout 与平台信息
from contextlib import nullcontext, suppress  # 上下文管理器工具
from pathlib import Path  # 路径处理
from typing import Any, Literal, cast  # 类型提示工具

# 强制 Windows 控制台使用 UTF-8 编码
if sys.platform == "win32":
    if sys.stdout.encoding != "utf-8":
        os.environ["PYTHONIOENCODING"] = "utf-8"
        # 以 UTF-8 编码重新打开 stdout/stderr
        with suppress(Exception):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

# 在导入 CLI UI/日志库之前完成控制台编码设置。
import typer  # noqa: E402  # 命令行框架，定义命令与选项
from loguru import logger  # noqa: E402  # 日志库

# 移除默认处理器，并以统一的 biscuitbot 格式重新添加
logger.remove()
_log_handler_id = logger.add(
    sys.stderr,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <5}</level> | "
        "<cyan>{extra[channel]}</cyan> | "
        "<level>{message}</level>"
    ),
    level="INFO",
    colorize=None,
    filter=lambda record: record["extra"].setdefault("channel", "-") or True,
)

from prompt_toolkit import PromptSession, print_formatted_text  # noqa: E402  # 交互式输入与会话
from prompt_toolkit.application import run_in_terminal  # noqa: E402  # 在终端中运行同步函数
from prompt_toolkit.formatted_text import ANSI, HTML  # noqa: E402  # 格式化文本
from prompt_toolkit.history import FileHistory  # noqa: E402  # 文件持久化输入历史
from prompt_toolkit.patch_stdout import patch_stdout  # noqa: E402  # 修补 stdout 与 prompt 冲突
from rich.console import Console  # noqa: E402  # Rich 终端控制台
from rich.markdown import Markdown  # noqa: E402  # Markdown 渲染
from rich.table import Table  # noqa: E402  # 表格渲染
from rich.text import Text  # noqa: E402  # 纯文本渲染

from biscuitbot import __logo__, __version__  # noqa: E402  # 项目 logo 与版本号
from biscuitbot.agent.loop import AgentLoop  # noqa: E402  # 智能体主循环
from biscuitbot.cli.stream import StreamRenderer, ThinkingSpinner  # noqa: E402  # 流式渲染与思考 spinner
from biscuitbot.config.paths import get_workspace_path, is_default_workspace  # noqa: E402  # 工作区路径工具
from biscuitbot.config.schema import Config  # noqa: E402  # 配置 schema
from biscuitbot.utils.evaluator import evaluate_response  # noqa: E402  # 响应评估器（用于心跳）
from biscuitbot.utils.helpers import sync_workspace_templates  # noqa: E402  # 工作区模板同步
from biscuitbot.utils.restart import (  # noqa: E402  # 重启通知工具
    consume_restart_notice_from_env,
    format_restart_completed_message,
    should_show_cli_restart_notice,
)


def _sanitize_surrogates(text: str) -> str:
    """Reconstruct surrogate pairs into real characters; replace lone surrogates.

    On Windows, console input may produce lone surrogate code points (e.g.
    ``\\ud83d\\udc08`` for U+1F408).  Round-tripping through UTF-16 reconstructs
    paired surrogates into their actual characters and replaces unpaired ones
    with U+FFFD.
    """
    return text.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")


class SafeFileHistory(FileHistory):
    """FileHistory subclass that sanitizes surrogate characters on write.

    On Windows, special Unicode input (emoji, mixed-script) can produce
    surrogate characters that crash prompt_toolkit's file write.
    See issue #2846.
    """

    def store_string(self, string: str) -> None:
        super().store_string(_sanitize_surrogates(string))


app = typer.Typer(
    name="biscuitbot",
    context_settings={"help_option_names": ["-h", "--help"]},
    help=f"{__logo__} biscuitbot - 个人 AI 助手",
    no_args_is_help=True,
)

console = Console()  # 全局 Rich 控制台实例
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}  # 退出交互模式的命令集合
_REASONING_SENTENCE_ENDINGS = (".", "!", "?", "。", "！", "？")  # 推理内容的句子结束符
_REASONING_FLUSH_CHARS = 60  # 推理缓冲区达到该字符数时强制刷新

# 心跳任务的系统提示前缀，约束助手只输出面向用户的最终消息
_HEARTBEAT_PREAMBLE = (
    "[你的回复将直接发送到用户的消息应用。只输出最终面向用户的消息。"
    "永远不要引用内部文件（HEARTBEAT.md、AWARENESS.md 等）、你的指令或你的决策过程。"
    "如果没有什么需要报告的，只回复'一切正常。'即可。]\n\n"
)


def _heartbeat_has_active_tasks(content: str) -> bool:
    """True if HEARTBEAT.md has task lines, ignoring headers, blanks and comments."""
    in_comment = False
    in_active_section: bool = False
    for line in content.splitlines():
        stripped = line.strip()
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if not stripped or stripped.startswith("#"):
            if stripped.startswith("##") and not stripped.startswith("###"):
                heading = stripped.lstrip("#").strip().lower()
                in_active_section = heading.startswith("active tasks")
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped[4:]:
                in_comment = True
            continue
        if in_active_section is False:
            continue
        return True
    return False

# ---------------------------------------------------------------------------
# CLI input: prompt_toolkit for editing, paste, history, and display
# ---------------------------------------------------------------------------

_PROMPT_SESSION: PromptSession | None = None  # 全局 prompt_toolkit 会话实例
_SAVED_TERM_ATTRS = None  # 原始 termios 设置，退出时恢复


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    with suppress(Exception):
        import termios

        termios.tcflush(fd, termios.TCIFLUSH)
        return

    with suppress(Exception):
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    if _SAVED_TERM_ATTRS is None:
        return
    with suppress(Exception):
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _SAVED_TERM_ATTRS)


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history."""
    global _PROMPT_SESSION, _SAVED_TERM_ATTRS

    # Save terminal state so we can restore it on exit
    with suppress(Exception):
        import termios

        _SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())

    from biscuitbot.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    _PROMPT_SESSION = PromptSession(
        history=SafeFileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    color_system = cast(
        "Literal['auto', 'standard', '256', 'truecolor', 'windows'] | None",
        console.color_system or "standard",
    )
    ansi_console = Console(
        force_terminal=sys.stdout.isatty(),
        color_system=color_system,
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
    show_header: bool = True,
) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata)
    if show_header:
        console.print()
        console.print(f"[cyan]{__logo__} biscuitbot[/cyan]")
    console.print(body)
    console.print()


def _response_renderable(content: str, render_markdown: bool, metadata: dict | None = None):
    """Render plain-text command output without markdown collapsing newlines."""
    if not render_markdown:
        return Text(content)
    if (metadata or {}).get("render_as") == "text":
        return Text(content)
    return Markdown(content)


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} biscuitbot[/cyan]"),
                c.print(_response_renderable(content, render_markdown, metadata)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


def _print_cli_progress_line(text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None) -> None:
    """Print a CLI progress line, pausing the spinner if needed."""
    if not text.strip():
        return
    target = renderer.console if renderer else console
    pause = renderer.pause_spinner() if renderer else (thinking.pause() if thinking else nullcontext())
    with pause:
        if renderer:
            renderer.ensure_header()
        target.print(f"  [dim]↳ {text}[/dim]")


class _ReasoningBuffer:
    """推理内容缓冲区，按句子或字符数阈值刷新输出。"""

    def __init__(self) -> None:
        self._text = ""  # 累积的推理文本

    def add(self, text: str) -> str | None:
        """追加推理文本，满足刷新条件时返回待输出内容。"""
        if not text:
            return None
        self._text += text
        if self._should_flush(text):
            return self.flush()
        return None

    def flush(self) -> str | None:
        """清空并返回缓冲区内容（去除首尾空白）。"""
        text = self._text.strip()
        self._text = ""
        return text or None

    def clear(self) -> None:
        """清空缓冲区。"""
        self._text = ""

    def _should_flush(self, text: str) -> bool:
        """判断是否应该刷新：遇到换行、句子结束符或达到字符阈值。"""
        stripped = text.rstrip()
        return (
            "\n" in text
            or stripped.endswith(_REASONING_SENTENCE_ENDINGS)
            or len(self._text) >= _REASONING_FLUSH_CHARS
        )


def _print_cli_reasoning(text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None) -> None:
    """Print reasoning/thinking content in a distinct style."""
    if not text.strip():
        return
    target = renderer.console if renderer else console
    pause = renderer.pause_spinner() if renderer else (thinking.pause() if thinking else nullcontext())
    with pause:
        if renderer:
            renderer.ensure_header()
        target.print(f"[dim italic]✻ {text}[/dim italic]")


def _flush_cli_reasoning(
    reasoning_buffer: _ReasoningBuffer,
    thinking: ThinkingSpinner | None,
    renderer: StreamRenderer | None = None,
) -> None:
    text = reasoning_buffer.flush()
    if text:
        _print_cli_reasoning(text, thinking, renderer)


async def _print_interactive_progress_line(text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None) -> None:
    """Print an interactive progress line, pausing the spinner if needed."""
    if not text.strip():
        return
    if renderer:
        with renderer.pause_spinner():
            renderer.ensure_header()
            renderer.console.print(f"  [dim]↳ {text}[/dim]")
    else:
        with thinking.pause() if thinking else nullcontext():
            await _print_interactive_line(text)


async def _maybe_print_interactive_progress(
    msg: Any,
    thinking: ThinkingSpinner | None,
    channels_config: Any,
    renderer: StreamRenderer | None = None,
    reasoning_buffer: _ReasoningBuffer | None = None,
) -> bool:
    metadata = msg.metadata or {}
    if metadata.get("_retry_wait"):
        await _print_interactive_progress_line(msg.content, thinking, renderer)
        return True

    if not metadata.get("_progress"):
        return False

    reasoning_buffer = reasoning_buffer or _ReasoningBuffer()

    if metadata.get("_reasoning_end"):
        if channels_config and not channels_config.show_reasoning:
            reasoning_buffer.clear()
        else:
            _flush_cli_reasoning(reasoning_buffer, thinking, renderer)
        return True

    is_tool_hint = metadata.get("_tool_hint", False)
    is_reasoning = metadata.get("_reasoning", False) or metadata.get("_reasoning_delta", False)
    if is_reasoning:
        if channels_config and not channels_config.show_reasoning:
            reasoning_buffer.clear()
            return True
        text = reasoning_buffer.add(msg.content)
        if text:
            _print_cli_reasoning(text, thinking, renderer)
        return True
    if channels_config and is_tool_hint and not channels_config.send_tool_hints:
        return True
    if channels_config and not is_tool_hint and not channels_config.send_progress:
        return True

    await _print_interactive_progress_line(msg.content, thinking, renderer)
    return True


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)
    """
    if _PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with patch_stdout():
            return await _PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>你：</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} biscuitbot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """biscuitbot - 个人 AI 助手。"""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


def _has_any_api_key(config) -> bool:
    """Check if any provider has an API key configured."""
    from biscuitbot.providers.registry import PROVIDERS

    for spec in PROVIDERS:
        if spec.is_direct or spec.is_local or spec.is_oauth:
            continue
        pc = getattr(config.providers, spec.name, None)
        if pc and pc.api_key:
            return True
    return False


def _run_quick_setup(config, config_path: Path) -> None:
    """Guided quick setup: pick provider → enter API key → select model → done.

    Runs automatically when onboard detects no API keys configured.
    Uses questionary for interactive prompts; falls back gracefully if unavailable.
    """
    try:
        import questionary
    except ImportError:
        console.print("[yellow]![/yellow] 快速安装需要 'questionary'。请安装：")
        console.print("  pip install questionary")
        console.print(f"\n  或手动编辑：[cyan]{config_path}[/cyan]")
        return

    # 非交互式环境（如 pytest CliRunner、CI、管道输入）下跳过交互式引导
    # questionary/prompt_toolkit 在无真实 console 时会抛 NoConsoleScreenBufferError
    import sys
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        console.print(f"\n  [dim]非交互式环境，请手动编辑配置: {config_path}[/dim]")
        console.print("  [dim]或附加 --wizard 在真实终端中运行[/dim]")
        return

    from biscuitbot.providers.registry import PROVIDERS
    from biscuitbot.config.loader import save_config
    from rich.align import Align

    # --- Step 0: Welcome ---
    console.print()
    console.print(Align.center(f"{__logo__} [bold cyan]biscuitbot 快速安装[/bold cyan]"))
    console.print(Align.center("[dim]3 步开始使用[/dim]"))
    console.print()

    # Build provider choices (skip gateway-only, oauth, direct, local providers)
    provider_choices = []
    for spec in PROVIDERS:
        if spec.is_direct or spec.is_local or spec.is_oauth or spec.is_transcription_only:
            continue
        label = spec.display_name or spec.name.title()
        provider_choices.append((spec.name, label))

    if not provider_choices:
        console.print("[red]没有可用的 LLM 提供商。[/red]")
        return

    # --- Step 1: Select Provider ---
    selected_name = questionary.select(
        "步骤 1/3 — 选择 LLM 提供商：",
        choices=[label for _, label in provider_choices],
        default=provider_choices[0][1] if provider_choices else None,
    ).ask()

    if not selected_name:
        console.print("[yellow]安装已取消。[/yellow]")
        return

    # Map display name back to internal name
    provider_name = next(name for name, label in provider_choices if label == selected_name)
    spec = next(s for s in PROVIDERS if s.name == provider_name)

    # --- Step 2: Enter API Key ---
    key_urls = {
        "deepseek": "https://platform.deepseek.com/api_keys",
        "openai": "https://platform.openai.com/api-keys",
        "anthropic": "https://console.anthropic.com/settings/keys",
        "dashscope": "https://dashscope.console.aliyun.com/apiKey",
        "zhipu": "https://open.bigmodel.cn/usercenter/apikeys",
        "moonshot": "https://platform.moonshot.cn/console/api-keys",
        "stepfun": "https://platform.stepfun.com/console/apikey",
    }
    url = key_urls.get(provider_name, "")
    if url:
        console.print(f"  [dim]获取密钥：{url}[/dim]")

    api_key = questionary.password("步骤 2/3 — 输入 API 密钥：").ask()
    if not api_key:
        console.print("[yellow]安装已取消。[/yellow]")
        return

    # Apply API key and base URL to config
    provider_cfg = getattr(config.providers, provider_name, None)
    if provider_cfg is not None:
        provider_cfg.api_key = api_key
        if spec.default_api_base and not provider_cfg.api_base:
            provider_cfg.api_base = spec.default_api_base

    # --- Step 3: Select Model (with sensible defaults per provider) ---
    # 随各服务商最新模型轮换更新（2026-08）：deepseek-chat 退役 → v4-flash；
    # gpt-4o-mini 退役 → gpt-5.6-terra；claude-sonnet-4 → claude-sonnet-5；
    # qwen-plus/qwen-max → qwen3.6-plus；glm-4-plus → glm-4.5-flash；
    # kimi-k2-preview → kimi-k3；step-1/2 → step-3.7-flash。
    model_defaults = {
        "deepseek": ("deepseek/deepseek-v4-pro", [
            "deepseek/deepseek-v4-pro (推荐，综合能力最强)",
            "deepseek/deepseek-v4-flash (轻量快速)",
        ]),
        "openai": ("openai/gpt-5.6-terra", [
            "openai/gpt-5.6-terra (推荐，日常对话)",
            "openai/gpt-5.6-sol (旗舰推理)",
            "openai/gpt-5.6-luna (高性价比)",
        ]),
        "anthropic": ("anthropic/claude-sonnet-5", [
            "anthropic/claude-sonnet-5 (推荐)",
            "anthropic/claude-opus-5 (最强)",
            "anthropic/claude-haiku-4-5 (经济)",
        ]),
        "dashscope": ("dashscope/qwen3.6-plus", [
            "dashscope/qwen3.6-plus (通义千问均衡推荐)",
            "dashscope/qwen3.7-max (旗舰)",
            "dashscope/qwen3.6-flash (经济)",
        ]),
        "zhipu": ("zhipu/glm-4.5-flash", [
            "zhipu/glm-4.5-flash (免费推荐)",
            "zhipu/glm-5.2 (旗舰)",
        ]),
        "moonshot": ("moonshot/kimi-k3", [
            "moonshot/kimi-k3 (Kimi K3 推荐)",
        ]),
        "stepfun": ("stepfun/step-3.7-flash", [
            "stepfun/step-3.7-flash (Step 推荐)",
        ]),
    }

    default_model, model_choices = model_defaults.get(provider_name, (f"{provider_name}/default", [f"{provider_name}/default"]))

    selected_model = questionary.select(
        f"步骤 3/3 — 选择模型 ({selected_name}):",
        choices=model_choices,
        default=model_choices[0],
    ).ask()

    if not selected_model:
        selected_model = default_model

    # Extract just the model identifier (strip the description in parentheses)
    model_id = selected_model.split(" (")[0].strip()

    # Apply model and provider to agent defaults
    config.agents.defaults.model = model_id
    config.agents.defaults.provider = provider_name

    # Auto-fill context window for known models
    _context_hints = {
        "deepseek/deepseek-v4-pro": 65536,
        "deepseek/deepseek-r1": 65536,
        "openai/gpt-4o": 128000,
        "openai/o3": 200000,
        "anthropic/claude-sonnet-4": 200000,
        "anthropic/claude-opus-4-5": 200000,
        "dashscope/qwen-max": 131072,
        "zhipu/glm-4-plus": 128000,
        "moonshot/kimi-k2-0711-preview": 131072,
        "stepfun/step-2-16k": 131072,
    }
    ctx = _context_hints.get(model_id)
    if ctx:
        config.agents.defaults.context_window_tokens = ctx

    # Save
    save_config(config, config_path)

    # --- Done ---
    console.print()
    console.print(Align.center("[bold green]✓ 安装完成！[/bold green]"))
    console.print()
    console.print(f"  提供商：[cyan]{selected_name}[/cyan]")
    console.print(f"  模型：[cyan]{model_id}[/cyan]")
    console.print(f"  配置：[cyan]{config_path}[/cyan]")
    console.print()
    console.print("  现在可以运行：")
    console.print("    [green]biscuitbot agent -m \"Hello!\"[/green]")
    console.print("    [green]biscuitbot gateway[/green]")
    console.print()
    console.print("  更多选项（频道、预设、工具），请运行：")
    console.print("    [dim]biscuitbot onboard --wizard[/dim]")
    console.print()


@app.command()
def onboard(
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    config_file: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    wizard: bool = typer.Option(False, "--wizard", help="使用交互式引导"),
):
    """初始化 biscuitbot 配置和工作区。"""
    from biscuitbot.config.loader import get_config_path, load_config, save_config, set_config_path
    from biscuitbot.config.schema import Config

    if config_file:
        config_path = Path(config_file).expanduser().resolve()
        set_config_path(config_path)
        console.print(f"[dim]使用配置：{config_path}[/dim]")
    else:
        config_path = get_config_path()

    def _apply_workspace_override(loaded: Config) -> Config:
        if workspace:
            loaded.agents.defaults.workspace = workspace
        return loaded

    # Create or update config
    if config_path.exists():
        if wizard:
            config = _apply_workspace_override(load_config(config_path))
        else:
            console.print(f"[yellow]配置文件已存在于 {config_path}[/yellow]")
            console.print(
                "  [bold]y[/bold] = 使用默认值覆盖（已有值将丢失）"
            )
            console.print(
                "  [bold]N[/bold] = 刷新配置，保留已有值并添加新字段"
            )
            if typer.confirm("是否覆盖？"):
                config = _apply_workspace_override(Config())
                save_config(config, config_path)
                console.print(f"[green]✓[/green] 配置已重置为默认值，位于 {config_path}")
            else:
                config = _apply_workspace_override(load_config(config_path))
                save_config(config, config_path)
                console.print(
                    f"[green]✓[/green] 配置已刷新，位于 {config_path}（已有值已保留）"
                )
    else:
        config = _apply_workspace_override(Config())
        # In wizard mode, don't save yet - the wizard will handle saving if should_save=True
        if not wizard:
            save_config(config, config_path)
            console.print(f"[green]✓[/green] 配置已创建，位于 {config_path}")

    # Run interactive wizard if enabled
    if wizard:
        from biscuitbot.cli.onboard import run_onboard

        try:
            result = run_onboard(initial_config=config)
            if not result.should_save:
                console.print("[yellow]配置已丢弃，未保存任何更改。[/yellow]")
                return

            config = result.config
            save_config(config, config_path)
            console.print(f"[green]✓[/green] 配置已保存，位于 {config_path}")
        except Exception as e:
            console.print(f"[red]✗[/red] 配置过程中出错：{e}")
            console.print("[yellow]请再次运行 'biscuitbot onboard' 完成安装。[/yellow]")
            raise typer.Exit(1)

    # Quick setup: when no API key is configured, guide user through essential steps
    if not _has_any_api_key(config):
        _run_quick_setup(config, config_path)
    _onboard_plugins(config_path)

    # Create workspace, preferring the configured workspace path.
    workspace_path = get_workspace_path(config.workspace_path)
    if not workspace_path.exists():
        workspace_path.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]✓[/green] 工作区已创建，位于 {workspace_path}")

    sync_workspace_templates(workspace_path)

    agent_cmd = 'biscuitbot agent -m "Hello!"'
    gateway_cmd = "biscuitbot gateway"
    if config:
        agent_cmd += f" --config {config_path}"
        gateway_cmd += f" --config {config_path}"

    console.print(f"\n{__logo__} biscuitbot 已就绪！")
    if _has_any_api_key(config):
        console.print("\n后续步骤：")
        console.print(f"  1. 聊天：     [cyan]{agent_cmd}[/cyan]")
        console.print(f"  2. 网关：  [cyan]{gateway_cmd}[/cyan]")
        console.print("  3. 高级设置：[cyan]biscuitbot onboard --wizard[/cyan]")
    elif wizard:
        console.print("\n后续步骤：")
        console.print(f"  1. 聊天：[cyan]{agent_cmd}[/cyan]")
        console.print(f"  2. 启动网关：[cyan]{gateway_cmd}[/cyan]")
    else:
        console.print("\n后续步骤：")
        console.print(f"  1. 将 API 密钥添加到 [cyan]{config_path}[/cyan]")
        console.print("     DeepSeek:   https://platform.deepseek.com/api_keys")
        console.print("     OpenAI:      https://platform.openai.com/api-keys")
        console.print("     Anthropic:   https://console.anthropic.com/settings/keys")
        console.print("     DashScope:   https://dashscope.console.aliyun.com/apiKey")
        console.print("     Zhipu (智谱): https://open.bigmodel.cn/usercenter/apikeys")
        console.print(f"  2. 聊天：[cyan]{agent_cmd}[/cyan]")
    console.print(
        "\n[dim]Docs: https://github.com/biscuitbot/biscuitbot[/dim]"
    )


def _merge_missing_defaults(existing: Any, defaults: Any) -> Any:
    """Recursively fill in missing values from defaults without overwriting user config."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return existing

    merged = dict(existing)
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = _merge_missing_defaults(merged[key], value)
    return merged


def _onboard_plugins(config_path: Path) -> None:
    """Inject default config for all discovered channels (built-in + plugins)."""
    from biscuitbot.channels.registry import discover_all
    from biscuitbot.config.loader import _load_config_file

    all_channels = discover_all()
    if not all_channels:
        return

    data = _load_config_file(config_path)

    channels = data.setdefault("channels", {})
    for name, cls in all_channels.items():
        if name not in channels:
            channels[name] = cls.default_config()
        else:
            channels[name] = _merge_missing_defaults(channels[name], cls.default_config())

    from biscuitbot.config.loader import save_config

    # Re-validate through the Config model to get proper serialization
    from biscuitbot.config.schema import Config

    config = Config.model_validate(data)
    save_config(config, config_path)


def _model_display(config: Config) -> tuple[str, str]:
    """Return (resolved_model_name, preset_tag) for display strings."""
    resolved = config.resolve_preset()
    name = config.agents.defaults.model_preset
    tag = f" (preset: {name})" if name else ""
    return resolved.model, tag


def _load_runtime_config(config: str | None = None, workspace: str | None = None) -> Config:
    """Load config and optionally override the active workspace."""
    from biscuitbot.config.loader import load_config, resolve_config_env_vars, set_config_path

    config_path = None
    if config:
        config_path = Path(config).expanduser().resolve()
        if not config_path.exists():
            console.print(f"[red]错误：配置文件未找到：{config_path}[/red]")
            raise typer.Exit(1)
        set_config_path(config_path)
        console.print(f"[dim]使用配置：{config_path}[/dim]")

    try:
        loaded = resolve_config_env_vars(load_config(config_path))
    except ValueError as e:
        console.print(f"[red]错误：{e}[/red]")
        raise typer.Exit(1)
    _warn_deprecated_config_keys(config_path)
    if workspace:
        loaded.agents.defaults.workspace = workspace
    return loaded


def _warn_deprecated_config_keys(config_path: Path | None) -> None:
    """Hint users to remove obsolete keys from their config file."""
    import json

    from biscuitbot.config.loader import get_config_path

    path = config_path or get_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to read config for memoryWindow migration check", exc_info=True)
        return
    if "memoryWindow" in raw.get("agents", {}).get("defaults", {}):
        console.print(
            "[dim]提示：配置中的 `memoryWindow` 已不再使用，可以安全删除。[/dim]"
        )


def _migrate_cron_store(config: "Config") -> None:
    """One-time migration: move legacy global cron store into the workspace."""
    from biscuitbot.config.paths import get_cron_dir

    legacy_path = get_cron_dir() / "jobs.json"
    new_path = config.workspace_path / "cron" / "jobs.json"
    if legacy_path.is_file() and not new_path.exists():
        new_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.move(str(legacy_path), str(new_path))


# ============================================================================
# OpenAI-Compatible API Server
# ============================================================================


@app.command()
def serve(
    port: int | None = typer.Option(None, "--port", "-p", help="API 服务器端口"),
    host: str | None = typer.Option(None, "--host", "-H", help="绑定地址"),
    timeout: float | None = typer.Option(None, "--timeout", "-t", help="单请求超时时间（秒）"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示 biscuitbot 运行时日志"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    config_file: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
):
    """启动 OpenAI 兼容 API 服务器 (/v1/chat/completions)。"""
    try:
        from aiohttp import web  # noqa: F401
    except ImportError:
        console.print("[red]需要 aiohttp。请安装：pip install 'biscuitbot[api]'[/red]")
        raise typer.Exit(1)

    from loguru import logger

    from biscuitbot.api.server import create_app
    from biscuitbot.bus.queue import MessageBus
    from biscuitbot.providers.image_generation import image_gen_provider_configs
    from biscuitbot.session.manager import SessionManager

    if verbose:
        logger.enable("biscuitbot")
    else:
        logger.disable("biscuitbot")

    runtime_config = _load_runtime_config(config_file, workspace)
    api_cfg = runtime_config.api
    host = host if host is not None else api_cfg.host
    port = port if port is not None else api_cfg.port
    timeout = timeout if timeout is not None else api_cfg.timeout
    sync_workspace_templates(runtime_config.workspace_path)
    bus = MessageBus()
    session_manager = SessionManager(runtime_config.workspace_path)
    try:
        agent_loop = AgentLoop.from_config(
            runtime_config, bus,
            session_manager=session_manager,
            image_generation_provider_configs=image_gen_provider_configs(runtime_config),
        )
    except ValueError as exc:
        console.print(f"[red]错误：{exc}[/red]")
        raise typer.Exit(1) from exc

    model_name, preset_tag = _model_display(runtime_config)
    console.print(f"{__logo__} 正在启动 OpenAI 兼容 API 服务器")
    console.print(f"  [cyan]端点[/cyan] : http://{host}:{port}/v1/chat/completions")
    console.print(f"  [cyan]模型[/cyan]    : {model_name}{preset_tag}")
    console.print("  [cyan]会话[/cyan]  : api:default")
    console.print(f"  [cyan]超时[/cyan]  : {timeout}s")
    if host in {"0.0.0.0", "::"}:
        console.print(
            "[yellow]警告：[/yellow]API 绑定到所有网络接口。请确保仅在受信任的网络边界、防火墙或反向代理后使用。"
        )
    console.print()

    api_app = create_app(agent_loop, model_name=model_name, request_timeout=timeout)

    async def on_startup(_app):
        await agent_loop._connect_mcp()

    async def on_cleanup(_app):
        await agent_loop.close_mcp()

    api_app.on_startup.append(on_startup)
    api_app.on_cleanup.append(on_cleanup)

    web.run_app(api_app, host=host, port=port, print=lambda msg: logger.info(msg))


# ============================================================================
# Gateway / Server
# ============================================================================


@app.command()
def gateway(
    port: int | None = typer.Option(None, "--port", "-p", help="网关端口"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="详细输出"),
    config_file: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
):
    """启动 biscuitbot 网关。"""
    if verbose:
        logger.remove(_log_handler_id)
        logger.add(
            sys.stderr,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <5}</level> | "
                "<cyan>{extra[channel]}</cyan> | "
                "<level>{message}</level>"
            ),
            level="DEBUG",
            colorize=None,
            filter=lambda record: record["extra"].setdefault("channel", "-") or True,
        )
    cfg = _load_runtime_config(config_file, workspace)
    # WebUI 由 websocket 频道提供，使用其端口而非 gateway 端口
    ws_cfg = getattr(cfg.channels, "websocket", None) or {}
    ws_enabled = ws_cfg.get("enabled", False) if isinstance(ws_cfg, dict) else getattr(ws_cfg, "enabled", False)
    ws_port = ws_cfg.get("port", 8765) if isinstance(ws_cfg, dict) else getattr(ws_cfg, "port", 8765)
    ws_host = ws_cfg.get("host", None) if isinstance(ws_cfg, dict) else getattr(ws_cfg, "host", None)
    webui_host = ws_host or cfg.gateway.host or "127.0.0.1"
    open_url = f"http://{webui_host}:{ws_port}/" if ws_enabled else None
    _run_gateway(cfg, port=port, open_browser_url=open_url)


def _run_gateway(
    config: Config,
    *,
    port: int | None = None,
    open_browser_url: str | None = None,
    webui_static_dist: bool = True,
    webui_runtime_surface: str = "browser",
    webui_runtime_capabilities: dict[str, Any] | None = None,
    health_server_enabled: bool = True,
    allow_unconfigured_provider: bool = False,
) -> None:
    """Shared gateway runtime; ``open_browser_url`` opens a tab once channels are up.

    ``allow_unconfigured_provider`` 为桌面端首启而设：未配置任何 LLM API Key
    时仍以占位 Provider 启动，以便网关先承载欢迎设置页；配置写入后对话层
    会自动热替换为真实 Provider。普通 ``biscuitbot gateway`` 保持快速失败。
    """
    from biscuitbot.agent.tools.message import MessageTool
    from biscuitbot.bus.queue import MessageBus
    from biscuitbot.bus.runtime_events import RuntimeEventBus
    from biscuitbot.channels.manager import ChannelManager
    from biscuitbot.cron.bound_runner import run_bound_cron_job
    from biscuitbot.cron.service import CronJobSkippedError, CronService
    from biscuitbot.cron.session_turns import is_bound_cron_job
    from biscuitbot.cron.types import CronJob
    from biscuitbot.providers.factory import (
        load_provider_snapshot,
        resolve_provider_snapshot,
    )
    from biscuitbot.providers.image_generation import image_gen_provider_configs
    from biscuitbot.session.manager import SessionManager
    from biscuitbot.session.webui_turns import WebuiTurnCoordinator
    from biscuitbot.webui.token_usage import TokenUsageHook

    port = port if port is not None else config.gateway.port

    console.print(f"{__logo__} 正在启动 biscuitbot 网关，版本 {__version__} 端口 {port}...")
    if open_browser_url:
        console.print(f"  WebUI 地址：[cyan]{open_browser_url}[/cyan]")
    sync_workspace_templates(config.workspace_path)
    bus = MessageBus()
    runtime_events = RuntimeEventBus()
    try:
        provider_snapshot = resolve_provider_snapshot(
            config, allow_unconfigured=allow_unconfigured_provider
        )
    except ValueError as exc:
        console.print(f"[red]错误：{exc}[/red]")
        raise typer.Exit(1) from exc
    session_manager = SessionManager(config.workspace_path)

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    agent = AgentLoop.from_config(
        config, bus,
        provider=provider_snapshot.provider,
        model=provider_snapshot.model,
        context_window_tokens=provider_snapshot.context_window_tokens,
        cron_service=cron,
        session_manager=session_manager,
        image_generation_provider_configs=image_gen_provider_configs(config),
        provider_snapshot_loader=load_provider_snapshot,
        runtime_events=runtime_events,
        provider_signature=provider_snapshot.signature,
        hooks=[TokenUsageHook(
            timezone_name=config.agents.defaults.timezone,
            session_manager=session_manager,
        )],
    )
    WebuiTurnCoordinator(
        bus=bus,
        sessions=session_manager,
        schedule_background=lambda coro: agent._schedule_background(coro),
    ).subscribe(runtime_events)

    # CLI 端口全链路追踪日志：彩色树形输出到 stderr
    from biscuitbot.bus.trace_logger import install_trace_logger
    install_trace_logger(runtime_events)

    from biscuitbot.bus.events import OutboundMessage
    from biscuitbot.session.keys import session_key_for_channel

    def _channel_session_key(channel: str, chat_id: str) -> str:
        return session_key_for_channel(
            channel,
            chat_id,
            unified_session=config.agents.defaults.unified_session,
        )

    async def _deliver_to_channel(
        msg: OutboundMessage, *, record: bool = False, session_key: str | None = None,
    ) -> None:
        """Publish a user-visible message and mirror it into that channel's session."""
        metadata = dict(msg.metadata or {})
        record = record or bool(metadata.pop("_record_channel_delivery", False))
        if metadata != (msg.metadata or {}):
            msg = OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=msg.content,
                reply_to=msg.reply_to,
                media=msg.media,
                metadata=metadata,
                buttons=msg.buttons,
            )
        if (
            record
            and msg.channel != "cli"
            and msg.content.strip()
            and hasattr(session_manager, "get_or_create")
            and hasattr(session_manager, "save")
        ):
            key = session_key or _channel_session_key(msg.channel, msg.chat_id)
            session = session_manager.get_or_create(key)
            extra: dict[str, Any] = {"_channel_delivery": True}
            if msg.media:
                extra["media"] = list(msg.media)
            session.add_message("assistant", msg.content, **extra)
            session_manager.save(session)
        await bus.publish_outbound(msg)

    message_tool = getattr(agent, "tools", {}).get("message")
    if isinstance(message_tool, MessageTool):
        message_tool.set_send_callback(_deliver_to_channel)

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        async def _silent(*_args, **_kwargs):
            pass

        # Dream is an internal job — run directly, not through the agent loop.
        if job.name == "dream":
            from biscuitbot.agent.memory import MemoryStore

            dream_session_key = MemoryStore.dream_session_key
            build_dream_commit_message = MemoryStore.build_dream_commit_message
            prune_dream_sessions = MemoryStore.prune_dream_sessions

            store = agent.context.memory
            last_resp = None
            batches_processed = 0
            from biscuitbot.command.builtin import _DREAM_BATCH_TIMEOUT_S, _DREAM_MAX_BATCHES
            try:
                while batches_processed < _DREAM_MAX_BATCHES:
                    result = store.build_dream_prompt()
                    if result is None:
                        break
                    prompt, last_cursor = result
                    key = dream_session_key()
                    try:
                        resp = await asyncio.wait_for(
                            agent.process_direct(
                                prompt,
                                session_key=key,
                                ephemeral=True,
                                tools=store.build_dream_tools(),
                                on_progress=_silent,
                            ),
                            timeout=_DREAM_BATCH_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Dream cron batch {} timed out after {:.0f}s; aborting remaining batches",
                            batches_processed + 1,
                            _DREAM_BATCH_TIMEOUT_S,
                        )
                        break
                    last_resp = resp
                    if not MemoryStore.dream_run_completed(resp):
                        logger.warning(
                            "Dream cron job did not complete (batch {}); cursor remains at {}",
                            batches_processed + 1,
                            store.get_last_dream_cursor(),
                        )
                        break
                    store.set_last_dream_cursor(last_cursor)
                    batches_processed += 1
                if batches_processed == 0:
                    logger.info("Dream: nothing to process")
                else:
                    remaining = store.count_unprocessed_history()
                    logger.info(
                        "Dream cron job completed: {} batch(es), cursor at {}, {} entries pending",
                        batches_processed,
                        store.get_last_dream_cursor(),
                        remaining,
                    )
            except Exception:
                logger.exception("Dream cron job failed")
            finally:
                from biscuitbot.webui.token_usage import record_response_token_usage

                record_response_token_usage(
                    last_resp,
                    source="dream",
                    timezone_name=config.agents.defaults.timezone,
                )
                if store.git.is_initialized():
                    msg = build_dream_commit_message(
                        "dream: periodic memory consolidation", last_resp,
                    )
                    sha = store.git.auto_commit(msg)
                    if sha:
                        logger.info("Dream commit: {}", sha)
                store.compact_history()
                prune_dream_sessions(agent.sessions.sessions_dir)
            return None

        # Heartbeat is a system job that checks HEARTBEAT.md for active tasks.
        if job.name == "heartbeat":
            heartbeat_file = config.workspace_path / "HEARTBEAT.md"
            try:
                content = heartbeat_file.read_text(encoding="utf-8")
            except OSError:
                logger.debug("Heartbeat: HEARTBEAT.md missing")
                return None
            if not _heartbeat_has_active_tasks(content):
                logger.debug("Heartbeat: HEARTBEAT.md has no active tasks")
                return None

            channel, chat_id = _pick_heartbeat_target()
            if channel == "cli":
                return None

            prompt = (
                _HEARTBEAT_PREAMBLE
                + f"Review the following HEARTBEAT.md and report any active tasks:\n\n{content}"
            )

            # Internal check: funnel all output through the post-run gate so the
            # turn can't deliver directly via the message tool and skip it.
            suppress_token = None
            if isinstance(message_tool, MessageTool):
                suppress_token = message_tool.set_suppress_delivery(True)
            try:
                resp = await agent.process_direct(
                    prompt,
                    session_key="heartbeat",
                    channel=channel,
                    chat_id=chat_id,
                    on_progress=_silent,
                )
            finally:
                if isinstance(message_tool, MessageTool) and suppress_token is not None:
                    message_tool.reset_suppress_delivery(suppress_token)
            response = resp.content if resp else ""

            # Keep a small tail of heartbeat history so the loop stays bounded.
            session = agent.sessions.get_or_create("heartbeat")
            session.retain_recent_legal_suffix(hb_cfg.keep_recent_messages)
            agent.sessions.save(session)

            if not response:
                return None

            # Fail closed: stay silent on evaluator failure instead of notifying.
            should_notify = await evaluate_response(
                response, prompt, agent.provider, agent.model,
                default_notify=False,
            )
            if should_notify:
                logger.info("Heartbeat: completed, delivering response")
                await _deliver_to_channel(
                    OutboundMessage(channel=channel, chat_id=chat_id, content=response),
                    record=True,
                )
            else:
                logger.info("Heartbeat: silenced by post-run evaluation")
            return response

        # Nightly maintenance: docs consistency + duplicate detection + cold rotation
        if job.name == "nightly_maintenance":
            summary_parts: list[str] = []

            # 1. Docs consistency check → spawn docs-repair subagent on mismatch
            from biscuitbot.agent.tools.docs_consistency import (
                build_repair_task,
                check_docs_consistency,
            )

            try:
                mismatches = check_docs_consistency(agent.tools, agent.workspace)
                if mismatches:
                    logger.warning(
                        "Nightly maintenance: {} doc mismatch(es): {}",
                        len(mismatches),
                        ", ".join(m.tool_name for m in mismatches),
                    )
                    task = build_repair_task(mismatches)
                    await agent.subagents.spawn(
                        task,
                        label="docs-repair",
                        origin_channel="cli",
                        origin_chat_id="direct",
                        session_key="docs-repair",
                    )
                    summary_parts.append(f"docs-repair: {len(mismatches)} mismatch(es)")
                else:
                    logger.info("Nightly maintenance: all docs match code")
            except Exception:
                logger.exception("Docs consistency check failed")

            # 2. Duplicate detection (report only, no auto-merge)
            try:
                from biscuitbot.agent.tools.duplicate_check import (
                    build_duplicate_report,
                    check_duplicates,
                )

                threshold = agent.tools_config.duplicate_similarity_threshold
                duplicates = check_duplicates(agent.tools, threshold=threshold)
                if duplicates:
                    report = build_duplicate_report(duplicates)
                    logger.warning(
                        "Nightly maintenance: {} duplicate pair(s) found\n{}",
                        len(duplicates),
                        report,
                    )
                    summary_parts.append(f"duplicates: {len(duplicates)} pair(s)")
                else:
                    logger.info("Nightly maintenance: no duplicates found")
            except Exception:
                logger.exception("Duplicate check failed")

            # 3. Cold storage rotation
            try:
                threshold_days = agent.tools_config.cold_storage_days
                stats = getattr(agent, "_usage_stats", None)
                if threshold_days > 0 and stats is not None:
                    newly_cold = stats.rotate_cold(agent.tools, threshold_days)
                    if newly_cold:
                        logger.warning(
                            "Nightly maintenance: {} tool(s) rotated to cold storage: {}",
                            len(newly_cold),
                            ", ".join(newly_cold),
                        )
                        summary_parts.append(f"cold-rotated: {len(newly_cold)} tool(s)")
                    else:
                        logger.info("Nightly maintenance: no tools rotated to cold storage")
            except Exception:
                logger.exception("Cold storage rotation failed")

            return "; ".join(summary_parts) if summary_parts else None

        if is_bound_cron_job(job):
            return await run_bound_cron_job(job, agent=agent, cron=cron)

        reason = "unbound agent cron job must be recreated from a chat session"
        logger.warning(
            "Cron: skipped unbound agent job '{}' ({}): {}",
            job.name,
            job.id,
            reason,
        )
        raise CronJobSkippedError(reason)

    cron.on_job = on_cron_job

    def _webui_runtime_model_name() -> str | None:
        model = getattr(agent, "model", None)
        if isinstance(model, str):
            stripped = model.strip()
            return stripped or None
        return None

    # Create channel manager (forwards SessionManager so the WebSocket channel
    # can serve the embedded webui's REST surface).
    channels = ChannelManager(
        config,
        bus,
        session_manager=session_manager,
        cron_service=cron,
        webui_runtime_model_name=_webui_runtime_model_name,
        webui_cron_pending_job_ids=getattr(agent, "pending_cron_job_ids_for_session", None),
        webui_static_dist=webui_static_dist,
        webui_runtime_surface=webui_runtime_surface,
        webui_runtime_capabilities=webui_runtime_capabilities,
    )

    def _pick_heartbeat_target() -> tuple[str, str]:
        """Pick a routable channel/chat target for heartbeat-triggered messages."""
        enabled = set(channels.enabled_channels)
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        return "cli", "direct"

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] 已启用频道：{', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]警告：未启用任何频道[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} 个定时任务")

    hb_cfg = config.gateway.heartbeat
    if hb_cfg.enabled:
        console.print(f"[green]✓[/green] Heartbeat: 每 {hb_cfg.interval_s} 秒")
    else:
        console.print("[yellow]✗[/yellow] 心跳：已禁用")

    async def _health_server(host: str, health_port: int):
        """Lightweight HTTP health endpoint on the gateway port."""
        import json as _json

        async def handle(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=5)
            except (asyncio.TimeoutError, ConnectionError):
                writer.close()
                return

            request_line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
            method, path = "", ""
            parts = request_line.split(" ")
            if len(parts) >= 2:
                method, path = parts[0], parts[1]

            if method == "GET" and path == "/health":
                body = _json.dumps({"status": "ok"})
                resp = (
                    f"HTTP/1.0 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )
            else:
                body = "Not Found"
                resp = (
                    f"HTTP/1.0 404 Not Found\r\n"
                    f"Content-Type: text/plain\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"\r\n{body}"
                )

            writer.write(resp.encode())
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, host, health_port)
        console.print(f"[green]✓[/green] 健康检查端点：http://{host}:{health_port}/health")
        async with server:
            await server.serve_forever()
    # Register Dream system job (idempotent on restart)
    from biscuitbot.cron.types import CronJob, CronPayload, CronSchedule
    dream_cfg = config.agents.defaults.dream
    if dream_cfg.enabled:
        cron.register_system_job(CronJob(
            id="dream",
            name="dream",
            schedule=dream_cfg.build_schedule(config.agents.defaults.timezone),
            payload=CronPayload(kind="system_event"),
        ))
        console.print(f"[green]✓[/green] 梦境：{dream_cfg.describe_schedule()}")
    else:
        console.print("[yellow]○[/yellow] 梦境：已禁用")

    # Register Heartbeat system job (idempotent on restart)
    if hb_cfg.enabled:
        cron.register_system_job(CronJob(
            id="heartbeat",
            name="heartbeat",
            schedule=CronSchedule(
                kind="every",
                every_ms=hb_cfg.interval_s * 1000,
                tz=config.agents.defaults.timezone,
            ),
            payload=CronPayload(kind="system_event"),
        ))

    # Register Nightly Maintenance system job (idempotent on restart)
    # Runs nightly: docs consistency check + duplicate detection + cold rotation
    cron.register_system_job(CronJob(
        id="nightly_maintenance",
        name="nightly_maintenance",
        schedule=CronSchedule(
            kind="cron",
            expr="0 23 * * *",  # 每天 23:00
            tz=config.agents.defaults.timezone,
        ),
        payload=CronPayload(kind="system_event"),
    ))

    async def _open_browser_when_ready() -> None:
        """Wait for the gateway to bind, then point the user's browser at the webui."""
        if not open_browser_url:
            return
        import webbrowser
        # Channels start asynchronously; a short poll lets us avoid racing the bind.
        for _ in range(40):  # ~4s max
            try:
                reader, writer = await asyncio.open_connection(
                    config.gateway.host or "127.0.0.1", port
                )
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.1)
        try:
            webbrowser.open(open_browser_url)
            console.print(f"[green]✓[/green] 已打开浏览器：{open_browser_url}")
        except Exception as e:
            console.print(f"[yellow]无法打开浏览器 ({e})，请访问 {open_browser_url}[/yellow]")

    async def run():
        try:
            await cron.start()
            tasks = [
                agent.run(),
                channels.start_all(),
            ]
            if health_server_enabled and "websocket" not in channels.enabled_channels:
                tasks.append(_health_server(config.gateway.host, port))
            if open_browser_url:
                tasks.append(_open_browser_when_ready())
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            console.print("\n正在关闭...")
        except Exception:
            import traceback

            console.print("\n[red]错误：网关意外崩溃[/red]")
            console.print(traceback.format_exc())
        finally:
            await agent.close_mcp()
            cron.stop()
            agent.stop()
            await channels.stop_all()
            # Flush all cached sessions to durable storage before exit.
            # This prevents data loss on filesystems with write-back
            # caching (rclone VFS, NFS, FUSE mounts, etc.).
            flushed = agent.sessions.flush_all()
            if flushed:
                logger.info("Shutdown: flushed {} session(s) to disk", flushed)

    asyncio.run(run())


# ============================================================================
# Desktop Command
# ============================================================================


@app.command()
def desktop(
    port: int | None = typer.Option(None, "--port", "-p", help="网关端口"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    config_file: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    width: int = typer.Option(1200, "--width", help="窗口宽度"),
    height: int = typer.Option(800, "--height", help="窗口高度"),
):
    """以原生桌面应用方式启动 biscuitbot。"""
    from biscuitbot.desktop.app import run_desktop

    cfg = _load_runtime_config(config_file, workspace)
    run_desktop(cfg, port=port, width=width, height=height)


@app.command()
def sidecar(
    config_file: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
):
    """以无头 sidecar 方式启动网关（供桌面壳程序调用）。"""
    from biscuitbot.desktop.sidecar import main as sidecar_main

    cfg = _load_runtime_config(config_file)
    sidecar_main(cfg)


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="发送给智能体的消息"),
    session_id: str = typer.Option("cli:direct", "--session", "-s", help="会话 ID"),
    workspace: str | None = typer.Option(None, "--workspace", "-w", help="工作区目录"),
    config_file: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
    markdown: bool = typer.Option(True, "--markdown/--no-markdown", help="以 Markdown 渲染助手输出"),
    logs: bool = typer.Option(False, "--logs/--no-logs", help="在聊天中显示 biscuitbot 运行时日志"),
):
    """直接与智能体交互。"""
    from loguru import logger

    from biscuitbot.bus.queue import MessageBus
    from biscuitbot.cron.service import CronService
    from biscuitbot.providers.image_generation import image_gen_provider_configs

    config = _load_runtime_config(config_file, workspace)
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()

    # Preserve existing single-workspace installs, but keep custom workspaces clean.
    if is_default_workspace(config.workspace_path):
        _migrate_cron_store(config)

    # Create cron service with workspace-scoped store
    cron_store_path = config.workspace_path / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    if logs:
        logger.enable("biscuitbot")
    else:
        logger.disable("biscuitbot")

    try:
        agent_loop = AgentLoop.from_config(
            config, bus,
            cron_service=cron,
            image_generation_provider_configs=image_gen_provider_configs(config),
        )
    except ValueError as exc:
        console.print(f"[red]错误：{exc}[/red]")
        raise typer.Exit(1) from exc
    restart_notice = consume_restart_notice_from_env()
    if restart_notice and should_show_cli_restart_notice(restart_notice, session_id):
        _print_agent_response(
            format_restart_completed_message(restart_notice.started_at_raw),
            render_markdown=False,
        )

    # Shared reference for progress callbacks
    _thinking: ThinkingSpinner | None = None

    def _make_progress(renderer: StreamRenderer | None = None):
        reasoning_buffer = _ReasoningBuffer()

        async def _cli_progress(content: str, *, tool_hint: bool = False, reasoning: bool = False, **_kwargs: Any) -> None:
            ch = agent_loop.channels_config

            if _kwargs.get("reasoning_end"):
                if ch and not ch.show_reasoning:
                    reasoning_buffer.clear()
                else:
                    _flush_cli_reasoning(reasoning_buffer, _thinking, renderer)
                return

            if reasoning:
                if ch and not ch.show_reasoning:
                    reasoning_buffer.clear()
                    return
                text = reasoning_buffer.add(content)
                if text:
                    _print_cli_reasoning(text, _thinking, renderer)
                return
            if ch and tool_hint and not ch.send_tool_hints:
                return
            if ch and not tool_hint and not ch.send_progress:
                return
            _print_cli_progress_line(content, _thinking, renderer)
        return _cli_progress

    if message:
        # Single message mode — direct call, no bus needed
        async def run_once():
            renderer = StreamRenderer(
                render_markdown=markdown,
                bot_name=config.agents.defaults.bot_name,
                bot_icon=config.agents.defaults.bot_icon,
            )
            response = await agent_loop.process_direct(
                message, session_id,
                on_progress=_make_progress(renderer),
                on_stream=renderer.on_delta,
                on_stream_end=renderer.on_end,
            )
            if not renderer.streamed:
                await renderer.close()
                print_kwargs: dict[str, Any] = {}
                if renderer.header_printed:
                    print_kwargs["show_header"] = False
                _print_agent_response(
                    response.content if response else "",
                    render_markdown=markdown,
                    metadata=response.metadata if response else None,
                    **print_kwargs,
                )
            await agent_loop.close_mcp()

        asyncio.run(run_once())
    else:
        # Interactive mode — route through bus like other channels
        from biscuitbot.bus.events import InboundMessage
        _init_prompt_session()
        _model, _preset_tag = _model_display(config)
        _icon = config.agents.defaults.bot_icon or __logo__
        console.print(f"{_icon} 交互模式 [bold blue]({_model})[/bold blue]{_preset_tag} — 输入 [bold]exit[/bold] 或 [bold]Ctrl+C[/bold] 退出\n")

        if ":" in session_id:
            cli_channel, cli_chat_id = session_id.split(":", 1)
        else:
            cli_channel, cli_chat_id = "cli", session_id

        def _handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            _restore_terminal()
            console.print(f"\n收到信号 {sig_name}，再见！")
            sys.exit(0)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        # SIGHUP is not available on Windows
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, _handle_signal)
        # Ignore SIGPIPE to prevent silent process termination when writing to closed pipes
        # SIGPIPE is not available on Windows
        if hasattr(signal, 'SIGPIPE'):
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)

        async def run_interactive():
            bus_task = asyncio.create_task(agent_loop.run())
            turn_done = asyncio.Event()
            turn_done.set()
            turn_response: list[tuple[str, dict]] = []
            renderer: StreamRenderer | None = None
            reasoning_buffer = _ReasoningBuffer()

            async def _consume_outbound():
                while True:
                    try:
                        msg = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)

                        if msg.metadata.get("_stream_delta"):
                            if renderer:
                                await renderer.on_delta(msg.content)
                            continue
                        if msg.metadata.get("_stream_end"):
                            if renderer:
                                await renderer.on_end(
                                    resuming=msg.metadata.get("_resuming", False),
                                )
                            continue
                        if msg.metadata.get("_streamed"):
                            turn_done.set()
                            continue

                        if await _maybe_print_interactive_progress(
                            msg,
                            _thinking,
                            agent_loop.channels_config,
                            renderer,
                            reasoning_buffer,
                        ):
                            continue

                        if not turn_done.is_set():
                            if msg.content:
                                turn_response.append((msg.content, dict(msg.metadata or {})))
                            turn_done.set()
                        elif msg.content:
                            await _print_interactive_response(
                                msg.content,
                                render_markdown=markdown,
                                metadata=msg.metadata,
                            )

                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

            outbound_task = asyncio.create_task(_consume_outbound())

            try:
                while True:
                    try:
                        _flush_pending_tty_input()
                        # Stop spinner before user input to avoid prompt_toolkit conflicts
                        if renderer:
                            renderer.stop_for_input()
                        user_input = _sanitize_surrogates(await _read_interactive_input_async())
                        command = user_input.strip()
                        if not command:
                            continue

                        if _is_exit_command(command):
                            _restore_terminal()
                            console.print("\n再见！")
                            break

                        turn_done.clear()
                        turn_response.clear()
                        reasoning_buffer.clear()
                        renderer = StreamRenderer(
                            render_markdown=markdown,
                            bot_name=config.agents.defaults.bot_name,
                            bot_icon=config.agents.defaults.bot_icon,
                        )

                        await bus.publish_inbound(InboundMessage(
                            channel=cli_channel,
                            sender_id="user",
                            chat_id=cli_chat_id,
                            content=user_input,
                            metadata={"_wants_stream": True},
                        ))

                        await turn_done.wait()

                        if turn_response:
                            content, meta = turn_response[0]
                            if content and not meta.get("_streamed"):
                                if renderer:
                                    await renderer.close()
                                print_kwargs: dict[str, Any] = {}
                                if renderer and renderer.header_printed:
                                    print_kwargs["show_header"] = False
                                _print_agent_response(
                                    content,
                                    render_markdown=markdown,
                                    metadata=meta,
                                    **print_kwargs,
                                )
                        elif renderer and not renderer.streamed:
                            await renderer.close()
                    except KeyboardInterrupt:
                        _restore_terminal()
                        console.print("\n再见！")
                        break
                    except EOFError:
                        _restore_terminal()
                        console.print("\n再见！")
                        break
            finally:
                agent_loop.stop()
                outbound_task.cancel()
                await asyncio.gather(bus_task, outbound_task, return_exceptions=True)
                await agent_loop.close_mcp()

        asyncio.run(run_interactive())


# ============================================================================
# Channel Commands
# ============================================================================


# 频道管理子命令组
channels_app = typer.Typer(help="管理频道", no_args_is_help=True)
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status(
    config_path: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
):
    """显示频道状态。"""
    from biscuitbot.channels.registry import discover_all
    from biscuitbot.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled")

    for name, cls in sorted(discover_all().items()):
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            "[green]\u2713[/green]" if enabled else "[dim]\u2717[/dim]",
        )

    console.print(table)


@channels_app.command("login")
def channels_login(
    channel_name: str = typer.Argument(..., help="频道名称（如 weixin、feishu）"),
    force: bool = typer.Option(False, "--force", "-f", help="强制重新认证（即使已登录）"),
    config_path: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
):
    """通过二维码或其他交互方式登录频道。"""
    from biscuitbot.channels.registry import discover_all
    from biscuitbot.config.loader import load_config, set_config_path

    resolved_config_path = Path(config_path).expanduser().resolve() if config_path else None
    if resolved_config_path is not None:
        set_config_path(resolved_config_path)

    config = load_config(resolved_config_path)
    channel_cfg = getattr(config.channels, channel_name, None) or {}

    # Validate channel exists
    all_channels = discover_all()
    if channel_name not in all_channels:
        available = ", ".join(all_channels.keys())
        console.print(f"[red]Unknown channel: {channel_name}[/red]  Available: {available}")
        raise typer.Exit(1)

    console.print(f"{__logo__} {all_channels[channel_name].display_name} Login\n")

    channel_cls = all_channels[channel_name]
    channel = channel_cls(channel_cfg, bus=None)

    success = asyncio.run(channel.login(force=force))

    if not success:
        raise typer.Exit(1)


# ============================================================================
# Plugin Commands
# ============================================================================

# 插件管理子命令组
plugins_app = typer.Typer(help="管理频道插件", no_args_is_help=True)
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list():
    """列出所有已发现的频道（内置和插件）。"""
    from biscuitbot.channels.registry import discover_all, discover_channel_names
    from biscuitbot.config.loader import load_config

    config = load_config()
    builtin_names = set(discover_channel_names())
    all_channels = discover_all()

    table = Table(title="Channel Plugins")
    table.add_column("Name", style="cyan")
    table.add_column("Source", style="magenta")
    table.add_column("Enabled")

    for name in sorted(all_channels):
        cls = all_channels[name]
        source = "builtin" if name in builtin_names else "plugin"
        section = getattr(config.channels, name, None)
        if section is None:
            enabled = False
        elif isinstance(section, dict):
            enabled = section.get("enabled", False)
        else:
            enabled = getattr(section, "enabled", False)
        table.add_row(
            cls.display_name,
            source,
            "[green]yes[/green]" if enabled else "[dim]no[/dim]",
        )

    console.print(table)


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def status():
    """显示 biscuitbot 状态。"""
    from biscuitbot.config.loader import get_config_path, load_config

    config_path = get_config_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} biscuitbot Status\n")

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

    if config_path.exists():
        from biscuitbot.providers.registry import PROVIDERS

        _model, _preset_tag = _model_display(config)
        console.print(f"Model: {_model}{_preset_tag}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_oauth:
                console.print(f"{spec.label}: [green]✓ (OAuth)[/green]")
            elif spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


talent_market_app = typer.Typer(help="管理人才市场注册表 URL", no_args_is_help=True)
app.add_typer(talent_market_app, name="talent-market")


def _resolve_talent_config_path(config_path: str | None) -> Path:
    """解析人才市场子命令的配置文件路径；同时设置全局配置路径。"""
    from biscuitbot.config.loader import get_config_path, set_config_path

    resolved = Path(config_path).expanduser().resolve() if config_path else None
    if resolved is not None:
        set_config_path(resolved)
    return get_config_path()


def _write_talent_config(path: Path, raw: dict) -> None:
    """把人才市场配置写回配置文件，保留所有既有键（含 API Key），权限 0o600。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    with open(path, "w", encoding="utf-8") as f:
        _json.dump(raw, f, indent=2, ensure_ascii=False)
    try:
        path.chmod(0o600)
    except OSError:
        pass


@talent_market_app.command("set")
def talent_market_set(
    url: str,
    config_path: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
):
    """设置人才市场注册表 URL（写入后台配置文件；WebUI/桌面应用只读，不可修改）。"""
    from biscuitbot.webui.talent_market import TalentMarketError, _validate_registry_url

    try:
        url = _validate_registry_url(url)
    except TalentMarketError as e:
        console.print(f"[red]错误：{e.message}[/red]")
        raise typer.Exit(1)

    path = _resolve_talent_config_path(config_path)
    import json as _json

    if path.exists():
        try:
            raw = _json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            console.print(f"[red]错误：无法读取配置文件 {path}：{e}[/red]")
            raise typer.Exit(1)
        if not isinstance(raw, dict):
            raw = {}
    else:
        raw = {}
    gateway = raw.get("gateway")
    if not isinstance(gateway, dict):
        gateway = {}
        raw["gateway"] = gateway
    # 手术式写入：仅改这一键，绝不触碰配置文件里的其他键（含 API Key）
    gateway["talent_market_registry_url"] = url
    _write_talent_config(path, raw)
    console.print(f"[green]已设置人才市场注册表：[/green]{url}")
    console.print(f"[dim]配置文件：{path}[/dim]")


@talent_market_app.command("show")
def talent_market_show(
    config_path: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
):
    """显示当前人才市场注册表 URL。"""
    from biscuitbot.webui.talent_market import read_talent_market_registry_url

    path = _resolve_talent_config_path(config_path)
    url = read_talent_market_registry_url(path)
    if url:
        console.print(f"人才市场注册表：{url}")
    else:
        console.print("人才市场注册表：[dim]未配置[/dim]")
        console.print("提示：使用 `biscuitbot talent-market set <url>` 进行配置。")
    console.print(f"[dim]配置文件：{path}[/dim]")


@talent_market_app.command("clear")
def talent_market_clear(
    config_path: str | None = typer.Option(None, "--config", "-c", help="配置文件路径"),
):
    """清除人才市场注册表 URL 配置。"""
    path = _resolve_talent_config_path(config_path)
    import json as _json

    if not path.exists():
        console.print("人才市场注册表：[dim]未配置（配置文件不存在）[/dim]")
        return
    try:
        raw = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        console.print(f"[red]错误：无法读取配置文件 {path}：{e}[/red]")
        raise typer.Exit(1)
    if not isinstance(raw, dict):
        raw = {}
    gateway = raw.get("gateway")
    if isinstance(gateway, dict) and "talent_market_registry_url" in gateway:
        del gateway["talent_market_registry_url"]
        _write_talent_config(path, raw)
        console.print("已清除人才市场注册表 URL 配置。")
    else:
        console.print("人才市场注册表：[dim]未配置[/dim]")
    console.print(f"[dim]配置文件：{path}[/dim]")


if __name__ == "__main__":
    app()
