"""Unit tests for BonsaiCameraRunner and MultiCameraRunner.

All subprocess calls are mocked: no Bonsai installation required.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from msw_flir_bonsai.bonsai_workflows import workflow_path
from msw_flir_bonsai.runner import (
    BonsaiCameraRunner,
    BonsaiStartError,
    MultiCameraRunner,
    _find_bonsai_exe,
)

# ---------------------------------------------------------------------------
# Helpers

_ACQDIR = "D:/DATA/video/mouse01__dt__video_flir"
_CAM_BASENAME = "mouse01__dt__video_flir.mswflir.cam0"


def _make_runner(
    workflow: str = "run-flir-flycap-1cam",
    acqdir: str = _ACQDIR,
    cam_basename: str = _CAM_BASENAME,
    cam_index: int = 0,
    fps: int | None = None,
    driver: str = "flycap",
    bonsai_exe: str = "C:/Bonsai/Bonsai.exe",
) -> BonsaiCameraRunner:
    return BonsaiCameraRunner(
        workflow=workflow,
        acqdir=acqdir,
        cam_basename=cam_basename,
        cam_index=cam_index,
        fps=fps,
        driver=driver,
        bonsai_exe=bonsai_exe,
    )


# ---------------------------------------------------------------------------
# _build_cmd


class TestBuildCmd:
    def test_preview_layout_shifted_per_camera(self) -> None:
        runner = _make_runner(workflow="run-flir-flycap-1cam", cam_index=1)
        cmd = runner._build_cmd()
        assert "--visualizer-layout" in cmd
        layout = Path(cmd[cmd.index("--visualizer-layout") + 1])
        try:
            assert "<Location><X>700</X>" in layout.read_text(encoding="utf-8")
        finally:
            layout.unlink(missing_ok=True)

    def test_no_preview_layout_without_layout_file(self) -> None:
        runner = _make_runner(workflow="run-flir-spinnaker-1cam", driver="spinnaker")
        assert "--visualizer-layout" not in runner._build_cmd()

    def test_flycap_includes_ts_and_framecounter(self) -> None:
        runner = _make_runner(driver="flycap")
        cmd = runner._build_cmd()
        joined = " ".join(cmd)
        assert "cam1ts=True" in joined
        assert "cam1framecounter=True" in joined

    def test_flycap_fps_passed_when_set(self) -> None:
        runner = _make_runner(driver="flycap", fps=30)
        cmd = runner._build_cmd()
        assert "cam1fps=30" in " ".join(cmd)

    def test_flycap_fps_omitted_when_none(self) -> None:
        runner = _make_runner(driver="flycap", fps=None)
        cmd = runner._build_cmd()
        assert "cam1fps" not in " ".join(cmd)

    def test_spinnaker_omits_fps(self) -> None:
        runner = _make_runner(driver="spinnaker")
        cmd = runner._build_cmd()
        joined = " ".join(cmd)
        assert "cam1fps" not in joined
        assert "cam1ts" not in joined

    def test_spinnaker_serial_passed_when_set(self) -> None:
        runner = BonsaiCameraRunner(
            workflow="run-flir-spinnaker-1cam",
            acqdir=_ACQDIR,
            cam_basename=_CAM_BASENAME,
            driver="spinnaker",
            cam_serial="12345678",
            bonsai_exe="C:/Bonsai.exe",
        )
        assert "cam1serial=12345678" in runner._build_cmd()

    def test_serial_omitted_when_empty_or_flycap(self) -> None:
        spin = _make_runner(workflow="run-flir-spinnaker-1cam", driver="spinnaker")
        flycap = BonsaiCameraRunner(
            workflow="run-flir-flycap-1cam",
            acqdir=_ACQDIR,
            cam_basename=_CAM_BASENAME,
            cam_serial="12345678",
            bonsai_exe="C:/Bonsai.exe",
        )
        assert "cam1serial" not in " ".join(spin._build_cmd())
        cmd = flycap._build_cmd()
        assert "cam1serial" not in " ".join(cmd)
        Path(cmd[cmd.index("--visualizer-layout") + 1]).unlink(missing_ok=True)

    def test_cam_index_passed(self) -> None:
        runner = _make_runner(cam_index=2)
        cmd = runner._build_cmd()
        assert "cam1idx=2" in " ".join(cmd)

    def test_start_and_no_editor_flags(self) -> None:
        runner = _make_runner()
        cmd = runner._build_cmd()
        assert "--start" in cmd
        assert "--no-editor" in cmd

    def test_acqdir_and_cam_basename_quoted(self) -> None:
        runner = _make_runner(acqdir=_ACQDIR, cam_basename=_CAM_BASENAME)
        cmd = runner._build_cmd()
        joined = " ".join(cmd)
        assert f'"{_ACQDIR}"' in joined
        assert f'"{_CAM_BASENAME}"' in joined

    def test_acqdir_and_cam_basename_prop_names(self) -> None:
        runner = _make_runner()
        cmd = runner._build_cmd()
        joined = " ".join(cmd)
        assert "acqdir=" in joined
        assert "cam_basename=" in joined
        assert "basepath=" not in joined
        assert "session=" not in joined

    def test_extra_props_forwarded(self) -> None:
        runner = BonsaiCameraRunner(
            workflow="run-flir-flycap-1cam",
            acqdir=_ACQDIR,
            cam_basename=_CAM_BASENAME,
            bonsai_exe="C:/Bonsai.exe",
            extra_props={"mykey": "myval"},
        )
        cmd = runner._build_cmd()
        assert "mykey=myval" in " ".join(cmd)


# ---------------------------------------------------------------------------
# start / stop / is_running


class TestStartStop:
    def _mock_process(self, returncode: int | None = None) -> MagicMock:
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.return_value = returncode
        proc.pid = 12345
        return proc

    def test_start_launches_subprocess(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()

        assert runner.is_running

    def test_start_twice_raises(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()
            with pytest.raises(RuntimeError, match="already running"):
                runner.start()

    def test_start_passes_stop_file_and_own_process_group(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)

        with patch("subprocess.Popen", return_value=mock_proc) as popen:
            runner.start()

        env = popen.call_args.kwargs["env"]
        assert env["MSW_BONSAI_STOP_FILE"].endswith("-cam0.stop")
        assert env["MSW_BONSAI_PARENT_PID"].isdigit()
        assert popen.call_args.kwargs["creationflags"] == getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )

    def test_stop_requests_graceful_stop(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)
        seen: list[bool] = []
        mock_proc.wait.side_effect = lambda timeout=None: seen.append(
            runner._stop_file is not None and runner._stop_file.exists()
        )

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()
            runner.stop()

        assert seen == [True]  # stop file existed while waiting for Bonsai
        assert runner._stop_file is not None
        assert not runner._stop_file.exists()  # and is cleaned up
        assert runner._layout_file is not None  # flycap-1cam bundles a layout
        assert not runner._layout_file.exists()
        mock_proc.terminate.assert_not_called()
        mock_proc.kill.assert_not_called()

    def test_exited_unexpectedly(self) -> None:
        runner = _make_runner()
        proc = self._mock_process(returncode=None)
        with patch("subprocess.Popen", return_value=proc):
            runner.start()
        assert not runner.exited_unexpectedly
        proc.poll.return_value = 0  # Bonsai gave up after the start window
        assert runner.exited_unexpectedly

    def test_exit_after_stop_is_expected(self) -> None:
        runner = _make_runner()
        proc = self._mock_process(returncode=None)
        with patch("subprocess.Popen", return_value=proc):
            runner.start()
            runner.stop()
        proc.poll.return_value = 0
        assert not runner.exited_unexpectedly

    def test_monitor_flags_its_own_process_only(self) -> None:
        runner = _make_runner()
        old = self._mock_process(returncode=0)
        new = self._mock_process(returncode=None)
        runner._process = new
        runner._monitor(old)  # an old thread after a relaunch
        assert not runner._exited_unexpectedly
        new.poll.return_value = 0
        runner._monitor(new)
        assert runner._exited_unexpectedly

    def test_stop_before_start_is_noop(self) -> None:
        runner = _make_runner()
        runner.stop()  # should not raise

    def test_is_running_false_when_exited(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=0)

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()

        assert not runner.is_running

    def test_pid_none_before_start(self) -> None:
        runner = _make_runner()
        assert runner.pid is None

    def test_pid_set_after_start(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)
        mock_proc.pid = 99

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()

        assert runner.pid == 99

    def test_wait_returns_returncode(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=0)
        mock_proc.wait.return_value = 0

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()

        assert runner.wait(timeout=1) == 0

    def test_wait_returns_none_before_start(self) -> None:
        runner = _make_runner()
        assert runner.wait() is None

    def test_wait_returns_none_on_timeout(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="bonsai", timeout=1)

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()

        assert runner.wait(timeout=0.001) is None

    def test_stop_kills_on_timeout(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="bonsai", timeout=0.001),
            None,
        ]

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()
        # only stop() takes the POSIX branch; start() builds a host-native path
        with patch("msw_flir_bonsai.runner.os.name", "posix"):
            runner.stop(timeout=0.001)

        mock_proc.kill.assert_called_once()

    def test_stop_kills_process_tree_on_windows(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="bonsai", timeout=0.001),
            None,
        ]

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()
        # only stop() takes the Windows branch; start() builds a host-native path
        with (
            patch("subprocess.run") as run,
            patch("msw_flir_bonsai.runner.os.name", "nt"),
        ):
            runner.stop(timeout=0.001)

        run.assert_called_once()
        assert run.call_args.args[0] == ["taskkill", "/F", "/T", "/PID", "12345"]
        mock_proc.kill.assert_not_called()


# ---------------------------------------------------------------------------
# start check: first frame in the .avi


class FakeProc:
    """A Bonsai process that exits ``exit_after`` s after launch (None: never)."""

    def __init__(self, exit_after: float | None = None) -> None:
        self.t0 = time.monotonic()
        self.exit_after = exit_after
        self.pid = 12345

    def poll(self) -> int | None:
        if self.exit_after is None:
            return None
        return 0 if time.monotonic() - self.t0 >= self.exit_after else None

    def wait(self, timeout: float | None = None) -> int:
        self.exit_after = 0.0  # exits cleanly once asked to stop
        return 0


def _grow_avi_later(path: Path, delay: float) -> threading.Timer:
    """Write frames to ``path`` after ``delay`` s, then keep appending."""

    def write() -> None:
        with path.open("ab") as f:
            for _ in range(3):
                f.write(b"\0" * 1024)
                f.flush()
                time.sleep(0.01)

    timer = threading.Timer(delay, write)
    timer.start()
    return timer


class TestFirstFrame:
    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("msw_flir_bonsai.runner._START_POLL_S", 0.005)

    def _runner(self, tmp_path: Path, **kwargs) -> BonsaiCameraRunner:
        return BonsaiCameraRunner(
            workflow="run-flir-spinnaker-1cam",
            acqdir=tmp_path,
            cam_basename="s1.cam0",
            driver="spinnaker",
            bonsai_exe="C:/Bonsai.exe",
            **kwargs,
        )

    def test_avi_path_matches_workflow(self, tmp_path: Path) -> None:
        assert self._runner(tmp_path).avi_path == tmp_path / "s1.cam0.avi"

    def test_started_when_avi_grows(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)
        with patch("subprocess.Popen", return_value=FakeProc()):
            runner.start()
        _grow_avi_later(runner.avi_path, 0.05).join()
        runner.check_started(timeout=5.0)
        assert runner.is_running

    def test_waits_for_avi(self, tmp_path: Path) -> None:
        runner = self._runner(tmp_path)
        with patch("subprocess.Popen", return_value=FakeProc()):
            runner.start()
        timer = _grow_avi_later(runner.avi_path, 0.1)
        runner.check_started(timeout=5.0)
        timer.join()
        assert runner.avi_path.stat().st_size > 0

    def test_exit_without_frame_no_relaunch_when_absent(self, tmp_path) -> None:
        runner = self._runner(tmp_path, retry_delay=0.01)
        with patch("subprocess.Popen", return_value=FakeProc(0.0)) as popen:
            runner.start()
            with pytest.raises(BonsaiStartError, match="no relaunch"):
                runner.check_started(timeout=5.0, expect_present=False)
        assert popen.call_count == 1

    def test_exit_without_frame_relaunches_once(self, tmp_path, caplog) -> None:
        runner = self._runner(tmp_path, retry_delay=0.01)
        procs = [FakeProc(0.0), FakeProc(0.0)]
        with patch("subprocess.Popen", side_effect=procs) as popen:
            runner.start()
            with pytest.raises(BonsaiStartError, match="after one relaunch"):
                runner.check_started(timeout=5.0)
        assert popen.call_count == 2
        assert "attempt 1 of 2" in caplog.text
        assert "attempt 2 of 2" in caplog.text

    def test_relaunch_records(self, tmp_path) -> None:
        runner = self._runner(tmp_path, retry_delay=0.01)
        with patch("subprocess.Popen", side_effect=[FakeProc(0.0), FakeProc()]):
            runner.start()
            timer = _grow_avi_later(runner.avi_path, 0.2)
            runner.check_started(timeout=5.0)
        timer.join()
        assert runner.is_running

    def test_timeout_alive_without_frame_stops(self, tmp_path) -> None:
        runner = self._runner(tmp_path, retry_delay=0.01)
        proc = FakeProc()
        with patch("subprocess.Popen", return_value=proc) as popen:
            runner.start()
            with pytest.raises(BonsaiStartError, match="no frame"):
                runner.check_started(timeout=0.05)
        assert popen.call_count == 1  # no relaunch after a timeout
        assert proc.poll() == 0  # stopped

    def test_stale_avi_is_not_a_frame(self, tmp_path) -> None:
        runner = self._runner(tmp_path)
        runner.avi_path.write_bytes(b"old run")
        with patch("subprocess.Popen", return_value=FakeProc()):
            runner.start()
            with pytest.raises(BonsaiStartError, match="no frame"):
                runner.check_started(timeout=0.05)

    def test_multi_stops_all_when_one_fails(self, tmp_path) -> None:
        ok, bad = self._runner(tmp_path), self._runner(tmp_path / "b")
        (tmp_path / "b").mkdir()
        procs = [FakeProc(), FakeProc(0.0)]
        multi = MultiCameraRunner([ok, bad])
        with patch("subprocess.Popen", side_effect=procs):
            timer = _grow_avi_later(ok.avi_path, 0.02)
            with pytest.raises(BonsaiStartError, match="exited"):
                multi.start(timeout=5.0, expect_present=False)
        timer.join()
        assert not multi.any_running


# ---------------------------------------------------------------------------
# _find_bonsai_exe


class TestFindBonsaiExe:
    def test_explicit_path_returned(self) -> None:
        assert _find_bonsai_exe("C:/Bonsai.exe") == "C:/Bonsai.exe"

    def test_env_var_used_as_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BONSAI_EXE", "C:/env/Bonsai.exe")
        assert _find_bonsai_exe(None) == "C:/env/Bonsai.exe"

    def test_neither_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BONSAI_EXE", raising=False)
        with pytest.raises(OSError, match="BONSAI_EXE"):
            _find_bonsai_exe(None)


# ---------------------------------------------------------------------------
# MultiCameraRunner.from_config


class TestFromConfig:
    def test_always_uses_1cam_workflow(self) -> None:
        multi = MultiCameraRunner.from_config(
            acqdir=_ACQDIR,
            cam_basenames=[_CAM_BASENAME, _CAM_BASENAME.replace("cam0", "cam1")],
            driver="flycap",
            bonsai_exe="C:/Bonsai.exe",
        )
        for runner in multi._runners:
            assert "1cam" in runner._workflow_path.name

    def test_consecutive_cam_indices(self) -> None:
        basenames = [f"stem.mswflir.cam{i}" for i in range(3)]
        multi = MultiCameraRunner.from_config(
            acqdir=_ACQDIR,
            cam_basenames=basenames,
            driver="flycap",
            bonsai_exe="C:/Bonsai.exe",
        )
        indices = [r._cam_index for r in multi._runners]
        assert indices == [0, 1, 2]

    def test_cam_basenames_stored(self) -> None:
        basenames = ["stem.mswflir.top.0", "stem.mswflir.front.1"]
        multi = MultiCameraRunner.from_config(
            acqdir=_ACQDIR,
            cam_basenames=basenames,
            driver="flycap",
            bonsai_exe="C:/Bonsai.exe",
        )
        stored = [r._cam_basename for r in multi._runners]
        assert stored == basenames

    def test_workflow_override(self) -> None:
        multi = MultiCameraRunner.from_config(
            acqdir=_ACQDIR,
            cam_basenames=[_CAM_BASENAME],
            driver="flycap",
            bonsai_exe="C:/Bonsai.exe",
            workflow="run-flir-flycap-1cam",
        )
        assert "flycap" in multi._runners[0]._workflow_path.name

    def test_spinnaker_workflow_name(self) -> None:
        basenames = [f"stem.mswflir.cam{i}" for i in range(2)]
        multi = MultiCameraRunner.from_config(
            acqdir=_ACQDIR,
            cam_basenames=basenames,
            driver="spinnaker",
            bonsai_exe="C:/Bonsai.exe",
        )
        for runner in multi._runners:
            assert "spinnaker" in runner._workflow_path.name

    def test_n_runners_matches_n_basenames(self) -> None:
        for n in (1, 2, 4):
            basenames = [f"stem.mswflir.cam{i}" for i in range(n)]
            multi = MultiCameraRunner.from_config(
                acqdir=_ACQDIR,
                cam_basenames=basenames,
                driver="flycap",
                bonsai_exe="C:/Bonsai.exe",
            )
            assert len(multi) == n


# ---------------------------------------------------------------------------
# workflow_path


class TestWorkflowPath:
    def test_unknown_workflow_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            workflow_path("nonexistent-workflow")


# ---------------------------------------------------------------------------
# MultiCameraRunner start/stop delegates to each runner


class TestMultiCameraRunnerDelegation:
    def _make_multi(self, n: int = 2) -> tuple[MultiCameraRunner, list[MagicMock]]:
        runners = [MagicMock(spec=BonsaiCameraRunner) for _ in range(n)]
        for r in runners:
            r.is_running = True
        multi = MultiCameraRunner(cast("list[BonsaiCameraRunner]", runners))
        return multi, runners

    def test_start_calls_all(self) -> None:
        multi, runners = self._make_multi()
        multi.start()
        for r in runners:
            r.start.assert_called_once()

    def test_start_waits_with_one_shared_deadline(self) -> None:
        multi, runners = self._make_multi()
        multi.start(timeout=2.0, expect_present=False)
        calls = [r.check_started.call_args for r in runners]
        assert all(c.args == (2.0,) for c in calls)
        assert all(c.kwargs["expect_present"] is False for c in calls)
        assert len({c.kwargs["deadline"] for c in calls}) == 1

    def test_start_timeout_zero_skips_wait(self) -> None:
        multi, runners = self._make_multi()
        multi.start(timeout=0)
        for r in runners:
            r.check_started.assert_not_called()

    def test_start_stops_all_on_early_exit(self) -> None:
        multi, runners = self._make_multi()
        runners[1].check_started.side_effect = BonsaiStartError("cam1 exited")
        with pytest.raises(BonsaiStartError, match="cam1 exited"):
            multi.start()
        for r in runners:
            r.stop.assert_called_once()

    def test_exited_unexpectedly_is_any(self) -> None:
        multi, runners = self._make_multi()
        for r in runners:
            r.exited_unexpectedly = False
        assert not multi.exited_unexpectedly
        runners[1].exited_unexpectedly = True
        assert multi.exited_unexpectedly

    def test_stop_calls_all(self) -> None:
        multi, runners = self._make_multi()
        multi.stop()
        for r in runners:
            r.stop.assert_called_once()

    def test_all_running_true_when_all_running(self) -> None:
        multi, runners = self._make_multi()
        for r in runners:
            r.is_running = True
        assert multi.all_running

    def test_all_running_false_when_one_stopped(self) -> None:
        multi, runners = self._make_multi()
        runners[1].is_running = False
        assert not multi.all_running

    def test_any_running_true_when_one_running(self) -> None:
        multi, runners = self._make_multi()
        runners[0].is_running = False
        runners[1].is_running = True
        assert multi.any_running

    def test_any_running_false_when_all_stopped(self) -> None:
        multi, runners = self._make_multi()
        for r in runners:
            r.is_running = False
        assert not multi.any_running
