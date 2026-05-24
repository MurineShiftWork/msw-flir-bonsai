"""BonsaiCameraRunner — launch and manage one Bonsai camera subprocess per camera.

Each camera runs in an isolated subprocess; a crash in one does not affect others.
The Bonsai executable is located via the BONSAI_EXE environment variable or a
direct path argument.  Workflow XMLs are shipped as package data in
``msw_flir_bonsai.bonsai_workflows``.

Typical usage::

    runner = BonsaiCameraRunner(
        bonsai_exe=r"C:\\Users\\user\\AppData\\Local\\Bonsai\\Bonsai.exe",
        workflow="run-flir-flycap-1cam",
        output_dir=r"D:\\DATA\\video",
        session="mouse001__20260518",
        cam_index=0,
        fps=60,
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
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from msw_flir_bonsai.bonsai_workflows import workflow_path

_BONSAI_EXE_ENV = "BONSAI_EXE"
_STARTUP_POLL_INTERVAL = 0.5
_STARTUP_TIMEOUT = 30.0


def _find_bonsai_exe(bonsai_exe: str | Path | None = None) -> str:
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
        output_dir: Root directory under which session subdirectories are created.
        session: Session base name used to name output files and folders.
        cam_index: Camera index passed to the workflow (``cam1idx`` property).
        fps: Target frame rate (FlyCapture only; ignored for Spinnaker).
        driver: ``"flycap"`` or ``"spinnaker"`` — controls which properties are passed.
        extra_props: Additional ``-p key=value`` pairs forwarded to Bonsai CLI.
        startup_timeout: Seconds to wait for the subprocess to start before raising.
    """

    def __init__(
        self,
        workflow: str,
        output_dir: str | Path,
        session: str,
        cam_index: int = 0,
        fps: int = 60,
        driver: str = "flycap",
        bonsai_exe: str | Path | None = None,
        extra_props: dict[str, str] | None = None,
        startup_timeout: float = _STARTUP_TIMEOUT,
    ) -> None:
        self._bonsai_exe = _find_bonsai_exe(bonsai_exe)
        self._workflow_path = workflow_path(workflow)
        self._output_dir = str(output_dir)
        self._session = session
        self._cam_index = cam_index
        self._fps = fps
        self._driver = driver
        self._extra_props = extra_props or {}
        self._startup_timeout = startup_timeout

        self._process: subprocess.Popen | None = None
        self._monitor_thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def _build_cmd(self) -> list[str]:
        props: dict[str, str] = {
            "basepath": f'"{self._output_dir}"',
            "session": f'"{self._session}"',
            "cam1idx": str(self._cam_index),
        }
        if self._driver == "flycap":
            props["cam1fps"] = str(self._fps)
            props["cam1ts"] = "True"
            props["cam1framecounter"] = "True"

        props.update(self._extra_props)

        cmd = [
            self._bonsai_exe,
            str(self._workflow_path),
            "--start",
            "--no-editor",
        ]
        for key, value in props.items():
            cmd += ["-p", f"{key}={value}"]
        return cmd

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            raise RuntimeError("BonsaiCameraRunner is already running.")

        cmd = self._build_cmd()
        logging.info(f"[BonsaiCamera cam{self._cam_index}] Starting: {' '.join(cmd)}")

        self._stopped.clear()
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self._monitor_thread = threading.Thread(
            target=self._monitor,
            daemon=True,
            name=f"bonsai-cam{self._cam_index}-monitor",
        )
        self._monitor_thread.start()
        logging.info(
            f"[BonsaiCamera cam{self._cam_index}] Process started "
            f"(pid={self._process.pid})"
        )

    def _monitor(self) -> None:
        assert self._process is not None
        while not self._stopped.is_set():
            rc = self._process.poll()
            if rc is not None:
                if not self._stopped.is_set():
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
            logging.info(f"[BonsaiCamera cam{self._cam_index}] Terminating process")
            self._process.terminate()
            try:
                self._process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logging.warning(
                    f"[BonsaiCamera cam{self._cam_index}] Terminate timed out; killing"
                )
                self._process.kill()
                self._process.wait()

    def wait(self, timeout: float | None = None) -> int | None:
        """Block until the subprocess exits. Returns the return code."""
        if self._process is None:
            return None
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
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
        n_cameras: int,
        driver: str,
        output_dir: str | Path,
        session: str,
        fps: int = 60,
        bonsai_exe: str | Path | None = None,
        workflow: str = "",
    ) -> MultiCameraRunner:
        """Build one BonsaiCameraRunner per camera index using the 1-cam workflow.

        Each camera runs in its own subprocess; ``n_cameras`` processes are spawned
        with consecutive ``cam_index`` values (0, 1, …).  The 1-cam workflow is used
        so each Bonsai process owns exactly one camera — a crash in one does not
        affect the others.
        """
        resolved_workflow = workflow or f"run-flir-{driver}-1cam"
        runners = [
            BonsaiCameraRunner(
                workflow=resolved_workflow,
                output_dir=output_dir,
                session=session,
                cam_index=i,
                fps=fps,
                driver=driver,
                bonsai_exe=bonsai_exe,
            )
            for i in range(n_cameras)
        ]
        return cls(runners)

    def start(self) -> None:
        for runner in self._runners:
            runner.start()

    def stop(self, timeout: float = 5.0) -> None:
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
        return all(r.is_running for r in self._runners)

    @property
    def any_running(self) -> bool:
        return any(r.is_running for r in self._runners)

    def __len__(self) -> int:
        return len(self._runners)
