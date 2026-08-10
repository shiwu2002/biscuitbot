"""聊天渠道模块（基于插件架构）。

所属模块与项目作用
===================
本文件位于 biscuitbot/channels 目录，是 Channel（聊天平台接入）层的包初始化组件。
在项目架构中起到的作用：统一导出渠道层的核心抽象基类 ``BaseChannel`` 与渠道管理器
``ChannelManager``，使上层（主进程、CLI、配置加载器等）可通过一行导入直接获取，
而无需感知各具体平台实现。各平台渠道以子模块形式存在，由 ``ChannelManager`` 负责发现、
加载与生命周期管理。
"""

from biscuitbot.channels.base import BaseChannel  # 所有渠道实现的抽象基类
from biscuitbot.channels.manager import ChannelManager  # 渠道生命周期与并发管理器

__all__ = ["BaseChannel", "ChannelManager"]  # 公开导出符号，控制 ``import *`` 的范围
