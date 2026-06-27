#!/usr/bin/env python3
"""Build hczkbot desktop application with PyInstaller.

Usage::

    python hczkbot/desktop/build.py           # 构建 WebUI + 打包
    python hczkbot/desktop/build.py --skip-webui  # 跳过 WebUI 构建
    python hczkbot/desktop/build.py --console     # 带控制台（调试用）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WEBUI_DIR = PROJECT_ROOT / "webui"
SPEC_FILE = PROJECT_ROOT / "hczkbot" / "desktop" / "hczkbot.spec"


def build_webui() -> None:
    """构建 WebUI 前端到 hczkbot/web/dist/"""
    print("[1/3] Building WebUI...")
    if not (WEBUI_DIR / "package.json").exists():
        print("  Skip: webui/package.json not found")
        return
    subprocess.run(["bun", "install"], cwd=WEBUI_DIR, check=True)
    subprocess.run(["bun", "run", "build"], cwd=WEBUI_DIR, check=True)
    print("  WebUI built to hczkbot/web/dist/")


def ensure_pyinstaller() -> None:
    """确保 PyInstaller 已安装"""
    print("[2/3] Checking PyInstaller...")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("  Installing PyInstaller...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"],
            check=True,
        )
    print("  PyInstaller ready")


def run_pyinstaller(*, console: bool = False) -> None:
    """运行 PyInstaller 打包"""
    print("[3/3] Building desktop application...")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_FILE),
        "--noconfirm",
        "--clean",
    ]
    if not console:
        cmd.append("--windowed")
    else:
        cmd.append("--console")
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True)

    if sys.platform == "darwin":
        print("\nDone! Output: dist/hczkbot.app")
    else:
        print("\nDone! Output: dist/hczkbot/")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build hczkbot desktop app")
    parser.add_argument("--skip-webui", action="store_true", help="Skip WebUI build")
    parser.add_argument("--console", action="store_true", help="Build with console (debug)")
    args = parser.parse_args()

    if not args.skip_webui:
        build_webui()
    ensure_pyinstaller()
    run_pyinstaller(console=args.console)


if __name__ == "__main__":
    main()
