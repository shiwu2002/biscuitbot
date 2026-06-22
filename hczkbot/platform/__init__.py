"""桓宸智科 AI 平台集成层。

将 hczkbot 接入桓宸智科 AI 平台，提供：
- JWT 鉴权（HS256，与平台共享密钥）
- 平台 Skills 工具动态加载与执行
- 平台规范 API 端点（/api/health, /api/chat, /api/chat/stream 等）
"""
from hczkbot.platform.auth import PlatformAuth, PlatformUser, verify_platform_jwt
from hczkbot.platform.config import PlatformConfig
from hczkbot.platform.skills_client import PlatformSkillsClient
from hczkbot.platform.skills_tool import PlatformSkillTool, build_skill_tools

__all__ = (
    "PlatformAuth",
    "PlatformUser",
    "PlatformConfig",
    "PlatformSkillsClient",
    "PlatformSkillTool",
    "build_skill_tools",
    "verify_platform_jwt",
)
