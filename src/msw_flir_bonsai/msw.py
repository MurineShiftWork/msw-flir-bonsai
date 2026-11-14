"""FLIR/Bonsai camera backend for the MSW ``msw.camera`` plugin contract.

MSW loads :class:`FlirBonsaiBackend` through the ``msw.camera`` entry point
``flir_bonsai`` and drives one camera group with it: ``configure`` at setup
load, ``preflight`` before the task, ``start`` and ``stop`` around the
recording, ``close`` at the end. Every call that touches cameras returns one
:class:`~msw_plugin_api.camera.UnitResult` per configured camera.

Mechanism:

- ``preflight`` probes the connected cameras (:func:`probe_cameras`) and
  resolves each camera's launch index (and, for Spinnaker, its serial) with
  :func:`resolve_camera_units`. A camera whose serial is not connected, or for
  which no camera is left, is ``missing`` and never launched.
- ``start`` launches one Bonsai process per ready camera with the 1-cam
  workflow and judges each by its first frame
  (:meth:`BonsaiCameraRunner.check_started`), in parallel.
- ``stop`` asks every Bonsai to finish, waits, and judges each camera by its
  exit and its timestamps file, because Bonsai exits 0 when it recorded
  nothing.

Critical choices:

- **Report, never raise, for a camera problem.** Only ``configure`` raises
  (:class:`CameraConfigError`); the host decides what a missing or failed
  camera means for the run.
- **Per-camera isolation.** The probe check runs on the whole group first; if
  it fails, cameras are checked one by one so a missing camera does not take
  the others down. Cameras that pass alone but not together (too few cameras,
  two cameras on one index) are all reported missing.
- **No probe answer means today's behaviour.** Without the SDK or the USB
  device list every camera is ``ready`` and launched by its configured index,
  with ``extra["probe"] == "unavailable"``.
- **File names do not depend on the probe.** A camera's file stem is
  ``<basename>.msw.<name>.<index>`` with the configured index (``cam<index>``
  for the unnamed default cameras), the ``.msw.`` artifact form used for every
  session-derived file, so names stay stable when the resolved index moves.

Where to start reading: :class:`FlirBonsaiBackend`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from msw_plugin_api.camera import (
    UNIT_FAILED,
    UNIT_MISSING,
    UNIT_READY,
    UNIT_RECORDING,
    UNIT_STOPPED,
    CameraConfigError,
    CameraTarget,
    CameraUnitSpec,
    UnitResult,
)

from msw_flir_bonsai import __version__
from msw_flir_bonsai.bonsai_workflows import workflow_path
from msw_flir_bonsai.probe import (
    DRIVERS,
    CameraLaunch,
    CameraMismatchError,
    ProbeResult,
    probe_cameras,
    resolve_camera_units,
)
from msw_flir_bonsai.runner import BonsaiCameraRunner, BonsaiStartError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

log = logging.getLogger(__name__)

_SETTING_KEYS = frozenset({"driver", "bonsai_exe", "workflow", "n_cameras"})
_UNIT_SETTING_KEYS = frozenset({"index"})
_STOP_TIMEOUT_S = 5.0
_EXIT_WAIT_S = 10.0
_PROBE_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class FlirSettings:
    """Validated group settings.

    Attributes:
        driver: ``"flycap"`` or ``"spinnaker"``.
        bonsai_exe: Path to ``Bonsai.exe``; empty uses the ``BONSAI_EXE`` env var.
        workflow: Workflow stem; ``run-flir-<driver>-1cam`` when not set.
        n_cameras: Number of default cameras (``cam0``, ``cam1``, ...) when the
            group lists no units.
    """

    driver: str = "flycap"
    bonsai_exe: str = ""
    workflow: str = ""
    n_cameras: int = 1


@dataclass(frozen=True)
class FlirUnit:
    """One validated camera.

    Attributes:
        name: Camera name from the setup.
        index: Configured SDK index; used when the probe cannot place the serial
            and in the file name.
        serial: Camera serial (``hw_id``), empty when not configured.
        label: Per-camera part of the file stem: ``<name>.<index>``, or
            ``cam<index>`` for the default cameras.
    """

    name: str
    index: int
    serial: str
    label: str


def camera_stem(basename: str, label: str) -> str:
    """Per-camera file stem: ``<basename>.msw.<label>``.

    Same name as ``murineshiftwork.namespace.msw_file(basename, label).name``.
    """
    return f"{Path(basename).name}.msw.{label}"


def parse_settings(settings: Mapping[str, Any]) -> FlirSettings:
    """Validate the group settings; raises :class:`CameraConfigError`."""
    unknown = sorted(set(settings) - _SETTING_KEYS)
    if unknown:
        raise CameraConfigError(
            f"unknown flir_bonsai setting(s) {unknown}; "
            f"expected {sorted(_SETTING_KEYS)}"
        )
    driver = settings.get("driver", "flycap") or "flycap"
    if driver not in DRIVERS:
        raise CameraConfigError(
            f"driver must be one of {list(DRIVERS)}, not {driver!r}"
        )
    bonsai_exe = settings.get("bonsai_exe") or ""
    if not isinstance(bonsai_exe, str | Path):
        raise CameraConfigError(f"bonsai_exe must be a path, not {bonsai_exe!r}")
    workflow = settings.get("workflow") or f"run-flir-{driver}-1cam"
    if not isinstance(workflow, str):
        raise CameraConfigError(f"workflow must be a name, not {workflow!r}")
    try:
        workflow_path(workflow)
    except FileNotFoundError as e:
        raise CameraConfigError(str(e)) from None
    n_cameras = settings.get("n_cameras", 1)
    if isinstance(n_cameras, bool) or not isinstance(n_cameras, int) or n_cameras < 1:
        raise CameraConfigError(f"n_cameras must be an integer >= 1, not {n_cameras!r}")
    return FlirSettings(
        driver=driver,
        bonsai_exe=str(bonsai_exe),
        workflow=workflow,
        n_cameras=n_cameras,
    )


def parse_units(units: list[CameraUnitSpec], n_cameras: int) -> list[FlirUnit]:
    """Validate the cameras; raises :class:`CameraConfigError`.

    Without units, ``n_cameras`` cameras ``cam0..`` at index 0.. are made. A
    unit without ``index`` takes its position in the list.
    """
    if not units:
        return [
            FlirUnit(name=f"cam{i}", index=i, serial="", label=f"cam{i}")
            for i in range(n_cameras)
        ]
    parsed: list[FlirUnit] = []
    for pos, u in enumerate(units):
        name = str(u.name or "").strip()
        if not name:
            raise CameraConfigError(f"camera {pos} has no name")
        unknown = sorted(set(u.settings or {}) - _UNIT_SETTING_KEYS)
        if unknown:
            raise CameraConfigError(
                f"unknown setting(s) {unknown} on camera {name!r}; "
                f"expected {sorted(_UNIT_SETTING_KEYS)}"
            )
        index = (u.settings or {}).get("index", pos)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise CameraConfigError(
                f"camera {name!r}: index must be an integer >= 0, not {index!r}"
            )
        serial = str(u.hw_id or "").strip()
        parsed.append(
            FlirUnit(name=name, index=index, serial=serial, label=f"{name}.{index}")
        )
    for what, values in (
        ("name", [u.name for u in parsed]),
        ("index", [str(u.index) for u in parsed]),
        ("serial", [u.serial for u in parsed if u.serial]),
    ):
        dupes = sorted({v for v in values if values.count(v) > 1})
        if dupes:
            raise CameraConfigError(f"duplicate camera {what}(s): {dupes}")
    return parsed


@dataclass
class _ProbeUnit:
    """The shape :func:`resolve_camera_units` reads."""

    name: str
    index: int
    serial: str


class FlirBonsaiBackend:
    """FLIR cameras recorded by one Bonsai process each.

    Args:
        probe: ``probe(driver) -> ProbeResult``; defaults to
            :func:`probe_cameras` with a 10 s timeout.
        runner_factory: Builds one runner per camera with the
            :class:`BonsaiCameraRunner` keyword arguments; defaults to that
            class.
    """

    acq_type = "video_flir"
    version = __version__

    def __init__(
        self,
        *,
        probe: Callable[[str], ProbeResult] | None = None,
        runner_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._probe = probe or (lambda d: probe_cameras(d, timeout=_PROBE_TIMEOUT_S))
        self._runner_factory = runner_factory or BonsaiCameraRunner
        self._settings = FlirSettings()
        self._units: list[FlirUnit] = []
        self._plan: dict[str, CameraLaunch] = {}
        self._preflight: dict[str, UnitResult] | None = None
        self._runners: dict[str, Any] = {}
        self._files: dict[str, list[str]] = {}
        self._results: dict[str, UnitResult] = {}
        self._stopped: dict[str, UnitResult] | None = None
        self._acqdir = ""

    # ------------------------------------------------------------------
    # configure / preflight

    def configure(
        self, settings: Mapping[str, Any], units: list[CameraUnitSpec]
    ) -> None:
        """Validate and store the settings and cameras.

        Raises:
            CameraConfigError: Unknown or invalid settings, or invalid cameras.
        """
        self._settings = parse_settings(settings)
        self._units = parse_units(list(units), self._settings.n_cameras)
        self._plan = {}
        self._preflight = None
        self._results = {}
        self._stopped = None

    def preflight(self) -> list[UnitResult]:
        """Probe the cameras and resolve each one's launch index.

        Returns:
            Per camera ``ready`` (``extra``: serial, resolved index, probe
            source) or ``missing`` (the probe message as ``error``).
        """
        driver = self._settings.driver
        try:
            probe = self._probe(driver)
        except Exception as e:  # noqa: BLE001  the probe must not break a run
            probe = ProbeResult(driver, error=f"probe raised {e!r}")
        source = probe.source if probe.source != "none" else "unavailable"
        self._plan = {}
        results: dict[str, UnitResult] = {}
        for unit, launch, error in self._resolve(probe):
            extra: dict[str, Any] = {"probe": source}
            if unit.serial:
                extra["serial"] = unit.serial
            if launch is None:
                results[unit.name] = UnitResult(
                    name=unit.name,
                    ok=False,
                    state=UNIT_MISSING,
                    error=error,
                    error_code="not_connected",
                    extra=extra,
                )
                continue
            self._plan[unit.name] = launch
            extra["index"] = launch.index
            results[unit.name] = UnitResult(
                name=unit.name, ok=True, state=UNIT_READY, extra=extra
            )
        self._preflight = results
        self._results = dict(results)
        return self._ordered(results)

    def _resolve(
        self, probe: ProbeResult
    ) -> list[tuple[FlirUnit, CameraLaunch | None, str]]:
        """Launch plan per unit, or None with the reason it is missing."""
        driver = self._settings.driver

        def probe_units(units: list[FlirUnit]) -> list[_ProbeUnit]:
            return [_ProbeUnit(u.name, u.index, u.serial) for u in units]

        try:
            plan = resolve_camera_units(probe_units(self._units), driver, probe)
            return [(u, p, "") for u, p in zip(self._units, plan, strict=True)]
        except CameraMismatchError as e:
            group_error = str(e)
        missing: dict[str, str] = {}
        for u in self._units:
            try:
                resolve_camera_units(probe_units([u]), driver, probe)
            except CameraMismatchError as e:
                missing[u.name] = str(e)
        rest = [u for u in self._units if u.name not in missing]
        resolved: dict[str, CameraLaunch] = {}
        if rest:
            try:
                plan = resolve_camera_units(probe_units(rest), driver, probe)
                resolved = {p.name: p for p in plan}
            except CameraMismatchError as e:
                missing.update({u.name: str(e) for u in rest})
        out: list[tuple[FlirUnit, CameraLaunch | None, str]] = []
        for u in self._units:
            if u.name in resolved:
                out.append((u, resolved[u.name], ""))
            else:
                out.append((u, None, missing.get(u.name, group_error)))
        return out

    # ------------------------------------------------------------------
    # start

    def start(self, target: CameraTarget) -> list[UnitResult]:
        """Launch one Bonsai per ready camera and wait for each first frame.

        Runs :meth:`preflight` first if it has not run. Missing cameras are not
        launched and stay ``missing``.

        Returns:
            Per camera ``recording``, ``failed`` (no first frame, or the launch
            itself failed) or ``missing``.

        Raises:
            RuntimeError: Called again while cameras are recording.
        """
        if self._runners:
            raise RuntimeError("flir_bonsai: start called while recording")
        if self._preflight is None:
            self.preflight()
        assert self._preflight is not None
        self._acqdir = target.acqdir
        self._stopped = None
        self._files = {}
        results: dict[str, UnitResult] = {
            name: r for name, r in self._preflight.items() if r.state == UNIT_MISSING
        }
        s = self._settings
        to_check: dict[str, Any] = {}
        for unit in self._units:
            launch = self._plan.get(unit.name)
            if launch is None:
                continue
            stem = camera_stem(target.basename, unit.label)
            self._files[unit.name] = [f"{stem}.avi", f"{stem}.timestamps.csv"]
            extra = self._extra(unit, launch)
            try:
                runner = self._runner_factory(
                    workflow=s.workflow,
                    acqdir=target.acqdir,
                    cam_basename=stem,
                    cam_index=launch.index,
                    driver=s.driver,
                    cam_serial=launch.serial,
                    bonsai_exe=s.bonsai_exe or None,
                )
                runner.start()
            except Exception as e:  # noqa: BLE001  one camera must not stop the rest
                log.error(f"flir_bonsai: {unit.name} could not launch: {e}")
                results[unit.name] = UnitResult(
                    name=unit.name,
                    ok=False,
                    state=UNIT_FAILED,
                    error=f"launch failed: {e}",
                    error_code="launch_failed",
                    extra=extra,
                )
                continue
            self._runners[unit.name] = runner
            to_check[unit.name] = runner

        errors: dict[str, str] = {}

        def check(name: str, runner: Any) -> None:
            try:
                runner.check_started()
            except BonsaiStartError as e:
                errors[name] = str(e)
            except Exception as e:  # noqa: BLE001
                errors[name] = f"start check failed: {e!r}"

        threads = [
            threading.Thread(target=check, args=item, daemon=True)
            for item in to_check.items()
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for unit in self._units:
            runner = to_check.get(unit.name)
            if runner is None:
                continue
            extra = self._extra(unit, self._plan[unit.name])
            if unit.name in errors:
                self._runners.pop(unit.name, None)
                _safe_stop(runner, _STOP_TIMEOUT_S)
                results[unit.name] = UnitResult(
                    name=unit.name,
                    ok=False,
                    state=UNIT_FAILED,
                    error=errors[unit.name],
                    error_code="no_first_frame",
                    files=self._existing_files(unit.name),
                    extra=extra,
                )
            else:
                results[unit.name] = UnitResult(
                    name=unit.name,
                    ok=True,
                    state=UNIT_RECORDING,
                    files=list(self._files[unit.name]),
                    extra={**extra, "pid": getattr(runner, "pid", None)},
                )
        self._results = dict(results)
        return self._ordered(results)

    # ------------------------------------------------------------------
    # stop / close

    def stop(self, reason: str) -> list[UnitResult]:
        """Stop every recording camera and judge what it saved.

        A camera is ``stopped`` when Bonsai exited after the stop request and
        its timestamps file has data, else ``failed``: it did not exit, it
        exited before the stop (Bonsai exits 0 without recording), or the
        timestamps file is missing or empty. ``extra["returncode"]`` holds the
        exit code. Cameras that never recorded keep their earlier result.
        Calling it again returns the same results.
        """
        if self._stopped is not None:
            return self._ordered(self._stopped)
        log.info(f"flir_bonsai: stopping {sorted(self._runners)} ({reason})")
        runners = dict(self._runners)
        early = {name: bool(r.exited_unexpectedly) for name, r in runners.items()}
        threads = [
            threading.Thread(target=_safe_stop, args=(r, _STOP_TIMEOUT_S), daemon=True)
            for r in runners.values()
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_STOP_TIMEOUT_S + 2)

        results = dict(self._results)
        for unit in self._units:
            runner = runners.get(unit.name)
            if runner is None:
                prev = results.get(unit.name)
                if prev is None or prev.state in (UNIT_READY, UNIT_RECORDING):
                    results[unit.name] = UnitResult(
                        name=unit.name, ok=True, state=UNIT_STOPPED
                    )
                continue
            rc = runner.wait(timeout=_EXIT_WAIT_S)
            extra = {**self._extra(unit, self._plan[unit.name]), "returncode": rc}
            files = self._existing_files(unit.name)
            error, code = "", ""
            if rc is None:
                error, code = "Bonsai did not exit after the stop request", "no_exit"
            elif early[unit.name]:
                error = f"Bonsai exited before the stop (returncode={rc})"
                code = "exited_early"
            elif not self._timestamps_ok(unit.name):
                error = "timestamps file missing or empty"
                code = "empty_timestamps"
            results[unit.name] = UnitResult(
                name=unit.name,
                ok=not error,
                state=UNIT_FAILED if error else UNIT_STOPPED,
                error=error,
                error_code=code,
                files=files,
                extra=extra,
            )
            if error:
                log.error(f"flir_bonsai: {unit.name}: {error}")
        self._runners = {
            n: r for n, r in runners.items() if getattr(r, "is_running", False)
        }
        self._results = dict(results)
        self._stopped = results
        return self._ordered(results)

    def close(self) -> None:
        """Kill any Bonsai still running. Safe to call repeatedly."""
        for name, runner in list(self._runners.items()):
            if getattr(runner, "is_running", False):
                log.warning(f"flir_bonsai: killing {name} on close")
                _safe_stop(runner, 0.0)
        self._runners = {}

    # ------------------------------------------------------------------
    # helpers

    def _ordered(self, results: dict[str, UnitResult]) -> list[UnitResult]:
        return [results[u.name] for u in self._units if u.name in results]

    def _extra(self, unit: FlirUnit, launch: CameraLaunch) -> dict[str, Any]:
        extra: dict[str, Any] = {"index": launch.index}
        if unit.serial:
            extra["serial"] = unit.serial
        return extra

    def _existing_files(self, name: str) -> list[str]:
        root = Path(self._acqdir)
        return [f for f in self._files.get(name, []) if (root / f).is_file()]

    def _timestamps_ok(self, name: str) -> bool:
        ts = Path(self._acqdir) / self._files[name][1]
        try:
            return ts.stat().st_size > 0
        except OSError:
            return False


def _safe_stop(runner: Any, timeout: float) -> None:
    try:
        runner.stop(timeout=timeout)
    except Exception:  # noqa: BLE001  stopping one camera must not stop the rest
        log.warning("flir_bonsai: runner stop failed", exc_info=True)
