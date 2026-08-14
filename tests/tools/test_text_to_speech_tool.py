"""text_to_speech 工具单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from biscuitbot.agent.tools.text_to_speech import TextToSpeechTool
from biscuitbot.audio.tts import TtsServiceError
from biscuitbot.config.schema import Config


def _tool(tmp_path: Path, **cfg: object) -> TextToSpeechTool:
    return TextToSpeechTool(workspace=tmp_path, config=Config(**cfg))


def _tool_with_config(tmp_path: Path, config: Config) -> TextToSpeechTool:
    return TextToSpeechTool(workspace=tmp_path, config=config)


def test_tool_metadata(tmp_path: Path) -> None:
    tool = _tool(tmp_path)
    assert tool.name == "text_to_speech"
    assert tool.config_key == ""
    assert tool._capability
    assert tool._usage_md == "docs/text_to_speech.md"
    assert tool._scopes == {"core"}
    assert tool._always_include is False
    # 参数 schema：text 必填
    assert tool.parameters["required"] == ["text"]
    for key in ("text", "output_path", "voice", "rate", "model", "provider"):
        assert key in tool.parameters["properties"]


def test_enabled_gated_by_tts_config(tmp_path: Path) -> None:
    ctx = SimpleNamespace(config=Config())
    assert TextToSpeechTool.enabled(ctx) is True
    ctx.config.tts.enabled = False
    assert TextToSpeechTool.enabled(ctx) is False
    # 无 tts 配置段时禁用
    bare = SimpleNamespace(config=SimpleNamespace())
    assert TextToSpeechTool.enabled(bare) is False


def test_create_wires_workspace_and_config(tmp_path: Path) -> None:
    cfg = Config()
    ctx = SimpleNamespace(workspace=tmp_path, config=cfg)
    tool = TextToSpeechTool.create(ctx)
    assert tool.workspace == tmp_path
    assert tool.config is cfg


@pytest.mark.asyncio
async def test_execute_returns_audio_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_synthesize(text, eff, output_path=None, *, workspace=None):  # noqa: ARG001
        path = Path(output_path or tmp_path) / "voice.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ID3")
        return str(path)

    tool = _tool(tmp_path)
    with patch(
        "biscuitbot.agent.tools.text_to_speech.synthesize_speech_file",
        side_effect=fake_synthesize,
    ):
        result = await tool.execute("你好，世界")

    payload = json.loads(result)
    assert payload["audio"]["path"].endswith("voice.mp3")
    assert payload["audio"]["provider"] == "edge-tts"
    assert payload["audio"]["voice"] == "zh-CN-XiaoxiaoNeural"
    assert "next_step" in payload


@pytest.mark.asyncio
async def test_execute_resolves_provider_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    async def fake_synthesize(text, eff, output_path=None, *, workspace=None):  # noqa: ARG001
        seen["provider"] = eff.provider
        seen["model"] = eff.model
        seen["voice"] = eff.voice
        path = Path(tmp_path) / "out.mp3"
        path.write_bytes(b"ID3")
        return str(path)

    tool = _tool(tmp_path)
    with patch(
        "biscuitbot.agent.tools.text_to_speech.synthesize_speech_file",
        side_effect=fake_synthesize,
    ):
        result = await tool.execute(
            "hi",
            provider="openai",
            voice="nova",
            rate="+10%",
            output_path=str(tmp_path / "out.mp3"),
        )

    assert seen["provider"] == "openai"
    assert seen["model"] == "gpt-4o-mini-tts"
    assert seen["voice"] == "nova"
    assert "out.mp3" in json.loads(result)["audio"]["path"]


@pytest.mark.asyncio
async def test_execute_returns_error_message_on_failure(
    tmp_path: Path,
) -> None:
    async def fake_synthesize(text, eff, output_path=None, *, workspace=None):  # noqa: ARG001
        raise TtsServiceError("合成失败：boom")

    tool = _tool(tmp_path)
    with patch(
        "biscuitbot.agent.tools.text_to_speech.synthesize_speech_file",
        side_effect=fake_synthesize,
    ):
        result = await tool.execute("hi")

    assert result.startswith("Error:")
    assert "boom" in result
