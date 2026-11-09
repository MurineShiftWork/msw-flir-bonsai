"""Unit tests for msw_flir_bonsai.cli: no Bonsai installation required."""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

from typer.testing import CliRunner

from msw_flir_bonsai.cli import app, register

runner = CliRunner()


# ---------------------------------------------------------------------------
# find-bonsai


class TestFindBonsai:
    def test_found_exits_zero(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "Bonsai.exe"
        fake_exe.touch()
        with patch(
            "msw_flir_bonsai.cli._BONSAI_SEARCH_PATHS",
            [fake_exe],
        ):
            result = runner.invoke(app, ["find-bonsai"])
        assert result.exit_code == 0
        assert str(fake_exe) in result.output

    def test_not_found_exits_one(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent" / "Bonsai.exe"
        with patch("msw_flir_bonsai.cli._BONSAI_SEARCH_PATHS", [missing]):
            result = runner.invoke(app, ["find-bonsai"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# list-cameras: import-error path (SDK never installed on Linux CI)


class TestListCameras:
    def test_unknown_driver_exits_one(self) -> None:
        result = runner.invoke(app, ["list-cameras", "--driver", "invalid"])
        assert result.exit_code == 1

    def test_flycap_not_installed_exits_one(self) -> None:
        with patch.dict("sys.modules", {"PyCapture2": None}):
            result = runner.invoke(app, ["list-cameras", "--driver", "flycap"])
        assert result.exit_code == 1

    def test_spinnaker_not_installed_exits_one(self) -> None:
        with patch.dict("sys.modules", {"PySpin": None}):
            result = runner.invoke(app, ["list-cameras", "--driver", "spinnaker"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# probe-cameras


class TestProbeCameras:
    def test_json_output(self) -> None:
        from msw_flir_bonsai.probe import CameraInfo, ProbeResult

        found = ProbeResult("flycap", "sdk", 1, [CameraInfo(0, "12345678")])
        with patch("msw_flir_bonsai.probe.probe_cameras", return_value=found):
            result = runner.invoke(app, ["probe-cameras", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["cameras"] == [
            {"index": 0, "serial": "12345678"}
        ]

    def test_unavailable_exits_one(self) -> None:
        from msw_flir_bonsai.probe import ProbeResult

        with patch(
            "msw_flir_bonsai.probe.probe_cameras",
            return_value=ProbeResult("spinnaker", error="not found"),
        ):
            result = runner.invoke(app, ["probe-cameras", "-d", "spinnaker"])
        assert result.exit_code == 1
        assert "via none" in result.output

    def test_unknown_driver_exits_one(self) -> None:
        result = runner.invoke(app, ["probe-cameras", "--driver", "pylon"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# register(): argparse bridge


class TestRegister:
    def test_dispatch_func_is_set(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["flir", "find-bonsai"])
        assert callable(args.func)
        assert args.flir_args == ["find-bonsai"]

    def test_remainder_args_forwarded(self) -> None:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["flir", "run", "D:/DATA", "session1"])
        assert args.flir_args == ["run", "D:/DATA", "session1"]

    def test_dispatch_accepts_msw_cli_keyword_call(self) -> None:
        # msw-core calls func(**args_dict), including keys such as "command"
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register(sub)
        args = vars(parser.parse_args(["flir", "list-cameras"]))
        with patch("msw_flir_bonsai.cli.main") as main:
            args["func"](**args)
        main.assert_called_once()
