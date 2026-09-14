"""渠道 SDK 依赖的检测与后台自动安装。

启用渠道（WebUI 设置页）或网关启动时，若渠道类声明了 ``requires_module``
且当前环境缺失，则后台调用 pip 自动安装 ``pip_requires`` 对应包；SDK 缺失
不再静默失效。桌面打包版（冻结环境无 pip）回退为仅提示。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
from typing import Any

# 正在后台安装的渠道名集合（按渠道 name）
_IN_PROGRESS: set[str] = set()
# 最近一次安装失败信息（按渠道 name），安装成功后清除
_ERRORS: dict[str, str] = {}


def channel_sdk_available(cls: type) -> bool:
    """渠道声明的必需 SDK 模块是否可用；未声明 ``requires_module`` 视为可用。"""
    module = getattr(cls, "requires_module", None)
    if not module:
        return True
    return importlib.util.find_spec(module) is not None


def deps_status(cls: type) -> dict[str, Any]:
    """返回渠道依赖状态（供渠道列表载荷逐行渲染）。"""
    name = getattr(cls, "name", "")
    return {
        "sdk_available": channel_sdk_available(cls),
        "deps_installing": name in _IN_PROGRESS,
        "deps_error": _ERRORS.get(name),
    }


def ensure_channel_deps(cls: type) -> bool:
    """确保渠道依赖已安装；缺失时在后台线程自动安装。

    返回 True 表示已触发后台安装。SDK 已就绪 / 未声明依赖 / 当前环境无 pip
    时返回 False，调用方据此决定提示方式。
    """
    if channel_sdk_available(cls):
        return False
    name = getattr(cls, "name", "")
    reqs = getattr(cls, "pip_requires", [])
    if not reqs:
        return False
    if importlib.util.find_spec("pip") is None:
        _ERRORS[name] = "当前环境无 pip，无法自动安装；请在 Python 环境安装渠道依赖。"
        return False
    if name in _IN_PROGRESS:
        return True
    _IN_PROGRESS.add(name)
    _ERRORS.pop(name, None)
    threading.Thread(
        target=_run_pip_install, args=(name, reqs), daemon=True, name=f"deps-{name}"
    ).start()
    return True


def _run_pip_install(name: str, reqs: list[str]) -> None:
    """后台执行 pip install，结果写入 _ERRORS（仅失败时）。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", *reqs],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            _ERRORS[name] = (result.stderr or result.stdout or "").strip()[-300:]
    except Exception as exc:  # noqa: BLE001 - 后台任务不向上抛
        _ERRORS[name] = str(exc)[-300:]
    finally:
        _IN_PROGRESS.discard(name)
