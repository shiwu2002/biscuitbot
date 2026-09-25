"""transcribe_media 工具单元测试。

守住两条产品不变量：

1. **永不门控**：未配置 API Key / 能力被关闭时，工具仍留在工具表里，错误由
   ``execute`` 返回可操作的中文说明（上下文层的附件说明会点名这个工具，摘掉它
   等于让模型对着 ``not found`` 干瞪眼）。
2. **只送音频**：工具只把**本地路径**交给既有 ASR 服务层；不接 URL、不读字节、
   不抽帧——成本口径是「按音频秒数」，画面永不进上下文。

另外钉住几个容易回归的点：体积是唯一护栏、language 覆盖真的到达配置、
服务层把失败折叠成空串时工具必须给出可诊断的错误。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from xianaibot.agent.tools import transcribe_media
from xianaibot.agent.tools.context import ToolContext
from xianaibot.agent.tools.loader import ToolLoader
from xianaibot.agent.tools.registry import ToolRegistry
from xianaibot.agent.tools.transcribe_media import TranscribeMediaTool
from xianaibot.audio.transcription import EffectiveTranscriptionConfig
from xianaibot.config.schema import Config
from xianaibot.security.workspace_access import (
    bind_workspace_scope,
    reset_workspace_scope,
    validate_workspace_scope_payload,
)

# 最小可识别的 ISO BMFF / EBML 容器头（``ftyp`` box 位于偏移 4）
_MP4_MAGIC = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
_WEBM_MAGIC = b"\x1a\x45\xdf\xa3" + b"\x00" * 64


def _eff(**over: object) -> EffectiveTranscriptionConfig:
    """构造一份「已配置可用」的生效配置，测试里直接替掉配置解析。"""
    base: dict[str, object] = {
        "enabled": True,
        "provider": "openai",
        "model": "whisper-1",
        "language": None,
        "api_key": "sk-test",
        "api_base": "https://api.openai.com/v1",
        "max_duration_sec": 120,
        "max_upload_mb": 25,
    }
    base.update(over)
    return EffectiveTranscriptionConfig(**base)  # type: ignore[arg-type]


def _make_tool(tmp_path: Path, cfg: Config | None = None) -> TranscribeMediaTool:
    cfg = cfg or Config()
    ctx = ToolContext(config=cfg.tools, root_config=cfg, workspace=str(tmp_path))
    tool = TranscribeMediaTool.create(ctx)
    assert isinstance(tool, TranscribeMediaTool)
    return tool


def _audio(tmp_path: Path, name: str = "voice.mp3", data: bytes = b"ID3" + b"\x00" * 32) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# ---------------------------------------------------------------------------
# 元数据 / 注册
# ---------------------------------------------------------------------------


def test_tool_metadata(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    assert tool.name == "transcribe_media"
    assert tool._usage_md == "docs/transcribe_media.md"
    assert tool._scopes == {"core"}
    assert tool._always_include is False
    assert tool._capability
    assert tool.read_only is True
    # 只有两个参数，且 path 必填
    assert tool.parameters["required"] == ["path"]
    assert set(tool.parameters["properties"]) == {"path", "language"}


def test_enabled_is_always_true_even_without_config() -> None:
    """永不门控：连配置段都没有时也返回 True（否则模型只会看到 not found）。"""
    assert TranscribeMediaTool.enabled(SimpleNamespace()) is True
    assert TranscribeMediaTool.enabled(SimpleNamespace(config=Config())) is True


def test_registers_via_loader_with_default_config(tmp_path: Path) -> None:
    """默认配置（transcription 未配置、provider 还是遗留的 groq）也必须注册。"""
    cfg = Config()
    ctx = ToolContext(config=cfg.tools, root_config=cfg, workspace=str(tmp_path))

    assert "transcribe_media" in ToolLoader().load(ctx, ToolRegistry())


def test_create_attaches_root_config(tmp_path: Path) -> None:
    cfg = Config()
    ctx = ToolContext(config=cfg.tools, root_config=cfg, workspace=str(tmp_path))

    tool = TranscribeMediaTool.create(ctx)

    assert tool._root_config is cfg  # 转写配置来自根配置，而非 ToolsConfig


# ---------------------------------------------------------------------------
# 前置检查：环境错误
# ---------------------------------------------------------------------------


async def test_disabled_by_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transcribe_media, "resolve_transcription_config", lambda _cfg: _eff(enabled=False)
    )
    tool = _make_tool(tmp_path)

    result = await tool.execute(path=str(_audio(tmp_path)))

    assert result.startswith("Error:")
    assert "transcription.enabled" in result
    assert "已被关闭" in result


async def test_unconfigured_names_the_effective_provider(tmp_path: Path) -> None:
    """默认配置下 provider 是遗留的 groq——错误必须点名它，否则用户无从下手。"""
    tool = _make_tool(tmp_path)

    result = await tool.execute(path=str(_audio(tmp_path)))

    assert result.startswith("Error:")
    assert "未配置 API Key" in result
    assert "groq" in result
    assert "模型厂商" in result


# ---------------------------------------------------------------------------
# 前置检查：参数错误
# ---------------------------------------------------------------------------


async def test_missing_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())
    tool = _make_tool(tmp_path)

    result = await tool.execute()

    assert "缺少 path" in result


async def test_rejects_http_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())
    tool = _make_tool(tmp_path)

    result = await tool.execute(path="https://cdn.example.com/a/voice.mp3")

    assert "直链" in result
    # 明确拒绝的同时给出可行路径（先下载到工作区）
    assert "下载" in result


async def test_directory_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())
    tool = _make_tool(tmp_path)
    sub = tmp_path / "recordings"
    sub.mkdir()

    result = await tool.execute(path=str(sub))

    assert "是目录" in result


async def test_missing_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())
    tool = _make_tool(tmp_path)

    result = await tool.execute(path=str(tmp_path / "gone.mp3"))

    assert "文件不存在" in result
    assert "list_dir" in result


async def test_unsupported_type_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())
    tool = _make_tool(tmp_path)
    txt = tmp_path / "notes.txt"
    txt.write_text("hello", encoding="utf-8")

    result = await tool.execute(path=str(txt))

    assert "不支持的文件类型" in result


async def test_path_outside_workspace_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工作区边界是结构性安全控制，与 guard_level 无关，必须仍在。"""
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = _audio(tmp_path, "outside.mp3")
    tool = _make_tool(workspace)

    scope = validate_workspace_scope_payload(
        {"project_path": str(workspace), "access_mode": "restricted"},
        default_workspace=workspace,
        default_restrict_to_workspace=True,
    )
    token = bind_workspace_scope(scope)
    try:
        result = await tool.execute(path=str(outside))
    finally:
        reset_workspace_scope(token)

    assert "路径越界" in result


