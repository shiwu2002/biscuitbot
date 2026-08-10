"""Agent 核心模块。

所属模块与项目作用
===================
本文件位于 ``biscuitbot/agent`` 目录，是 Agent 模块的包入口（``__init__.py``）。
在项目架构中起到的作用：
- 作为 Agent 子包的对外聚合点，统一暴露 Agent 运行所需的核心组件；
- 通过 ``__all__`` 显式声明公开 API，方便上层（CLI、SDK、调度器等）以
  ``from biscuitbot.agent import ...`` 的形式一次性导入相关组件，而无需
  关心各组件在子模块中的具体位置。

聚合的组件涵盖：上下文构建（ContextBuilder）、生命周期钩子（AgentHook 系列）、
主循环（AgentLoop）、记忆存储（MemoryStore）、技能加载（SkillsLoader）以及
子代理管理（SubagentManager）。
"""

from biscuitbot.agent.context import ContextBuilder  # 上下文构建器，组装系统提示与对话消息
from biscuitbot.agent.hook import AgentHook, AgentHookContext, AgentRunHookContext, CompositeHook  # 生命周期钩子及其上下文/组合实现
from biscuitbot.agent.loop import AgentLoop  # Agent 主循环，驱动迭代式 LLM 调用与工具执行
from biscuitbot.agent.memory import MemoryStore  # 记忆存储，负责长期记忆与历史读写
from biscuitbot.agent.skills import SkillsLoader  # 技能加载器，加载并管理可用技能
from biscuitbot.agent.subagent import SubagentManager  # 子代理管理器，协调子代理的派发与回收

__all__ = [
    "AgentHook",
    "AgentHookContext",
    "AgentRunHookContext",
    "AgentLoop",
    "CompositeHook",
    "ContextBuilder",
    "MemoryStore",
    "SkillsLoader",
    "SubagentManager",
]
