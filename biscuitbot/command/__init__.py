"""斜杠命令路由模块。

所属模块与项目作用
===================
本文件位于 biscuitbot/command 目录，是斜杠命令（slash command）模块的入口。
在项目架构中起到的作用：
- 汇总并暴露命令路由器（CommandRouter）、命令上下文（CommandContext）
  与内置命令注册函数（register_builtin_commands）；
- 作为包初始化文件，统一管理命令模块的对外导入接口。
"""

from biscuitbot.command.builtin import register_builtin_commands  # 内置命令注册函数
from biscuitbot.command.router import CommandContext, CommandRouter  # 命令上下文与路由器

__all__ = ["CommandContext", "CommandRouter", "register_builtin_commands"]
