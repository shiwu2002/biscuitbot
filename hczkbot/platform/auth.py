"""平台 JWT 鉴权模块。

实现桓宸智科 AI 平台 v4.1.0 接入规范的 JWT 验证流程：
1. 从 Authorization 请求头提取 Bearer Token
2. 使用共享密钥验证 JWT 签名（HS256）
3. 检查 iss == "hczk-platform"
4. 检查 exp 未过期（允许 30 秒时钟偏差）
5. 从 payload 中提取 user_id 和 apiKey
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from aiohttp import web


@dataclass(slots=True)
class PlatformUser:
    """从 JWT 中解析出的平台用户身份。"""

    user_id: str
    api_key: str | None
    is_trial: bool
    token: str  # 原始 JWT Token，用于调用平台工具 API 时复用
    payload: dict[str, Any]


class PlatformAuth:
    """平台 JWT 鉴权器。"""

    def __init__(self, shared_secret: str, issuer: str = "hczk-platform", leeway: int = 30) -> None:
        self._secret = shared_secret.encode("utf-8")
        self._issuer = issuer
        self._leeway = leeway

    def verify(self, token: str) -> PlatformUser:
        """验证 JWT Token 并返回平台用户身份。

        Raises:
            ValueError: 验证失败（签名无效/签发者不匹配/已过期等）
        """
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                issuer=self._issuer,
                leeway=self._leeway,
            )
        except jwt.MissingRequiredClaimError as e:
            raise ValueError(f"JWT 验证失败: 缺少必要字段 {e.claim}") from e
        except jwt.InvalidIssuerError as e:
            raise ValueError("JWT 验证失败: 无法识别的签发者") from e
        except jwt.ExpiredSignatureError as e:
            raise ValueError("JWT 验证失败: Token 已过期") from e
        except jwt.InvalidSignatureError as e:
            raise ValueError("JWT 验证失败: 签名无效") from e
        except jwt.InvalidTokenError as e:
            raise ValueError(f"JWT 验证失败: {e}") from e

        user_id = payload.get("user_id")
        if not user_id:
            raise ValueError("JWT 验证失败: 缺少 user_id 字段")

        api_key = payload.get("apiKey")
        is_trial = user_id == "trial"

        return PlatformUser(
            user_id=str(user_id),
            api_key=api_key,
            is_trial=is_trial,
            token=token,
            payload=payload,
        )

    def verify_request(self, request: web.Request) -> PlatformUser:
        """从 aiohttp 请求中提取并验证 JWT。

        Raises:
            web.HTTPUnauthorized: 缺少认证信息或验证失败
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise web.HTTPUnauthorized(
                text='{"error": "缺少认证信息"}',
                content_type="application/json",
            )

        token = auth_header[7:].strip()
        if not token:
            raise web.HTTPUnauthorized(
                text='{"error": "缺少认证信息"}',
                content_type="application/json",
            )

        try:
            return self.verify(token)
        except ValueError as e:
            raise web.HTTPUnauthorized(
                text=f'{{"error": "{e}"}}',
                content_type="application/json",
            ) from e


def verify_platform_jwt(request: web.Request, auth: PlatformAuth) -> PlatformUser:
    """便捷函数：验证请求中的 JWT。"""
    return auth.verify_request(request)
