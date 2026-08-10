"""biscuitbot 的高层编程接口（SDK Facade 层）。

所属模块与项目作用
===================
本文件位于 ``biscuitbot`` 包根目录，是整个项目的 **SDK 门面层（Facade）**，
介于 CLI/Channel 等产品层与 AgentLoop 核心引擎之间。

在项目架构中起到的作用：
1. **编程式入口** —— 让第三方程序无需启动 Channel（Telegram/Feishu 等）
   即可直接调用 agent 能力，常用于脚本自动化、CI 集成、Web 后端嵌入。
2. **配置驱动装配** —— 通过 ``from_config()`` 一行代码完成
   Config → Provider → AgentLoop 的完整依赖装配，对调用方屏蔽内部细节。
3. **生命周期管理** —— 提供 ``async with`` 上下文协议，确保 MCP 连接、
   Provider 资源等在退出时正确释放。
4. **结果捕获** —— 通过 ``SDKCaptureHook`` 钩子收集本次运行的
   工具调用与完整消息流，返回结构化 ``RunResult``。

典型用法::

    async with Biscuitbot.from_config() as bot:
        result = await bot.run("总结这个仓库", hooks=[MyHook()])
        print(result.content)
        print(result.tools_used)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# AgentHook：生命周期钩子基类，调用方可注入自定义回调
# SDKCaptureHook：SDK 专用钩子，自动捕获工具调用与消息流供 RunResult 返回
from biscuitbot.agent.hook import AgentHook, SDKCaptureHook
# AgentLoop：项目核心状态机引擎，Biscuitbot 是它的薄封装
from biscuitbot.agent.loop import AgentLoop
# image_gen_provider_configs：从配置解析图像生成 provider 映射，传给 AgentLoop
from biscuitbot.providers.image_generation import image_gen_provider_configs


@dataclass(slots=True)
class RunResult:
    """单次 agent 运行的结果。

    由 ``Biscuitbot.run()`` 返回，封装了 LLM 最终回复文本、本次调用的
    工具名列表、以及完整的消息流（含工具调用与结果），供调用方进一步分析。
    """

    # LLM 的最终文本回复；若 agent 仅调用工具无文本输出则为空字符串
    content: str
    # 本次运行按顺序调用的工具名列表，如 ["read_file", "exec"]
    tools_used: list[str]
    # 完整的消息流副本（role/content/tool_calls 等），用于离线分析或回放
    messages: list[dict[str, Any]]


class Biscuitbot:
    """运行 biscuitbot agent 的编程门面（facade）。

    本类是面向 SDK 使用者的唯一入口，内部持有 ``AgentLoop`` 实例。
    通过 ``from_config()`` 工厂方法完成所有依赖装配，调用方无需
    直接接触 Provider / SessionManager / ToolRegistry 等内部组件。

    用法::

        bot = Biscuitbot.from_config()
        result = await bot.run("总结这个仓库", hooks=[MyHook()])
        print(result.content)
    """

    def __init__(self, loop: AgentLoop) -> None:
        # 持有核心状态机引擎；所有 run() 调用最终都委托给 loop.process_direct
        self._loop = loop

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
    ) -> Biscuitbot:
        """从配置文件创建 Biscuitbot 实例。

        完成三步装配：
        1. 加载并解析 ``config.json``（支持环境变量占位符替换）
        2. 可选地用 *workspace* 参数覆盖配置中的工作区目录
        3. 调用 ``AgentLoop.from_config()`` 构建 Provider、SessionManager、
           ToolRegistry 等全部依赖，返回就绪的 Biscuitbot 实例

        Args:
            config_path: ``config.json`` 的路径。默认为
                ``~/.biscuitbot/config.json``。
            workspace: 覆盖配置中的工作区目录。

        Raises:
            FileNotFoundError: *config_path* 显式指定但文件不存在。
        """
        # 延迟导入避免循环依赖，且让 SDK 在仅 import 本类时不必加载配置模块
        from biscuitbot.config.loader import load_config, resolve_config_env_vars
        from biscuitbot.config.schema import Config

        # 解析配置路径：展开 ~ 符号并转为绝对路径
        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Config not found: {resolved}")

        # 加载配置并替换 ${ENV_VAR} 形式的环境变量占位符
        config: Config = resolve_config_env_vars(load_config(resolved))
        # 允许调用方临时覆盖工作区（不修改磁盘配置文件）
        if workspace is not None:
            config.agents.defaults.workspace = str(
                Path(workspace).expanduser().resolve()
            )

        # 构建 AgentLoop：一次性装配 Provider/Session/Tools 等全部依赖
        # image_generation_provider_configs 解析图像生成相关 provider 映射
        loop = AgentLoop.from_config(
            config,
            image_generation_provider_configs=image_gen_provider_configs(config),
        )
        return cls(loop)

    async def run(
        self,
        message: str,
        *,
        session_key: str = "sdk:default",
        hooks: list[AgentHook] | None = None,
    ) -> RunResult:
        """运行一次 agent 并返回结果。

        本方法是非 Channel 场景下的调用入口：直接向 AgentLoop 投递一条
        用户消息，等待完整 turn 跑完（RESTORE→...→DONE），返回结构化结果。

        内部通过 ``SDKCaptureHook`` 钩子捕获工具调用与消息流，因此即使
        AgentLoop 本身不返回这些信息，调用方也能拿到完整轨迹。

        Args:
            message: 要处理的用户消息。
            session_key: 用于会话隔离的会话标识。
                不同 key 拥有独立的历史记录。
            hooks: 本次运行的可选生命周期钩子。

        Returns:
            包含 content / tools_used / messages 的 RunResult。
        """
        # 创建捕获钩子，它会自动收集本次 turn 的工具调用与消息流
        capture = SDKCaptureHook()
        # 备份 loop 上既有的额外钩子，run 结束后恢复（防止并发污染）
        prev = self._loop._extra_hooks
        # 合并调用方传入的 hooks 与既有 hooks，capture 始终置首以确保捕获
        base_hooks = list(hooks) if hooks is not None else list(prev or [])
        self._loop._extra_hooks = [capture, *base_hooks]
        try:
            # 委托 AgentLoop 处理消息；process_direct 绕过 MessageBus 直接调用
            response = await self._loop.process_direct(
                message, session_key=session_key,
            )
        finally:
            # 无论成功失败都恢复原有钩子配置
            self._loop._extra_hooks = prev

        # 提取最终文本，None/空都归一为空字符串
        content = (response.content if response else None) or ""
        return RunResult(
            content=content,
            tools_used=capture.tools_used,
            messages=capture.messages,
        )

    async def aclose(self) -> None:
        """释放该实例持有的资源（MCP 连接等）。

        显式调用或通过 ``async with`` 退出时触发，确保 MCP 子进程、
        Provider HTTP 连接等底层资源被正确清理，避免资源泄漏。
        """
        await self._loop.close_mcp()

    async def __aenter__(self) -> Biscuitbot:
        """进入 async with 上下文，返回自身供 as 绑定。"""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """退出 async with 上下文，自动调用 aclose 释放资源。

        *exc* 接收异常信息但忽略——无论是否抛异常都执行清理。
        """
        await self.aclose()
