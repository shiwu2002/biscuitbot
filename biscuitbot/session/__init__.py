"""会话管理模块。

所属模块与项目作用
===================
本文件位于 biscuitbot/session 目录，是 session 会话管理模块的对外入口。
在项目架构中起到的作用：聚合会话管理器与会话数据类，对外暴露 Session（单个会话）
与 SessionManager（会话管理器），供智能体循环、渠道、WebUI 等组件加载、持久化与
操作对话历史。
"""

# 导入会话数据类与会话管理器
from biscuitbot.session.manager import Session, SessionManager

# 对外暴露的核心 API：会话管理器与会话数据类
__all__ = ["SessionManager", "Session"]
