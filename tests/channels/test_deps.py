"""渠道 SDK 依赖检测与后台自动安装。"""

from __future__ import annotations

import importlib.util

import pytest

from biscuitbot.channels import deps


class _FakeMissing:
    """声明了不存在 SDK 的渠道（用于触发安装路径）。"""

    name = "fake-missing"
    display_name = "FakeMissing"
    requires_module = "definitely_missing_sdk_module_xyz_123"
    pip_requires = ["fake-pkg>=1.0"]


class _NoSdkDeclared:
    name = "plain"
    display_name = "Plain"


class _SdkReady:
    name = "ready"
    display_name = "Ready"
    requires_module = "json"
    pip_requires = ["whatever"]


def test_sdk_available_without_declaration() -> None:
    assert deps.channel_sdk_available(_NoSdkDeclared) is True


def test_sdk_available_missing_module() -> None:
    assert deps.channel_sdk_available(_FakeMissing) is False


def test_sdk_available_present_module() -> None:
    assert deps.channel_sdk_available(_SdkReady) is True


def test_ensure_deps_noop_when_sdk_ready() -> None:
    assert deps.ensure_channel_deps(_SdkReady) is False


def test_ensure_deps_triggers_background_install(monkeypatch: pytest.MonkeyPatch) -> None:
    deps._IN_PROGRESS.clear()
    deps._ERRORS.clear()
    spec = importlib.util.find_spec("json")
    assert spec is not None

    # 渠道 SDK 缺失但 pip 可用 → 触发后台安装
    monkeypatch.setattr(
        "biscuitbot.channels.deps.importlib.util.find_spec",
        lambda m: spec if m == "pip" else None,
    )
    monkeypatch.setattr("biscuitbot.channels.deps._run_pip_install", lambda *a, **k: None)
    assert deps.ensure_channel_deps(_FakeMissing) is True
    assert _FakeMissing.name in deps._IN_PROGRESS
    status = deps.deps_status(_FakeMissing)
    assert status["sdk_available"] is False
    assert status["deps_installing"] is True
    assert status["deps_error"] is None

    deps._IN_PROGRESS.discard(_FakeMissing.name)
    deps._ERRORS.pop(_FakeMissing.name, None)


def test_ensure_deps_pip_unavailable_sets_error(monkeypatch: pytest.MonkeyPatch) -> None:
    deps._IN_PROGRESS.clear()
    deps._ERRORS.clear()
    monkeypatch.setattr("biscuitbot.channels.deps.importlib.util.find_spec", lambda m: None)
    assert deps.ensure_channel_deps(_FakeMissing) is False
    assert _FakeMissing.name in deps._ERRORS
    assert deps.deps_status(_FakeMissing)["deps_installing"] is False
    deps._ERRORS.pop(_FakeMissing.name, None)


def test_gated_channels_declare_deps() -> None:
    """有 SDK 门槛的渠道都应声明 requires_module 与 pip_requires。"""
    from biscuitbot.channels.dingtalk import DingTalkChannel
    from biscuitbot.channels.feishu import FeishuChannel
    from biscuitbot.channels.mochat import MochatChannel
    from biscuitbot.channels.qq import QQChannel
    from biscuitbot.channels.wecom import WecomChannel

    for cls in (WecomChannel, FeishuChannel, QQChannel, DingTalkChannel, MochatChannel):
        assert getattr(cls, "requires_module", None), cls.__name__
        assert getattr(cls, "pip_requires", []), cls.__name__
