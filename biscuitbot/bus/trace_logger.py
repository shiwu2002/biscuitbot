"""CLI 端口全链路追踪日志的彩色树形输出。

订阅 RuntimeEventBus 的 AgentTraceEvent，将智能体执行流程
以彩色缩进树形格式输出到终端，便于在纯后端端口查看执行步骤。

启用方式：
    from biscuitbot.bus.trace_logger import install_trace_logger
    install_trace_logger(runtime_events_bus)

或在 CLI 启动时自动安装（见 cli/commands.py）。
"""

from __future__ import annotations

import sys
from typing import Any

from biscuitbot.bus.runtime_events import AgentTraceEvent, RuntimeEventBus


# ANSI 颜色码
_COLOR_RESET = "\033[0m"
_COLOR_DIM = "\033[2m"
_COLOR_BOLD = "\033[1m"
_COLOR_GREEN = "\033[32m"
_COLOR_RED = "\033[31m"
_COLOR_YELLOW = "\033[33m"
_COLOR_BLUE = "\033[34m"
_COLOR_CYAN = "\033[36m"
_COLOR_MAGENTA = "\033[35m"
_COLOR_GRAY = "\033[90m"

# phase → 颜色 + 图标
_PHASE_STYLE: dict[str, tuple[str, str]] = {
    "turn_state": (_COLOR_CYAN, "●"),
    "tool_call": (_COLOR_MAGENTA, "▸"),
    "llm_call": (_COLOR_BLUE, "◆"),
    "error": (_COLOR_RED, "✗"),
}

# status → 颜色
_STATUS_COLOR: dict[str, str] = {
    "started": _COLOR_YELLOW,
    "completed": _COLOR_GREEN,
    "failed": _COLOR_RED,
}

# 是否支持 ANSI 颜色
_ANSI_ENABLED: bool | None = None


def _ansi_supported() -> bool:
    global _ANSI_ENABLED
    if _ANSI_ENABLED is None:
        _ANSI_ENABLED = (
            hasattr(sys.stderr, "isatty")
            and sys.stderr.isatty()
            and sys.platform != "win32"
        )
    return _ANSI_ENABLED or False


def _color(text: str, color: str) -> str:
    if not _ansi_supported():
        return text
    return f"{color}{text}{_COLOR_RESET}"


def _format_duration(ms: float | None) -> str:
    if ms is None:
        return ""
    if ms < 1:
        return f"{ms:.1f}ms"
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def _format_detail(detail: dict[str, Any], phase: str) -> str:
    """格式化 detail 字段为可读字符串。"""
    parts: list[str] = []
    if phase == "tool_call":
        args = detail.get("arguments", "")
        if args:
            parts.append(f"args={args}")
        error = detail.get("error", "")
        if error:
            parts.append(f"error={error}")
        result_detail = detail.get("detail", "")
        if result_detail and result_detail != "(empty)":
            parts.append(result_detail)
    elif phase == "llm_call":
        prompt = detail.get("prompt_tokens", 0)
        completion = detail.get("completion_tokens", 0)
        if prompt or completion:
            parts.append(f"tokens=in:{prompt} out:{completion}")
        iteration = detail.get("iteration")
        if iteration is not None:
            parts.append(f"iter={iteration}")
        has_tools = detail.get("has_tool_calls")
        if has_tools:
            parts.append("tool_calls=yes")
    elif phase == "turn_state":
        event = detail.get("event", "")
        if event:
            parts.append(f"event={event}")
        error = detail.get("error", "")
        if error:
            parts.append(f"error={error}")
    return " ".join(parts) if parts else ""


def _format_trace_line(event: AgentTraceEvent) -> str:
    """将 AgentTraceEvent 格式化为彩色树形日志行。"""
    color, icon = _PHASE_STYLE.get(event.phase, (_COLOR_GRAY, "•"))
    status_color = _STATUS_COLOR.get(event.status, _COLOR_DIM)

    # 缩进：tool_call 和 llm_call 在 turn_state RUN 步骤内，缩进 2 格
    indent = "  " if event.phase in ("tool_call", "llm_call") else ""

    # 步骤名
    step = _color(f"{icon} {event.step}", color)

    # 状态
    status = _color(event.status, status_color)

    # 耗时
    duration_str = _format_duration(event.duration_ms)
    duration = _color(f"({duration_str})", _COLOR_DIM) if duration_str else ""

    # 详情
    detail_str = _format_detail(event.detail, event.phase)
    detail = _color(detail_str, _COLOR_DIM) if detail_str else ""

    # 组装
    parts = [step, status]
    if duration:
        parts.append(duration)
    if detail:
        parts.append(detail)
    return f"{_COLOR_DIM}{indent}{_COLOR_RESET}" + " ".join(parts)


def _handle_trace_event(event: AgentTraceEvent) -> None:
    """处理 AgentTraceEvent，输出到终端。"""
    line = _format_trace_line(event)
    # 使用 print 而非 logger，因为这是用户可见的结构化输出
    print(line, file=sys.stderr, flush=True)


def install_trace_logger(bus: RuntimeEventBus) -> Any:
    """安装 CLI trace 日志订阅器。

    返回 unsubscribe 函数，调用可取消订阅。
    """
    return bus.subscribe(_handle_trace_event, AgentTraceEvent)
