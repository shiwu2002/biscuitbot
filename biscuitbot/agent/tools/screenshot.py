"""截图工具：捕获屏幕并使用视觉模型理解其内容。

所属模块与项目作用
==================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的截图分析组件。
在项目架构中起到的作用：让 agent 能够截取当前屏幕，将截图发送给配置的
视觉模型（vision model），并根据用户的问题返回对屏幕内容的描述。
适用于理解桌面状态、应用程序界面或屏幕上任何可视内容的场景。
"""

from __future__ import annotations

import base64  # base64 编解码，用于图片数据编码
import io  # 字节流 IO，用于图片内存读写
import os  # 操作系统接口，用于临时文件清理与环境变量
import platform  # 平台识别，用于选择截图方式
import subprocess  # 子进程调用，用于执行平台原生截图命令
import tempfile  # 临时文件，用于存储截图
from pathlib import Path  # 路径处理
from typing import TYPE_CHECKING, Any  # 类型检查与任意类型

from loguru import logger  # 日志库

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.schema import (  # schema 构造器
    StringSchema,
    tool_parameters_schema,
)
from biscuitbot.config_base import Base  # 配置基类

if TYPE_CHECKING:  # 仅类型检查时导入，避免运行时循环依赖
    from biscuitbot.providers.base import LLMProvider


class ScreenshotToolConfig(Base):
    """截图工具配置。

    职责：承载截图工具的运行时配置项，包括启停、最大宽高（超过则缩放）
    与 JPEG 压缩质量。
    """

    enable: bool = False  # 是否启用截图工具
    max_width: int = 1920  # 截图最大宽度，超过则等比缩小
    max_height: int = 1080  # 截图最大高度，超过则等比缩小
    quality: int = 85  # JPEG 压缩质量（1-100）


