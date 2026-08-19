"""工具型 Agent 的共享执行循环。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块中负责“工具型 LLM 执行循环”
的核心组件，与产品层（渠道、会话、调度）解耦。
在项目架构中起到的作用：
- ``AgentRunner`` 提供一个可在任意宿主（CLI、飞书、Slack 等）复用的执行引擎：
  接收初始消息与工具注册表，按迭代循环调用 LLM、解析工具调用、执行工具、
  回填结果，并在中途处理流式输出、推理内容、注入消息、上下文压缩、错误恢复
  与生命周期钩子；
- ``AgentRunSpec``/``AgentRunResult`` 分别描述一次执行的输入配置与输出结果；
- 通过 ``AgentHook`` 暴露 before/after run、迭代前后、流式、工具执行等生命周期
  扩展点，由上层（如 ``AgentProgressHook``）实现 UI 适配；
- 内置安全边界处理（SSRF、工作区越界）、token 预算估算、结果落盘与截断，
  确保长会话与异常输入下的稳定性。
"""

from __future__ import annotations

import asyncio  # 异步循环、超时与并发工具执行
import inspect  # 检查回调签名（如 injection_callback 是否接受 limit）
import os  # 读取环境变量（LLM 超时配置）
import time  # 计时（LLM/工具调用耗时）
from contextlib import suppress  # 忽略可预期异常（如 prepare_call 失败）
from copy import deepcopy  # 深拷贝消息列表，避免钩子修改污染原始数据
from dataclasses import dataclass, field  # 数据类装饰器与字段默认工厂
from pathlib import Path  # 工作区路径类型
from typing import Any, Callable  # 类型注解支持

from loguru import logger  # 日志记录

from biscuitbot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext  # 生命周期钩子基类与上下文
from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表
from biscuitbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest  # LLM 提供商基类、响应与工具调用请求类型
from biscuitbot.utils.file_edit_events import (  # 文件编辑流式事件追踪与构建
    StreamingFileEditTracker,
    build_file_edit_end_event,
    build_file_edit_error_event,
    build_file_edit_start_event,
    prepare_file_edit_trackers,
)
from biscuitbot.utils.file_edit_events import (  # 单文件追踪器（兼容旧测试/扩展的 monkeypatch 入口）
    prepare_file_edit_tracker as _prepare_file_edit_tracker,
)
from biscuitbot.utils.helpers import (  # 通用辅助函数
    IncrementalThinkExtractor,  # think 标签增量提取器
    build_assistant_message,  # 构造 assistant 消息
    estimate_message_tokens,  # 估算单条消息 token 数
    estimate_prompt_tokens_chain,  # 估算提示链 token 数
    extract_reasoning,  # 从响应中提取推理内容
    find_legal_message_start,  # 查找合法消息起始（避免破坏多模态/角色交替）
    maybe_persist_tool_result,  # 按需将工具结果落盘
    strip_think,  # 剥离 think 标签
    truncate_text,  # 按字符截断
)
from biscuitbot.utils.progress_events import (  # 进度事件辅助
    invoke_file_edit_progress,  # 触发文件编辑进度事件
    on_progress_accepts_file_edit_events,  # 判断进度回调是否接受文件编辑事件
)
from biscuitbot.utils.prompt_templates import render_template  # 模板渲染（如 max_iterations 提示）
from biscuitbot.utils.runtime import (  # 运行时辅助消息与判定
    EMPTY_FINAL_RESPONSE_MESSAGE,  # 空最终响应占位文案
    build_budget_exhausted_finalization_message,  # 预算耗尽时的收尾提示
    build_finalization_retry_message,  # 终结化重试提示
    build_goal_continue_message,  # 目标延续提示
    build_length_recovery_message,  # 输出截断后的恢复提示
    ensure_nonempty_tool_result,  # 确保工具结果非空
    is_blank_text,  # 判断文本是否空白
    repeated_external_lookup_error,  # 重复外部查找错误检测
    repeated_tool_call_error,  # 重复工具调用错误检测（死循环兜底）
    repeated_workspace_violation_error,  # 重复工作区越界错误检测
)

GoalContinueMessage = str | Callable[[], str | None]  # 目标延续消息：字符串或返回字符串/None 的回调

_DEFAULT_ERROR_MESSAGE = "Sorry, I encountered an error calling the AI model."  # 默认错误文案
_ARREARAGE_ERROR_MESSAGE = (  # 欠费/额度不足时的错误文案
    "The AI provider rejected the request because the API key is out of quota or the "
    "account is in arrears. Please top up / check the billing status of your API key and try again."
)
_PERSISTED_MODEL_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model error.]"  # 模型出错时持久化的占位消息
_MAX_EMPTY_RETRIES = 2  # 空响应最大重试次数
_MAX_LENGTH_RECOVERIES = 3  # 输出被截断（finish_reason=length）时的最大恢复次数
_MAX_INJECTIONS_PER_TURN = 3  # 单轮允许注入的最大用户消息数
_MAX_INJECTION_CYCLES = 5  # 单次执行允许的注入循环上限，避免无限延续
_SNIP_SAFETY_BUFFER = 1024  # 历史裁剪的安全缓冲 token 数
_MICROCOMPACT_KEEP_RECENT = 10  # 微压缩时保留的最近可压缩工具结果条数
_MICROCOMPACT_MIN_CHARS = 500  # 微压缩仅处理超过该字符数的工具结果
_COMPACTABLE_TOOLS = frozenset({  # 可被微压缩的工具名集合
    "read_file", "exec", "grep", "find_files",
    "web_search", "web_fetch", "list_dir", "list_exec_sessions",
})
# read_file is the recovery path for persisted results; exempting it prevents persist->read->persist loops.
_TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS = frozenset({"read_file"})  # 工具结果落盘豁免集合（read_file 是落盘结果的恢复路径，豁免可避免 persist→read→persist 死循环）
_BACKFILL_CONTENT = "[Tool result unavailable — call was interrupted or lost]"  # 孤儿 tool_use 的回填占位内容

# Backward-compatible module attribute for tests/extensions that monkeypatch
# the former single-file tracker hook. Runtime uses prepare_file_edit_trackers.
prepare_file_edit_tracker = _prepare_file_edit_tracker  # 兼容旧测试/扩展的模块级属性，运行时实际使用 prepare_file_edit_trackers


def _latest_user_query(messages: list[dict[str, Any]]) -> str:
    """返回最近一条 user 消息的文本，用于工具检索。

    当不存在 user 消息时（例如由 tool_result 消息驱动的延续轮次）回退为空字符串。

    参数:
        messages: 消息列表。

    返回:
        最近一条 user 消息的文本内容；不存在时返回空字符串。
    """
    for msg in reversed(messages):  # 从后向前查找最近的 user 消息
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # Multimodal: concatenate text parts  # 多模态：拼接文本分片
                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                if parts:
                    return " ".join(parts)
            return ""
    return ""


@dataclass(slots=True)
class AgentRunSpec:
    """单次 Agent 执行的配置规格。

    职责与项目角色：
    - 封装执行一次工具型 LLM 循环所需的全部输入：初始消息、工具注册表、模型名、
      迭代上限、生成参数、生命周期钩子、各类回调等；
    - 由 ``AgentLoop`` 在每个回合构造后传给 ``AgentRunner.run``。
    """

    initial_messages: list[dict[str, Any]]  # 初始消息列表（含 system 与历史）
    tools: ToolRegistry  # 工具注册表
    model: str  # 模型名
    max_iterations: int  # 最大迭代次数
    max_tool_result_chars: int  # 工具结果最大字符数（超出将截断或落盘）
    temperature: float | None = None  # 采样温度
    max_tokens: int | None = None  # 单次生成最大 token 数
    reasoning_effort: str | None = None  # 推理强度（部分模型支持）
    hook: AgentHook | None = None  # 生命周期钩子
    error_message: str | None = _DEFAULT_ERROR_MESSAGE  # 自定义错误文案
    max_iterations_message: str | None = None  # 达到最大迭代时的提示文案
    concurrent_tools: bool = False  # 是否允许并发执行可并发工具
    fail_on_tool_error: bool = False  # 工具出错时是否中止整轮
    workspace: Path | None = None  # 工作区路径（用于结果落盘等）
    session_key: str | None = None  # 会话标识
    context_window_tokens: int | None = None  # 上下文窗口 token 数
    context_block_limit: int | None = None  # 上下文块 token 上限
    provider_retry_mode: str = "standard"  # 提供商重试模式
    progress_callback: Any | None = None  # 进度回调
    stream_progress_deltas: bool = True  # 是否以增量方式推送进度
    retry_wait_callback: Any | None = None  # 重试等待回调
    checkpoint_callback: Any | None = None  # 检查点回调（用于持久化中间状态）
    injection_callback: Any | None = None  # 注入回调（排空待注入的 user 消息）
    llm_timeout_s: float | None = None  # LLM 调用超时秒数
    goal_active_predicate: Callable[[], bool] | None = None  # 目标是否仍活跃的判定
    goal_continue_message: GoalContinueMessage | None = None  # 目标延续消息
    finalize_on_max_iterations: bool = True  # 达到最大迭代时是否尝试收尾
    turn_id: str = ""  # 回合 ID（用于追踪）
    runtime_publisher: Any | None = None  # 运行时事件总线发布器
    inbound_msg: Any | None = None  # 入站原始消息（用于追踪回执）


