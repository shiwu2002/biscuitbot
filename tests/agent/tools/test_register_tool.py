"""Tests for RegisterToolTool / UnregisterToolTool and the custom-tool pipeline.

These meta-tools let the agent create, install, and uninstall custom tools
at runtime.  Validation failures must produce a Chinese error message
explaining *why* the tool cannot be installed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from biscuitbot.agent.tools.base import Tool, tool_parameters
from biscuitbot.agent.tools.discover import DiscoverToolsTool
from biscuitbot.agent.tools.register_tool import RegisterToolTool, UnregisterToolTool
from biscuitbot.agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Fixtures: valid + invalid tool-file content
# ---------------------------------------------------------------------------

_VALID_TOOL_PY = '''\
from __future__ import annotations

from typing import Any

from biscuitbot.agent.tools.base import Tool, tool_parameters


@tool_parameters({"type": "object", "properties": {}})
class WeatherTool(Tool):
    _capability = "Get current weather for a city."
    _usage_md = "docs/weather.md"

    @property
    def name(self) -> str:
        return "weather"

    @property
    def description(self) -> str:
        return "Get current weather for a city."

    async def execute(self, **kwargs: Any) -> Any:
        return "sunny"
'''

_VALID_TOOL_MD = """\
# weather

Get current weather for a city.

## Parameters
None.

