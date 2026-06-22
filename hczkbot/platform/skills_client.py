"""平台 Skills 工具客户端。

负责调用平台工具发现 API 和工具执行 API，符合规范第 4 节。

调用流程：
1. 平台在对话请求中传递 available_skills（工具组目录）和 tools_discovery_endpoint
2. 智能体按需 GET {tools_discovery_endpoint}/{skill_name} 查询工具详情
3. 智能体 POST {tool.endpoint} 执行工具，请求体 {tool_name, arguments}
4. 所有请求复用对话请求的 JWT Token 鉴权
"""
from __future__ import annotations

import json
from typing import Any

import httpx
from loguru import logger


class PlatformSkillsClient:
    """平台 Skills 工具发现与执行客户端。"""

    def __init__(
        self,
        jwt_token: str,
        request_timeout: float = 30.0,
        tool_timeout: float = 60.0,
    ) -> None:
        self._token = jwt_token
        self._request_timeout = request_timeout
        self._tool_timeout = tool_timeout

    @property
    def auth_header(self) -> dict[str, str]:
        """返回带 JWT 的 Authorization 请求头。"""
        return {"Authorization": f"Bearer {self._token}"}

    async def discover_tools(
        self,
        tools_discovery_endpoint: str,
        skill_name: str,
    ) -> list[dict[str, Any]]:
        """查询指定工具组下的所有工具详情。

        Args:
            tools_discovery_endpoint: 平台工具发现端点（如 http://平台:8080/api/tools/group）
            skill_name: 工具组名（如 knowledge）

        Returns:
            工具定义列表，每个条目格式::
                {
                  "type": "function",
                  "function": {"name", "description", "parameters"},
                  "endpoint": "http://平台/api/tools/execute"
                }
        """
        url = f"{tools_discovery_endpoint.rstrip('/')}/{skill_name}"
        logger.debug("发现平台工具: GET {}", url)
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout) as client:
                resp = await client.get(url, headers=self.auth_header)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning("平台工具发现失败 [{}]: {}", url, e.response.text[:200])
            return []
        except Exception as e:
            logger.warning("平台工具发现异常 [{}]: {}", url, e)
            return []

        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        logger.warning("平台工具发现返回格式异常: {}", json.dumps(data)[:200])
        return []

    async def execute_tool(
        self,
        endpoint: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """执行平台工具。

        Args:
            endpoint: 工具执行端点（如 http://平台:8080/api/tools/execute）
            tool_name: 工具名称（与 function.name 一致）
            arguments: 工具参数

        Returns:
            平台返回的 data 字段内容。失败时返回 {"success": False, "error": ...}
        """
        body = {"tool_name": tool_name, "arguments": arguments}
        logger.debug("执行平台工具: POST {} tool={}", endpoint, tool_name)
        try:
            async with httpx.AsyncClient(timeout=self._tool_timeout) as client:
                resp = await client.post(
                    endpoint,
                    json=body,
                    headers={**self.auth_header, "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                result = resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning("平台工具执行失败 [{}]: {}", tool_name, e.response.text[:200])
            return {"success": False, "error": f"HTTP {e.response.status_code}: {e.response.text[:200]}"}
        except Exception as e:
            logger.warning("平台工具执行异常 [{}]: {}", tool_name, e)
            return {"success": False, "error": str(e)}

        # 平台响应格式：{"code": 200, "data": {...}}
        if isinstance(result, dict):
            if "data" in result:
                return result["data"]
            return result
        return {"success": False, "error": f"平台返回格式异常: {result}"}
