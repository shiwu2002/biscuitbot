"""PyInstaller 打包入口：以无头 sidecar 方式启动 biscuitbot 网关。

桌面壳（Tauri）以子进程方式启动本可执行文件，通过 stdout 握手行
（``BISCUITBOT_GATEWAY_READY <host> <port>``）获知网关就绪地址。
"""

import multiprocessing

from biscuitbot.desktop.sidecar import main

if __name__ == "__main__":
    # frozen（PyInstaller）环境必须最先调用 freeze_support()：
    # 网关日志 sink 用了 loguru enqueue=True，其内部会创建
    # multiprocessing 原语并拉起 resource_tracker 子进程；frozen 下该
    # 子进程以本可执行文件重新启动并完整重放入口脚本。没有
    # freeze_support() 拦截时，每个子进程又启动一个完整网关、再 spawn
    # 下一个子进程——无限递归、进程爆炸（曾导致 100+ 个 sidecar 进程
    # 占满 CPU/端口、壳反复收到 READY 行而界面闪烁、整机卡死）。
    multiprocessing.freeze_support()
    main()
