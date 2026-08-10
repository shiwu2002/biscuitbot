"""Provider 包装器 —— 在主模型出错时透明地切换到 fallback 模型。

所属模块与项目作用
===================
本文件位于 biscuitbot/providers 目录，是 LLM Provider 层的失败转移
（failover）组件。在项目架构中起到的作用：
- 包装一个主 Provider，并在主模型返回可转移错误（且尚未流式输出正文）
  时，依次尝试各 fallback 模型，实现模型级容灾。
- 支持流式超时恢复：调用方关闭当前流式段后，可在新流式段中继续
  failover，避免重复输出。
- 通过工厂回调按需为各 fallback 模型创建对应 Provider（可能位于不同
  后端），并通过对主 Provider 的熔断（circuit breaker）避免持续向
  已知故障的端点发请求。
"""

from __future__ import annotations

import time  # monotonic 时钟用于熔断冷却计时
from collections.abc import Awaitable, Callable  # 回调与工厂类型签名
from typing import Any

from loguru import logger

from biscuitbot.providers.base import LLMProvider, LLMResponse  # 基类与响应类型

# 熔断阈值：与 OpenAICompatProvider 的 Responses API 熔断器保持一致
_PRIMARY_FAILURE_THRESHOLD = 3
_PRIMARY_COOLDOWN_S = 60  # 主 Provider 熔断后的冷却时间（秒）
_MISSING = object()  # 用于区分"参数未传"与"显式传 None"的哨兵
# 可触发 failover 的错误大类
_FALLBACK_ERROR_KINDS = frozenset({
    "timeout",
    "connection",
    "server_error",
    "rate_limit",
    "overloaded",
})
# 不可触发 failover 的错误大类（通常是调用方问题，换模型也无法解决）
_NON_FALLBACK_ERROR_KINDS = frozenset({
    "authentication",
    "auth",
    "permission",
    "content_filter",
    "refusal",
    "context_length",
    "invalid_request",
})
# 可触发 failover 的错误文本 token（用于兜底匹配）
_FALLBACK_ERROR_TOKENS = (
    "rate_limit",
    "rate limit",
    "too_many_requests",
    "too many requests",
    "overloaded",
    "server_error",
    "server error",
    "temporarily unavailable",
    "timeout",
    "timed out",
    "connection",
    "insufficient_quota",
    "insufficient quota",
    "quota_exceeded",
    "quota exceeded",
    "quota_exhausted",
    "quota exhausted",
    "billing_hard_limit",
    "insufficient_balance",
    "balance",
    "out of credits",
)


