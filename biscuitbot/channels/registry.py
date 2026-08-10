"""渠道自动发现模块，用于发现内置渠道模块和外部插件。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的发现组件。
在项目架构中起到的作用：扫描并加载所有可用的渠道类，支持内置渠道模块的
自动发现和通过 entry_points 注册的外部插件加载。

平台特点与接入方式
------------------
- 内置渠道发现：通过 pkgutil.iter_modules 扫描 biscuitbot.channels 包下的模块，
  零导入成本，仅返回模块名称列表。
- 外部插件发现：通过 importlib.metadata.entry_points 加载 ``biscuitbot.channels``
  组下注册的第三方渠道插件。
- 按需导入：仅导入已启用的渠道模块，跳过未启用渠道的重量级第三方 SDK 导入。
- 优先级：内置渠道优先于外部插件，外部插件不能覆盖同名内置渠道。
"""
from __future__ import annotations

import importlib  # 动态模块导入
import pkgutil  # 包模块迭代扫描
from typing import TYPE_CHECKING  # 类型检查时导入支持

from loguru import logger  # 日志记录

if TYPE_CHECKING:
    from biscuitbot.channels.base import BaseChannel  # 渠道抽象基类（仅类型检查时导入）

# 内部模块集合（非渠道模块，发现时需排除）
_INTERNAL = frozenset({"base", "manager", "registry"})


def discover_channel_names() -> list[str]:
    """通过扫描包返回所有内置渠道模块名称（零导入）。"""
    import biscuitbot.channels as pkg

    return [
        name
        for _, name, ispkg in pkgutil.iter_modules(pkg.__path__)
        if name not in _INTERNAL and not ispkg
    ]


def load_channel_class(module_name: str) -> type[BaseChannel]:
    """导入 *module_name* 并返回找到的第一个 BaseChannel 子类。"""
    from biscuitbot.channels.base import BaseChannel as _Base

    mod = importlib.import_module(f"biscuitbot.channels.{module_name}")
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, _Base) and obj is not _Base:
            return obj
    raise ImportError(f"No BaseChannel subclass in biscuitbot.channels.{module_name}")


def discover_plugins(enabled_names: set[str] | None = None) -> dict[str, type[BaseChannel]]:
    """发现通过 entry_points 注册的外部渠道插件。"""
    from importlib.metadata import entry_points

    plugins: dict[str, type[BaseChannel]] = {}
    for ep in entry_points(group="biscuitbot.channels"):
        if enabled_names is not None and ep.name not in enabled_names:
            continue
        try:
            cls = ep.load()
            plugins[ep.name] = cls
        except Exception as e:
            logger.warning("Failed to load channel plugin '{}': {}", ep.name, e)
    return plugins


def discover_enabled(
    enabled_names: set[str],
    *,
    _names: list[str] | None = None,
    _include_all_external: bool = False,
) -> dict[str, type[BaseChannel]]:
    """返回模块名称在 *enabled_names* 中的渠道。

    使用低成本的 ``pkgutil.iter_modules`` 列出名称，然后仅导入匹配的模块
    —— 跳过未启用渠道的重量级第三方 SDK 导入。
    """
    names = _names if _names is not None else discover_channel_names()
    result: dict[str, type[BaseChannel]] = {}
    for modname in names:
        if modname not in enabled_names:
            continue
        try:
            result[modname] = load_channel_class(modname)
        except ImportError as e:
            logger.debug("Skipping built-in channel '{}': {}", modname, e)

    external = discover_plugins(None if _include_all_external else enabled_names)
    shadowed = set(external) & set(result)
    if shadowed:
        logger.warning("Plugin(s) shadowed by built-in channels (ignored): {}", shadowed)
    if _include_all_external:
        result.update({k: v for k, v in external.items() if k not in shadowed})
    else:
        result.update({k: v for k, v in external.items() if k not in shadowed and k in enabled_names})

    return result


def discover_all() -> dict[str, type[BaseChannel]]:
    """返回所有渠道：内置（pkgutil）与外部（entry_points）合并。

    内置渠道优先 —— 外部插件不能覆盖同名内置渠道。
    """
    names = discover_channel_names()
    return discover_enabled(set(names), _names=names, _include_all_external=True)
