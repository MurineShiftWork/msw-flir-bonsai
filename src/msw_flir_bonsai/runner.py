"""BonsaiCameraRunner: launch and manage one Bonsai camera subprocess per camera.

Each camera runs in an isolated subprocess; a crash in one does not affect others.
The Bonsai executable is located via the BONSAI_EXE environment variable or a
direct path argument.  Workflow XMLs are shipped as package data in
``msw_flir_bonsai.bonsai_workflows``.

Typical usage::

    runner = BonsaiCameraRunner(
        bonsai_exe=r"C:\\Users\\user\\AppData\\Local\\Bonsai\\Bonsai.exe",
        workflow="run-flir-flycap-1cam",
        acqdir=r"D:\\DATA\\video\\mouse01__dt\\mouse01__dt__video_flir",
        cam_basename="mouse01__dt__video_flir.mswflir.top.0",
        cam_index=0,
        driver="flycap",
    )
    runner.start()
    # ... task runs ...
    runner.stop()
    runner.wait(timeout=10)
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from msw_flir_bonsai.bonsai_workflows import workflow_path

_BONSAI_EXE_ENV = "BONSAI_EXE"
_STARTUP_POLL_INTERVAL = 0.5
_STARTUP_TIMEOUT = 30.0
# A start succeeds when the camera's .avi has data: the first frame. Not the
# exit code, which is 0 when Bonsai finds no camera (it exits ~4-5 s after
# launch). Not the timestamps CSV, which stays 0 B until its first 4 KB block
# (several seconds of frames). The .avi has data ~5 s after launch.
_START_TIMEOUT_S = 15.0
_START_POLL_S = 0.25
# After a failed launch the driver can hold the camera for a moment; a relaunch
# straight away fails the same way.
_EARLY_EXIT_RETRY_DELAY_S = 3.0


class BonsaiStartError(RuntimeError):
    """A Bonsai camera process did not write its first frame."""


def _find_bonsai_exe(bonsai_exe: str | Path | None = None) -> str:
    """Resolve the Bonsai executable path from the argument or BONSAI_EXE env var."""
    if bonsai_exe:
        return str(bonsai_exe)
    env = os.environ.get(_BONSAI_EXE_ENV, "").strip()
    if env:
        return env
    raise OSError(
        f"Bonsai executable not specified. Pass bonsai_exe= or set {_BONSAI_EXE_ENV}."
    )


class BonsaiCameraRunner:
    """Manage a single Bonsai camera acquisition subprocess.

    Args:
        bonsai_exe: Path to ``Bonsai.exe``. Falls back to ``BONSAI_EXE`` env var.
        workflow: Workflow stem name (without ``.bonsai``),
            e.g. ``"run-flir-flycap-1cam"``.
        acqdir: Full path to the pre-created video_flir acquisition directory.
        cam_basename: Pre-computed file stem for this camera, e.g.
            ``"mouse01__dt__video_flir.mswflir.top.0"``.  The workflow appends
            ``.avi`` / ``.timestamps.csv`` to produce output filenames.
        cam_index: Camera SDK enumeration index (``cam1idx`` workflow property).
        cam_serial: Camera serial number (``cam1serial``, Spinnaker only). When
            set, the Spinnaker node selects the camera by serial and ignores
            the index; empty keeps index selection.
        fps: Target frame rate (FlyCapture only; ignored for Spinnaker).  Frame
            rate should be configured in camera non-volatile memory; only pass
            this for an explicit CLI override.
        driver: ``"flycap"`` or ``"spinnaker"``: controls which properties are passed.
        extra_props: Additional ``-p key=value`` pairs forwarded to Bonsai CLI.
        startup_timeout: Seconds to wait for the subprocess to start before raising.
        start_timeout: Seconds after launch for the first frame to reach the
            ``.avi`` (:meth:`check_started`).
        retry_delay: Seconds to wait before the single relaunch after Bonsai
            exited without a frame (:meth:`check_started`); None disables it.
    """

    def __init__(
        self,
        workflow: str,
        acqdir: str | Path,
        cam_basename: str,
        cam_index: int = 0,
        fps: int | None = None,
        driver: str = "flycap",
        cam_serial: str = "",
        bonsai_exe: str | Path | None = None,
        extra_props: dict[str, str] | None = None,
        startup_timeout: float = _STARTUP_TIMEOUT,
        start_timeout: float = _START_TIMEOUT_S,
        retry_delay: float | None = _EARLY_EXIT_RETRY_DELAY_S,
    ) -> None:
        self._bonsai_exe = _find_bonsai_exe(bonsai_exe)
        self._workflow_path = workflow_path(workflow)
        self._acqdir = str(acqdir)
        self._cam_basename = cam_basename
        self._cam_index = cam_index
        self._cam_serial = cam_serial
        self._fps = fps
        self._driver = driver
        self._extra_props = extra_props or {}
        self._startup_timeout = startup_timeout
        self._start_timeout = start_timeout
        self._avi_before: tuple[int, int] | None = None
        self._retry_delay = retry_delay
        self._exited_unexpectedly = False
        # The monitor stays quiet while check_started runs and about an exit
        # the check has already logged, so one failure is logged exactly once.
        self._checking = False
        self._exit_reported: subprocess.Popen | None = None

        self._process: subprocess.Popen | None = None
        self._monitor_thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._stop_file: Path | None = None
        self._layout_file: Path | None = None
        self._started_at = 0.0

    def _build_cmd(self) -> list[str]:
        """Assemble the Bonsai subprocess command list with all -p property flags."""
        props: dict[str, str] = {
            "acqdir": f'"{self._acqdir}"',
            "cam_basename": f'"{self._cam_basename}"',
            "cam1idx": str(self._cam_index),
        }
        if self._driver == "flycap":
            if self._fps is not None:
                props["cam1fps"] = str(self._fps)
            props["cam1ts"] = "True"
            props["cam1framecounter"] = "True"
        elif self._driver == "spinnaker" and self._cam_serial:
            props["cam1serial"] = self._cam_serial

        props.update(self._extra_props)

        cmd = [
            self._bonsai_exe,
            str(self._workflow_path),
            "--start",
            "--no-editor",
        ]
        layout = self._preview_layout()
        if layout is not None:
            cmd += ["--visualizer-layout", str(layout)]
        for key, value in props.items():
            cmd += ["-p", f"{key}={value}"]
        return cmd

    def _preview_layout(self) -> Path | None:
        """Per-camera copy of the workflow's ``.bonsai.layout`` (live preview window).

        The window is shifted right by camera index so several cameras sit side by
        side. Returns None when the workflow has no layout file.
        """
        template = Path(f"{self._workflow_path}.layout")
        if not template.exists():
            return None
        x = 40 + self._cam_index * 660
        xml = template.read_text(encoding="utf-8").replace(
            "<Location><X>40</X>", f"<Location><X>{x}</X>", 1
        )
        out = (
            Path(tempfile.gettempdir())
            / f"msw-bonsai-{os.getpid()}-cam{self._cam_index}.layout"
        )
        out.write_text(xml, encoding="utf-8")
        self._layout_file = out
        return out

    def start(self) -> None:
        """Launch the Bonsai subprocess and begin background monitoring."""
        if self._process is not None and self._process.poll() is None:
            raise RuntimeError("BonsaiCameraRunner is already running.")

        cmd = self._build_cmd()
        logging.info(f"[BonsaiCamera cam{self._cam_index}] Starting: {' '.join(cmd)}")

        self._stopped.clear()
        self._exited_unexpectedly = False
        self._avi_before = self._avi_stat()
        # Graceful stop: Extensions/key-ctrl-stop.bonsai polls this file and
        # completes the stream once it exists, so the AVI/CSV writers flush and
        # close and Bonsai exits.
        self._stop_file = (
            Path(tempfile.gettempdir())
            / f"msw-bonsai-{os.getpid()}-cam{self._cam_index}.stop"
        )
        self._stop_file.unlink(missing_ok=True)
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                **os.environ,
                "MSW_BONSAI_STOP_FILE": str(self._stop_file),
                "MSW_BONSAI_PARENT_PID": str(os.getpid()),
            },
            # Own process group: a Ctrl+C in the caller's console must not kill
            # Bonsai before the caller has shut down and requested a clean stop.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )

        self._started_at = time.monotonic()
        self._monitor_thread = threading.Thread(
            target=self._monitor,
            args=(self._process,),
            daemon=True,
            name=f"bonsai-cam{self._cam_index}-monitor",
        )
        self._monitor_thread.start()
        logging.info(
            f"[BonsaiCamera cam{self._cam_index}] Process started "
            f"(pid={self._process.pid})"
        )

    @property
    def avi_path(self) -> Path:
        """The video file the workflow writes: ``<acqdir>/<cam_basename>.avi``."""
        return Path(self._acqdir) / f"{self._cam_basename}.avi"

    def _avi_stat(self) -> tuple[int, int] | None:
        try:
            st = self.avi_path.stat()
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns

    def _start_status(self) -> str | None:
        """``""`` once the first frame is written, a failure reason, or None."""
        stat = self._avi_stat()
        # a file left from an earlier run (same size and mtime) is not a frame
        if stat is not None and stat[0] > 0 and stat != self._avi_before:
            return ""
        assert self._process is not None
        rc = self._process.poll()
        if rc is not None:
            self._exit_reported = self._process  # check_started logs it
            age = time.monotonic() - self._started_at
            return (
                f"Bonsai cam{self._cam_index} exited {age:.1f} s after launch "
                f"without a frame (returncode={rc}): camera missing or in use, "
                "or the workflow failed"
            )
        return None

    def check_started(
        self,
        timeout: float | None = None,
        expect_present: bool = True,
        deadline: float | None = None,
    ) -> None:
        """Block until the first frame is in the ``.avi``; raise if it never is.

        Polls every 0.25 s. The start fails when Bonsai exits first (at any
        time; its return code is 0 either way) or when ``timeout`` (default
        ``start_timeout``) passes with Bonsai alive but no frame; Bonsai is
        then stopped. After an exit without a frame Bonsai is relaunched once
        after ``retry_delay`` s, because the camera may not be released yet,
        unless ``expect_present=False`` (the caller knows the camera is
        missing, so the relaunch would fail too). The relaunch gets a fresh
        ``timeout``.

        Args:
            timeout: Seconds from launch to wait for the first frame.
            expect_present: False skips the relaunch.
            deadline: ``time.monotonic()`` deadline for the first attempt,
                shared by several cameras; overrides ``timeout`` for it.

        Raises:
            BonsaiStartError: No frame: Bonsai exited (on the relaunch too)
                or the timeout passed.
        """
        if self._process is None:
            return
        self._checking = True
        try:
            self._check_started(timeout, expect_present, deadline)
        finally:
            self._checking = False

    def _check_started(
        self, timeout: float | None, expect_present: bool, deadline: float | None
    ) -> None:
        if timeout is None:
            timeout = self._start_timeout
        if deadline is None:
            deadline = self._started_at + timeout
        msg = self._wait_first_frame(deadline)
        if msg is None:
            return
        exited = msg.startswith("Bonsai")  # a timeout is never relaunched
        if exited and not expect_present:
            msg += " (no relaunch: camera not detected)"
        if not exited or not expect_present or self._retry_delay is None:
            logging.error(f"[BonsaiCamera cam{self._cam_index}] {msg}")
            raise BonsaiStartError(msg)
        logging.warning(
            f"[BonsaiCamera cam{self._cam_index}] {msg}; attempt 1 of 2, "
            f"relaunching in {self._retry_delay:g} s"
        )
        time.sleep(self._retry_delay)
        self.start()
        msg = self._wait_first_frame(self._started_at + timeout)
        if msg is None:
            logging.info(f"[BonsaiCamera cam{self._cam_index}] Relaunch recording")
            return
        logging.error(f"[BonsaiCamera cam{self._cam_index}] {msg}; attempt 2 of 2")
        raise BonsaiStartError(f"{msg} (after one relaunch)")

    def _wait_first_frame(self, deadline: float) -> str | None:
        """None once a frame is written, else the failure reason.

        Exit reasons start with ``"Bonsai"``; on a timeout Bonsai is stopped
        and the reason starts with ``"no frame"``.
        """
        while True:
            status = self._start_status()
            if status == "":
                return None
            if status is not None:
                return status
            if time.monotonic() >= deadline:
                self.stop()
                return (
                    f"no frame from cam{self._cam_index} in {self.avi_path.name} "
                    "before the start timeout; Bonsai stopped"
                )
            time.sleep(_START_POLL_S)

    def _monitor(self, proc: subprocess.Popen) -> None:
        """Poll ``proc`` in a daemon thread and record an exit before stop.

        Takes the process it watches: after a relaunch the old thread must not
        flag the new process. While check_started runs, and for an exit it
        has logged, the monitor only flags the exit; otherwise it logs it.
        """
        while not self._stopped.is_set() and proc is self._process:
            rc = proc.poll()
            if rc is not None:
                if not self._stopped.is_set() and proc is self._process:
                    self._exited_unexpectedly = True
                    if not self._checking and proc is not self._exit_reported:
                        logging.error(
                            f"[BonsaiCamera cam{self._cam_index}] "
                            f"Process exited unexpectedly (returncode={rc})"
                        )
                break
            time.sleep(0.5)

    def stop(self, timeout: float = 5.0) -> None:
        """Gracefully terminate the Bonsai subprocess."""
        if self._process is None:
            return
        self._stopped.set()
        if self._process.poll() is None:
            logging.info(f"[BonsaiCamera cam{self._cam_index}] Requesting stop")
            if self._stop_file is not None:
                self._stop_file.touch()
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logging.warning(
                    f"[BonsaiCamera cam{self._cam_index}] Stop timed out; killing"
                )
                if os.name == "nt":
                    # Bonsai.exe relaunches itself as a child worker; kill() would only
                    # kill the launcher and orphan the worker. Kill the whole tree.
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._process.pid)],
                        capture_output=True,
                    )
                else:
                    self._process.kill()
                self._process.wait(timeout=5.0)
        for f in (self._stop_file, self._layout_file):
            if f is not None:
                f.unlink(missing_ok=True)

    def wait(self, timeout: float | None = None) -> int | None:
        """Block until the subprocess exits. Returns the return code."""
        if self._process is None:
            return None
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    @property
    def exited_unexpectedly(self) -> bool:
        """True if Bonsai exited before :meth:`stop` was requested.

        Bonsai exits 0 when it cannot record, so this, not the return code,
        tells a caller that the recording failed. Check it before :meth:`stop`.
        """
        if self._exited_unexpectedly:
            return True
        return (
            self._process is not None
            and not self._stopped.is_set()
            and self._process.poll() is not None
        )

    @property
    def is_running(self) -> bool:
        """True if the subprocess exists and has not yet exited."""
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        """OS process ID of the Bonsai subprocess, or None if not started."""
        return self._process.pid if self._process else None


class MultiCameraRunner:
    """Launch and manage multiple independent camera subprocesses.

    Each camera is an independent :class:`BonsaiCameraRunner`.  A crash in one
    camera does not propagate to others.  Cameras are stopped in parallel on
    :meth:`stop`.

    Args:
        runners: Pre-constructed :class:`BonsaiCameraRunner` instances.
    """

    def __init__(self, runners: list[BonsaiCameraRunner]) -> None:
        self._runners = runners

    @classmethod
    def from_config(
        cls,
        acqdir: str | Path,
        cam_basenames: list[str],
        driver: str,
        fps: int | None = None,
        bonsai_exe: str | Path | None = None,
        workflow: str = "",
    ) -> MultiCameraRunner:
        """Build one BonsaiCameraRunner per camera using the 1-cam workflow.

        Each camera runs in its own subprocess.  The 1-cam workflow is used so
        each Bonsai process owns exactly one camera: a crash in one does not
        affect the others.

        Args:
            acqdir: Pre-created video_flir acquisition directory.
            cam_basenames: One pre-computed basename per camera (index order).
            driver: ``"flycap"`` or ``"spinnaker"``.
            fps: Optional FlyCapture frame-rate override.
            bonsai_exe: Path to Bonsai.exe (falls back to BONSAI_EXE env var).
            workflow: Workflow stem; auto-derived as ``run-flir-{driver}-1cam``
                if empty.
        """
        resolved_workflow = workflow or f"run-flir-{driver}-1cam"
        runners = [
            BonsaiCameraRunner(
                workflow=resolved_workflow,
                acqdir=acqdir,
                cam_basename=cam_basenames[i],
                cam_index=i,
                fps=fps,
                driver=driver,
                bonsai_exe=bonsai_exe,
            )
            for i in range(len(cam_basenames))
        ]
        return cls(runners)

    def start(self, timeout: float | None = None, expect_present: bool = True) -> None:
        """Start all camera subprocesses, then wait for every first frame.

        The cameras are waited for in parallel against one shared deadline
        (``timeout`` from now, default each runner's ``start_timeout``).
        ``timeout=0`` skips the wait. ``expect_present`` is passed to
        :meth:`BonsaiCameraRunner.check_started`.

        Raises:
            BonsaiStartError: A camera wrote no frame; all cameras are stopped
                before raising.
        """
        for runner in self._runners:
            runner.start()
        if timeout is not None and timeout <= 0:
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        failed: list[str] = []

        def wait(runner: BonsaiCameraRunner) -> None:
            try:
                runner.check_started(
                    timeout, expect_present=expect_present, deadline=deadline
                )
            except BonsaiStartError as e:
                failed.append(str(e))

        threads = [
            threading.Thread(target=wait, args=(r,), daemon=True) for r in self._runners
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if failed:
            self.stop()
            raise BonsaiStartError("; ".join(failed))

    def stop(self, timeout: float = 5.0) -> None:
        """Stop all camera subprocesses in parallel and wait for them to exit."""
        threads = []
        for runner in self._runners:
            t = threading.Thread(
                target=runner.stop, kwargs={"timeout": timeout}, daemon=True
            )
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=timeout + 2)

    @property
    def all_running(self) -> bool:
        """True if every camera subprocess is still active."""
        return all(r.is_running for r in self._runners)

    @property
    def exited_unexpectedly(self) -> bool:
        """True if any camera's Bonsai exited before stop was requested."""
        return any(r.exited_unexpectedly for r in self._runners)

    @property
    def any_running(self) -> bool:
        """True if at least one camera subprocess is still active."""
        return any(r.is_running for r in self._runners)

    def __len__(self) -> int:
        return len(self._runners)

    def __iter__(self):
        return iter(self._runners)