class FallbackProvider(LLMProvider):
    """包装主 Provider 并在出错时透明地 failover 到 fallback 模型。

    当主模型在正文流式输出前返回可转移错误时，依次尝试各 fallback 模型。
    流式超时是恢复的特例：调用方可关闭当前流式段，包装器在新流式段中
    继续 failover。每个 fallback 模型可能位于不同 Provider —— 通过工厂
    回调按需创建底层 Provider。

    关键设计：
    - failover 是请求级作用域（包装器在轮次间无状态）。
    - 已流式输出正文时跳过 failover 以避免重复输出，超时恢复例外。
    - 工厂返回的是普通 Provider，从而避免递归 failover。
    - 主 Provider 在连续失败后被熔断，避免向已知故障端点浪费请求。
    """

    supports_stream_recover_callback = True  # 声明支持流式段恢复回调

    def __init__(
        self,
        primary: LLMProvider,
        fallback_presets: list[Any],
        provider_factory: Callable[[Any], LLMProvider],
    ):
        """初始化 FallbackProvider。

        :param primary: 主 Provider 实例。
        :param fallback_presets: fallback 预设列表。
        :param provider_factory: 按 fallback 预设创建对应 Provider 的工厂回调。
        """
        self._primary = primary
        self._fallback_presets = list(fallback_presets)
        self._provider_factory = provider_factory
        self._has_fallbacks = bool(fallback_presets)
        self._primary_failures = 0  # 主 Provider 连续失败计数
        self._primary_tripped_at: float | None = None  # 主 Provider 熔断时刻

    @property
    def generation(self):
        """透传主 Provider 的生成参数。"""
        return self._primary.generation

    @generation.setter
    def generation(self, value):
        self._primary.generation = value

    def get_default_model(self) -> str:
        """返回主 Provider 的默认模型名。"""
        return self._primary.get_default_model()

    @property
    def supports_progress_deltas(self) -> bool:
        """透传主 Provider 是否支持进度增量。"""
        return bool(getattr(self._primary, "supports_progress_deltas", False))

    def _primary_available(self) -> bool:
        """判断主 Provider 是否可用（未被熔断，或冷却期已过）。

        冷却期过后采用半开策略：允许一次探测请求以验证是否恢复。
        """
        if self._primary_tripped_at is None:
            return True
        if time.monotonic() - self._primary_tripped_at >= _PRIMARY_COOLDOWN_S:
            # 半开：允许一次探测请求
            return True
        return False

    async def chat(self, **kwargs: Any) -> LLMResponse:
        """非流式聊天：无 fallback 时直接调用主 Provider，否则走 failover。"""
        if not self._has_fallbacks:
            return await self._primary.chat(**kwargs)
        return await self._try_with_fallback(
            lambda p, kw: p.chat(**kw), kwargs, has_streamed=None
        )

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        """流式聊天：包装 on_content_delta 以跟踪是否已输出正文，再走 failover。"""
        on_stream_recover = kwargs.pop("on_stream_recover", None)
        if not self._has_fallbacks:
            return await self._primary.chat_stream(**kwargs)

        # 用 list 包装以便在嵌套闭包中修改
        has_streamed: list[bool] = [False]
        original_delta = kwargs.get("on_content_delta")

        async def _tracking_delta(text: str) -> None:
            """包装正文增量回调，同时记录是否已输出正文。"""
            if text:
                has_streamed[0] = True
            if original_delta:
                await original_delta(text)

        kwargs["on_content_delta"] = _tracking_delta
        return await self._try_with_fallback(
            lambda p, kw: p.chat_stream(**kw),
            kwargs,
            has_streamed=has_streamed,
            on_stream_recover=on_stream_recover,
        )

    async def _try_with_fallback(
        self,
        call: Callable[[LLMProvider, dict[str, Any]], Awaitable[LLMResponse]],
        kwargs: dict[str, Any],
        has_streamed: list[bool] | None,
        on_stream_recover: Callable[[], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """核心 failover 编排：先试主 Provider，失败则依次尝试各 fallback。

        - 主 Provider 成功则重置熔断计数并返回。
        - 已流式输出正文时默认跳过 failover（避免重复输出），超时例外。
        - 主 Provider 连续失败达到阈值则熔断。
        - 遍历 fallback 列表，按预设覆盖 model/max_tokens/temperature/reasoning_effort。
        """
        primary_model = kwargs.get("model") or self._primary.get_default_model()

        if self._primary_available():
            # 主 Provider 可用：先尝试主 Provider
            response = await call(self._primary, kwargs)
            if response.finish_reason != "error":
                # 成功：重置熔断状态
                self._primary_failures = 0
                self._primary_tripped_at = None
                return response

            # 流式已输出正文后的处理
            if has_streamed is not None and has_streamed[0]:
                is_timeout = (response.error_kind or "").lower() == "timeout"
                if is_timeout:
                    # 超时：仍可尝试 failover（在新流式段中继续）
                    logger.warning(
                        "Primary model '{}' stream stalled after content was emitted; "
                        "attempting failover anyway",
                        primary_model,
                    )
                    has_streamed[0] = False
                    if on_stream_recover:
                        await on_stream_recover()
                    else:
                        # 无恢复回调：抑制后续增量避免重复
                        kwargs["on_content_delta"] = None
                else:
                    # 非超时错误且已输出正文：跳过 failover
                    logger.warning(
                        "Primary model error but content already streamed; skipping failover"
                    )
                    return response

            # 判断错误是否可转移
            if not self._should_fallback(response):
                logger.warning(
                    "Primary model '{}' returned non-fallbackable error: {}",
                    primary_model,
                    (response.content or "")[:120],
                )
                return response

            # 累计失败计数，达到阈值则熔断
            self._primary_failures += 1
            if self._primary_failures >= _PRIMARY_FAILURE_THRESHOLD:
                self._primary_tripped_at = time.monotonic()
                logger.warning(
                    "Primary model '{}' circuit open after {} consecutive failures",
                    primary_model, self._primary_failures,
                )
        else:
            # 主 Provider 已熔断：直接跳过
            logger.debug("Primary model '{}' circuit open; skipping", primary_model)

        # 遍历 fallback 列表
        last_response: LLMResponse | None = None
        primary_skipped = not self._primary_available()
        for idx, fallback in enumerate(self._fallback_presets):
            fallback_model = fallback.model
            # 已输出正文后的 fallback 跳过逻辑（超时恢复例外）
            if has_streamed is not None and has_streamed[0]:
                is_timeout = (
                    last_response is not None
                    and (last_response.error_kind or "").lower() == "timeout"
                )
                if is_timeout and on_stream_recover:
                    logger.warning(
                        "Fallback model '{}' stream stalled after content was emitted; "
                        "starting a new stream segment and trying next fallback",
                        self._fallback_presets[idx - 1].model if idx > 0 else primary_model,
                    )
                    has_streamed[0] = False
                    await on_stream_recover()
                else:
                    break
            # 日志：区分主被熔断、主失败、上一个 fallback 失败
            if idx == 0 and primary_skipped:
                logger.info(
                    "Primary model '{}' circuit open, trying fallback '{}'",
                    primary_model, fallback_model,
                )
            elif idx == 0:
                logger.info(
                    "Primary model '{}' failed, trying fallback '{}'",
                    primary_model, fallback_model,
                )
            else:
                logger.info(
                    "Fallback '{}' also failed, trying next fallback '{}'",
                    self._fallback_presets[idx - 1].model, fallback_model,
                )
            # 按需创建 fallback Provider
            try:
                fallback_provider = self._provider_factory(fallback)
            except Exception as exc:
                logger.warning(
                    "Failed to create provider for fallback '{}': {}", fallback_model, exc
                )
                continue

            # 保存原始参数，便于 fallback 失败后恢复给下一个 fallback 使用
            original_values = {
                name: kwargs.get(name, _MISSING)
                for name in ("model", "max_tokens", "temperature", "reasoning_effort")
            }
            # 用 fallback 预设覆盖生成参数
            kwargs["model"] = fallback_model
            kwargs["max_tokens"] = fallback.max_tokens
            kwargs["temperature"] = fallback.temperature
            if fallback.reasoning_effort is None:
                kwargs.pop("reasoning_effort", None)
            else:
                kwargs["reasoning_effort"] = fallback.reasoning_effort
            try:
                fallback_response = await call(fallback_provider, kwargs)
            finally:
                # 恢复原始参数，供下一个 fallback 使用
                for name, value in original_values.items():
                    if value is _MISSING:
                        kwargs.pop(name, None)
                    else:
                        kwargs[name] = value

            if fallback_response.finish_reason != "error":
                # fallback 成功
                logger.info(
                    "Fallback '{}' succeeded after primary '{}' failed",
                    fallback_model, primary_model,
                )
                return fallback_response

            last_response = fallback_response
            logger.warning(
                "Fallback '{}' also failed: {}",
                fallback_model,
                (fallback_response.content or "")[:120],
            )

        # 所有 fallback 均失败
        logger.warning(
            "All {} fallback model(s) failed",
            len(self._fallback_presets),
        )
        # 返回最后看到的错误响应（主或最后一个 fallback）
        if last_response is not None:
            return last_response
        # 主被熔断且无可用 fallback：合成错误响应
        return LLMResponse(
            content=f"Primary model '{primary_model}' circuit open and no fallbacks available",
            finish_reason="error",
        )

    @staticmethod
    def _should_fallback(response: LLMResponse) -> bool:
        """判断响应是否应触发 failover。

        优先使用结构化错误元数据（error_should_retry / error_status_code /
        error_kind），退化到文本 token 兜底匹配。明确不可转移的错误
        （4xx 客户端错误、鉴权、内容过滤等）直接返回 False。
        """
        if response.error_should_retry is False:
            return False
        status = response.error_status_code
        kind = (response.error_kind or "").lower()
        error_type = (response.error_type or "").lower()
        code = (response.error_code or "").lower()
        text = (response.content or "").lower()

        # 4xx 客户端错误：换模型也无法解决
        if status in {400, 401, 403, 404, 422}:
            return False
        # 不可转移的错误大类
        if kind in _NON_FALLBACK_ERROR_KINDS:
            return False
        if any(token in value for value in (kind, error_type, code) for token in _NON_FALLBACK_ERROR_KINDS):
            return False
        # Provider 明确建议重试：可转移
        if response.error_should_retry is True:
            return True
        # 状态码层面：超时/冲突/限流/5xx 可转移
        if status is not None and (status in {408, 409, 429} or 500 <= status <= 599):
            return True
        # 错误大类匹配：可转移
        if kind in _FALLBACK_ERROR_KINDS:
            return True
        # 兜底：文本 token 匹配
        return any(token in value for value in (kind, error_type, code, text) for token in _FALLBACK_ERROR_TOKENS)
