"""CLI 端口全链路追踪日志的彩色树形输出。

所属模块与项目作用
===================
本文件位于 biscuitbot/bus 目录，是 bus 模块的追踪日志渲染组件。
在项目架构中起到的作用：订阅 RuntimeEventBus 的 AgentTraceEvent，将智能体执行流程
以彩色缩进树形格式输出到终端，便于在纯后端端口（CLI）查看执行步骤，方便调试与观察。

启用方式：
    from biscuitbot.bus.trace_logger import install_trace_logger
    install_trace_logger(runtime_events_bus)

或在 CLI 启动时自动安装（见 cli/commands.py）。
"""

from __future__ import annotations

import sys  # 用于检测终端与写入 stderr
from typing import Any  # 任意类型标注

from biscuitbot.bus.runtime_events import AgentTraceEvent, RuntimeEventBus  # 运行时事件类型与总线


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

# 是否支持 ANSI 颜色（惰性初始化）
_ANSI_ENABLED: bool | None = None


def _ansi_supported() -> bool:
    """检测当前终端是否支持 ANSI 颜色（结果会缓存）。"""
    global _ANSI_ENABLED
    if _ANSI_ENABLED is None:
        # 仅当 stderr 是 TTY 且非 Windows 平台时才启用颜色
        _ANSI_ENABLED = (
            hasattr(sys.stderr, "isatty")
            and sys.stderr.isatty()
            and sys.platform != "win32"
        )
    return _ANSI_ENABLED or False


def _color(text: str, color: str) -> str:
    """为文本包裹 ANSI 颜色码；不支持颜色时原样返回。"""
    if not _ansi_supported():
        return text
    return f"{color}{text}{_COLOR_RESET}"


def _format_duration(ms: float | None) -> str:
    """将毫秒耗时格式化为可读字符串，按数量级选择 ms/s 单位。"""
    if ms is None:
        return ""
    if ms < 1:
        return f"{ms:.1f}ms"  # 不足 1ms 保留一位小数
    if ms < 1000:
        return f"{ms:.0f}ms"  # 1ms~1s 取整毫秒
    return f"{ms / 1000:.2f}s"  # 超过 1s 转换为秒


def _format_detail(detail: dict[str, Any], phase: str) -> str:
    """格式化 detail 字段为可读字符串，按 phase 提取关键字段。"""
    parts: list[str] = []
    if phase == "tool_call":
        # 工具调用：展示参数、错误与结果详情
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
        # LLM 调用：展示 token 用量、迭代轮次、是否触发工具调用
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
        # turn 状态机：展示事件名与错误
        event = detail.get("event", "")
        if event:
            parts.append(f"event={event}")
        error = detail.get("error", "")
        if error:
            parts.append(f"error={error}")
    return " ".join(parts) if parts else ""


def _format_trace_line(event: AgentTraceEvent) -> str:
    """将 AgentTraceEvent 格式化为彩色树形日志行。"""
    color, icon = _PHASE_STYLE.get(event.phase, (_COLOR_GRAY, "•"))  # 缺省灰色圆点
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

    # 组装各部分为单行输出
    parts = [step, status]
    if duration:
        parts.append(duration)
    if detail:
        parts.append(detail)
    return f"{_COLOR_DIM}{indent}{_COLOR_RESET}" + " ".join(parts)


def _handle_trace_event(event: AgentTraceEvent) -> None:
    """处理 AgentTraceEvent，输出到终端。

    作为订阅器回调，由 RuntimeEventBus 在事件发布时调用。
    """
    line = _format_trace_line(event)
    # 使用 print 而非 logger，因为这是用户可见的结构化输出
    print(line, file=sys.stderr, flush=True)


def install_trace_logger(bus: RuntimeEventBus) -> Any:
    """安装 CLI trace 日志订阅器。

    将 _handle_trace_event 订阅到指定总线，仅监听 AgentTraceEvent 事件。
    返回 unsubscribe 函数，调用可取消订阅。
    """
    return bus.subscribe(_handle_trace_event, AgentTraceEvent)
