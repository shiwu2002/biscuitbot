"""打包环境下的「用 bundle 内的解释器跑 Python 脚本」能力。

PyInstaller 冻结后（桌面 sidecar）bundle 里没有独立的 python 可执行文件，唯一可
执行的是 sidecar 本体。``xianaibot.desktop.sidecar`` 为此提供了一个隐藏的**解释器
模式**：以 ``<sidecar> __xianaibot_python__ <args>`` 调用时，它会用打包进 bundle 的
解释器执行 ``<args>``，从而复用 bundle 里的标准库与第三方依赖。

本模块把这份能力抽出来给两处复用：

- ``xianaibot.agent.tools.shell``：把 shim 目录前置到 PATH，让 agent 在
  shell 里直接敲 ``python3``（需要磁盘上真实存在的可执行文件）；
- ``xianaibot.webui.skill_hub``：以子进程直接跑技能商店 CLI 脚本（只需要命令
  前缀，不需要 shim 文件）。

两处都只在冻结环境下生效，源码运行时返回 ``None``，由调用方自行回退
（venv bin / ``sys.executable``）。
"""

from __future__ import annotations

import hashlib
import shlex
import sys
import tempfile
from pathlib import Path

from loguru import logger

#: 隐藏参数：``python xianaibot/desktop/sidecar.py`` 之外唯一进入解释器模式的入口，
#: 与 ``xianaibot.desktop.sidecar`` 共用同一常量（单一事实来源）。
PYTHON_MODE_MARKER = "__xianaibot_python__"

_IS_WINDOWS = sys.platform == "win32"
#: shim 目录里的解释器文件名（Windows 需要 ``.cmd`` 才能被 shell 直接调用）
_PYTHON_SHIM_NAME = "python3.cmd" if _IS_WINDOWS else "python3"


def _frozen_executable() -> Path | None:
    """冻结环境下的宿主可执行文件（sidecar 本体）；非冻结或不可用时 None。"""
    if not getattr(sys, "frozen", False):
        return None
    try:
        exe = Path(sys.executable).resolve()
    except OSError:
        return None
    return exe if exe.is_file() else None


def bundled_interpreter_prefix() -> list[str] | None:
    """返回「用 bundle 内解释器执行 Python 脚本」的命令前缀，形如
    ``["<sidecar>", "__xianaibot_python__"]``；非冻结环境返回 ``None``。

    直接以 argv 列表执行时用这个，而不是 PATH 上的 shim 文件——Windows 的
    ``.cmd`` 无法被 ``CreateProcess`` 直接执行，只有 shell 才会按 PATHEXT 解析。
    """
    exe = _frozen_executable()
    if exe is None:
        return None
    return [str(exe), PYTHON_MODE_MARKER]


def bundled_python_bin_dir() -> str | None:
    """冻结环境下生成（或复用）解释器 shim 目录，内含 python/python3/pip/pip3。

    目录以可执行路径指纹命名、放在临时目录，sidecar 重装后指纹变化自动失效重建。
    非冻结环境返回 ``None``（调用方应改用 venv bin）。
    """
    exe = _frozen_executable()
    if exe is None:
        return None
    key = hashlib.sha256(str(exe).encode()).hexdigest()[:12]
    bindir = Path(tempfile.gettempdir()) / f"xianaibot-py-{key}"
    # 以平台对应的文件名判缓存命中：Windows 下生成的是 ``python3.cmd``，
    # 用无后缀的 ``python3`` 判断会永远 miss、每次调用都重写一遍 shim。
    if bindir.is_dir() and (bindir / _PYTHON_SHIM_NAME).exists():
        return str(bindir)
    try:
        bindir.mkdir(parents=True, exist_ok=True)
        if _IS_WINDOWS:
            for name in ("python.cmd", "python3.cmd"):
                (bindir / name).write_text(
                    f'@echo off\r\n"{exe}" {PYTHON_MODE_MARKER} %*\r\n',
                    encoding="utf-8",
                )
            for name in ("pip.cmd", "pip3.cmd"):
                (bindir / name).write_text(
                    f'@echo off\r\n"{exe}" {PYTHON_MODE_MARKER} -m pip %*\r\n',
                    encoding="utf-8",
                )
        else:
            exe_q = shlex.quote(str(exe))
            for name in ("python", "python3"):
                (bindir / name).write_text(
                    f'#!/bin/sh\nexec {exe_q} {PYTHON_MODE_MARKER} "$@"\n',
                    encoding="utf-8",
                )
            for name in ("pip", "pip3"):
                (bindir / name).write_text(
                    f'#!/bin/sh\nexec {exe_q} {PYTHON_MODE_MARKER} -m pip "$@"\n',
                    encoding="utf-8",
                )
        for shim in bindir.iterdir():
            shim.chmod(0o755)
    except OSError:
        logger.warning("无法创建 bundled python shim 目录：{}", bindir)
        return None
    return str(bindir)
