"""工作区范围工具共享的路径辅助函数。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的路径工具组件。
在项目架构中起到的作用：为各文件系统类工具提供统一的路径解析与
范围校验能力，确保工具访问的路径始终在允许的目录范围内，防止越权访问。
"""

from pathlib import Path

from biscuitbot.config.paths import get_media_dir  # 媒体目录获取函数
from biscuitbot.security.workspace_policy import (  # 工作区路径安全策略
    is_path_within,
    resolve_allowed_path,
)


def is_under(path: Path, directory: Path) -> bool:
    """判断路径是否解析到指定目录之下。

    参数:
        path: 待检查的路径。
        directory: 目标目录。

    返回:
        路径在目录下返回 True，否则 False。
    """
    return is_path_within(path, directory)


def resolve_workspace_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
    extra_allowed_dirs: list[Path] | None = None,
) -> Path:
    """基于工作区解析路径，并强制校验路径在允许的目录范围内。

    参数:
        path: 待解析的路径字符串。
        workspace: 工作区根目录。
        allowed_dir: 允许访问的目录。
        extra_allowed_dirs: 额外允许的目录列表。

    返回:
        解析后的绝对路径。
    """
    extra_roots = [get_media_dir(), *(extra_allowed_dirs or [])] if allowed_dir else None
    return resolve_allowed_path(
        path,
        workspace=workspace,
        allowed_root=allowed_dir,
        extra_allowed_roots=extra_roots,
    )