@dataclass(slots=True)
class AgentRunResult:
    """一次 Agent 执行的输出结果。

    职责与项目角色：
    - 封装执行结束后的最终内容、完整消息列表、已用工具、token 用量、
      停止原因、错误信息、工具事件及是否发生过注入；
    - 由 ``AgentRunner.run`` 返回，供上层（``AgentLoop``）据此持久化与回复。
    """

    final_content: str | None  # 最终回复内容
    messages: list[dict[str, Any]]  # 执行结束后的完整消息列表
    tools_used: list[str] = field(default_factory=list)  # 成功执行的工具名列表
    usage: dict[str, int] = field(default_factory=dict)  # token 用量统计
    stop_reason: str = "completed"  # 停止原因（completed/max_iterations/error 等）
    error: str | None = None  # 错误信息（无错时为 None）
    tool_events: list[dict[str, str]] = field(default_factory=list)  # 工具调用事件列表
    had_injections: bool = False  # 是否发生过消息注入


class AgentRunner:
    """运行工具型 LLM 循环，不涉及产品层关注点。

    职责与项目角色：
    - 项目中 Agent 的核心执行引擎，与渠道/会话/调度等产品层解耦；
    - 通过 ``run`` 方法按迭代循环调用 LLM、解析并执行工具调用、回填结果，
      并在过程中处理流式输出、推理内容、注入消息、上下文压缩、错误恢复
      与安全边界（SSRF、工作区越界）；
    - 通过传入的 ``AgentHook`` 暴露生命周期扩展点。

    典型用法：由 ``AgentLoop`` 持有并调用 ``run(spec)``。
    """

    def __init__(self, provider: LLMProvider):
        self.provider = provider  # LLM 提供商实例

    @staticmethod
    def _publish_tool_trace(
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        status: str,
        *,
        duration_ms: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """发布工具调用追踪事件到 RuntimeEventBus（非阻塞）。

        参数:
            spec: 执行规格（提供 publisher、turn_id 等上下文）；
            tool_call: 工具调用请求；
            status: 状态（started/completed/failed）；
            duration_ms: 调用耗时（毫秒）；
            detail: 附加详情。
        """
        publisher = spec.runtime_publisher
        if publisher is None or not spec.turn_id:  # 无发布器或无回合 ID 时直接跳过
            return
        args_summary: str = ""
        try:
            if tool_call.arguments:
                args_summary = str(tool_call.arguments)
                if len(args_summary) > 200:  # 截断过长的参数摘要
                    args_summary = args_summary[:200] + "..."
        except Exception:
            args_summary = ""
        publisher.publish_trace(
            msg=spec.inbound_msg,
            session_key=spec.session_key or "default",
            turn_id=spec.turn_id,
            phase="tool_call",
            step=tool_call.name,
            status=status,
            duration_ms=duration_ms,
            detail={
                "call_id": tool_call.id,
                "arguments": args_summary,
                **(detail or {}),
            },
        )

    @staticmethod
    def _publish_llm_trace(
        spec: AgentRunSpec,
        status: str,
        *,
        duration_ms: float | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """发布 LLM 调用追踪事件到 RuntimeEventBus（非阻塞）。

        参数:
            spec: 执行规格；
            status: 状态（started/completed）；
            duration_ms: 调用耗时（毫秒）；
            detail: 附加详情（如 token 用量）。
        """
        publisher = spec.runtime_publisher
        if publisher is None or not spec.turn_id:  # 无发布器或无回合 ID 时直接跳过
            return
        publisher.publish_trace(
            msg=spec.inbound_msg,
            session_key=spec.session_key or "default",
            turn_id=spec.turn_id,
            phase="llm_call",
            step=spec.model,
            status=status,
            duration_ms=duration_ms,
            detail={
                "model": spec.model,
                **(detail or {}),
            },
        )

    @staticmethod
    def _merge_message_content(left: Any, right: Any) -> str | list[dict[str, Any]]:
        """合并两条消息的内容，保持文本或分块结构。

        参数:
            left: 左侧内容（str/list/None）；
            right: 右侧内容（str/list/None）。

        返回:
            合并后的内容：两者均为字符串时返回以空行分隔的字符串；
            否则返回拼接后的分块列表。
        """
        if isinstance(left, str) and isinstance(right, str):
            return f"{left}\n\n{right}" if left else right

        def _to_blocks(value: Any) -> list[dict[str, Any]]:
            """将任意内容统一为分块列表形式。"""
            if isinstance(value, list):
                return [
                    item if isinstance(item, dict) else {"type": "text", "text": str(item)}
                    for item in value
                ]
            if value is None:
                return []
            return [{"type": "text", "text": str(value)}]

        return _to_blocks(left) + _to_blocks(right)

    @classmethod
    def _append_injected_messages(
        cls,
        messages: list[dict[str, Any]],
        injections: list[dict[str, Any]],
    ) -> None:
        """追加注入的 user 消息，同时保持角色交替。

        当末尾已是 user 消息且注入也是 user 时，合并内容而非新增条目，
        避免出现连续 user 消息违反模型的角色交替约束。

        参数:
            messages: 待追加的消息列表（原地修改）；
            injections: 待注入的消息列表。
        """
        for injection in injections:
            if (
                messages
                and injection.get("role") == "user"
                and messages[-1].get("role") == "user"
            ):
                # 末尾已是 user：合并内容以保持角色交替
                merged = dict(messages[-1])
                merged["content"] = cls._merge_message_content(
                    merged.get("content"),
                    injection.get("content"),
                )
                messages[-1] = merged
                continue
            messages.append(injection)

    async def _try_drain_injections(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        assistant_message: dict[str, Any] | None,
        injection_cycles: int,
        *,
        phase: str = "after error",
        iteration: int | None = None,
        allow_goal_continue: bool = False,
    ) -> tuple[bool, int]:
        """排空待注入消息。返回 (是否继续, 更新后的注入循环数)。

        若发现注入且未超过 ``_MAX_INJECTION_CYCLES``，则将其追加到 *messages*
        （当 *assistant_message* 与 *iteration* 同时提供时还会发出检查点），
        并返回 (True, cycles+1) 以便调用方继续迭代循环；否则返回 (False, cycles)。

        参数:
            spec: 执行规格；
            messages: 消息列表（原地修改）；
            assistant_message: 待追加的助手消息（可为 None）；
            injection_cycles: 当前注入循环计数；
            phase: 阶段描述（用于日志）；
            iteration: 当前迭代序号（用于检查点）；
            allow_goal_continue: 是否允许目标延续注入。
        """
        injections: list[dict[str, Any]] = []
        real_injection = False
        if injection_cycles < _MAX_INJECTION_CYCLES:  # 未超注入循环上限时尝试排空
            injections = await self._drain_injections(spec)
            real_injection = bool(injections)
        if not injections and allow_goal_continue and assistant_message is not None:
            # 无外部注入但允许目标延续：检查目标是否仍活跃
            predicate = spec.goal_active_predicate
            if predicate is not None and predicate():
                injections = [self._build_goal_continue_message(spec)]
        if not injections:  # 无任何注入：无需继续
            return False, injection_cycles
        if real_injection:
            injection_cycles += 1
        if assistant_message is not None:
            messages.append(assistant_message)
            if iteration is not None:
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "final_response",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [],
                    },
                )
        self._append_injected_messages(messages, injections)
        if real_injection:
            logger.info(
                "Injected {} follow-up message(s) {} ({}/{})",
                len(injections), phase, injection_cycles, _MAX_INJECTION_CYCLES,
            )
        else:
            logger.info("Injected sustained-goal continuation {}", phase)
        return True, injection_cycles

    def _build_goal_continue_message(self, spec: AgentRunSpec) -> dict[str, str]:
        """构造目标延续消息，支持字符串或回调形式。

        参数:
            spec: 执行规格（提供 goal_continue_message）。

        返回:
            包含 role=user 与延续提示内容的消息字典。
        """
        custom = spec.goal_continue_message
        if callable(custom):  # 回调形式：调用获取动态内容
            try:
                custom = custom()
            except Exception:
                logger.exception("goal_continue_message callback failed")
                custom = None
        return build_goal_continue_message(custom)

    async def _drain_injections(self, spec: AgentRunSpec) -> list[dict[str, Any]]:
        """通过注入回调排空待注入的 user 消息。

        返回规范化的 user 消息列表（受 ``_MAX_INJECTIONS_PER_TURN`` 上限约束）；
        无可注入内容时返回空列表。超出上限的消息会被记录日志，避免静默丢失。

        参数:
            spec: 执行规格（提供 injection_callback）。

        返回:
            待注入的 user 消息列表。
        """
        if spec.injection_callback is None:
            return []
        try:
            signature = inspect.signature(spec.injection_callback)
            # 检测回调是否接受 limit 参数或 **kwargs
            accepts_limit = (
                "limit" in signature.parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in signature.parameters.values()
                )
            )
            if accepts_limit:
                items = await spec.injection_callback(limit=_MAX_INJECTIONS_PER_TURN)
            else:
                items = await spec.injection_callback()
        except Exception:
            logger.exception("injection_callback failed")
            return []
        if not items:
            return []
        injected_messages: list[dict[str, Any]] = []
        for item in items:
            if item is None:
                continue
            if isinstance(item, dict) and item.get("role") == "user" and "content" in item:
                if self._has_injection_content(item.get("content")):
                    injected_messages.append(item)
                continue
            if isinstance(item, dict):  # 非 user 角色的字典直接跳过
                continue
            content = getattr(item, "content") if hasattr(item, "content") else str(item)
            if self._has_injection_content(content):
                injected_messages.append({"role": "user", "content": content})
        if len(injected_messages) > _MAX_INJECTIONS_PER_TURN:  # 超限：截断并告警
            dropped = len(injected_messages) - _MAX_INJECTIONS_PER_TURN
            logger.warning(
                "Injection callback returned {} messages, capping to {} ({} dropped)",
                len(injected_messages), _MAX_INJECTIONS_PER_TURN, dropped,
            )
            injected_messages = injected_messages[:_MAX_INJECTIONS_PER_TURN]
        return injected_messages

    @staticmethod
    def _has_injection_content(content: Any) -> bool:
        """判断注入内容是否有效（非空字符串或非空列表）。

        参数:
            content: 待检测的内容。

        返回:
            内容有效返回 True，否则 False。
        """
        if content is None:
            return False
        if isinstance(content, str):
            return bool(content.strip())
        if isinstance(content, list):
            return bool(content)
        return True

    async def run(self, spec: AgentRunSpec) -> AgentRunResult:
        """执行 Agent 循环并返回结果。

        流程：调用 before_run 钩子 → 执行核心循环 → 正常时更新上下文并调用
        after_run；取消时回写上下文并重新抛出；异常时调用 on_error 并重新抛出；
        finally 中调用 on_finally（异常时容忍其自身出错）。

        参数:
            spec: 执行规格。

        返回:
            执行结果 ``AgentRunResult``。

        异常:
            asyncio.CancelledError: 被取消时重新抛出；
            Exception: 其它异常在调用 on_error 后重新抛出。
        """
        hook = spec.hook or AgentHook()
        messages = list(spec.initial_messages)
        context = AgentRunHookContext(messages=deepcopy(messages))

        try:
            await hook.before_run(context)
            result = await self._run_core(spec, hook, messages)
        except asyncio.CancelledError as exc:
            # 被取消：回写上下文并重新抛出，不调用 on_error
            context.messages = deepcopy(messages)
            context.stop_reason = "cancelled"
            context.error = None
            context.exception = exc
            raise
        except Exception as exc:
            # 异常：回写上下文，调用 on_error 后重新抛出
            context.messages = deepcopy(messages)
            context.stop_reason = "error"
            context.error = f"Error: {type(exc).__name__}: {exc}"
            context.exception = exc
            await hook.on_error(context)
            raise
        else:
            # 正常完成：把结果同步到上下文并调用 after_run
            context.messages = deepcopy(result.messages)
            context.final_content = result.final_content
            context.tools_used = list(result.tools_used)
            context.usage = dict(result.usage)
            context.stop_reason = result.stop_reason
            context.error = result.error
            context.tool_events = deepcopy(result.tool_events)
            context.had_injections = result.had_injections
            context.exception = None
            if context.error is not None:
                await hook.on_error(context)
            await hook.after_run(context)
            return result
        finally:
            # finally：始终回写最新消息并调用 on_finally；异常路径下容忍其自身出错
            context.messages = deepcopy(messages)
            if context.exception is None:
                await hook.on_finally(context)
            else:
                try:
                    await hook.on_finally(context)
                except Exception:
                    logger.exception(
                        "AgentHook.on_finally error after {}",
                        context.stop_reason or "run exception",
                    )

    async def _run_core(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
    ) -> AgentRunResult:
        """执行核心迭代循环：调用 LLM → 解析工具调用 → 执行工具 → 回填结果。

        每轮迭代的处理顺序：
        1. 上下文治理（清理孤儿 tool 结果、回填缺失、微压缩、预算裁剪）；
        2. 调用 before_iteration 钩子并请求模型；
        3. 提取推理内容、累计 token 用量；
        4. 若需执行工具：构造 assistant 消息、执行工具、回填 tool 消息、排空注入；
        5. 否则进入终结化路径：处理空响应重试、截断恢复、注入、错误/空响应收尾；
        6. 达到最大迭代时尝试收尾或回退。

        参数:
            spec: 执行规格；
            hook: 生命周期钩子；
            messages: 消息列表（原地修改）。

        返回:
            执行结果 ``AgentRunResult``。
        """
        final_content: str | None = None
        tools_used: list[str] = []
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}  # 累计 token 用量
        error: str | None = None
        stop_reason = "completed"
        tool_events: list[dict[str, str]] = []
        external_lookup_counts: dict[str, int] = {}  # 外部查找重复次数（节流）
        # Per-turn throttle for repeated attempts against the same outside target.
        workspace_violation_counts: dict[str, int] = {}  # 工作区越界重复次数（逐轮节流）
        # Per-turn throttle for repeated identical tool calls (generic loop guard).
        tool_call_counts: dict[str, int] = {}  # 重复工具调用次数（死循环兜底）
        empty_content_retries = 0  # 空响应重试计数
        length_recovery_count = 0  # 截断恢复计数
        had_injections = False  # 是否发生过注入
        injection_cycles = 0  # 注入循环计数

        for iteration in range(spec.max_iterations):
            try:
                # Keep the persisted conversation untouched. Context governance
                # may repair or compact historical messages for the model, but
                # those synthetic edits must not shift the append boundary used
                # later when the caller saves only the new turn.
                # 保持持久化会话不变：上下文治理仅作用于送入模型的副本，
                # 其合成编辑不得偏移调用方后续保存新回合时使用的追加边界。
                messages_for_model = self._drop_orphan_tool_results(messages)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                messages_for_model = self._microcompact(messages_for_model)
                messages_for_model = self._apply_tool_result_budget(spec, messages_for_model)
                messages_for_model = self._snip_history(spec, messages_for_model)
                # Snipping may have created new orphans; clean them up.  # 裁剪可能产生新孤儿，再次清理
                messages_for_model = self._drop_orphan_tool_results(messages_for_model)
                messages_for_model = self._backfill_missing_tool_results(messages_for_model)
            except Exception:
                logger.exception(
                    "Context governance failed on turn {} for {}; applying minimal repair",
                    iteration,
                    spec.session_key or "default",
                )
                # 上下文治理失败：退回到最小修复，再不行直接用原始消息
                try:
                    messages_for_model = self._drop_orphan_tool_results(messages)
                    messages_for_model = self._backfill_missing_tool_results(messages_for_model)
                except Exception:
                    messages_for_model = messages
            context = AgentHookContext(
                iteration=iteration,
                messages=messages,
                session_key=spec.session_key,
                model=spec.model,
            )
            await hook.before_iteration(context)
            llm_t0 = time.perf_counter()
            self._publish_llm_trace(spec, "started", detail={"iteration": iteration})
            response = await self._request_model(spec, messages_for_model, hook, context)
            llm_duration_ms = (time.perf_counter() - llm_t0) * 1000
            context.response = response
            context.tool_calls = list(response.tool_calls)

            reasoning_text, cleaned_content = extract_reasoning(  # 分离推理内容与正文
                response.reasoning_content,
                response.thinking_blocks,
                response.content,
            )
            response.content = cleaned_content
            raw_usage = self._usage_or_estimate(spec, messages_for_model, response)
            context.usage = dict(raw_usage)
            self._accumulate_usage(usage, raw_usage)
            self._publish_llm_trace(
                spec, "completed",
                duration_ms=llm_duration_ms,
                detail={
                    "iteration": iteration,
                    "prompt_tokens": raw_usage.get("prompt_tokens", 0),
                    "completion_tokens": raw_usage.get("completion_tokens", 0),
                    "has_tool_calls": response.has_tool_calls,
                },
            )
            if reasoning_text and not context.streamed_reasoning:  # 未流式输出过推理时补发
                await hook.emit_reasoning(reasoning_text)
                await hook.emit_reasoning_end()
                context.streamed_reasoning = True

            if response.should_execute_tools:  # 需要执行工具的分支
                context.tool_calls = list(response.tool_calls)
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=True)

                assistant_message = build_assistant_message(
                    response.content or "",
                    tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                messages.append(assistant_message)
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "awaiting_tools",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": [],
                        "pending_tool_calls": [tc.to_openai_tool_call() for tc in response.tool_calls],
                    },
                )

                await hook.before_execute_tools(context)

                results, new_events, fatal_error = await self._execute_tools(
                    spec,
                    response.tool_calls,
                    external_lookup_counts,
                    workspace_violation_counts,
                    tool_call_counts,
                )
                tool_events.extend(new_events)
                tools_used.extend(
                    tool_call.name
                    for tool_call, event in zip(response.tool_calls, new_events)
                    if event.get("status") == "ok"
                )
                context.tool_results = list(results)
                context.tool_events = list(new_events)
                completed_tool_results: list[dict[str, Any]] = []
                for tool_call, result in zip(response.tool_calls, results):
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                        "content": self._normalize_tool_result(
                            spec,
                            tool_call.id,
                            tool_call.name,
                            result,
                        ),
                    }
                    messages.append(tool_message)
                    completed_tool_results.append(tool_message)
                if fatal_error is not None:  # 致命工具错误：收尾并尝试注入延续
                    error = f"Error: {type(fatal_error).__name__}: {fatal_error}"
                    final_content = error
                    stop_reason = "tool_error"
                    self._append_final_message(messages, final_content)
                    context.final_content = final_content
                    context.error = error
                    context.stop_reason = stop_reason
                    await hook.after_iteration(context)
                    should_continue, injection_cycles = await self._try_drain_injections(
                        spec, messages, None, injection_cycles,
                        phase="after tool error",
                    )
                    if should_continue:
                        had_injections = True
                        continue
                    break
                await self._emit_checkpoint(
                    spec,
                    {
                        "phase": "tools_completed",
                        "iteration": iteration,
                        "model": spec.model,
                        "assistant_message": assistant_message,
                        "completed_tool_results": completed_tool_results,
                        "pending_tool_calls": [],
                    },
                )
                empty_content_retries = 0  # 工具执行后重置空响应与截断计数
                length_recovery_count = 0
                # Checkpoint 1: drain injections after tools, before next LLM call  # 检查点 1：工具执行后、下一次 LLM 调用前排空注入
                _drained, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after tool execution",
                )
                if _drained:
                    had_injections = True
                await hook.after_iteration(context)
                continue

            if response.has_tool_calls:  # finish_reason 非 tool 但带 tool_calls：忽略并告警
                logger.warning(
                    "Ignoring tool calls under finish_reason='{}' for {}",
                    response.finish_reason,
                    spec.session_key or "default",
                )

            clean = hook.finalize_content(context, response.content)
            if response.finish_reason != "error" and is_blank_text(clean):  # 空响应处理
                empty_content_retries += 1
                if empty_content_retries < _MAX_EMPTY_RETRIES:
                    logger.warning(
                        "Empty response on turn {} for {} ({}/{}); retrying",
                        iteration,
                        spec.session_key or "default",
                        empty_content_retries,
                        _MAX_EMPTY_RETRIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=False)
                    await hook.after_iteration(context)
                    continue
                # 重试次数用尽：尝试终结化重试
                logger.warning(
                    "Empty response on turn {} for {} after {} retries; attempting finalization",
                    iteration,
                    spec.session_key or "default",
                    empty_content_retries,
                )
                if hook.wants_streaming():
                    await hook.on_stream_end(context, resuming=False)
                retry_messages = self._finalization_retry_messages(messages_for_model)
                response = await self._request_finalization_retry(spec, messages_for_model)
                retry_usage = self._usage_or_estimate(spec, retry_messages, response)
                self._accumulate_usage(usage, retry_usage)
                raw_usage = self._merge_usage(raw_usage, retry_usage)
                context.response = response
                context.usage = dict(raw_usage)
                context.tool_calls = list(response.tool_calls)
                clean = hook.finalize_content(context, response.content)

            if response.finish_reason == "length" and not is_blank_text(clean):  # 输出被截断：追加恢复提示后继续
                length_recovery_count += 1
                if length_recovery_count <= _MAX_LENGTH_RECOVERIES:
                    logger.info(
                        "Output truncated on turn {} for {} ({}/{}); continuing",
                        iteration,
                        spec.session_key or "default",
                        length_recovery_count,
                        _MAX_LENGTH_RECOVERIES,
                    )
                    if hook.wants_streaming():
                        await hook.on_stream_end(context, resuming=True)
                    messages.append(build_assistant_message(
                        clean,
                        reasoning_content=response.reasoning_content,
                        thinking_blocks=response.thinking_blocks,
                    ))
                    messages.append(build_length_recovery_message())
                    await hook.after_iteration(context)
                    continue

            assistant_message: dict[str, Any] | None = None
            if response.finish_reason != "error" and not is_blank_text(clean):
                assistant_message = build_assistant_message(
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

            # Check for mid-turn injections BEFORE signaling stream end.
            # If injections are found we keep the stream alive (resuming=True)
            # so streaming channels don't prematurely finalize the card.
            # 在通知流结束前检查中途注入：若发现注入则保持流活跃（resuming=True），
            # 避免流式渠道过早终结消息卡片。
            should_continue, injection_cycles = await self._try_drain_injections(
                spec, messages, assistant_message, injection_cycles,
                phase="after final response",
                iteration=iteration,
                allow_goal_continue=True,
            )
            if should_continue:
                had_injections = True

            if hook.wants_streaming():
                await hook.on_stream_end(context, resuming=should_continue)

            if should_continue:  # 有注入：继续下一轮迭代
                await hook.after_iteration(context)
                continue

            if response.finish_reason == "error":  # LLM 错误分支
                if LLMProvider.is_arrearage_response(response):
                    final_content = _ARREARAGE_ERROR_MESSAGE
                else:
                    final_content = clean or spec.error_message or _DEFAULT_ERROR_MESSAGE
                stop_reason = "error"
                error = final_content
                self._append_model_error_placeholder(messages)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after LLM error",
                )
                if should_continue:
                    had_injections = True
                    continue
                break
            if is_blank_text(clean):  # 最终仍为空：使用占位文案收尾
                final_content = EMPTY_FINAL_RESPONSE_MESSAGE
                stop_reason = "empty_final_response"
                error = final_content
                self._append_final_message(messages, final_content)
                context.final_content = final_content
                context.error = error
                context.stop_reason = stop_reason
                await hook.after_iteration(context)
                should_continue, injection_cycles = await self._try_drain_injections(
                    spec, messages, None, injection_cycles,
                    phase="after empty response",
                )
                if should_continue:
                    had_injections = True
                    continue
                break

            messages.append(assistant_message or build_assistant_message(  # 正常完成：追加最终 assistant 消息
                clean,
                reasoning_content=response.reasoning_content,
                thinking_blocks=response.thinking_blocks,
            ))
            await self._emit_checkpoint(
                spec,
                {
                    "phase": "final_response",
                    "iteration": iteration,
                    "model": spec.model,
                    "assistant_message": messages[-1],
                    "completed_tool_results": [],
                    "pending_tool_calls": [],
                },
            )
            final_content = clean
            context.final_content = final_content
            context.stop_reason = stop_reason
            await hook.after_iteration(context)
            break
        else:
            # 达到最大迭代次数：排空剩余注入，再尝试收尾或回退
            stop_reason = "max_iterations"
            # Drain any remaining injections so they are appended to the
            # conversation history instead of being re-published as
            # independent inbound messages by _dispatch's finally block.
            # We include them before the no-tools finalization pass so the
            # final response can account for every known follow-up.
            # 排空剩余注入，使其追加到会话历史，而非被 _dispatch 的 finally
            # 作为独立入站消息重新发布；在无工具收尾前纳入，确保最终回复覆盖所有已知后续。
            drained_after_max_iterations, injection_cycles = await self._try_drain_injections(
                spec, messages, None, injection_cycles,
                phase="after max_iterations",
            )
            if drained_after_max_iterations:
                had_injections = True
            final_content = None
            if spec.finalize_on_max_iterations:  # 尝试无工具收尾
                final_content = await self._try_finalize_after_max_iterations(
                    spec,
                    hook,
                    messages,
                    usage,
                )
            if final_content is None:  # 收尾失败：使用回退文案
                final_content = self._max_iterations_fallback(spec)
            self._append_final_message(messages, final_content)

        return AgentRunResult(
            final_content=final_content,
            messages=messages,
            tools_used=tools_used,
            usage=usage,
            stop_reason=stop_reason,
            error=error,
            tool_events=tool_events,
            had_injections=had_injections,
        )

    def _build_request_kwargs(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """构造调用 LLM 提供商所需的参数字典。

        参数:
            spec: 执行规格；
            messages: 送入模型的消息列表；
            tools: 工具定义列表（终结化请求时为 None）。

        返回:
            包含 messages/tools/model/retry_mode 及可选生成参数的字典。
        """
        kwargs: dict[str, Any] = {
            "messages": messages,
            "tools": tools,
            "model": spec.model,
            "retry_mode": spec.provider_retry_mode,
            "on_retry_wait": spec.retry_wait_callback,
        }
        if spec.temperature is not None:
            kwargs["temperature"] = spec.temperature
        if spec.max_tokens is not None:
            kwargs["max_tokens"] = spec.max_tokens
        if spec.reasoning_effort is not None:
            kwargs["reasoning_effort"] = spec.reasoning_effort
        return kwargs

    async def _request_model(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        hook: AgentHook,
        context: AgentHookContext,
    ):
        """请求 LLM 并返回响应，支持流式、进度流式与非流式三种模式。

        根据钩子与配置选择调用方式：
        - 流式（wants_streaming）：通过 on_content_delta/on_thinking_delta 回调推送增量；
        - 进度流式（wants_progress_streaming）：把增量经 think 提取后推给 progress_callback；
        - 非流式：直接 chat_with_retry。
        同时处理 LLM 超时（流式不应用外层超时）、文件编辑流式追踪与超时降级。

        参数:
            spec: 执行规格；
            messages: 送入模型的消息列表；
            hook: 生命周期钩子；
            context: 迭代上下文。

        返回:
            LLM 响应 ``LLMResponse``；超时返回 error_kind=timeout 的响应。
        """
        timeout_s: float | None = spec.llm_timeout_s
        if timeout_s is None:
            # Default to a finite timeout to avoid per-session lock starvation when an LLM
            # request hangs indefinitely (e.g. gateway/network stall).
            # Set BISCUITBOT_LLM_TIMEOUT_S=0 to disable.
            raw = os.environ.get("BISCUITBOT_LLM_TIMEOUT_S", "300").strip()
            try:
                timeout_s = float(raw)
            except (TypeError, ValueError):
                timeout_s = 300.0
        if timeout_s is not None and timeout_s <= 0:
            timeout_s = None

        kwargs = self._build_request_kwargs(
            spec,
            messages,
            tools=spec.tools.get_always_include_definitions(),
        )
        wants_streaming = hook.wants_streaming()
        wants_progress_streaming = (
            not wants_streaming
            and spec.stream_progress_deltas
            and spec.progress_callback is not None
            and getattr(self.provider, "supports_progress_deltas", False) is True
        )

        progress_state: dict[str, bool] | None = None
        live_file_edits: StreamingFileEditTracker | None = None

        progress_cb = spec.progress_callback
        if (
            progress_cb is not None
            and on_progress_accepts_file_edit_events(progress_cb)
        ):
            async def _emit_live_file_edits(events: list[dict[str, Any]]) -> None:
                await invoke_file_edit_progress(progress_cb, events)

            live_file_edits = StreamingFileEditTracker(
                workspace=spec.workspace,
                tools=spec.tools,
                emit=_emit_live_file_edits,
            )

        async def _tool_call_delta(delta: dict[str, Any]) -> None:
            if live_file_edits is not None:
                await live_file_edits.update(delta)

        if wants_streaming:
            async def _stream(delta: str) -> None:
                if delta:
                    context.streamed_content = True
                await hook.on_stream(context, delta)

            async def _thinking(delta: str) -> None:
                if not delta:
                    return
                context.streamed_reasoning = True
                await hook.emit_reasoning(delta)

            async def _stream_recover() -> None:
                await hook.on_stream_end(context, resuming=True)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream,
                on_thinking_delta=_thinking,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
                on_stream_recover=_stream_recover,
            )
        elif wants_progress_streaming:
            assert progress_cb is not None  # wants_progress_streaming requires progress_callback
            stream_buf = ""
            think_extractor = IncrementalThinkExtractor()
            progress_state = {"reasoning_open": False}

            async def _stream_progress(delta: str) -> None:
                nonlocal stream_buf
                if not delta:
                    return
                prev_clean = strip_think(stream_buf)
                stream_buf += delta
                new_clean = strip_think(stream_buf)
                incremental = new_clean[len(prev_clean):]

                if await think_extractor.feed(stream_buf, hook.emit_reasoning):
                    context.streamed_reasoning = True
                    progress_state["reasoning_open"] = True

                if incremental:
                    if progress_state["reasoning_open"]:
                        await hook.emit_reasoning_end()
                        progress_state["reasoning_open"] = False
                    context.streamed_content = True
                    await progress_cb(incremental)

            coro = self.provider.chat_stream_with_retry(
                **kwargs,
                on_content_delta=_stream_progress,
                on_tool_call_delta=_tool_call_delta if live_file_edits is not None else None,
            )
        else:
            coro = self.provider.chat_with_retry(**kwargs)

        # Streaming requests already have provider-level idle timeouts
        # (BISCUITBOT_STREAM_IDLE_TIMEOUT_S). Do not also apply the outer wall-clock
        # LLM timeout here, or healthy long reasoning streams can be killed just
        # because total elapsed time exceeded BISCUITBOT_LLM_TIMEOUT_S.
        outer_timeout_s = None if (wants_streaming or wants_progress_streaming) else timeout_s
        try:
            response = (
                await coro if outer_timeout_s is None
                else await asyncio.wait_for(coro, timeout=outer_timeout_s)
            )
            if live_file_edits is not None:
                await live_file_edits.flush()
                if response.should_execute_tools:
                    live_file_edits.apply_final_call_ids(response.tool_calls)
                await live_file_edits.error_unmatched(
                    response.tool_calls if response.should_execute_tools else [],
                    "Tool call did not complete.",
                )
        except asyncio.TimeoutError:
            if outer_timeout_s is None:
                return LLMResponse(
                    content="Error calling LLM: stream stalled",
                    finish_reason="error",
                    error_kind="timeout",
                )
            return LLMResponse(
                content=f"Error calling LLM: timed out after {outer_timeout_s:g}s",
                finish_reason="error",
                error_kind="timeout",
            )
        if progress_state and progress_state.get("reasoning_open"):
            await hook.emit_reasoning_end()
        return response

    async def _request_finalization_retry(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ):
        """空响应重试用尽后，附加终结化提示并请求无工具回复。

        参数:
            spec: 执行规格；
            messages: 送入模型的消息列表。

        返回:
            LLM 响应 ``LLMResponse``。
        """
        retry_messages = self._finalization_retry_messages(messages)
        return await self._request_no_tools(spec, retry_messages)

    @staticmethod
    def _finalization_retry_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """构造终结化重试消息：在末尾追加终结化提示。"""
        retry_messages = list(messages)
        retry_messages.append(build_finalization_retry_message())
        return retry_messages

    async def _try_finalize_after_max_iterations(
        self,
        spec: AgentRunSpec,
        hook: AgentHook,
        messages: list[dict[str, Any]],
        usage: dict[str, int],
    ) -> str | None:
        """达到最大迭代后尝试无工具收尾，返回收尾文本或 None。

        参数:
            spec: 执行规格；
            hook: 生命周期钩子；
            messages: 消息列表；
            usage: token 用量字典（会累加本次收尾的用量）。

        返回:
            收尾文本；收尾失败或返回错误/工具调用时返回 None。
        """
        retry_messages = self._budget_exhausted_finalization_messages(messages)
        try:
            response = await self._request_no_tools(spec, retry_messages)
        except Exception:
            logger.exception(
                "Budget-exhausted finalization failed for {}; using fallback",
                spec.session_key or "default",
            )
            return None

        raw_usage = self._usage_or_estimate(spec, retry_messages, response)
        self._accumulate_usage(usage, raw_usage)
        if response.finish_reason == "error" or response.has_tool_calls:
            logger.warning(
                "Budget-exhausted finalization returned finish_reason='{}' "
                "with {} tool call(s) for {}; using fallback",
                response.finish_reason,
                len(response.tool_calls),
                spec.session_key or "default",
            )
            return None

        context = AgentHookContext(
            iteration=spec.max_iterations,
            messages=messages,
            response=response,
            usage=dict(raw_usage),
            session_key=spec.session_key,
            model=spec.model,
        )
        clean = hook.finalize_content(context, response.content)
        if is_blank_text(clean):
            return None
        return clean

    async def _request_no_tools(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> LLMResponse:
        """不带工具定义地请求 LLM（用于收尾/终结化场景）。

        参数:
            spec: 执行规格；
            messages: 送入模型的消息列表。

        返回:
            LLM 响应 ``LLMResponse``。
        """
        kwargs = self._build_request_kwargs(spec, messages, tools=None)
        return await self.provider.chat_with_retry(**kwargs)

    @staticmethod
    def _budget_exhausted_finalization_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """构造预算耗尽收尾消息：在末尾追加预算耗尽提示。"""
        retry_messages = list(messages)
        retry_messages.append(build_budget_exhausted_finalization_message())
        return retry_messages

    @staticmethod
    def _max_iterations_fallback(spec: AgentRunSpec) -> str:
        """达到最大迭代且收尾失败时的回退文案。

        优先使用 spec 自定义文案，否则渲染默认模板。
        """
        if spec.max_iterations_message:
            return spec.max_iterations_message.format(
                max_iterations=spec.max_iterations,
            )
        return render_template(
            "agent/max_iterations_message.md",
            strip=True,
            max_iterations=spec.max_iterations,
        )

    def _usage_or_estimate(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        """获取 token 用量：优先用提供商返回值，缺失时本地估算。

        参数:
            spec: 执行规格；
            messages: 送入模型的消息列表；
            response: LLM 响应。

        返回:
            token 用量字典；错误响应且无用量时返回空字典。
        """
        usage = self._usage_dict(response.usage)
        total = self._usage_total(usage)
        if total > 0:  # 提供商已返回有效用量
            usage["total_tokens"] = total
            usage.setdefault("provider_tokens", total)
            return usage
        if response.finish_reason == "error":  # 错误响应不估算
            return {}
        return self._estimate_response_usage(spec, messages, response)

    def _estimate_response_usage(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
        response: LLMResponse,
    ) -> dict[str, int]:
        """本地估算单次响应的 token 用量（prompt + completion）。

        参数:
            spec: 执行规格；
            messages: 送入模型的消息列表；
            response: LLM 响应。

        返回:
            估算的 token 用量字典（含 estimated_tokens 标记）；无法估算时返回空字典。
        """
        try:
            tools = spec.tools.get_always_include_definitions()
        except Exception:
            tools = None
        prompt_tokens, _ = estimate_prompt_tokens_chain(self.provider, spec.model, messages, tools)
        assistant_message = build_assistant_message(
            response.content or "",
            tool_calls=[tc.to_openai_tool_call() for tc in response.tool_calls],
            reasoning_content=response.reasoning_content,
            thinking_blocks=response.thinking_blocks,
        )
        completion_tokens = estimate_message_tokens(assistant_message)
        total_tokens = max(0, prompt_tokens) + max(0, completion_tokens)
        if total_tokens <= 0:
            return {}
        return {
            "prompt_tokens": max(0, prompt_tokens),
            "completion_tokens": max(0, completion_tokens),
            "total_tokens": total_tokens,
            "estimated_tokens": total_tokens,
        }

    @staticmethod
    def _usage_dict(usage: dict[str, Any] | None) -> dict[str, int]:
        """将原始用量字典规范化为 int 值字典，忽略非法值。"""
        if not usage:
            return {}
        result: dict[str, int] = {}
        for key, value in usage.items():
            try:
                result[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _usage_total(usage: dict[str, int]) -> int:
        """计算总 token 数：优先用 total_tokens，否则 prompt+completion。"""
        return max(0, usage.get("total_tokens", 0) or (
            usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        ))

    @staticmethod
    def _accumulate_usage(target: dict[str, int], addition: dict[str, int]) -> None:
        """把 addition 累加到 target（原地修改）。"""
        for key, value in addition.items():
            target[key] = target.get(key, 0) + value

    @staticmethod
    def _merge_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
        """合并两份用量字典，返回新字典（不修改入参）。"""
        merged = dict(left)
        for key, value in right.items():
            merged[key] = merged.get(key, 0) + value
        return merged

    async def _execute_tools(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        tool_call_counts: dict[str, int],
    ) -> tuple[list[Any], list[dict[str, str]], BaseException | None]:
        """执行一批工具调用，返回结果、事件与首个致命错误。

        按 ``_partition_tool_batches`` 分批：可并发工具批量 gather，其余串行。

        参数:
            spec: 执行规格；
            tool_calls: 工具调用请求列表；
            external_lookup_counts: 外部查找计数（节流用）；
            workspace_violation_counts: 工作区越界计数（节流用）；
            tool_call_counts: 重复工具调用计数（死循环兜底）。

        返回:
            (结果列表, 事件列表, 首个致命错误或 None)。
        """
        batches = self._partition_tool_batches(spec, tool_calls)
        tool_results: list[tuple[Any, dict[str, str], BaseException | None]] = []
        for batch in batches:
            if spec.concurrent_tools and len(batch) > 1:
                batch_results = await asyncio.gather(*(
                    self._run_tool(
                        spec, tool_call, external_lookup_counts, workspace_violation_counts,
                        tool_call_counts,
                    )
                    for tool_call in batch
                ))
                tool_results.extend(batch_results)
            else:
                batch_results = []
                for tool_call in batch:
                    result = await self._run_tool(
                        spec, tool_call, external_lookup_counts, workspace_violation_counts,
                        tool_call_counts,
                    )
                    tool_results.append(result)
                    batch_results.append(result)

        results: list[Any] = []
        events: list[dict[str, str]] = []
        fatal_error: BaseException | None = None
        for result, event, error in tool_results:
            results.append(result)
            events.append(event)
            if error is not None and fatal_error is None:
                fatal_error = error
        return results, events, fatal_error

    async def _run_tool(
        self,
        spec: AgentRunSpec,
        tool_call: ToolCallRequest,
        external_lookup_counts: dict[str, int],
        workspace_violation_counts: dict[str, int],
        tool_call_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None]:
        """执行单个工具调用，返回结果、事件与可能的致命错误。

        处理流程：重复外部查找拦截 → 重复工具调用拦截 → prepare_call 预处理 →
        文件编辑追踪启动 → 执行工具 → 错误分类（SSRF/工作区越界）→
        文件编辑追踪结束 → 发布追踪。

        参数:
            spec: 执行规格；
            tool_call: 工具调用请求；
            external_lookup_counts: 外部查找计数（节流用）；
            workspace_violation_counts: 工作区越界计数（节流用）；
            tool_call_counts: 重复工具调用计数（死循环兜底）。

        返回:
            (结果, 事件字典, 致命错误或 None)。
        """
        hint = "\n\n[Analyze the error above and try a different approach.]"  # 错误后附加的引导提示
        self._publish_tool_trace(spec, tool_call, "started")
        tool_t0 = time.perf_counter()
        lookup_error = repeated_external_lookup_error(
            tool_call.name,
            tool_call.arguments,
            external_lookup_counts,
        )
        if lookup_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated external lookup blocked",
            }
            self._publish_tool_trace(
                spec, tool_call, "failed",
                duration_ms=(time.perf_counter() - tool_t0) * 1000,
                detail={"error": "repeated external lookup blocked"},
            )
            if spec.fail_on_tool_error:
                return lookup_error + hint, event, RuntimeError(lookup_error)
            return lookup_error + hint, event, None
        repeat_error = repeated_tool_call_error(
            tool_call.name,
            tool_call.arguments,
            tool_call_counts,
        )
        if repeat_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": "repeated tool call blocked",
            }
            self._publish_tool_trace(
                spec, tool_call, "failed",
                duration_ms=(time.perf_counter() - tool_t0) * 1000,
                detail={"error": "repeated tool call blocked"},
            )
            if spec.fail_on_tool_error:
                return repeat_error + hint, event, RuntimeError(repeat_error)
            return repeat_error + hint, event, None
        prepare_call = getattr(spec.tools, "prepare_call", None)
        tool, params, prep_error = None, tool_call.arguments, None
        if callable(prepare_call):
            with suppress(Exception):
                prepared = prepare_call(tool_call.name, tool_call.arguments)
                if isinstance(prepared, tuple) and len(prepared) == 3:
                    tool, params, prep_error = prepared
        if prep_error:
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": prep_error.split(": ", 1)[-1][:120],
            }
            handled = self._classify_violation(
                raw_text=prep_error,
                soft_payload=prep_error + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            return prep_error + hint, event, (
                RuntimeError(prep_error) if spec.fail_on_tool_error else None
            )
        emit_file_edit_events = (
            spec.progress_callback is not None
            and on_progress_accepts_file_edit_events(spec.progress_callback)
        )
        progress_callback = spec.progress_callback if emit_file_edit_events else None
        file_edit_trackers = (
            prepare_file_edit_trackers(
                call_id=tool_call.id,
                tool_name=tool_call.name,
                tool=tool,
                workspace=spec.workspace,
                params=params if isinstance(params, dict) else None,
            )
            if progress_callback is not None
            else None
        )
        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [build_file_edit_start_event(
                    file_edit_tracker,
                    params if isinstance(params, dict) else None,
                ) for file_edit_tracker in file_edit_trackers],
            )
        try:
            if tool is not None:
                result = await tool.execute(**params)
            else:
                result = await spec.tools.execute(tool_call.name, params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, str(exc))
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": str(exc),
            }
            self._publish_tool_trace(
                spec, tool_call, "failed",
                duration_ms=(time.perf_counter() - tool_t0) * 1000,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )
            payload = f"Error: {type(exc).__name__}: {exc}"
            handled = self._classify_violation(
                raw_text=str(exc),
                # Preserve legacy exception payloads without the retry hint.
                soft_payload=payload,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return payload, event, exc
            return payload, event, None

        if isinstance(result, str) and result.startswith("Error"):
            if file_edit_trackers and progress_callback is not None:
                await invoke_file_edit_progress(
                    progress_callback,
                    [
                        build_file_edit_error_event(file_edit_tracker, result)
                        for file_edit_tracker in file_edit_trackers
                    ],
                )
            event = {
                "name": tool_call.name,
                "status": "error",
                "detail": result.replace("\n", " ").strip()[:120],
            }
            self._publish_tool_trace(
                spec, tool_call, "failed",
                duration_ms=(time.perf_counter() - tool_t0) * 1000,
                detail={"error": result.replace("\n", " ").strip()[:200]},
            )
            handled = self._classify_violation(
                raw_text=result,
                soft_payload=result + hint,
                event=event,
                tool_call=tool_call,
                workspace_violation_counts=workspace_violation_counts,
            )
            if handled is not None:
                return handled
            if spec.fail_on_tool_error:
                return result + hint, event, RuntimeError(result)
            return result + hint, event, None

        if file_edit_trackers and progress_callback is not None:
            await invoke_file_edit_progress(
                progress_callback,
                [build_file_edit_end_event(
                    file_edit_tracker,
                    params if isinstance(params, dict) else None,
                ) for file_edit_tracker in file_edit_trackers],
            )

        detail = "" if result is None else str(result)
        detail = detail.replace("\n", " ").strip()
        if not detail:
            detail = "(empty)"
        elif len(detail) > 120:
            detail = detail[:120] + "..."
        self._publish_tool_trace(
            spec, tool_call, "completed",
            duration_ms=(time.perf_counter() - tool_t0) * 1000,
            detail={"detail": detail},
        )
        return result, {"name": tool_call.name, "status": "ok", "detail": detail}, None

    # SSRF is a hard security block at the tool boundary, but the agent turn
    # should recover conversationally instead of aborting the runtime.
    # SSRF 是工具边界的硬性安全拦截，但 Agent 回合应以对话方式恢复而非中止运行时。
    _SSRF_MARKERS: tuple[str, ...] = (  # SSRF 违规标记（小写匹配）
        "internal/private url detected",
        "private/internal address",
        "private address",
    )
    _SSRF_BOUNDARY_NOTE: str = (  # SSRF 拦截后附加给 LLM 的不可绕过提示
        "This is a non-bypassable security boundary. Stop trying to access "
        "private/internal URLs. Do not retry with curl, wget, encoded IPs, "
        "alternate DNS, redirects, proxies, or another tool. Ask the user for "
        "local files, logs, screenshots, or an explicit safe public URL instead. "
        "If the user explicitly trusts this private URL, ask them to whitelist "
        "the exact IP/CIDR via tools.ssrfWhitelist."
    )

    # Non-SSRF boundary markers returned to the LLM as recoverable tool errors.
    # 非 SSRF 的边界标记，作为可恢复的工具错误返回给 LLM。
    _WORKSPACE_VIOLATION_MARKERS: tuple[str, ...] = (  # 工作区越界标记（小写匹配）
        "outside the configured workspace",
        "outside allowed directory",
        "working_dir is outside",
        "working_dir could not be resolved",
        "path outside working dir",
        "path traversal detected",
    )

    @classmethod
    def _is_ssrf_violation(cls, text: str) -> bool:
        """判断文本是否包含 SSRF 违规标记。"""
        if not text:
            return False
        lowered = text.lower()
        return any(marker in lowered for marker in cls._SSRF_MARKERS)

    @classmethod
    def _is_workspace_violation(cls, text: str) -> bool:
        """判断文本是否为任意策略边界拒绝（SSRF 或工作区越界）。"""
        if not text:
            return False
        lowered = text.lower()
        if cls._is_ssrf_violation(lowered):
            return True
        return any(marker in lowered for marker in cls._WORKSPACE_VIOLATION_MARKERS)

    def _classify_violation(
        self,
        *,
        raw_text: str,
        soft_payload: str,
        event: dict[str, str],
        tool_call: ToolCallRequest,
        workspace_violation_counts: dict[str, int],
    ) -> tuple[Any, dict[str, str], BaseException | None] | None:
        """分类安全边界失败；返回处理结果或 None（表示交由调用方默认处理）。

        参数:
            raw_text: 原始错误文本；
            soft_payload: 软性负载（含提示）；
            event: 事件字典（会就地更新 detail）；
            tool_call: 工具调用请求；
            workspace_violation_counts: 工作区越界计数（用于升级提示）。

        返回:
            处理后的 (结果, 事件, 错误) 或 None（非边界违规，交调用方处理）。
        """
        if self._is_ssrf_violation(raw_text):  # SSRF：返回不可重试的软负载
            logger.warning(
                "Tool {} blocked by SSRF guard; returning non-retryable tool error: {}",
                tool_call.name,
                raw_text.replace("\n", " ").strip()[:200],
            )
            event["detail"] = self._event_detail("ssrf_violation: ", raw_text)
            return self._ssrf_soft_payload(raw_text), event, None

        if self._is_workspace_violation(raw_text):  # 工作区越界：检查是否需升级提示
            escalation = repeated_workspace_violation_error(
                tool_call.name,
                tool_call.arguments,
                workspace_violation_counts,
            )
            event["detail"] = self._event_detail("workspace_violation: ", raw_text)
            if escalation is not None:  # 重复越界：返回升级提示
                logger.warning(
                    "Tool {} hit workspace boundary repeatedly; escalating hint",
                    tool_call.name,
                )
                event["detail"] = self._event_detail(
                    "workspace_violation_escalated: ",
                    raw_text,
                )
                return escalation, event, None
            return soft_payload, event, None

        return None  # 非边界违规：交调用方默认处理

    @classmethod
    def _ssrf_soft_payload(cls, raw_text: str) -> str:
        """构造 SSRF 软负载：原始文本 + 不可绕过提示。"""
        text = raw_text.strip() or "Error: request blocked by SSRF guard"
        return f"{text}\n\n{cls._SSRF_BOUNDARY_NOTE}"

    @staticmethod
    def _event_detail(prefix: str, text: str, limit: int = 160) -> str:
        """构造事件详情：前缀 + 压缩文本，并截断到 limit。"""
        return (prefix + text.replace("\n", " ").strip())[:limit]

    async def _emit_checkpoint(
        self,
        spec: AgentRunSpec,
        payload: dict[str, Any],
    ) -> None:
        """发出检查点回调（用于持久化中间状态）。

        参数:
            spec: 执行规格（提供 checkpoint_callback）；
            payload: 检查点负载。
        """
        callback = spec.checkpoint_callback
        if callback is not None:
            await callback(payload)

    @staticmethod
    def _append_final_message(messages: list[dict[str, Any]], content: str | None) -> None:
        """追加最终 assistant 消息：末尾已是纯 assistant 时替换，否则追加。

        参数:
            messages: 消息列表（原地修改）；
            content: 最终内容。
        """
        if not content:
            return
        if (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("tool_calls")
        ):
            if messages[-1].get("content") == content:
                return
            messages[-1] = build_assistant_message(content)
            return
        messages.append(build_assistant_message(content))

    @staticmethod
    def _append_model_error_placeholder(messages: list[dict[str, Any]]) -> None:
        """模型出错时追加占位 assistant 消息（末尾已是纯 assistant 时跳过）。

        参数:
            messages: 消息列表（原地修改）。
        """
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
            return
        messages.append(build_assistant_message(_PERSISTED_MODEL_ERROR_PLACEHOLDER))

    def _normalize_tool_result(
        self,
        spec: AgentRunSpec,
        tool_call_id: str,
        tool_name: str,
        result: Any,
    ) -> Any:
        """规范化工具结果：确保非空、按需落盘、超长截断。

        参数:
            spec: 执行规格；
            tool_call_id: 工具调用 ID；
            tool_name: 工具名；
            result: 原始结果。

        返回:
            规范化后的结果内容。
        """
        result = ensure_nonempty_tool_result(tool_name, result)
        if tool_name in _TOOL_RESULT_OFFLOAD_EXEMPT_TOOLS:
            # Exempt tools bound their own output; skip generic offload and truncation.
            # 豁免工具自行绑定输出：跳过通用落盘与截断
            return result
        try:
            content = maybe_persist_tool_result(
                spec.workspace,
                spec.session_key,
                tool_call_id,
                result,
                max_chars=spec.max_tool_result_chars,
            )
        except Exception:
            logger.exception(
                "Tool result persist failed for {} in {}; using raw result",
                tool_call_id,
                spec.session_key or "default",
            )
            content = result
        if isinstance(content, str) and len(content) > spec.max_tool_result_chars:  # 超长截断
            return truncate_text(content, spec.max_tool_result_chars)
        return content

    @staticmethod
    def _drop_orphan_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """丢弃历史中无匹配 assistant tool_call 的孤儿 tool 结果。"""
        declared: set[str] = set()
        updated: list[dict[str, Any]] | None = None
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        declared.add(str(tc["id"]))
            if role == "tool":
                tid = msg.get("tool_call_id")
                if tid and str(tid) not in declared:
                    if updated is None:
                        updated = [dict(m) for m in messages[:idx]]
                    continue
            if updated is not None:
                updated.append(dict(msg))

        if updated is None:
            return messages
        return updated

    @staticmethod
    def _backfill_missing_tool_results(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Insert synthetic error results for orphaned tool_use blocks."""
        """为孤儿 tool_use 块插入合成的错误结果，避免模型因缺失 tool 结果报错。"""
        declared: list[tuple[int, str, str]] = []  # (assistant_idx, call_id, name)
        fulfilled: set[str] = set()
        for idx, msg in enumerate(messages):
            role = msg.get("role")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        name = ""
                        func = tc.get("function")
                        if isinstance(func, dict):
                            name = func.get("name", "")
                        declared.append((idx, str(tc["id"]), name))
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid:
                    fulfilled.add(str(tid))

        missing = [(ai, cid, name) for ai, cid, name in declared if cid not in fulfilled]
        if not missing:
            return messages

        updated = list(messages)
        offset = 0
        for assistant_idx, call_id, name in missing:
            insert_at = assistant_idx + 1 + offset
            while insert_at < len(updated) and updated[insert_at].get("role") == "tool":
                insert_at += 1
            updated.insert(insert_at, {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": _BACKFILL_CONTENT,
            })
            offset += 1
        return updated

    @staticmethod
    def _microcompact(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace old compactable tool results with one-line summaries."""
        """将较早的可压缩工具结果替换为一行摘要，保留最近若干条完整结果。"""
        compactable_indices: list[int] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "tool" and msg.get("name") in _COMPACTABLE_TOOLS:
                compactable_indices.append(idx)

        if len(compactable_indices) <= _MICROCOMPACT_KEEP_RECENT:
            return messages

        stale = compactable_indices[: len(compactable_indices) - _MICROCOMPACT_KEEP_RECENT]
        updated: list[dict[str, Any]] | None = None
        for idx in stale:
            msg = messages[idx]
            content = msg.get("content")
            if not isinstance(content, str) or len(content) < _MICROCOMPACT_MIN_CHARS:
                continue
            name = msg.get("name", "tool")
            summary = f"[{name} result omitted from context]"
            if updated is None:
                updated = [dict(m) for m in messages]
            updated[idx]["content"] = summary

        return updated if updated is not None else messages

    def _apply_tool_result_budget(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """对历史中所有 tool 消息应用结果预算（落盘/截断），返回处理后的副本。"""
        updated = messages
        for idx, message in enumerate(messages):
            if message.get("role") != "tool":
                continue
            normalized = self._normalize_tool_result(
                spec,
                str(message.get("tool_call_id") or f"tool_{idx}"),
                str(message.get("name") or "tool"),
                message.get("content"),
            )
            if normalized != message.get("content"):
                if updated is messages:
                    updated = [dict(m) for m in messages]
                updated[idx]["content"] = normalized
        return updated

    def _snip_history(
        self,
        spec: AgentRunSpec,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按 token 预算裁剪历史：保留 system 消息与最近的非系统消息。

        当估算 token 超过预算时，从非系统消息末尾向前保留，直至预算耗尽；
        并确保保留窗口以合法的 user 消息起始（避免破坏角色交替）。
        """
        if not messages or not spec.context_window_tokens:
            return messages

        provider_max_tokens = getattr(getattr(self.provider, "generation", None), "max_tokens", 4096)
        max_output = spec.max_tokens if isinstance(spec.max_tokens, int) else (
            provider_max_tokens if isinstance(provider_max_tokens, int) else 4096
        )
        budget = spec.context_block_limit or (
            spec.context_window_tokens - max_output - _SNIP_SAFETY_BUFFER
        )
        if budget <= 0:
            return messages

        turn_tools = spec.tools.get_always_include_definitions()
        estimate, _ = estimate_prompt_tokens_chain(
            self.provider,
            spec.model,
            messages,
            turn_tools,
        )
        if estimate <= budget:
            return messages

        system_messages = [dict(msg) for msg in messages if msg.get("role") == "system"]
        non_system = [dict(msg) for msg in messages if msg.get("role") != "system"]
        if not non_system:
            return messages

        system_tokens = sum(estimate_message_tokens(msg) for msg in system_messages)
        fixed_tokens, _ = estimate_prompt_tokens_chain(
            self.provider,
            spec.model,
            system_messages,
            turn_tools,
        )
        remaining_budget = max(0, budget - max(system_tokens, fixed_tokens))
        kept: list[dict[str, Any]] = []
        kept_tokens = 0
        for message in reversed(non_system):
            msg_tokens = estimate_message_tokens(message)
            if kept and kept_tokens + msg_tokens > remaining_budget:
                break
            kept.append(message)
            kept_tokens += msg_tokens
        kept.reverse()

        if kept:
            for i, message in enumerate(kept):
                if message.get("role") == "user":
                    kept = kept[i:]
                    break
            else:
                # Recover nearest user message from outside the kept window;
                # GLM rejects system→assistant (error 1214).  Budget is
                # intentionally exceeded — oversized beats invalid.
                for idx in range(len(non_system) - 1, -1, -1):
                    if non_system[idx].get("role") == "user":
                        kept = non_system[idx:]
                        break
                # If no user exists at all, _enforce_role_alternation
                # will insert a synthetic one as a safety net.
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        if not kept:
            kept = non_system[-min(len(non_system), 4) :]
            start = find_legal_message_start(kept)
            if start:
                kept = kept[start:]
        return system_messages + kept

    def _partition_tool_batches(
        self,
        spec: AgentRunSpec,
        tool_calls: list[ToolCallRequest],
    ) -> list[list[ToolCallRequest]]:
        """将工具调用划分为可并发执行的批次。

        非并发模式下每个调用单独成批；并发模式下将连续的 concurrency_safe
        工具聚为一批，不可并发的工具单独成批。

        参数:
            spec: 执行规格；
            tool_calls: 工具调用请求列表。

        返回:
            批次列表（每批为一个工具调用列表）。
        """
        if not spec.concurrent_tools:
            return [[tool_call] for tool_call in tool_calls]

        batches: list[list[ToolCallRequest]] = []
        current: list[ToolCallRequest] = []
        for tool_call in tool_calls:
            get_tool = getattr(spec.tools, "get", None)
            tool = get_tool(tool_call.name) if callable(get_tool) else None
            can_batch = bool(tool and getattr(tool, "concurrency_safe", False))
            if can_batch:
                current.append(tool_call)
                continue
            if current:
                batches.append(current)
                current = []
            batches.append([tool_call])
        if current:
            batches.append(current)
        return batches
