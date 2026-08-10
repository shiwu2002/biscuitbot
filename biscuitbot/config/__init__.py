"""biscuitbot 配置模块入口。

所属模块与项目作用
===================
本文件位于 biscuitbot/config 目录，是 config 配置模块的统一对外入口。
在项目架构中起到的作用：聚合配置加载（loader）、运行时路径（paths）与配置数据模型
（schema）三大部分，对外暴露 Config 配置对象、load_config 加载函数及一系列路径获取
辅助函数，供 CLI、网关、渠道等组件统一获取配置与数据目录。
"""

# 配置加载相关：获取配置文件路径与加载配置对象
from biscuitbot.config.loader import get_config_path, load_config
# 运行时路径辅助：派生数据目录、媒体目录、日志目录等实例级路径
from biscuitbot.config.paths import (
    get_bridge_install_dir,
    get_cli_history_path,
    get_cron_dir,
    get_data_dir,
    get_legacy_sessions_dir,
    is_default_workspace,
    get_logs_dir,
    get_media_dir,
    get_runtime_subdir,
    get_webui_dir,
    get_workspace_path,
)
# 配置数据模型根类
from biscuitbot.config.schema import Config

# 对外暴露的公共 API：配置对象、加载函数与各类路径辅助函数
__all__ = [
    "Config",
    "load_config",
    "get_config_path",
    "get_data_dir",
    "get_runtime_subdir",
    "get_media_dir",
    "get_cron_dir",
    "get_logs_dir",
    "get_webui_dir",
    "get_workspace_path",
    "is_default_workspace",
    "get_cli_history_path",
    "get_bridge_install_dir",
    "get_legacy_sessions_dir",
]
