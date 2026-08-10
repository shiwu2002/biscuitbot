"""运行后通知评估器，用于心跳检查的结果判定。

所属模块与项目作用
===================
本文件位于 biscuitbot/utils 目录，是工具函数模块的通知评估组件。
在项目架构中起到的作用：
在心跳执行一次内部检查后，发起一次轻量 LLM 调用，由模型判断该
后台任务结果是否值得通知用户，从而过滤掉例行或空结果。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger  # 结构化日志记录

from biscuitbot.utils.prompt_templates import render_template  # 渲染提示词模板

if TYPE_CHECKING:
    from biscuitbot.providers.base import LLMProvider  # 仅用于类型注解，避免循环导入

# 提供给 LLM 的 function-calling 工具定义，用于结构化判定是否通知
_EVALUATE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "evaluate_notification",
            "description": "Decide whether the user should be notified about this background task result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "should_notify": {
                        "type": "boolean",
                        "description": "true = result contains actionable/important info the user should see; false = routine or empty, safe to suppress",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One-sentence reason for the decision",
                    },
                },
                "required": ["should_notify"],
            },
        },
    }
]

async def evaluate_response(
    response: str,
    task_context: str,
    provider: LLMProvider,
    model: str,
    default_notify: bool = True,
) -> bool:
    """判断心跳结果是否应推送给用户。

    任何失败时回退到 ``default_notify``。心跳场景会传入 ``False``
    以实现“失败即静默”的保守策略。
    """
    try:
        llm_response = await provider.chat_with_retry(
            messages=[
                {"role": "system", "content": render_template("agent/evaluator.md", part="system")},
                {"role": "user", "content": render_template(
                    "agent/evaluator.md",
                    part="user",
                    task_context=task_context,
                    response=response,
                )},
            ],
            tools=_EVALUATE_TOOL,
            model=model,
            max_tokens=256,
            temperature=0.0,  # 温度为 0 以获得稳定的判定
        )

        if not llm_response.should_execute_tools:
            # 模型未按预期调用工具时，记录警告并回退到默认值
            if llm_response.has_tool_calls:
                logger.warning(
                    "evaluate_response: ignoring tool calls under finish_reason='{}', "
                    "defaulting to notify={}",
                    llm_response.finish_reason,
                    default_notify,
                )
            else:
                logger.warning(
                    "evaluate_response: no tool call returned, defaulting to notify={}",
                    default_notify,
                )
            return default_notify

        args = llm_response.tool_calls[0].arguments  # 取首个工具调用的参数
        should_notify = args.get("should_notify", default_notify)
        reason = args.get("reason", "")
        logger.info("evaluate_response: should_notify={}, reason={}", should_notify, reason)
        return bool(should_notify)

    except Exception:
        # 任意异常都回退到默认值，避免心跳因评估失败而崩溃
        logger.exception("evaluate_response failed, defaulting to notify={}", default_notify)
        return default_notify
