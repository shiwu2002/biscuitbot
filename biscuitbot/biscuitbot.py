"""biscuitbot 的高层编程接口。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from biscuitbot.agent.hook import AgentHook, SDKCaptureHook
from biscuitbot.agent.loop import AgentLoop
from biscuitbot.providers.image_generation import image_gen_provider_configs


@dataclass(slots=True)
class RunResult:
    """单次 agent 运行的结果。"""

    content: str
    tools_used: list[str]
    messages: list[dict[str, Any]]


class Biscuitbot:
    """运行 biscuitbot agent 的编程门面（facade）。

    用法::

        bot = Biscuitbot.from_config()
        result = await bot.run("总结这个仓库", hooks=[MyHook()])
        print(result.content)
    """

    def __init__(self, loop: AgentLoop) -> None:
        self._loop = loop

    @classmethod
    def from_config(
        cls,
        config_path: str | Path | None = None,
        *,
        workspace: str | Path | None = None,
    ) -> Biscuitbot:
        """从配置文件创建 Biscuitbot 实例。

        Args:
            config_path: ``config.json`` 的路径。默认为
                ``~/.biscuitbot/config.json``。
            workspace: 覆盖配置中的工作区目录。
        """
        from biscuitbot.config.loader import load_config, resolve_config_env_vars
        from biscuitbot.config.schema import Config

        resolved: Path | None = None
        if config_path is not None:
            resolved = Path(config_path).expanduser().resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"Config not found: {resolved}")

        config: Config = resolve_config_env_vars(load_config(resolved))
        if workspace is not None:
            config.agents.defaults.workspace = str(
                Path(workspace).expanduser().resolve()
            )

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

        Args:
            message: 要处理的用户消息。
            session_key: 用于会话隔离的会话标识。
                不同 key 拥有独立的历史记录。
            hooks: 本次运行的可选生命周期钩子。
        """
        capture = SDKCaptureHook()
        prev = self._loop._extra_hooks
        base_hooks = list(hooks) if hooks is not None else list(prev or [])
        self._loop._extra_hooks = [capture, *base_hooks]
        try:
            response = await self._loop.process_direct(
                message, session_key=session_key,
            )
        finally:
            self._loop._extra_hooks = prev

        content = (response.content if response else None) or ""
        return RunResult(
            content=content,
            tools_used=capture.tools_used,
            messages=capture.messages,
        )

    async def aclose(self) -> None:
        """释放该实例持有的资源（MCP 连接等）。"""
        await self._loop.close_mcp()

    async def __aenter__(self) -> Biscuitbot:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
