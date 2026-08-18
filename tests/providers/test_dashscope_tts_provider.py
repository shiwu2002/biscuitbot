"""DashScope TTS Provider（biscuitbot/providers/tts.py）单元测试。"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from biscuitbot.providers.tts import DashScopeTtsProvider, TtsError


def _install_fake_dashscope(
    monkeypatch: pytest.MonkeyPatch, audio: bytes = b"MP3"
) -> MagicMock:
    """把假 dashscope SDK 注入 sys.modules，返回 SpeechSynthesizer 类 mock。"""
    instance = MagicMock()
    instance.call.return_value = audio
    synth_cls = MagicMock(return_value=instance)

    tts_v2 = ModuleType("dashscope.audio.tts_v2")
    tts_v2.SpeechSynthesizer = synth_cls
    audio_mod = ModuleType("dashscope.audio")
    audio_mod.tts_v2 = tts_v2
    dashscope = ModuleType("dashscope")
    dashscope.audio = audio_mod

    monkeypatch.setitem(sys.modules, "dashscope", dashscope)
    monkeypatch.setitem(sys.modules, "dashscope.audio", audio_mod)
    monkeypatch.setitem(sys.modules, "dashscope.audio.tts_v2", tts_v2)
    return synth_cls


@pytest.mark.asyncio
async def test_synthesize_uses_sdk_and_writes_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synth_cls = _install_fake_dashscope(monkeypatch, audio=b"MP3DATA")
    provider = DashScopeTtsProvider(
        api_key="sk-dash",
        model="cosyvoice-v2",
        voice="longxiaochun_v2",
    )
    out = tmp_path / "voice"  # 无扩展名，应补 .mp3
    path = await provider.synthesize("你好", out)

    assert path.endswith(".mp3")
    assert Path(path).read_bytes() == b"MP3DATA"
    synth_cls.assert_called_once_with(
        model="cosyvoice-v2", voice="longxiaochun_v2", speech_rate=1.0
    )
    assert synth_cls.return_value.async_call is False
    synth_cls.return_value.call.assert_called_once_with("你好", timeout_millis=120_000)
    # SDK 从全局读取密钥，适配器应在调用前写入
    assert sys.modules["dashscope"].api_key == "sk-dash"


@pytest.mark.asyncio
async def test_missing_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    provider = DashScopeTtsProvider(api_key=None)
    with pytest.raises(TtsError, match="API key"):
        await provider.synthesize("hi", tmp_path / "o.mp3")


@pytest.mark.asyncio
async def test_missing_sdk_raises_helpful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):  # noqa: ANN001
        if name == "dashscope" or name.startswith("dashscope."):
            raise ImportError("No module named 'dashscope'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    provider = DashScopeTtsProvider(api_key="sk-dash")
    with pytest.raises(TtsError, match="dashscope"):
        await provider.synthesize("hi", tmp_path / "o.mp3")
