"""冷门仓库只读载荷的单元测试。"""

from __future__ import annotations

import json
from pathlib import Path

from xianaibot.webui.cold_storage_api import cold_storage_payload

_NOW = 1_780_000_000.0
_DAY = 86400.0


def _seed_cold(workspace: Path, entries: dict[str, dict]) -> Path:
    """按 UsageStats 的落盘格式写入 cold_storage.json。"""
    target = workspace / ".agent_tools" / "cold_storage.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return target


def _entry(name: str, cold_since: float, **extra) -> dict:
    return {
        "name": name,
        "capability": f"{name} 的能力",
        "usage_md": f"{name}.md",
        "source_file": "xianaibot.agent.tools.demo",
        "cold_since": cold_since,
        **extra,
    }


def test_empty_without_file(tmp_path: Path) -> None:
    payload = cold_storage_payload(tmp_path, now=_NOW)

    assert payload == {"cold_count": 0, "entries": []}


def test_lists_entries_newest_first_with_fields(tmp_path: Path) -> None:
    _seed_cold(
        tmp_path,
        {
            "old_tool": _entry("old_tool", _NOW - 30 * _DAY),
            "new_tool": _entry("new_tool", _NOW - 3 * _DAY),
            "never_called": _entry("never_called", 0.0),
        },
    )

    payload = cold_storage_payload(tmp_path, now=_NOW)

    assert payload["cold_count"] == 3
    assert [e["name"] for e in payload["entries"]] == [
        "new_tool",
        "old_tool",
        "never_called",
    ]
    assert [e["cold_days"] for e in payload["entries"]] == [3, 30, 0]
    newest = payload["entries"][0]
    assert newest["capability"] == "new_tool 的能力"
    assert newest["usage_md"] == "new_tool.md"
    assert newest["source_file"] == "xianaibot.agent.tools.demo"
    assert newest["cold_since"] == _NOW - 3 * _DAY


def test_corrupt_json_yields_empty_list(tmp_path: Path) -> None:
    """坏文件不能让只读接口 500；合法但非对象的 JSON 同样要兜住。"""
    for raw in ("not json at all", "[]", "null", '"x"'):
        _seed_cold(tmp_path, {})
        (tmp_path / ".agent_tools" / "cold_storage.json").write_text(raw, encoding="utf-8")

        payload = cold_storage_payload(tmp_path, now=_NOW)

        assert payload["cold_count"] == 0, raw
        assert payload["entries"] == [], raw


def test_clamps_future_and_zero_timestamps(tmp_path: Path) -> None:
    _seed_cold(
        tmp_path,
        {
            "future_tool": _entry("future_tool", _NOW + 10 * _DAY),
            "never_tool": _entry("never_tool", 0.0),
        },
    )

    payload = cold_storage_payload(tmp_path, now=_NOW)

    assert sorted(e["cold_days"] for e in payload["entries"]) == [0, 0]
