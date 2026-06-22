"""平台 Skills 工具包装器。

将平台返回的工具定义包装为 hczkbot 的 Tool 子类，使其可注册到 ToolRegistry
并被 LLM 通过 function calling 调用。

每个平台工具对应一个 PlatformSkillTool 实例，execute() 时通过
PlatformSkillsClient 调用平台工具执行 API。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from hczkbot.agent.tools.base import Tool
from hczkbot.platform.skills_client import PlatformSkillsClient


class PlatformSkillTool(Tool):
    """平台 Skills 工具包装器。

    将平台返回的单个工具定义（OpenAI function 格式）包装为 hczkbot Tool。
    """

    _plugin_discoverable = False  # 不参与自动发现，手动注册

    def __init__(
        self,
        tool_def: dict[str, Any],
        client: PlatformSkillsClient,
        user_id: str,
    ) -> None:
        self._tool_def = deepcopy(tool_def)
        function = tool_def.get("function", {})
        self._name = function.get("name", "")
        self._description = function.get("description", self._name)
        self._parameters = function.get("parameters", {"type": "object", "properties": {}})
        self._endpoint = tool_def.get("endpoint", "")
        self._client = client
        self._user_id = user_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return deepcopy(self._parameters)

    @property
    def read_only(self) -> bool:
        """平台工具默认非只读（保守策略）。"""
        return False

    @property
    def concurrency_safe(self) -> bool:
        """平台工具默认不并发执行（保守策略）。"""
        return False

    async def execute(self, **kwargs: Any) -> Any:
        """调用平台工具执行 API。

        自动注入 user_id（从 JWT 获取），用于平台定位用户的 Milvus 集合。
        """
        arguments = dict(kwargs)
        # 规范 4.5：arguments.user_id 建议传递，用于定位该用户的物理集合
        arguments.setdefault("user_id", self._user_id)

        result = await self._client.execute_tool(
            endpoint=self._endpoint,
            tool_name=self._name,
            arguments=arguments,
        )

        # 将平台返回结果转为字符串供 LLM 使用
        import json as _json

        if isinstance(result, dict):
            if result.get("success") is False:
                error = result.get("error", "工具执行失败")
                return f"Error: {error}"
            # 成功时返回 data 内容的 JSON 字符串
            return _json.dumps(result, ensure_ascii=False, default=str)
        return str(result)


async def build_skill_tools(
    client: PlatformSkillsClient,
    tools_discovery_endpoint: str,
    available_skills: list[dict[str, Any]],
    user_id: str,
) -> list[PlatformSkillTool]:
    """按需查询并构建平台 Skills 工具列表。

    遍历 available_skills 中的每个工具组，查询其下所有工具详情，
    包装为 PlatformSkillTool 实例。

    Args:
        client: 平台 Skills 客户端（已绑定 JWT Token）
        tools_discovery_endpoint: 工具发现端点
        available_skills: 工具组目录（对话请求中的 available_skills 字段）
        user_id: 当前用户 ID（从 JWT 获取，用于工具执行时注入）

    Returns:
        PlatformSkillTool 实例列表
    """
    tools: list[PlatformSkillTool] = []
    for skill in available_skills or []:
        skill_name = skill.get("name")
        if not skill_name:
            continue
        tool_defs = await client.discover_tools(tools_discovery_endpoint, skill_name)
        for tool_def in tool_defs:
            try:
                wrapper = PlatformSkillTool(tool_def, client, user_id)
                if wrapper.name:
                    tools.append(wrapper)
            except Exception as e:
                from loguru import logger

                logger.warning("构建平台工具失败 [{}]: {}", skill_name, e)
    return tools
