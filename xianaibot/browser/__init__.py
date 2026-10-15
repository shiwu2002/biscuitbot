"""常驻浏览器服务（内置浏览器）。

把浏览器当成一个可复用的服务来管理：

- 优先复用用户已经启动的、带 CDP 调试端口的浏览器（不关掉、不产生副本）；
- 探测不到时才拉起一个本应用专属的常驻 Chromium/Edge（独立 user-data-dir，
  默认 headless——界面里的内置浏览器面板就是它的"窗口"）并保活。

对上提供两类能力：

- WebUI 内置浏览器面板：CDP screencast 截图流 + 输入事件回传（遥控）；
- 智能体的浏览器自动化：playwright MCP 通过 ``--cdp-endpoint`` 挂到同一实例，
  不再每次连接都新起一个浏览器；skill 脚本同样可以用 ``connect_over_cdp``。
"""

from __future__ import annotations

from xianaibot.browser.manager import BrowserServiceManager, get_browser_manager

__all__ = ["BrowserServiceManager", "get_browser_manager"]