async def test_oversized_file_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """体积是唯一护栏：超限要在本地就拒掉，别让服务端白收一次。"""
    monkeypatch.setattr(
        transcribe_media, "resolve_transcription_config", lambda _cfg: _eff(max_upload_mb=1)
    )
    tool = _make_tool(tmp_path)
    big = _audio(tmp_path, "long.mp3", b"ID3" + b"\x00" * (1024 * 1024 + 512))

    result = await tool.execute(path=str(big))

    assert "文件过大" in result
    assert "1MB" in result
    # 报错不能只说「做不到」：它必须把模型指向能解决这一步的地方——只抽音轨的
    # ffmpeg 命令写在 video-understanding 技能里。
    assert "video-understanding" in result
    assert "ffmpeg" in result


async def test_invalid_language_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())
    tool = _make_tool(tmp_path)

    result = await tool.execute(path=str(_audio(tmp_path)), language="zh-CN")

    assert "ISO-639-1" in result


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["voice.mp3", "voice.m4a", "voice.wav", "voice.ogg", "clip.mp4", "clip.webm"],
)
async def test_accepts_common_audio_and_video_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """音频容器与视频容器都接受（视频只取音轨）——含易混淆的 mp4/webm。"""
    data = {"clip.mp4": _MP4_MAGIC, "clip.webm": _WEBM_MAGIC}.get(name, b"ID3" + b"\x00" * 32)
    target = _audio(tmp_path, name, data)
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())

    async def fake_transcribe(path, config):  # noqa: ARG001
        return "大家好，这是一段测试录音。"

    monkeypatch.setattr(transcribe_media, "transcribe_audio_file", fake_transcribe)
    tool = _make_tool(tmp_path)

    result = await tool.execute(path=str(target))

    payload = json.loads(result)
    assert payload["transcription"]["text"] == "大家好，这是一段测试录音。"
    assert payload["transcription"]["path"] == str(target)
    assert payload["transcription"]["bytes"] == target.stat().st_size
    assert "next_step" in payload


