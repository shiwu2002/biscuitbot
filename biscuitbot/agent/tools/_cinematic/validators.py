"""AI 导演工作流纯校验函数。

职责与项目角色：
- 提供 :class:`CinematicDirectorError`（校验 / 状态机错误）；
- 校验项目 ID / 资产 ID 格式、资产 ID 与 kind 前缀一致；
- 校验角色→动作权限（软校验）；
- 提供阶段硬门辅助（``require`` / ``require_stage``）。
"""

from __future__ import annotations

from typing import Any

from .constants import _ASSET_KINDS, _ASSET_ID_RE, _PROJECT_ID_RE, _ROLE_ACTIONS


class CinematicDirectorError(Exception):
    """AI 导演工作流的校验 / 状态机错误（作为工具结果返回给模型）。"""


def require(condition: Any, message: str) -> None:
    """断言条件成立，否则抛出 CinematicDirectorError。"""
    if not condition:
        raise CinematicDirectorError(message)


def validate_project_id(project_id: Any) -> str:
    """校验项目 ID 为安全 slug，返回原值。"""
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.match(project_id):
        raise CinematicDirectorError(
            f"invalid project_id {project_id!r}: must match [a-z0-9][a-z0-9_-]{{0,63}}"
        )
    return project_id


def validate_asset_id(asset_id: Any) -> str:
    """校验资产 ID 格式（前缀 + 3 位数字）。"""
    if not isinstance(asset_id, str) or not _ASSET_ID_RE.match(asset_id):
        raise CinematicDirectorError(f"invalid asset id {asset_id!r}; expect like CHAR001")
    return asset_id


def validate_kind_id(kind: Any, asset_id: Any) -> None:
    """校验资产类型合法且资产 ID 前缀与 kind 一致。"""
    require(kind in _ASSET_KINDS, f"kind must be one of {list(_ASSET_KINDS)}")
    validate_asset_id(asset_id)
    require(
        isinstance(asset_id, str) and asset_id.startswith(kind),
        f"asset id {asset_id!r} must start with kind {kind!r}",
    )


def check_role(role: Any, action: str) -> str:
    """校验角色能执行指定动作（软校验，默认 director）。返回规范化角色名。"""
    if role is None:
        role = "director"
    require(role in _ROLE_ACTIONS, f"unknown role {role!r}")
    allowed = _ROLE_ACTIONS[role]
    require(action in allowed, f"role {role!r} is not allowed to perform action {action!r}")
    return role


def require_stage(stage: str, action: str, *allowed: str) -> None:
    """阶段硬门：当前 stage 必须属于 allowed，否则拒绝（禁止跳阶段）。"""
    if stage not in allowed:
        raise CinematicDirectorError(
            f"action {action!r} not allowed at stage {stage!r}; expected one of {list(allowed)}"
        )
