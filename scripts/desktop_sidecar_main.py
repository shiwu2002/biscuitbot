"""PyInstaller 打包入口：以无头 sidecar 方式启动 biscuitbot 网关。

桌面壳（Tauri）以子进程方式启动本可执行文件，通过 stdout 握手行
（``BISCUITBOT_GATEWAY_READY <host> <port>``）获知网关就绪地址。
"""

from biscuitbot.desktop.sidecar import main

if __name__ == "__main__":
    main()