## Example
```
weather()
```
"""


def _write(tmp_path: Path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_register_tool_metadata():
    assert RegisterToolTool().name == "register_tool"
    assert "custom tool" in RegisterToolTool().description.lower()
    assert RegisterToolTool._always_include is True
    assert RegisterToolTool._usage_md == "docs/register_tool.md"


def test_unregister_tool_metadata():
    assert UnregisterToolTool().name == "unregister_tool"
    assert "custom tool" in UnregisterToolTool().description.lower()
    assert UnregisterToolTool._always_include is True
    assert UnregisterToolTool._usage_md == "docs/unregister_tool.md"


# ---------------------------------------------------------------------------
# Registry not bound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_tool_returns_error_when_registry_not_bound():
    tool = RegisterToolTool()
    result = await tool.execute(file_path="x.py", docs_md_path="x.md")
    payload = json.loads(result)
    assert "error" in payload
    assert "not bound" in payload["error"]


@pytest.mark.asyncio
async def test_unregister_tool_returns_error_when_registry_not_bound():
    tool = UnregisterToolTool()
    result = await tool.execute(name="weather")
    payload = json.loads(result)
    assert "error" in payload
    assert "not bound" in payload["error"]


# ---------------------------------------------------------------------------
# Successful registration round-trip
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_tool_success(tmp_path):
    reg = ToolRegistry()
    py = _write(tmp_path, "weather.py", _VALID_TOOL_PY)
    md = _write(tmp_path, "weather.md", _VALID_TOOL_MD)

    tool = RegisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(file_path=py, docs_md_path=md)
    payload = json.loads(result)

    assert payload["ok"] is True
    assert reg.has("weather")
    assert getattr(reg.get("weather"), "_custom", False) is True


@pytest.mark.asyncio
async def test_register_then_unregister_round_trip(tmp_path):
    reg = ToolRegistry()
    py = _write(tmp_path, "weather.py", _VALID_TOOL_PY)
    md = _write(tmp_path, "weather.md", _VALID_TOOL_MD)

    reg_tool = RegisterToolTool()
    reg_tool.bind_registry(reg)
    await reg_tool.execute(file_path=py, docs_md_path=md)
    assert reg.has("weather")

    unreg_tool = UnregisterToolTool()
    unreg_tool.bind_registry(reg)
    result = await unreg_tool.execute(name="weather")
    payload = json.loads(result)

    assert payload["ok"] is True
    assert not reg.has("weather")


# ---------------------------------------------------------------------------
# Validation failures — each must explain *why* in Chinese
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_fails_when_py_file_missing(tmp_path):
    reg = ToolRegistry()
    md = _write(tmp_path, "weather.md", _VALID_TOOL_MD)
    tool = RegisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(file_path=str(tmp_path / "nope.py"), docs_md_path=md)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "不存在" in payload["message"]


@pytest.mark.asyncio
async def test_register_fails_when_md_file_missing(tmp_path):
    reg = ToolRegistry()
    py = _write(tmp_path, "weather.py", _VALID_TOOL_PY)
    tool = RegisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(file_path=py, docs_md_path=str(tmp_path / "nope.md"))
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "不存在" in payload["message"]


@pytest.mark.asyncio
async def test_register_fails_when_no_tool_subclass(tmp_path):
    reg = ToolRegistry()
    py = _write(tmp_path, "empty.py", "# nothing here\nx = 1\n")
    md = _write(tmp_path, "x.md", _VALID_TOOL_MD)
    tool = RegisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(file_path=py, docs_md_path=md)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "Tool 子类" in payload["message"]


@pytest.mark.asyncio
async def test_register_fails_when_multiple_tool_subclasses(tmp_path):
    two_classes = (
        "from typing import Any\n"
        "from biscuitbot.agent.tools.base import Tool, tool_parameters\n"
        "@tool_parameters({'type':'object','properties':{}})\n"
        "class A(Tool):\n"
        "    _capability='a'\n"
        "    _usage_md='docs/a.md'\n"
        "    @property\n"
        "    def name(self): return 'a'\n"
        "    @property\n"
        "    def description(self): return 'a'\n"
        "    async def execute(self, **kwargs): return 'a'\n"
        "@tool_parameters({'type':'object','properties':{}})\n"
        "class B(Tool):\n"
        "    _capability='b'\n"
        "    _usage_md='docs/b.md'\n"
        "    @property\n"
        "    def name(self): return 'b'\n"
        "    @property\n"
        "    def description(self): return 'b'\n"
        "    async def execute(self, **kwargs): return 'b'\n"
    )
    reg = ToolRegistry()
    py = _write(tmp_path, "two.py", two_classes)
    md = _write(tmp_path, "two.md", _VALID_TOOL_MD)
    tool = RegisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(file_path=py, docs_md_path=md)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "多个" in payload["message"]


@pytest.mark.asyncio
async def test_register_fails_when_abstract_not_implemented(tmp_path):
    # Missing execute() — abstractmethods non-empty
    no_exec = (
        "from biscuitbot.agent.tools.base import Tool, tool_parameters\n"
        "@tool_parameters({'type':'object','properties':{}})\n"
        "class HalfTool(Tool):\n"
        "    _capability='half'\n"
        "    _usage_md='docs/half.md'\n"
        "    @property\n"
        "    def name(self): return 'half'\n"
        "    @property\n"
        "    def description(self): return 'half'\n"
    )
    reg = ToolRegistry()
    py = _write(tmp_path, "half.py", no_exec)
    md = _write(tmp_path, "half.md", _VALID_TOOL_MD)
    tool = RegisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(file_path=py, docs_md_path=md)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "抽象方法" in payload["message"]


@pytest.mark.asyncio
async def test_register_fails_when_capability_missing(tmp_path):
    no_cap = (
        "from typing import Any\n"
        "from biscuitbot.agent.tools.base import Tool, tool_parameters\n"
        "@tool_parameters({'type':'object','properties':{}})\n"
        "class NoCapTool(Tool):\n"
        "    _usage_md='docs/nocap.md'\n"
        "    @property\n"
        "    def name(self): return 'nocap'\n"
        "    @property\n"
        "    def description(self): return 'nocap'\n"
        "    async def execute(self, **kwargs): return 'ok'\n"
    )
    reg = ToolRegistry()
    py = _write(tmp_path, "nocap.py", no_cap)
    md = _write(tmp_path, "nocap.md", _VALID_TOOL_MD)
    tool = RegisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(file_path=py, docs_md_path=md)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "capability" in payload["message"]


@pytest.mark.asyncio
async def test_register_fails_when_usage_md_missing(tmp_path):
    no_usage = (
        "from typing import Any\n"
        "from biscuitbot.agent.tools.base import Tool, tool_parameters\n"
        "@tool_parameters({'type':'object','properties':{}})\n"
        "class NoUsageTool(Tool):\n"
        "    _capability='no usage'\n"
        "    @property\n"
        "    def name(self): return 'nousage'\n"
        "    @property\n"
        "    def description(self): return 'no usage'\n"
        "    async def execute(self, **kwargs): return 'ok'\n"
    )
    reg = ToolRegistry()
    py = _write(tmp_path, "nousage.py", no_usage)
    md = _write(tmp_path, "nousage.md", _VALID_TOOL_MD)
    tool = RegisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(file_path=py, docs_md_path=md)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "_usage_md" in payload["message"]


@pytest.mark.asyncio
async def test_register_fails_on_name_conflict_with_builtin(tmp_path):
    reg = ToolRegistry()
    # Pre-register a built-in-style tool named "weather"
    @tool_parameters({"type": "object", "properties": {}})
    class _BuiltinWeather(Tool):
        _capability = "builtin weather"
        _always_include = False
        _usage_md = "docs/builtin_weather.md"

        @property
        def name(self) -> str:
            return "weather"

        @property
        def description(self) -> str:
            return "builtin weather"

        async def execute(self, **kwargs: Any) -> Any:
            return "builtin"

    reg.register(_BuiltinWeather())

    py = _write(tmp_path, "weather.py", _VALID_TOOL_PY)
    md = _write(tmp_path, "weather.md", _VALID_TOOL_MD)
    tool = RegisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(file_path=py, docs_md_path=md)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "占用" in payload["message"]


# ---------------------------------------------------------------------------
# UnregisterToolTool failures
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unregister_fails_on_builtin_tool():
    reg = ToolRegistry()

    @tool_parameters({"type": "object", "properties": {}})
    class _FakeBuiltin(Tool):
        _capability = "fake builtin"
        _usage_md = "docs/fake.md"

        @property
        def name(self) -> str:
            return "fake_builtin"

        @property
        def description(self) -> str:
            return "fake builtin"

        async def execute(self, **kwargs: Any) -> Any:
            return "ok"

    reg.register(_FakeBuiltin())  # not in _custom_tools → treated as builtin

    tool = UnregisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(name="fake_builtin")
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "内置工具" in payload["message"]


@pytest.mark.asyncio
async def test_unregister_fails_on_unknown_tool():
    reg = ToolRegistry()
    tool = UnregisterToolTool()
    tool.bind_registry(reg)
    result = await tool.execute(name="nonexistent")
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "未注册" in payload["message"]


# ---------------------------------------------------------------------------
# Registry-level helpers: manifest persistence
# ---------------------------------------------------------------------------

def test_custom_tools_manifest_round_trip(tmp_path):
    reg = ToolRegistry()
    py = _write(tmp_path, "weather.py", _VALID_TOOL_PY)
    md = _write(tmp_path, "weather.md", _VALID_TOOL_MD)

    ok, _ = reg.register_custom_tool(py, md)
    assert ok

    manifest = reg.custom_tools_manifest()
    assert len(manifest) == 1
    assert manifest[0]["name"] == "weather"
    assert manifest[0]["file_path"].endswith("weather.py")
    assert manifest[0]["docs_md_path"].endswith("weather.md")


def test_load_custom_tools_from_manifest_success(tmp_path):
    reg = ToolRegistry()
    py = _write(tmp_path, "weather.py", _VALID_TOOL_PY)
    md = _write(tmp_path, "weather.md", _VALID_TOOL_MD)

    manifest = [{"name": "weather", "file_path": py, "docs_md_path": md}]
    errors = reg.load_custom_tools_from_manifest(manifest)
    assert errors == []
    assert reg.has("weather")


def test_load_custom_tools_from_manifest_reports_failure(tmp_path):
    reg = ToolRegistry()
    manifest = [{
        "name": "weather",
        "file_path": str(tmp_path / "deleted.py"),
        "docs_md_path": str(tmp_path / "deleted.md"),
    }]
    errors = reg.load_custom_tools_from_manifest(manifest)
    assert len(errors) == 1
    assert "weather" in errors[0]
    assert not reg.has("weather")


# ---------------------------------------------------------------------------
# Integration: custom tool shows in INDEX + discover_tools
# ---------------------------------------------------------------------------

def test_registered_custom_tool_appears_in_index(tmp_path):
    reg = ToolRegistry()
    py = _write(tmp_path, "weather.py", _VALID_TOOL_PY)
    md = _write(tmp_path, "weather.md", _VALID_TOOL_MD)
    reg.register_custom_tool(py, md)

    index = reg.generate_index()
    assert "weather" in index
    assert "docs/weather.md" in index


@pytest.mark.asyncio
async def test_registered_custom_tool_discoverable(tmp_path):
    reg = ToolRegistry()
    py = _write(tmp_path, "weather.py", _VALID_TOOL_PY)
    md = _write(tmp_path, "weather.md", _VALID_TOOL_MD)
    reg.register_custom_tool(py, md)

    discover = DiscoverToolsTool()
    discover.bind_registry(reg)
    result = await discover.execute(query="weather")
    payload = json.loads(result)
    assert "tools" in payload
    assert payload["tools"][0]["function"]["name"] == "weather"


def test_index_shows_missing_usage_md_marker(tmp_path):
    """A tool without _usage_md should render '(missing)' in the index."""
    reg = ToolRegistry()

    @tool_parameters({"type": "object", "properties": {}})
    class _NoMd(Tool):
        _capability = "no md tool"

        @property
        def name(self) -> str:
            return "no_md_tool"

        @property
        def description(self) -> str:
            return "no md tool"

        async def execute(self, **kwargs: Any) -> Any:
            return "ok"

    reg.register(_NoMd())
    index = reg.generate_index()
    assert "no_md_tool" in index
    assert "(missing)" in index
