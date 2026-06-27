"""PyInstaller entry point for the hczkbot desktop application.

This script bypasses typer's command dispatch and directly launches
the desktop runner, avoiding the overhead of full CLI parsing in a
frozen application.
"""

from __future__ import annotations

import sys


def main() -> None:
    from hczkbot.cli.commands import _load_runtime_config
    from hczkbot.desktop.app import run_desktop

    # 解析简单的命令行参数（在 frozen 环境中 typer 的解析可能不可靠）
    config = None
    workspace = None
    port = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--config", "-c") and i + 1 < len(args):
            config = args[i + 1]
            i += 2
        elif arg in ("--workspace", "-w") and i + 1 < len(args):
            workspace = args[i + 1]
            i += 2
        elif arg in ("--port", "-p") and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        else:
            i += 1

    cfg = _load_runtime_config(config, workspace)
    run_desktop(cfg, port=port)


if __name__ == "__main__":
    main()
