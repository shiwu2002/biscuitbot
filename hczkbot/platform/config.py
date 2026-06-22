"""平台集成配置 schema。"""
from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, ConfigDict, Field

from hczkbot.config_base import Base


class PlatformConfig(Base):
    """桓宸智科 AI 平台集成配置。

    通过 ``~/.hczkbot/config.json`` 的 ``platform`` 字段配置，或通过环境变量
    ``JWT_AGENT_SHARED_SECRET`` 覆盖共享密钥。

    示例::

        {
          "platform": {
            "enabled": true,
            "jwtSecret": "hczk-ai-platform-secret-key-2024-very-long-and-secure",
            "agentName": "客服智能体",
            "agentDescription": "基于 hczkbot 的客服智能体"
          }
        }
    """

    model_config = ConfigDict(extra="allow")

    # 是否启用平台集成（注册 /api/* 平台规范端点）
    enabled: bool = False

    # JWT 共享密钥（HS256）。生产环境推荐用环境变量 JWT_AGENT_SHARED_SECRET 注入。
    jwt_secret: str = Field(
        default="hczk-ai-platform-secret-key-2024-very-long-and-secure",
        validation_alias=AliasChoices("jwtSecret", "jwt_secret", "sharedSecret", "shared_secret"),
    )

    # JWT 签发者标识（固定为 hczk-platform）
    jwt_issuer: str = Field(
        default="hczk-platform",
        validation_alias=AliasChoices("jwtIssuer", "jwt_issuer"),
    )

    # JWT 时钟偏差（秒），允许 30 秒
    jwt_leeway_seconds: int = Field(
        default=30,
        validation_alias=AliasChoices("jwtLeewaySeconds", "jwt_leeway_seconds"),
    )

    # 智能体元信息（/api/agent/info 返回）
    agent_name: str = Field(
        default="hczkbot 智能体",
        validation_alias=AliasChoices("agentName", "agent_name"),
    )
    agent_version: str = Field(
        default="1.0.0",
        validation_alias=AliasChoices("agentVersion", "agent_version"),
    )
    agent_description: str = Field(
        default="基于 hczkbot 的智能体",
        validation_alias=AliasChoices("agentDescription", "agent_description"),
    )
    agent_type: str = Field(
        default="",
        validation_alias=AliasChoices("agentType", "agent_type"),
    )

    # 工具调用配置
    skills_request_timeout: float = Field(
        default=30.0,
        validation_alias=AliasChoices("skillsRequestTimeout", "skills_request_timeout"),
        description="平台工具发现/执行 HTTP 请求超时（秒）",
    )
    skills_tool_timeout: float = Field(
        default=60.0,
        validation_alias=AliasChoices("skillsToolTimeout", "skills_tool_timeout"),
        description="单个平台工具执行超时（秒）",
    )

    # 文档上传配置
    documents_dir: str = Field(
        default="",
        validation_alias=AliasChoices("documentsDir", "documents_dir"),
        description="知识库文档存储目录，默认使用 workspace 下的 platform-documents",
    )

    def resolve_jwt_secret(self) -> str:
        """解析最终的 JWT 共享密钥，优先环境变量。"""
        import os

        return os.environ.get("JWT_AGENT_SHARED_SECRET") or self.jwt_secret

    def resolve_documents_dir(self, workspace: str) -> Any:
        """解析文档存储目录。"""
        from pathlib import Path

        if self.documents_dir:
            return Path(self.documents_dir).expanduser()
        return Path(workspace).expanduser() / "platform-documents"
