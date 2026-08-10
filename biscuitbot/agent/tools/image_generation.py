"""图像生成工具。

所属模块与项目作用
==================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的图像生成组件。
在项目架构中起到的作用：将配置的图像生成提供商（OpenAI、DashScope 等）
封装为统一的 ``Tool``，让 agent 能够通过工具调用生成或编辑图片，并将
结果作为持久化 artifact 存储，返回 artifact id 与本地路径供后续引用。
"""

from __future__ import annotations

from pathlib import Path  # 路径处理，用于工作区与 artifact 路径
from typing import TYPE_CHECKING, Any  # 类型检查与任意类型

from pydantic import Field  # Pydantic 字段校验

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.schema import (  # schema 构造器
    ArraySchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from biscuitbot.config.paths import get_media_dir  # 媒体目录获取函数
from biscuitbot.config_base import Base  # 配置基类
from biscuitbot.providers.image_generation import (  # 图像生成提供商抽象
    ImageGenerationError,
    ImageGenerationProvider,
    extract_domain,
    get_image_gen_provider,
)
from biscuitbot.security.workspace_access import current_tool_workspace  # 当前工具工作区访问
from biscuitbot.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path  # 工作区路径安全策略
from biscuitbot.utils.artifacts import (  # artifact 存储工具
    ArtifactError,
    generated_image_tool_result,
    store_generated_image_artifact,
)
from biscuitbot.utils.helpers import detect_image_mime  # 图片 MIME 类型探测

if TYPE_CHECKING:  # 仅类型检查时导入，避免运行时循环依赖
    from biscuitbot.config.schema import ProviderConfig


class ImageGenerationToolConfig(Base):
    """图像生成工具配置。

    职责：承载图像生成工具的运行时配置项，包括启停、提供商、模型、
    默认宽高比/尺寸、每轮最大生成数与 artifact 保存目录。
    """
    enabled: bool = False  # 是否启用图像生成工具
    provider: str = "openai"  # 图像生成提供商名称
    model: str = "openai/gpt-5.4-image-2"  # 默认使用的图像生成模型
    default_aspect_ratio: str = "1:1"  # 默认输出宽高比
    default_image_size: str = "1K"  # 默认输出尺寸提示
    max_images_per_turn: int = Field(default=4, ge=1, le=8)  # 每轮最大生成图片数（1-8）
    save_dir: str = "generated"  # artifact 保存子目录名


@tool_parameters(
    tool_parameters_schema(
        prompt=StringSchema(
            "Detailed image generation or edit prompt. Include style, subject, composition, colors, and constraints.",
            min_length=1,
        ),
        reference_images=ArraySchema(
            StringSchema("Local path of an existing image artifact or user-provided image to use as an edit reference."),
            description="Optional local image paths. Use generated artifact paths for iterative edits.",
        ),
        aspect_ratio=StringSchema(
            "Optional output aspect ratio, e.g. 1:1, 16:9, 9:16, 4:3.",
        ),
        image_size=StringSchema(
            "Optional output size hint supported by the configured provider, e.g. 1K, 2K, 4K, or 1024x1024.",
        ),
        count=IntegerSchema(
            description="Number of images to generate in this turn.",
            minimum=1,
            maximum=8,
        ),
        required=["prompt"],
    )
)
class ImageGenerationTool(Tool):
    """通过配置的图像生成提供商生成持久化图片 artifact。

    职责：将图像生成/编辑能力封装为 agent 可调用的工具。调用时传入
    prompt（必填）与可选的参考图片、宽高比、尺寸、数量，生成结果会作为
    artifact 持久化存储，并返回 artifact id 与本地路径。
    """

    _capability = (
        "Generate or edit images and persist them as artifacts (returns paths)."
    )
    _usage_md = "docs/generate_image.md"  # 工具使用说明文档路径

    config_key = "image_generation"  # 配置键名

    @classmethod
    def config_cls(cls):
        """返回该工具使用的配置类。"""
        return ImageGenerationToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """仅当配置中 image_generation.enabled 为 True 时启用。"""
        return ctx.config.image_generation.enabled

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        return cls(
            workspace=ctx.workspace,
            config=ctx.config.image_generation,
            provider_configs=ctx.image_generation_provider_configs,
        )

    def __init__(
        self,
        *,
        workspace: str | Path,
        config: ImageGenerationToolConfig,
        provider_config: ProviderConfig | None = None,
        provider_configs: dict[str, ProviderConfig] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser()  # 工作区路径，展开 ~
        self.config = config  # 工具配置
        self.provider_configs = dict(provider_configs or {})  # 提供商配置字典
        # 兼容旧的单 provider_config 参数：若未在 provider_configs 中则补入
        if provider_config is not None and config.provider not in self.provider_configs:
            self.provider_configs[config.provider] = provider_config

    @property
    def name(self) -> str:
        """工具名称。"""
        return "generate_image"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何调用。"""
        return (
            "Generate or edit images and store them as persistent artifacts. "
            "Returns artifact ids and local paths. For edits, pass prior generated image paths "
            "or user image paths as reference_images."
        )

    def _provider_config(self) -> ProviderConfig | None:
        """返回当前配置提供商的 ProviderConfig。"""
        return self.provider_configs.get(self.config.provider)

    def _provider_client(self) -> ImageGenerationProvider | None:
        """构造图像生成提供商客户端实例。

        返回 None 表示当前提供商不受支持。
        """
        provider = self._provider_config()
        cls = get_image_gen_provider(self.config.provider)
        if cls is None:
            return None
        # api_base 解析优先级：LLM 提供商的 domain > 提供商默认值。
        # 仅继承 LLM 提供商的域名（而非完整路径），因为图像/转录 API
        # 可能使用与 LLM API 不同的路径（例如 DashScope LLM 使用
        # /compatible-mode/v1，而图像使用 /api/v1/...）。
        # 每个图像生成客户端会自行拼接服务特定路径。
        api_base = None
        if provider and provider.api_base:
            api_base = extract_domain(provider.api_base)
        kwargs = {
            "api_key": provider.api_key if provider else None,
            "api_base": api_base,
            "extra_headers": provider.extra_headers if provider else None,
            "extra_body": provider.extra_body if provider else None,
        }
        return cls(**kwargs)

    def _resolve_reference_image(self, value: str) -> str:
        """解析单个参考图片路径并校验其安全性。

        参数:
            value: 用户提供的图片路径字符串。

        返回:
            解析后的绝对路径字符串。

        抛出:
            ImageGenerationError: 路径越界、文件不存在或不支持该图片格式时抛出。
        """
        access = current_tool_workspace(self.workspace, restrict_to_workspace=True)
        workspace = access.project_path or self.workspace
        try:
            resolved = resolve_allowed_path(
                value,
                workspace=workspace,
                allowed_root=access.allowed_root,
                extra_allowed_roots=[get_media_dir()] if access.allowed_root is not None else None,
                strict=True,
            )
        except WorkspaceBoundaryError as exc:
            # 参考图片必须在工作区或 biscuitbot 媒体目录内
            raise ImageGenerationError(
                "reference_images must be inside the workspace or biscuitbot media directory"
            ) from exc
        except OSError as exc:
            raise ImageGenerationError(f"reference image not found: {value}") from exc
        if not resolved.is_file():
            raise ImageGenerationError(f"reference image is not a file: {value}")
        raw = resolved.read_bytes()
        # 校验图片 MIME 类型是否受支持
        if detect_image_mime(raw) is None:
            raise ImageGenerationError(f"unsupported reference image: {value}")
        return str(resolved)

    def _resolve_reference_images(self, values: list[str] | None) -> list[str]:
        """解析参考图片路径列表，过滤空值。

        参数:
            values: 用户提供的图片路径列表，可为空。

        返回:
            解析后的绝对路径列表。
        """
        if not values:
            return []
        return [self._resolve_reference_image(value) for value in values if value]

    async def execute(
        self,
        prompt: str,
        reference_images: list[str] | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        count: int | None = None,
        **kwargs: Any,
    ) -> str:
        """执行图像生成或编辑。

        参数:
            prompt: 图像生成/编辑的详细描述（必填）。
            reference_images: 可选的参考图片本地路径列表，用于编辑场景。
            aspect_ratio: 可选的输出宽高比（如 1:1、16:9）。
            image_size: 可选的输出尺寸提示（如 1K、2K、1024x1024）。
            count: 本轮要生成的图片数量。

        返回:
            包含 artifact id 与本地路径的结果字符串；出错时返回错误信息。
        """
        client = self._provider_client()
        if client is None:
            return f"Error: unsupported image generation provider '{self.config.provider}'"

        requested = count or 1  # 默认生成 1 张
        # 校验请求数量不超过每轮上限
        if requested > self.config.max_images_per_turn:
            return (
                "Error: count exceeds tools.imageGeneration.maxImagesPerTurn "
                f"({self.config.max_images_per_turn})"
            )

        try:
            refs = self._resolve_reference_images(reference_images)
            artifacts: list[dict[str, Any]] = []
            # 循环调用直到收集到所需数量的图片
            while len(artifacts) < requested:
                response = await client.generate(
                    prompt=prompt,
                    model=self.config.model,
                    reference_images=refs,
                    aspect_ratio=aspect_ratio or self.config.default_aspect_ratio,
                    image_size=image_size or self.config.default_image_size,
                )
                for image_data_url in response.images:
                    # 将每张图片持久化为 artifact
                    artifact = store_generated_image_artifact(
                        image_data_url,
                        prompt=prompt,
                        model=self.config.model,
                        source_images=refs,
                        save_dir=self.config.save_dir,
                        provider=self.config.provider,
                    )
                    artifacts.append(artifact)
                    if len(artifacts) >= requested:
                        break
            return generated_image_tool_result(artifacts)
        except (ArtifactError, ImageGenerationError, OSError) as exc:
            return f"Error: {exc}"
