"""受控的已安装 CLI 应用运行器。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的 CLI 应用执行组件。
在项目架构中起到的作用：提供 ``run_cli_app`` 工具，让 agent 能够以受控的
argv 子进程方式运行用户在 Settings 中显式安装的 CLI 应用（如 gimp、
pandoc 等），而非任意系统命令，从而保证执行安全可控。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field
from loguru import logger  # 结构化日志（加载 CLI 应用列表失败告警）

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.schema import ArraySchema, BooleanSchema, IntegerSchema, StringSchema, tool_parameters_schema  # Schema 构造器
from biscuitbot.security.workspace_access import current_tool_workspace  # 工作区访问控制
from biscuitbot.apps.cli import CliAppError, CliAppManager, CliAppsRuntimeConfig  # CLI 应用管理
from biscuitbot.config_base import Base  # 配置基类


class CliAppsToolConfig(Base):
    """CLI Apps 工具配置。

    用于控制 CLI 应用的安装与运行超时、目录缓存有效期等运行时参数。
    """

    enable: bool = True  # 是否启用 CLI Apps 工具
    install_timeout: int = Field(default=300, ge=1, le=3600)  # 安装超时（秒）
    run_timeout: int = Field(default=60, ge=1, le=600)  # 运行超时（秒）
    catalog_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)  # 目录缓存有效期（秒）


@tool_parameters(
    tool_parameters_schema(
        required=["name"],
        name=StringSchema("Installed CLI app registry name, for example gimp, safari, or obsidian."),
        args=ArraySchema(
            StringSchema("One command-line argument."),
            description="Arguments to pass to the CLI entry point. Do not include the entry point itself.",
            nullable=True,
        ),
        json=BooleanSchema(
            description="Whether to prepend --json when supported by the CLI.",
            default=False,
            nullable=True,
        ),
        working_dir=StringSchema("Optional working directory for the CLI call.", nullable=True),
        timeout=IntegerSchema(
            description="Timeout in seconds for this CLI call.",
            minimum=1,
            maximum=600,
            nullable=True,
        ),
    )
)
class CliAppsTool(Tool):
    """通过受控 argv 子进程运行已安装的 CLI-Anything 或公共 CLI 应用。

    职责：仅允许运行用户在 Settings 中显式安装或以 @app 附加的 CLI 应用，
    使用 argv 而非 shell 执行，避免任意命令注入风险。

    用法：由 agent 调用，传入 CLI 应用名、参数及可选的工作目录与超时。
    """

    _capability = (
        "Run installed CLI apps (e.g. youtube-dl, pandoc) via controlled argv subprocess."
    )
    _usage_md = "docs/run_cli_app.md"  # 工具使用说明文档路径

    config_key = "cli_apps"  # 配置键名
    _scopes = {"core", "subagent"}  # 工具可用作用域

    @classmethod
    def config_cls(cls):
        """返回该工具对应的配置类。"""
        return CliAppsToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """根据配置判断工具是否启用。"""
        return ctx.config.cli_apps.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """工厂方法：依据上下文创建工具实例并注入运行时配置。"""
        cfg = ctx.config.cli_apps
        return cls(
            workspace=Path(ctx.workspace),
            restrict_to_workspace=ctx.config.restrict_to_workspace,
            runtime=CliAppsRuntimeConfig(
                install_timeout=cfg.install_timeout,
                run_timeout=cfg.run_timeout,
                catalog_ttl_seconds=cfg.catalog_ttl_seconds,
            ),
        )

    def __init__(
        self,
        *,
        workspace: Path,
        restrict_to_workspace: bool = False,
        runtime: CliAppsRuntimeConfig | None = None,
    ) -> None:
        """初始化 CLI 应用工具。

        参数:
            workspace: 工作区路径。
            restrict_to_workspace: 是否限制在工作区内执行。
            runtime: CLI 应用运行时配置，为空时使用默认配置。
        """
        self.workspace = workspace
        self.restrict_to_workspace = restrict_to_workspace
        self.runtime = runtime or CliAppsRuntimeConfig()

    @property
    def name(self) -> str:
        """工具名称。"""
        return "run_cli_app"

    @property
    def description(self) -> str:
        """工具描述，动态附带当前已安装的 CLI 应用列表。"""
        try:
            installed = CliAppManager(workspace=self.workspace, runtime=self.runtime).installed_names()
        except Exception:
            logger.exception("加载已安装 CLI 应用列表失败")
            installed = []
        installed_note = (
            f" Installed Settings CLI Apps: {', '.join(installed)}."
            if installed
            else " No Settings CLI Apps are currently installed."
        )
        return (
            "Run a CLI App that the user explicitly installed in Settings or attached as @app. "
            "Do not use this for ordinary system CLIs such as git, gh, python, npm, or brew; "
            "unknown names are rejected. Execution uses argv, not shell."
            + installed_note
        )

    async def execute(
        self,
        name: str,
        args: list[str] | None = None,
        json: bool | None = False,
        working_dir: str | None = None,
        timeout: int | None = None,
    ) -> str:
        """执行指定的 CLI 应用。

        参数:
            name: 已安装的 CLI 应用注册名。
            args: 传给 CLI 入口的参数列表（不含入口本身）。
            json: 是否在支持时前置 --json 参数。
            working_dir: 可选的工作目录。
            timeout: 本次调用的超时时间（秒）。

        返回:
            CLI 执行输出字符串；失败时返回错误信息。
        """
        # 获取当前工具的工作区访问上下文（处理工作区限制）
        access = current_tool_workspace(
            self.workspace,
            restrict_to_workspace=self.restrict_to_workspace,
        )
        workspace = access.project_path or self.workspace
        manager = CliAppManager(workspace=workspace, runtime=self.runtime)
        try:
            return manager.run(
                name,
                args=args or [],
                json_output=bool(json),
                working_dir=working_dir,
                timeout=timeout,
                restrict_to_workspace=access.restrict_to_workspace,
            )
        except CliAppError as exc:
            return f"Error: CLI 应用执行失败：{exc.message}。请确认该应用已在「设置 → CLI 应用」中安装并可用"
