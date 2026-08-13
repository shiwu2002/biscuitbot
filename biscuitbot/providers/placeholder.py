"""占位 LLM Provider —— 未配置任何 API Key 时供网关启动使用。

桌面端首启场景：网关必须先启动才能承载欢迎设置页，但此时用户尚未配置
任何 LLM Provider。本模块提供不真正调用模型的占位 Provider，其 ``chat``
抛出的错误会把用户引导回设置页。

用户通过欢迎设置页写入 API Key 后，下一次对话开始时会自动热替换为真实
Provider（见 :func:`biscuitbot.agent.loop.AgentLoop._refresh_provider_snapshot`
—— 它按配置签名变化重建 Provider 快照）。
"""

from __future__ import annotations

from typing import Any

from biscuitbot.providers.base import LLMProvider, LLMResponse


class PlaceholderProvider(LLMProvider):
    """未配置 Provider 时的占位实现：任何对话请求都会给出明确指引。"""

    # 用户可见的引导文案（WebUI 以中文渲染）
    NOT_CONFIGURED_HINT = (
        "尚未配置 LLM API Key。请点击左上角设置，或按提示先完成首次配置后再发送消息。"
    )

    def get_default_model(self) -> str:
        return "placeholder"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        raise RuntimeError(self.NOT_CONFIGURED_HINT)
