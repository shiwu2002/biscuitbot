"""
hczkbot - 轻量级 AI agent 框架
"""

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path


def _read_pyproject_version() -> str | None:
    """当包元数据不可用时，从源码树读取版本号。"""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        return None
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data.get("project", {}).get("version")


def _resolve_version() -> str:
    try:
        return _pkg_version("hczkbot-ai")
    except PackageNotFoundError:
        # 源码检出通常在没有安装 dist-info 的情况下导入 hczkbot
        return _read_pyproject_version() or "0.2.1"


__version__ = _resolve_version()
__logo__ = "🐺"

_LAZY_EXPORTS = {
    "Hczkbot": ".hczkbot",
    "RunResult": ".hczkbot",
}


def __getattr__(name: str):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    mod = import_module(module_path, __name__)
    val = getattr(mod, name)
    globals()[name] = val
    return val


__all__ = ["Hczkbot", "RunResult"]