async def test_accepts_workspace_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())

    async def fake_transcribe(path, config):  # noqa: ARG001
        return "ok"

    monkeypatch.setattr(transcribe_media, "transcribe_audio_file", fake_transcribe)
    target = _audio(tmp_path, "recordings/meeting.m4a")
    tool = _make_tool(tmp_path)

    payload = json.loads(await tool.execute(path="recordings/meeting.m4a"))

    assert payload["transcription"]["path"] == str(target)


async def test_language_override_reaches_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """language 走 ``dataclasses.replace``，零改动复用既有链路。"""
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())
    seen: dict[str, object] = {}

    async def fake_transcribe(path, config):
        seen["path"] = str(path)
        seen["language"] = config.language
        seen["provider"] = config.provider
        seen["model"] = config.model
        return "你好"

    monkeypatch.setattr(transcribe_media, "transcribe_audio_file", fake_transcribe)
    target = _audio(tmp_path)
    tool = _make_tool(tmp_path)

    payload = json.loads(await tool.execute(path=str(target), language="ZH"))

    assert seen["language"] == "zh"  # 统一小写后覆盖
    assert payload["transcription"]["language"] == "zh"
    assert seen["provider"] == "openai"
    assert seen["model"] == "whisper-1"
    # 只传路径，不读字节：服务层自己读文件
    assert seen["path"] == str(target)


async def test_configured_language_is_kept_when_not_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        transcribe_media, "resolve_transcription_config", lambda _cfg: _eff(language="ja")
    )

    async def fake_transcribe(path, config):  # noqa: ARG001
        return "こんにちは"

    monkeypatch.setattr(transcribe_media, "transcribe_audio_file", fake_transcribe)
    tool = _make_tool(tmp_path)

    payload = json.loads(await tool.execute(path=str(_audio(tmp_path))))

    assert payload["transcription"]["language"] == "ja"


# ---------------------------------------------------------------------------
# 失败诊断
# ---------------------------------------------------------------------------


async def test_empty_transcript_returns_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务层把 4xx/超时/无人声全折叠成空串，工具必须把它翻译成可排查的错误。"""
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())

    async def fake_transcribe(path, config):  # noqa: ARG001
        return "   "

    monkeypatch.setattr(transcribe_media, "transcribe_audio_file", fake_transcribe)
    tool = _make_tool(tmp_path)

    result = await tool.execute(path=str(_audio(tmp_path)))

    assert result.startswith("Error:")
    assert "provider=openai" in result
    assert "voice.mp3" in result
    assert "transcription HTTP" in result  # 指向日志里的一手证据
    assert "不要" in result  # 明确劝阻重复调用


async def test_service_exception_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcribe_media, "resolve_transcription_config", lambda _cfg: _eff())

    async def fake_transcribe(path, config):  # noqa: ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(transcribe_media, "transcribe_audio_file", fake_transcribe)
    tool = _make_tool(tmp_path)

    result = await tool.execute(path=str(_audio(tmp_path)))

    assert result.startswith("Error:")
    assert "boom" in result
