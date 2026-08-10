"""CLI 流式输出渲染器。

所属模块与项目作用
===================
本文件位于 biscuitbot/cli 目录，提供 CLI 终端流式输出的渲染能力。
在项目架构中起到的作用：
- 使用 Rich Live（``transient=True``）实现流式过程中的就地 Markdown 更新；
- 流式结束后打印最终干净渲染结果，使内容持久保留在屏幕上；
- ``transient=True`` 确保 Live 区域在 ``stop()`` 返回前被擦除，
  避免早期实现中出现的重复显示问题。
"""

from __future__ import annotations

import sys  # 访问标准输出流，判断是否为 TTY 终端
from contextlib import contextmanager, nullcontext  # 上下文管理器工具，用于暂停/恢复 spinner

from rich.console import Console  # Rich 终端控制台，负责输出渲染
from rich.live import Live  # Rich 就地刷新渲染容器
from rich.markdown import Markdown  # Markdown 渲染器
from rich.text import Text  # 纯文本渲染器


def _clear_current_line(console: Console) -> None:
    """在打印持久输出前擦除瞬态状态行。"""
    file = console.file
    isatty = getattr(file, "isatty", lambda: False)
    if not isatty():
        return
    file.write("\r\x1b[2K")  # 回车并清除整行
    file.flush()


def _make_console() -> Console:
    """创建 Console：当 stdout 不是 TTY 时输出纯文本。

    Rich 的 spinner、Live 渲染与光标可见性转义码都依赖于
    ``Console.is_terminal``。强制 ``force_terminal=True`` 会覆盖
    ``isatty()`` 检查，导致控制序列（``\\x1b[?25l``、点阵 spinner 帧）
    污染 ``docker exec -i`` 或管道等程序化消费者，即使设置了
    ``NO_COLOR`` 或 ``TERM=dumb`` 也无效。改用 ``isatty()`` 判断，
    在交互终端保留 Rich 输出，其它场合退化为纯文本 (#3265)。
    """
    return Console(file=sys.stdout, force_terminal=sys.stdout.isatty())


class ThinkingSpinner:
    """显示 '<bot_name> is thinking...' 的 spinner，支持暂停。"""

    def __init__(self, console: Console | None = None, bot_name: str = "biscuitbot"):
        c = console or _make_console()
        self._console = c
        self._spinner = c.status(f"[dim]{bot_name} is thinking...[/dim]", spinner="dots")
        self._active = False  # 标记 spinner 当前是否处于活动状态

    def __enter__(self):
        self._spinner.start()
        self._active = True
        return self

    def __exit__(self, *exc):
        self._active = False
        self._spinner.stop()
        _clear_current_line(self._console)
        return False

    def pause(self):
        """上下文管理器：临时停止 spinner 以输出干净内容。"""
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            if self._spinner and self._active:
                self._spinner.stop()
                _clear_current_line(self._console)
            try:
                yield
            finally:
                if self._spinner and self._active:
                    self._spinner.start()

        return _ctx()


