"""工具发现与注册：通过包扫描自动发现并注册工具。

所属模块与项目作用
==================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的加载与发现组件。
在项目架构中起到的作用：扫描 ``biscuitbot.agent.tools`` 包下的所有模块，
自动发现 ``Tool`` 子类并注册到工具注册表；同时支持通过 setuptools
entry_points 加载外部插件工具。它是工具系统从"代码定义"到"运行时可用"
的桥梁。
"""
from __future__ import annotations

import importlib  # 动态导入模块
import pkgutil  # 包遍历工具，用于扫描工具模块
from importlib.metadata import entry_points  # 入口点加载，用于发现外部插件
from typing import Any  # 任意类型

from loguru import logger  # 日志库

from biscuitbot.agent.tools.base import Tool  # 工具基类
from biscuitbot.agent.tools.registry import ToolRegistry  # 工具注册表

# 扫描时跳过的模块名集合：这些模块是基础设施（基类、schema、注册表、
# 上下文、加载器自身、配置、文件状态、沙箱、MCP 集成、运行时状态协议），
# 不包含可直接发现的 Tool 子类。
_SKIP_MODULES = frozenset({
    "base", "schema", "registry", "context", "loader", "config",
    "file_state", "sandbox", "mcp", "__init__", "runtime_state",
})


class ToolLoader:
    """工具加载器：扫描包并注册发现的工具类。

    职责：遍历指定包下的所有模块，发现符合条件的 ``Tool`` 子类，
    并按作用域与启用状态将其注册到工具注册表。支持内置工具发现与
    外部插件发现两种来源，内置工具优先于同名插件。
    """

    def __init__(self, package: Any = None, *, test_classes: list[type[Tool]] | None = None):
        if package is None:
            # 默认扫描 biscuitbot.agent.tools 包
            import biscuitbot.agent.tools as _pkg
            package = _pkg
        self._package = package  # 待扫描的包
        self._test_classes = test_classes  # 测试用注入的工具类列表（绕过扫描）
        self._discovered: list[type[Tool]] | None = None  # 已发现的内置工具类缓存
        self._plugins: dict[str, type[Tool]] | None = None  # 已发现的插件工具类缓存

    def discover(self) -> list[type[Tool]]:
        """扫描包并返回所有可发现的内置 Tool 子类。

        返回:
            按类名排序的 Tool 子类列表。若注入了 test_classes 则直接返回。
        """
        # 测试注入的工具类优先返回，绕过包扫描
        if self._test_classes is not None:
            return list(self._test_classes)
        if self._discovered is not None:
            return self._discovered
        seen: set[int] = set()  # 已收集的工具类 id 集合，去重
        results: list[type[Tool]] = []
        # 遍历包下所有模块
        for _importer, module_name, _ispkg in pkgutil.iter_modules(self._package.__path__):
            # 跳过私有模块（_ 开头）与基础设施模块
            if module_name.startswith("_") or module_name in _SKIP_MODULES:
                continue
            try:
                module = importlib.import_module(f".{module_name}", self._package.__name__)
            except Exception:
                logger.exception("Failed to import tool module: %s", module_name)
                continue
            # 在模块中查找符合条件的 Tool 子类
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, Tool)
                    and attr is not Tool  # 排除基类自身
                    and not attr_name.startswith("_")  # 排除私有命名
                    and not getattr(attr, "__abstractmethods__", None)  # 必须已实现所有抽象方法
                    and getattr(attr, "_plugin_discoverable", True)  # 允许插件发现
                    and id(attr) not in seen  # 去重
                ):
                    seen.add(id(attr))
                    results.append(attr)
        # 按类名排序，保证注册顺序稳定
        results.sort(key=lambda cls: cls.__name__)
        self._discovered = results
        return results

    def _discover_plugins(self) -> dict[str, type[Tool]]:
        """发现通过 entry_points 注册的外部工具插件。

        返回:
            插件名 -> Tool 子类的字典。
        """
        if self._plugins is not None:
            return self._plugins
        plugins: dict[str, type[Tool]] = {}
        try:
            # 读取 biscuitbot.tools 入口点组
            eps = entry_points(group="biscuitbot.tools")
        except Exception:
            return plugins
        for ep in eps:
            try:
                cls = ep.load()
                if (
                    isinstance(cls, type)
                    and issubclass(cls, Tool)
                    and not getattr(cls, "__abstractmethods__", None)
                    and getattr(cls, "_plugin_discoverable", True)
                ):
                    plugins[ep.name] = cls
            except Exception:
                logger.exception("Failed to load tool plugin: %s", ep.name)
        self._plugins = plugins
        return plugins

    def load(self, ctx: Any, registry: ToolRegistry, *, scope: str = "core") -> list[str]:
        """加载所有已启用工具到注册表。

        参数:
            ctx: 工具创建上下文，提供配置、工作区等。
            registry: 目标工具注册表。
            scope: 工具作用域，仅加载声明了该作用域的工具。

        返回:
            成功注册的工具名称列表。内置工具优先于同名插件。
        """
        registered: list[str] = []
        builtin_names: set[str] = set()  # 已注册的内置工具名集合
        # 工具来源：内置工具（非插件）在前，插件工具在后
        sources = [(self.discover(), False), (self._discover_plugins().values(), True)]
        for source, is_plugin_source in sources:
            for tool_cls in source:
                cls_label = tool_cls.__name__
                try:
                    # 按作用域过滤
                    if scope not in getattr(tool_cls, "_scopes", {"core"}):
                        continue
                    # 按启用状态过滤
                    if not tool_cls.enabled(ctx):
                        continue
                    tool = tool_cls.create(ctx)
                    # 处理工具名冲突
                    if registry.has(tool.name):
                        # 插件与内置工具同名时跳过插件
                        if is_plugin_source and tool.name in builtin_names:
                            logger.warning(
                                "Plugin %s skipped: conflicts with built-in tool %s",
                                cls_label, tool.name,
                            )
                            continue
                        logger.warning(
                            "Tool name collision: %s from %s overwrites existing",
                            tool.name, cls_label,
                        )
                    registry.register(tool)
                    registered.append(tool.name)
                    # 记录内置工具名，用于后续插件冲突检测
                    if not is_plugin_source:
                        builtin_names.add(tool.name)
                except Exception:
                    logger.exception("Failed to register tool: %s", cls_label)
        return registered
