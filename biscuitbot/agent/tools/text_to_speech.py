"""文本转语音（TTS）工具。

所属模块与项目作用
===================
本文件位于 biscuitbot/agent/tools 目录，是工具系统的语音合成组件。
在项目架构中起到的作用：把文本转语音能力封装为统一的 ``Tool``，让 agent
通过一次函数调用即可把文案合成为本地音频文件（mp3），返回文件路径供后续
交付或剪辑引用。支持 openai / dashscope / edge-tts 三个 provider。

相比脚本方案，本工具把「配置解析 → provider 选择 → 合成 → 落盘」收敛进
原生 Python 代码，避免 agent 写脚本 + exec 的往返与依赖环境问题。
"""

from __future__ import annotations

import json  # 序列化工具返回结果
from pathlib import Path  # 路径处理
from typing import Any  # 任意类型

from biscuitbot.agent.tools.base import Tool, tool_parameters  # 工具基类与参数装饰器
from biscuitbot.agent.tools.schema import (  # schema 构造器
    StringSchema,
    tool_parameters_schema,
)
from biscuitbot.audio.tts import (  # TTS 服务层
    TtsServiceError,
    resolve_tts_config_with_overrides,
    synthesize_speech_file,
)


@tool_parameters(
    tool_parameters_schema(
        text=StringSchema(
            "要合成语音的文本内容（必填）。",
            min_length=1,
        ),
        output_path=StringSchema(
            "保存音频的文件路径（可省略）。默认保存到工作区 generated/tts/<日期>/ 下。",
        ),
        voice=StringSchema(
            "音色覆盖。OpenAI 如 alloy/echo/fable/onyx/nova/shimmer；"
            "DashScope 如 longxiaochun/longwan；edge-tts 如 zh-CN-XiaoxiaoNeural。",
        ),
        rate=StringSchema(
            "语速，如 \"+10%\" 加快、\"-20%\" 减慢。",
        ),
        model=StringSchema(
            "模型覆盖，如 gpt-4o-mini-tts / cosyvoice-v2。",
        ),
        provider=StringSchema(
            "TTS 服务商覆盖：openai / dashscope / edge-tts。",
            enum=["openai", "dashscope", "edge-tts"],
        ),
        required=["text"],
    )
)
class TextToSpeechTool(Tool):
    """把文本合成为语音音频文件，返回本地文件路径。

    职责：把文本转语音能力封装为 agent 可调用的工具。传入文本（必填）与
    可选的 provider / voice / rate / model / output_path，工具内部解析配置、
    选择 provider 合成音频并落盘，返回音频文件路径与元数据。
    """

    _capability = (
        "Synthesize speech audio from text and save it as a local audio file."
    )
    _usage_md = "docs/text_to_speech.md"  # 工具使用说明文档路径

    config_key = ""  # 无独立工具配置段：从顶层 config.tts 读取

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        """仅当 config.tts.enabled 为 True 时启用。"""
        return bool(getattr(ctx.config, "tts", None) and ctx.config.tts.enabled)

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        """从上下文创建工具实例。"""
        return cls(workspace=ctx.workspace, config=ctx.config)

    def __init__(self, *, workspace: str | Path, config: Any) -> None:
        self.workspace = Path(workspace).expanduser()  # 工作区路径，展开 ~
        self.config = config  # 根配置

    @property
    def name(self) -> str:
        """工具名称。"""
        return "text_to_speech"

    @property
    def description(self) -> str:
        """工具描述，指导模型如何调用。"""
        return (
            "Synthesize speech audio from text and save it as a local mp3 file. "
            "Supports openai (gpt-4o-mini-tts), dashscope (cosyvoice-v2) and edge-tts "
            "(free, no API key). Returns the local audio file path; pass it to the user "
            "via the message tool or to a video/audio editor as input."
        )

    async def execute(
        self,
        text: str,
        output_path: str | None = None,
        voice: str | None = None,
        rate: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        **kwargs: Any,
    ) -> str:
        """执行文本转语音。

        参数:
            text: 要合成的文本（必填）。
            output_path: 可选输出路径。
            voice: 音色覆盖。
            rate: 语速覆盖。
            model: 模型覆盖。
            provider: 服务商覆盖。

        返回:
            包含音频本地路径与元数据的 JSON 字符串；出错时返回错误说明。
        """
        try:
            eff = resolve_tts_config_with_overrides(
                self.config,
                provider=provider,
                model=model,
                voice=voice,
                rate=rate,
            )
            path = await synthesize_speech_file(
                text,
                eff,
                output_path,
                workspace=self.workspace,
            )
            return json.dumps(
                {
                    "audio": {
                        "path": path,
                        "provider": eff.provider,
                        "model": eff.model,
                        "voice": eff.voice,
                    },
                    "next_step": (
                        "音频已生成并保存到本地，可把 path 作为后续剪辑/交付的输入，"
                        "或通过 message 工具把音频文件交付给用户。"
                    ),
                },
                ensure_ascii=False,
            )
        except TtsServiceError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            return f"Error: {exc}"
