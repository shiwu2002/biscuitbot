"""biscuitbot 工具函数包入口。

所属模块与项目作用
===================
本文件位于 biscuitbot/utils 目录，是工具函数模块的包初始化文件。
在项目架构中起到的作用：
1. 暴露常用的工具函数（如 ensure_dir、abbreviate_path）作为包级公共 API；
2. 通过惰性别名机制为已迁移的旧模块路径提供向后兼容的导入入口，
   避免外部代码因模块重组而失效。
"""

from __future__ import annotations

import sys  # 操作 sys.modules 以注入惰性模块别名
from importlib import import_module  # 按需加载真实目标模块
from types import ModuleType

from biscuitbot.utils.helpers import ensure_dir  # 目录创建工具
from biscuitbot.utils.path import abbreviate_path  # 路径缩写工具

# 包级公开导出的工具函数名
__all__ = ["ensure_dir", "abbreviate_path"]


class _LazyModuleAlias(ModuleType):
    """惰性模块别名，首次访问属性时才加载真实模块。

    用于把已迁移的旧模块路径映射到新路径，避免在包导入阶段就
    加载较重的子模块，从而减少启动开销。
    """

    def __init__(self, name: str, target: str) -> None:
        super().__init__(name)
        # 记录真实目标模块的完整路径，避免与同名属性冲突
        self.__dict__["_target"] = target

    def _load(self) -> ModuleType:
        # 首次加载真实模块，并替换 sys.modules 中的别名，使后续访问直接命中
        module = import_module(self.__dict__["_target"])
        sys.modules[self.__name__] = module
        return module

    def __getattr__(self, name: str) -> object:
        # 首次访问任意属性时触发真实模块加载
        return getattr(self._load(), name)

    def __dir__(self) -> list[str]:
        # 合并别名自身与真实模块的属性列表，便于补全与自省
        return sorted(set(super().__dir__()) | set(dir(self._load())))


# 旧模块名到新模块路径的映射，用于向后兼容
_LEGACY_MODULE_ALIASES = {
    "webui_thread_disk": "biscuitbot.webui.thread_disk",
    "webui_transcript": "biscuitbot.webui.transcript",
    "webui_turn_helpers": "biscuitbot.session.webui_turns",
}

# 为每个旧模块名注册惰性别名，未真实访问前不触发导入
for _legacy_name, _target_name in _LEGACY_MODULE_ALIASES.items():
    sys.modules.setdefault(
        f"{__name__}.{_legacy_name}",
        _LazyModuleAlias(f"{__name__}.{_legacy_name}", _target_name),
    )