@tool_parameters(
    tool_parameters_schema(
        question=StringSchema(
            "What you want to know about the current screen state. "
            "The vision model will answer this question based on the screenshot.",
            min_length=1,
        ),
        required=["question"],
    )
)
class ScreenshotTool(Tool):
    """截取屏幕并使用配置的视觉模型理解其内容。

    职责：捕获当前屏幕截图，将其与用户的问题一起发送给视觉模型，返回
    模型对屏幕内容的描述。需要在 agent 配置中设置 ``visionModel``
    （如 ``qwen-vl``、``gpt-4o``）。
    """

    _capability = (
        "Capture a screenshot and analyze it with the configured vision model."
    )
    _usage_md = "docs/screenshot.md"  # 工具使用说明文档路径

    config_key = "screenshot"  # 配置键名
    _plugin_discoverable = False  # 需要 vision_provider_loader，手动注册而非自动发现

    @classmethod
    def config_cls(cls):
        """返回该工具使用的配置类。"""
        return ScreenshotToolConfig

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """仅当配置中 screenshot.enable 为 True 时启用。"""
        return ctx.config.screenshot.enable

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        return cls(
            vision_provider_loader=ctx.vision_provider_loader,
            config=ctx.config.screenshot,
        )

    def __init__(
        self,
        *,
        vision_provider_loader: Any = None,
        config: ScreenshotToolConfig | None = None,
    ) -> None:
        self._vision_provider_loader = vision_provider_loader  # 视觉模型提供商加载器
        self.config = config or ScreenshotToolConfig()  # 工具配置

    @property
    def name(self) -> str:
        """工具名称。"""
        return "screenshot"

    @property
    def description(self) -> str:
        """工具描述，指导模型何时调用。"""
        return (
            "Capture a screenshot of the current screen and use the vision model to "
            "answer a question about what's visible. Useful for understanding the "
            "current state of the desktop, applications, or any visual content on screen."
        )

    @property
    def read_only(self) -> bool:
        """截图工具为只读（不修改系统状态）。"""
        return True

    async def execute(self, **kwargs: Any) -> str:
        """执行截图并调用视觉模型分析。

        参数:
            **kwargs: 包含 question（必填）——关于屏幕内容的问题。

        返回:
            视觉模型对截图的描述；出错时返回错误信息。
        """
        question = kwargs.get("question", "").strip()
        if not question:
            return "Error: question parameter is required."

        # 1. 获取视觉模型提供商
        provider = self._get_vision_provider()
        if provider is None:
            return (
                "Error: vision model is not configured. "
                "Set `visionModel` in the agent config to a vision-capable model preset "
                "(e.g. \"qwen-vl\", \"gpt-4o\")."
            )

        # 2. 捕获屏幕截图
        screenshot_path = await self._capture_screenshot()
        if screenshot_path is None:
            return self._format_capture_error()

        try:
            # 3. 读取图片并按需缩放
            image_data, mime = self._read_and_resize(screenshot_path)

            # 4. 调用视觉模型分析
            result = await self._ask_vision_model(provider, image_data, mime, question)
            return result
        finally:
            # 清理临时截图文件
            try:
                os.unlink(screenshot_path)
            except OSError:
                pass

    def _get_vision_provider(self) -> LLMProvider | None:
        """懒加载视觉模型提供商。

        返回 None 表示未配置或加载失败。
        """
        if self._vision_provider_loader is None:
            return None
        try:
            return self._vision_provider_loader()
        except Exception:
            logger.exception("Failed to load vision provider")
            return None

    @staticmethod
    def _is_headless_environment() -> bool:
        """检测当前环境是否无图形显示（headless）。

        返回:
            无图形显示环境返回 True，否则 False。
        """
        system = platform.system()
        if system == "Linux":
            # Linux 通过 DISPLAY 环境变量判断是否有 X11 显示
            return not os.environ.get("DISPLAY")
        if system == "Darwin":
            # macOS 使用 WindowServer（非 X11）作为 GUI。Mac 仅在通过 SSH
            # 访问且无已登录 GUI 会话时才可能是 headless。检查 X11 是错误
            # 的——macOS 默认不附带 X11，该检查会对每台桌面 Mac 误报。
            return bool(os.environ.get("SSH_CONNECTION"))
        return False

    def _format_capture_error(self) -> str:
        """截图失败时构造描述性错误信息。

        返回:
            包含失败原因与平台信息的错误字符串。
        """
        if self._is_headless_environment():
            return (
                "Error: screenshot capture failed — this appears to be a headless "
                f"environment ({platform.system()}) with no graphical display. "
                "The screenshot tool cannot capture a physical screen here. "
                "If you need to inspect a web page, use a browser-automation tool "
                "(e.g. Playwright) or fetch the page content directly instead."
            )
        return (
            "Error: failed to capture screenshot. "
            "The platform's native screenshot utility may be missing, misconfigured, "
            "or lacking permissions (e.g. macOS Screen Recording permission). "
            f"Platform: {platform.system()}."
        )

    async def _capture_screenshot(self) -> str | None:
        """捕获屏幕截图并返回临时文件路径。

        返回:
            截图临时文件路径；失败返回 None。
        """
        system = platform.system()
        # 创建临时 PNG 文件
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp.name
        tmp.close()

        try:
            if system == "Darwin":
                # macOS：使用 screencapture 命令
                result = subprocess.run(
                    ["screencapture", "-x", "-t", "png", tmp_path],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    logger.warning("screencapture failed: {}", result.stderr.decode())
                    return None
            elif system == "Linux":
                # Linux：依次尝试 gnome-screenshot、scrot、import（ImageMagick）
                for cmd in [
                    ["gnome-screenshot", "-f", tmp_path],
                    ["scrot", tmp_path],
                    ["import", "-window", "root", tmp_path],
                ]:
                    try:
                        result = subprocess.run(
                            cmd, capture_output=True, timeout=10,
                        )
                        if result.returncode == 0:
                            break
                    except FileNotFoundError:
                        continue
                else:
                    logger.warning("No supported screenshot tool found on Linux")
                    return None
            elif system == "Windows":
                # Windows：使用 PowerShell 调用 .NET 截图
                ps_script = (
                    'Add-Type -AssemblyName System.Windows.Forms;'
                    '[System.Windows.Forms.Screen]::PrimaryScreen | ForEach-Object {'
                    '$bmp = New-Object System.Drawing.Bitmap($_.Bounds.Width, $_.Bounds.Height);'
                    '$g = [System.Drawing.Graphics]::FromImage($bmp);'
                    '$g.CopyFromScreen($_.Bounds.Location, [System.Drawing.Point]::Empty, $_.Bounds.Size);'
                    f"$bmp.Save('{tmp_path}');"
                    '$g.Dispose(); $bmp.Dispose()'
                    '}'
                )
                result = subprocess.run(
                    ["powershell", "-Command", ps_script],
                    capture_output=True,
                    timeout=10,
                )
                if result.returncode != 0:
                    logger.warning("PowerShell screenshot failed: {}", result.stderr.decode())
                    return None
            else:
                logger.warning("Unsupported platform for screenshot: {}", system)
                return None

            # 校验截图文件存在且非空
            if not Path(tmp_path).exists() or Path(tmp_path).stat().st_size == 0:
                return None
            return tmp_path
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.warning("Screenshot capture failed: {}", exc)
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return None

    def _read_and_resize(self, path: str) -> tuple[bytes, str]:
        """读取图片文件并按需缩小尺寸。

        参数:
            path: 图片文件路径。

        返回:
            (图片字节数据, MIME 类型) 元组。超限时缩放为 JPEG，
            否则也尽量转为 JPEG 以减小体积；PIL 不可用时返回原始 PNG。
        """
        raw = Path(path).read_bytes()

        # 若 PIL 可用且图片超过尺寸限制，则尝试缩放
        try:
            from PIL import Image

            img = Image.open(io.BytesIO(raw))
            needs_resize = (
                img.width > self.config.max_width
                or img.height > self.config.max_height
            )
            if needs_resize:
                # 等比缩放到最大尺寸内
                img.thumbnail(
                    (self.config.max_width, self.config.max_height),
                    Image.LANCZOS,
                )
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.config.quality)
                return buf.getvalue(), "image/jpeg"
            # 即使无需缩放也转为 JPEG 以减小体积
            if img.format != "JPEG":
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=self.config.quality)
                return buf.getvalue(), "image/jpeg"
        except ImportError:
            logger.debug("PIL not available; using raw screenshot")
        except Exception:
            logger.debug("Image resize failed; using raw screenshot")

        # PIL 不可用或处理失败时返回原始 PNG 数据
        return raw, "image/png"

    async def _ask_vision_model(
        self,
        provider: LLMProvider,
        image_data: bytes,
        mime: str,
        question: str,
    ) -> str:
        """将截图与问题发送给视觉模型。

        参数:
            provider: 视觉模型提供商实例。
            image_data: 图片字节数据。
            mime: 图片 MIME 类型。
            question: 关于截图内容的问题。

        返回:
            视觉模型的描述文本；出错时返回错误信息。
        """
        # 将图片编码为 base64 data URL
        b64 = base64.b64encode(image_data).decode()
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                    {
                        "type": "text",
                        "text": question,
                    },
                ],
            }
        ]
        try:
            response = await provider.chat_with_retry(
                messages=messages,
                tools=None,
                tool_choice=None,
            )
            if response.content:
                return response.content
            return "Vision model returned an empty response."
        except Exception as exc:
            logger.exception("Vision model call failed")
            return f"Error: vision model call failed: {exc}"
