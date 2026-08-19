"""运行时路径辅助工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/config 目录，提供基于当前配置上下文派生的运行时路径。
在项目架构中起到的作用：根据配置文件所在位置（实例目录）派生数据目录、媒体目录、
日志目录、cron 目录、WebUI 目录等实例级运行时路径，并提供共享的 CLI 历史、
WhatsApp bridge 安装目录与遗留会话目录路径，统一管理文件落盘位置。
"""

from __future__ import annotations

from pathlib import Path  # 跨平台路径处理

from biscuitbot.utils.helpers import ensure_dir  # 确保目录存在（不存在则创建）


def get_config_path() -> Path:
    """获取配置文件路径（惰性导入以打破循环依赖）。

    在调用时委托给 ``biscuitbot.config.loader.get_config_path``，从而避免
    在启动阶段导入本模块时触发循环导入。
    """
    from biscuitbot.config.loader import get_config_path as _loader_get_config_path
    return _loader_get_config_path()


def get_data_dir() -> Path:
    """返回实例级运行时数据目录（配置文件所在目录）。"""
    return ensure_dir(get_config_path().parent)


def get_runtime_subdir(name: str) -> Path:
    """返回实例数据目录下指定名称的运行时子目录（自动创建）。"""
    return ensure_dir(get_data_dir() / name)


def get_media_dir(channel: str | None = None) -> Path:
    """返回媒体目录，可按渠道命名空间隔离。

    channel 非空时返回该渠道专属的媒体子目录，否则返回媒体根目录。
    渠道入站媒体统一收敛到 ``media/channels/<channel>/``（如 ``channels/weixin``），
    与生成资产（``generated`` / ``generated_video`` / ``api``）分开。
    """
    base = get_runtime_subdir("media")
    return ensure_dir(base / channel) if channel else base


# 历史上直接把入站媒体写到 ``media/<channel>/`` 的渠道名，迁移时逐个收敛。
_LEGACY_CHANNEL_MEDIA_NAMES: tuple[str, ...] = (
    "weixin",
    "feishu",
    "dingtalk",
    "qq",
    "napcat",
    "email",
    "wecom",
)


def migrate_legacy_channel_media(media_root: Path | None = None) -> list[str]:
    """把旧布局 ``media/<channel>/`` 的渠道入站媒体迁到 ``media/channels/<channel>/``。

    仅迁移 ``_LEGACY_CHANNEL_MEDIA_NAMES`` 列出的已知渠道，不触碰 ``websocket`` /
    ``generated`` / ``generated_video`` / ``api`` 等目录。同名文件冲突时旧文件加
    ``_legacy<N>`` 后缀保留，不覆盖新文件。幂等：迁移后旧目录被清空移除。

    返回实际迁移的渠道名列表。
    """
    import shutil

    root = media_root or get_media_dir()
    channels_dir = root / "channels"
    migrated: list[str] = []

    def _merge(src: Path, dst: Path) -> None:
        dst.mkdir(parents=True, exist_ok=True)
        for entry in sorted(src.iterdir(), key=lambda p: p.name):
            dest = dst / entry.name
            if entry.is_dir():
                _merge(entry, dest)
                continue
            if dest.exists():
                for i in range(1, 1000):
                    alt = dest.with_suffix(f".legacy{i}{dest.suffix}")
                    if not alt.exists():
                        shutil.move(str(entry), str(alt))
                        break
                continue
            shutil.move(str(entry), str(dest))

    def _drop_empty(path: Path) -> None:
        for child in list(path.iterdir()):
            if child.is_dir():
                _drop_empty(child)
        try:
            path.rmdir()
        except OSError:
            pass

    for name in _LEGACY_CHANNEL_MEDIA_NAMES:
        legacy = root / name
        if not legacy.is_dir():
            continue
        _merge(legacy, channels_dir / name)
        _drop_empty(legacy)
        if not legacy.exists():
            migrated.append(name)

    return migrated


def get_cron_dir() -> Path:
    """返回定时任务（cron）存储目录。"""
    return get_runtime_subdir("cron")


def get_logs_dir() -> Path:
    """返回日志输出目录。"""
    return get_runtime_subdir("logs")


def get_webui_dir() -> Path:
    """返回 WebUI 专用持久化展示线程目录（JSON 格式）。"""
    return get_runtime_subdir("webui")


def get_workspace_path(workspace: str | Path | None = None) -> Path:
    """解析并确保智能体工作区路径存在。

    未传入 workspace 时回退到默认工作区 ~/.biscuitbot/workspace。
    """
    path = Path(workspace).expanduser() if workspace else Path.home() / ".biscuitbot" / "workspace"
    return ensure_dir(path)


def is_default_workspace(workspace: str | Path | None) -> bool:
    """判断给定工作区是否解析为 biscuitbot 默认工作区路径。"""
    current = Path(workspace).expanduser() if workspace is not None else Path.home() / ".biscuitbot" / "workspace"
    default = Path.home() / ".biscuitbot" / "workspace"
    return current.resolve(strict=False) == default.resolve(strict=False)


def get_cli_history_path() -> Path:
    """返回共享的 CLI 历史记录文件路径。"""
    return Path.home() / ".biscuitbot" / "history" / "cli_history"


def get_bridge_install_dir() -> Path:
    """返回共享的 WhatsApp bridge 安装目录。"""
    return Path.home() / ".biscuitbot" / "bridge"


def get_legacy_sessions_dir() -> Path:
    """返回用于迁移回退的遗留全局会话目录。"""
    return Path.home() / ".biscuitbot" / "sessions"
