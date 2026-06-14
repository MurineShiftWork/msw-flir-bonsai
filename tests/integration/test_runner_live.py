"""Integration tests for BonsaiCameraRunner: require a real Bonsai installation.

These tests launch and stop actual Bonsai subprocesses.  They are skipped
automatically on machines where BONSAI_EXE is not set in the environment.

To run on a Windows machine with Bonsai installed:
    set BONSAI_EXE=C:\\Users\\<user>\\AppData\\Local\\Bonsai\\Bonsai.exe
    pytest tests/integration/test_runner_live.py -v

Or point directly:
    pytest tests/integration/test_runner_live.py -v \\
        --bonsai-exe "C:\\...\\Bonsai.exe" --driver flycap --cam-index 0
"""

from __future__ import annotations

import os
import time

import pytest

BONSAI_EXE = os.environ.get("BONSAI_EXE", "")
SKIP_REASON = "BONSAI_EXE not set: skipping live Bonsai tests"

requires_bonsai = pytest.mark.skipif(not BONSAI_EXE, reason=SKIP_REASON)


def _default_runner(
    tmp_path,
    cam_index: int = 0,
    driver: str = "flycap",
    fps: int = 30,
):
    from msw_flir_bonsai.runner import BonsaiCameraRunner

    return BonsaiCameraRunner(
        workflow=f"run-flir-{driver}-1cam",
        output_dir=str(tmp_path),
        session="live_test",
        cam_index=cam_index,
        fps=fps,
        driver=driver,
        bonsai_exe=BONSAI_EXE,
    )


# ---------------------------------------------------------------------------
# Launch and stop


@requires_bonsai
def test_runner_starts_and_stops(tmp_path) -> None:
    """Bonsai process launches, reports running, and stops cleanly."""
    runner = _default_runner(tmp_path)
    runner.start()
    assert runner.pid is not None
    assert runner.is_running
    time.sleep(2)  # allow Bonsai to fully initialise
    runner.stop()
    runner.wait(timeout=15)
    assert not runner.is_running


@requires_bonsai
def test_runner_creates_output_dir(tmp_path) -> None:
    """Bonsai workflow creates the session output directory."""
    runner = _default_runner(tmp_path)
    runner.start()
    time.sleep(3)
    runner.stop()
    runner.wait(timeout=15)
    # The IronPython script inside the workflow creates a subdir under basepath
    subdirs = list(tmp_path.iterdir())
    assert len(subdirs) >= 1, "No output directory created by Bonsai workflow"


@requires_bonsai
def test_runner_stop_timeout_kills(tmp_path) -> None:
    """stop() falls back to kill() if terminate() times out."""
    runner = _default_runner(tmp_path)
    runner.start()
    time.sleep(1)
    runner.stop(timeout=0.001)  # force kill path
    runner.wait(timeout=10)
    assert not runner.is_running


# ---------------------------------------------------------------------------
# MultiCameraRunner: two independent subprocesses


@requires_bonsai
def test_multi_runner_two_cameras(tmp_path) -> None:
    """Two camera processes start, run briefly, and stop independently."""
    from msw_flir_bonsai.runner import MultiCameraRunner

    multi = MultiCameraRunner.from_config(
        n_cameras=2,
        driver=os.environ.get("FLIR_DRIVER", "flycap"),
        output_dir=str(tmp_path),
        session="multi_test",
        fps=30,
        bonsai_exe=BONSAI_EXE,
    )
    multi.start()
    assert multi.all_running
    time.sleep(3)
    multi.stop()
    # allow both processes to exit
    for runner in multi._runners:
        runner.wait(timeout=15)
    assert not multi.any_running


# ---------------------------------------------------------------------------
# CLI smoke tests


@requires_bonsai
def test_cli_find_bonsai(capsys) -> None:
    """find-bonsai command finds BONSAI_EXE if it exists on disk."""
    import pathlib

    from typer.testing import CliRunner

    from msw_flir_bonsai.cli import app

    if not pathlib.Path(BONSAI_EXE).exists():
        pytest.skip("BONSAI_EXE path does not exist on disk")

    result = CliRunner().invoke(app, ["find-bonsai"])
    assert result.exit_code == 0
    assert BONSAI_EXE in result.output or "Bonsai.exe" in result.output


@requires_bonsai
def test_cli_test_record(tmp_path) -> None:
    """test-record command completes a short recording without error."""
    from typer.testing import CliRunner

    from msw_flir_bonsai.cli import app

    result = CliRunner().invoke(
        app,
        [
            "test-record",
            "--output-dir",
            str(tmp_path),
            "--session",
            "cli_test",
            "--cam-index",
            "0",
            "--driver",
            os.environ.get("FLIR_DRIVER", "flycap"),
            "--fps",
            "30",
            "--duration",
            "3",
            "--bonsai-exe",
            BONSAI_EXE,
        ],
    )
    assert result.exit_code == 0
