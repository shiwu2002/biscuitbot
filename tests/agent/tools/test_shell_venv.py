"""Tests for the exec tool's project-venv injection (prefer_venv_python).

Ensures that when the gateway runs inside a virtualenv, exec commands get the
venv bin dir prepended to PATH (so python3/pip3 resolve to the project
interpreter) and VIRTUAL_ENV is set — while disabling the feature or running
outside a venv leaves behavior unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from biscuitbot.agent.tools.shell import ExecTool

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="venv bin dir semantics differ on Windows"
)

VENV_BIN = "/proj/.venv/bin"


def _prepared(tool: ExecTool, command: str, cwd: str):
    result = tool._prepare_command(command, cwd)
    assert not isinstance(result, str), f"_prepare_command returned error: {result}"
    return result


class TestVenvDetection:
    def test_returns_none_outside_venv(self) -> None:
        with patch.object(ExecTool, "_venv_bin_dir", return_value=None) as mocked:
            assert ExecTool._venv_bin_dir() is None
        mocked.assert_called_once()

    def test_real_venv_roundtrip(self) -> None:
        # 仅在确实运行于 venv 时验证：探测到的目录存在且含解释器
        if sys.prefix == sys.base_prefix:
            pytest.skip("not running inside a venv")
        bindir = ExecTool._venv_bin_dir()
        assert bindir is not None
        assert bindir == str(Path(sys.prefix) / "bin")
        assert (Path(bindir) / "python3").exists() or (Path(bindir) / "python").exists()


class TestPathComposition:
    def test_compose_path_prepends_venv_bin(self) -> None:
        tool = ExecTool(guard_level="off", working_dir="/tmp")
        assert (
            tool._compose_path("/usr/bin:/bin", VENV_BIN)
            == f"{VENV_BIN}:/usr/bin:/bin"
        )

    def test_compose_path_venv_before_config_prepend(self) -> None:
        tool = ExecTool(guard_level="off", working_dir="/tmp", path_prepend="/cfg/bin")
        assert (
            tool._compose_path("/usr/bin", VENV_BIN)
            == f"{VENV_BIN}:/cfg/bin:/usr/bin"
        )

    def test_compose_path_without_venv(self) -> None:
        tool = ExecTool(guard_level="off", working_dir="/tmp")
        assert tool._compose_path("/usr/bin:/bin", None) == "/usr/bin:/bin"

    def test_wrap_path_export_prepends_venv_and_sets_env(self) -> None:
        tool = ExecTool(guard_level="off", working_dir="/tmp")
        env: dict[str, str] = {}
        command = tool._wrap_path_export("python3 --version", env, VENV_BIN)
        assert command == 'export PATH="$BISCUITBOT_VENV_BIN:$PATH"; python3 --version'
        assert env["BISCUITBOT_VENV_BIN"] == VENV_BIN

    def test_wrap_path_export_without_venv(self) -> None:
        tool = ExecTool(guard_level="off", working_dir="/tmp")
        env: dict[str, str] = {}
        command = tool._wrap_path_export("echo hi", env, None)
        assert command == 'export PATH="$PATH"; echo hi'
        assert "BISCUITBOT_VENV_BIN" not in env


class TestPrepareCommand:
    def test_injects_venv_when_preferred(self, tmp_path: Path) -> None:
        with patch.object(ExecTool, "_venv_bin_dir", return_value=VENV_BIN):
            tool = ExecTool(
                guard_level="off", working_dir=str(tmp_path), prefer_venv_python=True
            )
            prepped = _prepared(tool, "python3 --version", str(tmp_path))
        assert (
            prepped.command
            == 'export PATH="$BISCUITBOT_VENV_BIN:$PATH"; python3 --version'
        )
        assert prepped.env["BISCUITBOT_VENV_BIN"] == VENV_BIN
        assert prepped.env["VIRTUAL_ENV"] == "/proj/.venv"

    def test_no_injection_when_disabled(self, tmp_path: Path) -> None:
        # 即使探测到 venv，关闭 prefer_venv_python 后命令与环境保持原样
        with patch.object(ExecTool, "_venv_bin_dir", return_value=VENV_BIN):
            tool = ExecTool(
                guard_level="off", working_dir=str(tmp_path), prefer_venv_python=False
            )
            prepped = _prepared(tool, "echo hi", str(tmp_path))
        assert prepped.command == "echo hi"
        assert "BISCUITBOT_VENV_BIN" not in prepped.env
        assert "VIRTUAL_ENV" not in prepped.env

    def test_no_injection_when_not_in_venv(self, tmp_path: Path) -> None:
        with patch.object(ExecTool, "_venv_bin_dir", return_value=None):
            tool = ExecTool(
                guard_level="off", working_dir=str(tmp_path), prefer_venv_python=True
            )
            prepped = _prepared(tool, "echo hi", str(tmp_path))
        assert prepped.command == "echo hi"
        assert "VIRTUAL_ENV" not in prepped.env

    def test_prefer_venv_python_defaults_true(self) -> None:
        tool = ExecTool(guard_level="off", working_dir="/tmp")
        assert tool.prefer_venv_python is True


class TestFrozenBundledShims:
    """PyInstaller 冻结（桌面 sidecar）：生成指向 sidecar 解释器模式的 shim。"""

    def test_bundled_bin_none_when_not_frozen(self) -> None:
        tool = ExecTool(guard_level="off", working_dir="/tmp")
        assert tool._bundled_python_bin() is None

    def test_prefer_python_prefers_venv_over_frozen(self) -> None:
        with (
            patch.object(ExecTool, "_venv_bin_dir", return_value="/venv/bin"),
            patch.object(ExecTool, "_bundled_python_bin", return_value="/frozen/shims"),
        ):
            tool = ExecTool(guard_level="off", working_dir="/tmp")
            assert tool._prefer_python_bin() == "/venv/bin"

    def test_prefer_python_falls_back_to_frozen(self) -> None:
        with (
            patch.object(ExecTool, "_venv_bin_dir", return_value=None),
            patch.object(ExecTool, "_bundled_python_bin", return_value="/frozen/shims"),
        ):
            tool = ExecTool(guard_level="off", working_dir="/tmp")
            assert tool._prefer_python_bin() == "/frozen/shims"

    def test_bundled_bin_generates_shims(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "biscuitbot-sidecar"
        fake_exe.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_exe.chmod(0o755)
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(fake_exe)),
            patch(
                "biscuitbot.agent.tools.shell.tempfile.gettempdir",
                return_value=str(tmp_path),
            ),
        ):
            tool = ExecTool(guard_level="off", working_dir="/tmp")
            bindir = tool._bundled_python_bin()
        assert bindir is not None
        d = Path(bindir)
        for name in ("python", "python3", "pip", "pip3"):
            assert (d / name).exists(), name
            assert (d / name).stat().st_mode & 0o111, f"{name} not executable"
            content = (d / name).read_text(encoding="utf-8")
            assert "__biscuitbot_python__" in content, name
            assert str(fake_exe) in content, name
        # pip shim 走 -m pip
        assert "-m pip" in (d / "pip3").read_text(encoding="utf-8")
        # 幂等：再次调用返回同一目录，不重复生成
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(fake_exe)),
            patch(
                "biscuitbot.agent.tools.shell.tempfile.gettempdir",
                return_value=str(tmp_path),
            ),
        ):
            tool2 = ExecTool(guard_level="off", working_dir="/tmp")
            assert tool2._bundled_python_bin() == bindir

    def test_bundled_bin_none_when_executable_missing(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", "/no/such/sidecar"),
            patch(
                "biscuitbot.agent.tools.shell.tempfile.gettempdir",
                return_value="/tmp",
            ),
        ):
            tool = ExecTool(guard_level="off", working_dir="/tmp")
            assert tool._bundled_python_bin() is None