class StreamRenderer:
    """基于 Rich Live 的流式渲染器，支持就地更新。

    流式过程中：通过 Rich Live 就地更新内容。
    结束时：停止 Live（transient=True 会擦除它），然后打印最终渲染结果。

    每轮流程：
      spinner -> 首个 delta -> 头部 + Live 更新 ->
      on_end -> 停止 Live + 最终渲染
    """

    def __init__(
        self,
        render_markdown: bool = True,
        show_spinner: bool = True,
        bot_name: str = "biscuitbot",
        bot_icon: str = "🍪",
    ):
        self._md = render_markdown  # 是否以 Markdown 渲染
        self._show_spinner = show_spinner  # 是否显示思考 spinner
        self._bot_name = bot_name  # 机器人名称
        self._bot_icon = bot_icon  # 机器人图标
        self._buf = ""  # 流式内容缓冲区
        self.streamed = False  # 标记本轮是否发生过流式输出
        self._console = _make_console()
        self._live: Live | None = None  # Rich Live 实例
        self._spinner: ThinkingSpinner | None = None  # 思考 spinner 实例
        self._header_printed = False  # 标记本轮是否已打印助手头部
        self._start_spinner()

    def _renderable(self):
        """根据当前缓冲区创建可渲染对象。"""
        if self._md and self._buf:
            return Markdown(self._buf)
        return Text(self._buf or "")

    def _render_str(self) -> str:
        """通过 Rich 将当前缓冲区渲染为纯字符串。"""
        with self._console.capture() as cap:
            self._console.print(self._renderable())
        return cap.get()

    def _start_spinner(self) -> None:
        """启动思考 spinner（若启用）。"""
        if self._show_spinner:
            self._spinner = ThinkingSpinner(bot_name=self._bot_name)
            self._spinner.__enter__()

    def _stop_spinner(self) -> None:
        """停止思考 spinner。"""
        if self._spinner:
            self._spinner.__exit__(None, None, None)
            self._spinner = None

    @property
    def console(self) -> Console:
        """暴露 Live 的 console，供外部打印函数使用。"""
        return self._console

    @property
    def header_printed(self) -> bool:
        """标记本轮是否已开启助手输出块。"""
        return self._header_printed

    def ensure_header(self) -> None:
        """停止瞬态状态并打印一次助手头部。"""
        # 一轮对话可能在最终答案前先打印追踪行，然后在工具运行期间重启 spinner。
        # 即使头部已打印，下一个答案 delta 仍需停止该 spinner。
        self._stop_spinner()
        if self._header_printed:
            return
        self._console.print()
        header = f"{self._bot_icon} {self._bot_name}" if self._bot_icon else self._bot_name
        self._console.print(f"[cyan]{header}[/cyan]")
        self._header_printed = True

    def pause_spinner(self):
        """上下文管理器：临时停止瞬态输出以打印干净的追踪行。"""
        @contextmanager
        def _pause():
            live_was_active = self._live is not None
            if self._live:
                # 追踪/推理可能在答案流式开始后到达。
                # 先停止瞬态 Live 视图，避免在追踪行前泄漏原始的部分 Markdown 帧。
                self._live.stop()
                self._live = None
            with self._spinner.pause() if self._spinner else nullcontext():
                yield
            # 如果追踪之后还有更多答案 delta，on_delta() 会用现有缓冲区创建新的 Live。
            # 如果没有 delta，on_end() 会一次性打印最终缓冲的答案。
            if live_was_active:
                return

        return _pause()

    async def on_delta(self, delta: str) -> None:
        """处理流式增量内容：追加到缓冲区并刷新 Live 显示。"""
        self.streamed = True
        self._buf += delta
        if self._live is None:
            if not self._buf.strip():
                return
            self.ensure_header()
            self._live = Live(
                self._renderable(),
                console=self._console,
                auto_refresh=False,
                transient=True,  # 停止时擦除 Live 区域，避免重复显示
            )
            self._live.start()
        else:
            self._live.update(self._renderable())
        self._live.refresh()

    async def on_end(self, *, resuming: bool = False) -> None:
        """处理流式结束：停止 Live 并打印最终渲染结果。"""
        if self._live:
            # 双重刷新以在 stop() 调用 refresh() 前同步 _shape。
            self._live.refresh()
            self._live.update(self._renderable())
            self._live.refresh()
            self._live.stop()
            self._live = None
        self._stop_spinner()
        if self._buf.strip():
            # 打印最终渲染内容（在 Live 消失后持久保留）。
            out = sys.stdout
            out.write(self._render_str())
            out.flush()
        if resuming:
            self._buf = ""
            self._start_spinner()

    def stop_for_input(self) -> None:
        """在等待用户输入前停止 spinner，避免与 prompt_toolkit 冲突。"""
        self._stop_spinner()

    def pause(self):
        """上下文管理器：为外部输出暂停 spinner。流式开始后为空操作。"""
        if self._spinner:
            return self._spinner.pause()
        return nullcontext()

    async def close(self) -> None:
        """停止 spinner/live，但不渲染最终的流式轮次。"""
        if self._live:
            self._live.stop()
            self._live = None
        self._stop_spinner()
