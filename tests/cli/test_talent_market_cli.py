"""人才市场注册表 URL 的 CLI 管理测试。

验证 ``biscuitbot talent-market set/show/clear``：
- ``set`` 手术式写入 ``gateway.talent_market_registry_url``，绝不触碰配置文件里
  的其他键（含 API Key）；
- ``set`` 校验 http/https，非法地址拒绝并保持文件不变；
- ``clear`` 只移除该键，其余键原样保留。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from biscuitbot.cli.commands import app
from biscuitbot.config.loader import _current_config_path

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_config_path():
    saved = _current_config_path
    yield
    import biscuitbot.config.loader as loader

    loader._current_config_path = saved


def _config_with_secret(tmp_path: Path) -> Path:
    """写一份带 API Key 的配置，验证 CLI 不会破坏既有键。"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "providers": {"openai": {"apiKey": "sk-should-survive"}},
                "gateway": {"host": "127.0.0.1", "port": 8765},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def test_set_writes_registry_url_and_preserves_api_key(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    url = "https://example.com/employees.json"

    result = runner.invoke(
        app, ["talent-market", "set", url, "--config", str(path)]
    )

    assert result.exit_code == 0, result.stdout
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gateway"]["talent_market_registry_url"] == url
    # API Key 等既有键必须原样保留
    assert raw["providers"]["openai"]["apiKey"] == "sk-should-survive"
    assert raw["gateway"]["host"] == "127.0.0.1"


def test_set_creates_config_when_missing(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    url = "http://127.0.0.1:8899/employees.json"

    result = runner.invoke(
        app, ["talent-market", "set", url, "--config", str(path)]
    )

    assert result.exit_code == 0, result.stdout
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gateway"]["talent_market_registry_url"] == url


@pytest.mark.parametrize("bad_url", ["file:///etc/passwd", "ftp://example.com/x", "  "])
def test_set_rejects_invalid_url(tmp_path: Path, bad_url: str) -> None:
    path = _config_with_secret(tmp_path)
    original = path.read_text(encoding="utf-8")

    result = runner.invoke(
        app, ["talent-market", "set", bad_url, "--config", str(path)]
    )

    assert result.exit_code == 1
    # 非法地址 → 文件必须保持原样
    assert path.read_text(encoding="utf-8") == original


def test_show_prints_configured_url(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    url = "https://example.com/employees.json"
    runner.invoke(app, ["talent-market", "set", url, "--config", str(path)])

    result = runner.invoke(
        app, ["talent-market", "show", "--config", str(path)]
    )

    assert result.exit_code == 0
    assert url in result.stdout


def test_show_unconfigured(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"gateway": {}}), encoding="utf-8")

    result = runner.invoke(
        app, ["talent-market", "show", "--config", str(path)]
    )

    assert result.exit_code == 0
    assert "未配置" in result.stdout


def test_clear_removes_only_registry_url(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    runner.invoke(
        app,
        ["talent-market", "set", "https://example.com/employees.json", "--config", str(path)],
    )

    result = runner.invoke(
        app, ["talent-market", "clear", "--config", str(path)]
    )

    assert result.exit_code == 0, result.stdout
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "talent_market_registry_url" not in raw["gateway"]
    # 其余键保留
    assert raw["providers"]["openai"]["apiKey"] == "sk-should-survive"
    assert raw["gateway"]["host"] == "127.0.0.1"
