from __future__ import annotations

from pathlib import Path

import pytest

from biscuitbot.config.loader import save_config
from biscuitbot.config.schema import Config


def test_diagnostics_report_includes_runtime_and_no_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from biscuitbot.webui.ws_http import GatewayHTTPHandler

    config_path = tmp_path / "config.json"
    secret = "sk-super-secret-value"
    config = Config.model_validate(
        {
            "providers": {
                "custom": {
                    "apiKey": secret,
                }
            }
        }
    )
    save_config(config, config_path)
    monkeypatch.setattr("biscuitbot.config.loader._current_config_path", config_path)

    # _build_diagnostics_report doesn't reference instance state, so a bare self works.
    report = GatewayHTTPHandler._build_diagnostics_report(None)

    assert "generated:" in report
    assert "version:" in report
    assert "python:" in report
    assert str(config_path) in report
    # 绝不泄漏密钥
    assert secret not in report


def test_recent_log_tail_reads_latest_log(tmp_path: Path) -> None:
    from biscuitbot.webui.ws_http import GatewayHTTPHandler

    log = tmp_path / "gateway.log"
    log.write_text("line1\nline2\nline3\n", encoding="utf-8")

    tail = GatewayHTTPHandler._recent_log_tail(tmp_path)
    assert any("gateway.log: line3" in line for line in tail)


def test_recent_log_tail_falls_back_on_empty_dir(tmp_path: Path) -> None:
    from biscuitbot.webui.ws_http import GatewayHTTPHandler

    tail = GatewayHTTPHandler._recent_log_tail(tmp_path)
    assert "no .log files found" in tail[0]
