"""人才市场注册表 URL 的 CLI 管理测试。

验证 ``biscuitbot talent-market set/add/remove/list/show/clear``：
- ``set`` 整体替换为列表、``add`` 追加、``remove`` 精确删除、``list``/``show`` 打印全部；
- 手术式写回 ``gateway`` 相关键，绝不触碰配置文件里的其他键（含 API Key）；
- 校验 http/https，非法地址拒绝并保持文件不变；
- 兼容旧单值键（``talent_market_registry_url``）迁移进列表。
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


def test_set_replaces_all_registry_urls_and_preserves_api_key(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    url = "https://example.com/employees.json"

    result = runner.invoke(
        app, ["talent-market", "set", url, "--config", str(path)]
    )

    assert result.exit_code == 0, result.stdout
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gateway"]["talent_market_registry_urls"] == [url]
    # 旧单值键应被删除，避免遗留双形态
    assert "talent_market_registry_url" not in raw["gateway"]
    assert "talentMarketRegistryUrl" not in raw["gateway"]
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
    assert raw["gateway"]["talent_market_registry_urls"] == [url]


def test_set_overwrites_existing_multi_urls(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    runner.invoke(
        app,
        ["talent-market", "add", "https://a.example/x.json", "--config", str(path)],
    )
    runner.invoke(
        app,
        ["talent-market", "add", "https://b.example/y.json", "--config", str(path)],
    )

    result = runner.invoke(
        app,
        ["talent-market", "set", "https://c.example/z.json", "--config", str(path)],
    )

    assert result.exit_code == 0, result.stdout
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gateway"]["talent_market_registry_urls"] == ["https://c.example/z.json"]
    assert raw["providers"]["openai"]["apiKey"] == "sk-should-survive"


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


def test_clear_removes_all_registry_url_keys(tmp_path: Path) -> None:
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
    assert "talent_market_registry_urls" not in raw["gateway"]
    assert "talent_market_registry_url" not in raw["gateway"]
    # 其余键保留
    assert raw["providers"]["openai"]["apiKey"] == "sk-should-survive"
    assert raw["gateway"]["host"] == "127.0.0.1"


def test_add_appends_and_is_idempotent(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    url_a = "https://a.example/x.json"
    url_b = "https://b.example/y.json"

    for url in (url_a, url_b):
        r = runner.invoke(app, ["talent-market", "add", url, "--config", str(path)])
        assert r.exit_code == 0, r.stdout
    # 重复 add 幂等
    r = runner.invoke(app, ["talent-market", "add", url_a, "--config", str(path)])
    assert r.exit_code == 0

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gateway"]["talent_market_registry_urls"] == [url_a, url_b]
    assert raw["providers"]["openai"]["apiKey"] == "sk-should-survive"


def test_add_migrates_legacy_single_value(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    path.write_text(
        json.dumps(
            {
                "gateway": {"talent_market_registry_url": "https://legacy.example/x.json"},
                "providers": {"openai": {"apiKey": "sk-should-survive"}},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    r = runner.invoke(
        app, ["talent-market", "add", "https://new.example/y.json", "--config", str(path)]
    )

    assert r.exit_code == 0, r.stdout
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gateway"]["talent_market_registry_urls"] == [
        "https://legacy.example/x.json",
        "https://new.example/y.json",
    ]
    assert "talent_market_registry_url" not in raw["gateway"]


def test_remove_deletes_exact_url(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    url_a = "https://a.example/x.json"
    url_b = "https://b.example/y.json"
    for url in (url_a, url_b):
        runner.invoke(app, ["talent-market", "add", url, "--config", str(path)])

    r = runner.invoke(app, ["talent-market", "remove", url_a, "--config", str(path)])

    assert r.exit_code == 0, r.stdout
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gateway"]["talent_market_registry_urls"] == [url_b]


def test_remove_last_url_clears_config(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    runner.invoke(
        app,
        ["talent-market", "add", "https://a.example/x.json", "--config", str(path)],
    )

    r = runner.invoke(
        app, ["talent-market", "remove", "https://a.example/x.json", "--config", str(path)]
    )

    assert r.exit_code == 0, r.stdout
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "talent_market_registry_urls" not in raw["gateway"]
    assert raw["providers"]["openai"]["apiKey"] == "sk-should-survive"


def test_remove_unknown_url_is_noop(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    runner.invoke(
        app,
        ["talent-market", "add", "https://a.example/x.json", "--config", str(path)],
    )
    original = path.read_text(encoding="utf-8")

    r = runner.invoke(
        app, ["talent-market", "remove", "https://nope.example/x.json", "--config", str(path)]
    )

    assert r.exit_code == 0, r.stdout
    assert path.read_text(encoding="utf-8") == original


def test_list_prints_all_urls(tmp_path: Path) -> None:
    path = _config_with_secret(tmp_path)
    url_a = "https://a.example/x.json"
    url_b = "https://b.example/y.json"
    for url in (url_a, url_b):
        runner.invoke(app, ["talent-market", "add", url, "--config", str(path)])

    result = runner.invoke(
        app, ["talent-market", "list", "--config", str(path)]
    )

    assert result.exit_code == 0
    assert url_a in result.stdout
    assert url_b in result.stdout
