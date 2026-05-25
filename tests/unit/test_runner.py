"""Unit tests for BonsaiCameraRunner and MultiCameraRunner.

All subprocess calls are mocked — no Bonsai installation required.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from msw_flir_bonsai.bonsai_workflows import workflow_path
from msw_flir_bonsai.runner import (
    BonsaiCameraRunner,
    MultiCameraRunner,
    _find_bonsai_exe,
)

# ---------------------------------------------------------------------------
# Helpers


def _make_runner(
    workflow: str = "run-flir-flycap-1cam",
    output_dir: str = "D:/DATA/video",
    session: str = "test",
    cam_index: int = 0,
    fps: int = 60,
    driver: str = "flycap",
    bonsai_exe: str = "C:/Bonsai/Bonsai.exe",
) -> BonsaiCameraRunner:
    return BonsaiCameraRunner(
        workflow=workflow,
        output_dir=output_dir,
        session=session,
        cam_index=cam_index,
        fps=fps,
        driver=driver,
        bonsai_exe=bonsai_exe,
    )


# ---------------------------------------------------------------------------
# _build_cmd


class TestBuildCmd:
    def test_flycap_includes_fps_and_ts(self) -> None:
        runner = _make_runner(driver="flycap", fps=30)
        cmd = runner._build_cmd()
        joined = " ".join(cmd)
        assert "cam1fps=30" in joined
        assert "cam1ts=True" in joined
        assert "cam1framecounter=True" in joined

    def test_spinnaker_omits_fps(self) -> None:
        runner = _make_runner(driver="spinnaker")
        cmd = runner._build_cmd()
        joined = " ".join(cmd)
        assert "cam1fps" not in joined
        assert "cam1ts" not in joined

    def test_cam_index_passed(self) -> None:
        runner = _make_runner(cam_index=2)
        cmd = runner._build_cmd()
        assert "cam1idx=2" in " ".join(cmd)

    def test_start_and_no_editor_flags(self) -> None:
        runner = _make_runner()
        cmd = runner._build_cmd()
        assert "--start" in cmd
        assert "--no-editor" in cmd

    def test_basepath_and_session_quoted(self) -> None:
        runner = _make_runner(output_dir="D:/DATA/video", session="mouse001")
        cmd = runner._build_cmd()
        joined = " ".join(cmd)
        assert '"D:/DATA/video"' in joined
        assert '"mouse001"' in joined

    def test_extra_props_forwarded(self) -> None:
        runner = BonsaiCameraRunner(
            workflow="run-flir-flycap-1cam",
            output_dir="D:/DATA",
            session="s1",
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

    def test_stop_calls_terminate(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()
            runner.stop()

        mock_proc.terminate.assert_called_once()

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

    def test_stop_kills_on_terminate_timeout(self) -> None:
        runner = _make_runner()
        mock_proc = self._mock_process(returncode=None)
        mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="bonsai", timeout=0.001),
            None,
        ]

        with patch("subprocess.Popen", return_value=mock_proc):
            runner.start()
            runner.stop(timeout=0.001)

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()


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
            n_cameras=3,
            driver="flycap",
            output_dir="D:/DATA",
            session="s",
            bonsai_exe="C:/Bonsai.exe",
        )
        for runner in multi._runners:
            assert "1cam" in runner._workflow_path.name

    def test_consecutive_cam_indices(self) -> None:
        multi = MultiCameraRunner.from_config(
            n_cameras=3,
            driver="flycap",
            output_dir="D:/DATA",
            session="s",
            bonsai_exe="C:/Bonsai.exe",
        )
        indices = [r._cam_index for r in multi._runners]
        assert indices == [0, 1, 2]

    def test_workflow_override(self) -> None:
        multi = MultiCameraRunner.from_config(
            n_cameras=1,
            driver="flycap",
            output_dir="D:/DATA",
            session="s",
            bonsai_exe="C:/Bonsai.exe",
            workflow="run-flir-flycap-1cam",
        )
        assert "flycap" in multi._runners[0]._workflow_path.name

    def test_spinnaker_workflow_name(self) -> None:
        multi = MultiCameraRunner.from_config(
            n_cameras=2,
            driver="spinnaker",
            output_dir="D:/DATA",
            session="s",
            bonsai_exe="C:/Bonsai.exe",
        )
        for runner in multi._runners:
            assert "spinnaker" in runner._workflow_path.name

    def test_n_runners_matches_n_cameras(self) -> None:
        for n in (1, 2, 4):
            multi = MultiCameraRunner.from_config(
                n_cameras=n,
                driver="flycap",
                output_dir="D:/DATA",
                session="s",
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
        multi = MultiCameraRunner(runners)  # type: ignore[arg-type]
        return multi, runners

    def test_start_calls_all(self) -> None:
        multi, runners = self._make_multi()
        multi.start()
        for r in runners:
            r.start.assert_called_once()

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
