"""FlirBonsaiBackend with a fake probe and fake runners: no Bonsai, no cameras."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from msw_plugin_api.camera import (
    CameraBackend,
    CameraConfigError,
    CameraTarget,
    CameraUnitSpec,
)

from msw_flir_bonsai import __version__
from msw_flir_bonsai.msw import FlirBonsaiBackend, camera_stem
from msw_flir_bonsai.probe import CameraInfo, ProbeResult
from msw_flir_bonsai.runner import BonsaiStartError

_BASENAME = "mouse01__20260518_120000__video_flir"


def _sdk(*serials: str, driver: str = "flycap") -> ProbeResult:
    cams = [CameraInfo(index=i, serial=s) for i, s in enumerate(serials)]
    return ProbeResult(driver, source="sdk", count=len(cams), cameras=cams)


class FakeRunner:
    """Records its kwargs; behaviour set per camera by ``FakeFactory``."""

    def __init__(self, factory: FakeFactory, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.name = kwargs["cam_basename"].rsplit(".msw.", 1)[1].split(".")[0]
        self.mode = factory.modes.get(self.name, "ok")
        self.acqdir = Path(kwargs["acqdir"])
        self.stem = kwargs["cam_basename"]
        self.running = False
        self.stop_calls: list[float] = []
        self.pid = 1000 + kwargs["cam_index"]

    def start(self) -> None:
        if self.mode == "launch_error":
            raise OSError("Bonsai.exe not found")
        self.running = True

    def check_started(self) -> None:
        if self.mode == "no_frame":
            self.running = False
            raise BonsaiStartError("Bonsai cam exited without a frame")
        (self.acqdir / f"{self.stem}.avi").write_bytes(b"frame")

    @property
    def exited_unexpectedly(self) -> bool:
        return self.mode == "early_exit"

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_calls.append(timeout)
        if self.mode == "hang" and timeout > 0:
            return
        if self.running and self.mode != "empty_ts":
            (self.acqdir / f"{self.stem}.timestamps.csv").write_text("1,2,3\n")
        self.running = False

    def wait(self, timeout: float | None = None) -> int | None:
        return None if self.running else 0

    @property
    def is_running(self) -> bool:
        return self.running


class FakeFactory:
    def __init__(self, **modes: str) -> None:
        self.modes = modes
        self.runners: dict[str, FakeRunner] = {}

    def __call__(self, **kwargs: Any) -> FakeRunner:
        r = FakeRunner(self, **kwargs)
        self.runners[r.name] = r
        return r


def _units(*specs: tuple[str, int, str]) -> list[CameraUnitSpec]:
    return [CameraUnitSpec(name=n, hw_id=s, settings={"index": i}) for n, i, s in specs]


def _backend(
    probe: ProbeResult, factory: FakeFactory | None = None
) -> FlirBonsaiBackend:
    return FlirBonsaiBackend(
        probe=lambda _driver: probe, runner_factory=factory or FakeFactory()
    )


def _target(tmp_path: Path) -> CameraTarget:
    return CameraTarget(acqdir=str(tmp_path), basename=_BASENAME)


def _by_name(results: list[Any]) -> dict[str, Any]:
    return {r.name: r for r in results}


# ---------------------------------------------------------------------------
# contract and packaging


def test_satisfies_camera_backend_protocol() -> None:
    backend = FlirBonsaiBackend()
    assert isinstance(backend, CameraBackend)
    assert backend.acq_type == "video_flir"
    assert backend.version == __version__


def test_entry_point_declared() -> None:
    root = Path(__file__).resolve().parents[2]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    eps = data["project"]["entry-points"]["msw.camera"]
    assert eps == {"flir_bonsai": "msw_flir_bonsai.msw:FlirBonsaiBackend"}


def test_camera_stem_matches_msw_file_naming() -> None:
    # msw_file(basename, "top.0").name == "<basename>.msw.top.0"
    assert camera_stem(_BASENAME, "top.0") == f"{_BASENAME}.msw.top.0"
    assert camera_stem(f"D:/data/{_BASENAME}", "cam1") == f"{_BASENAME}.msw.cam1"


# ---------------------------------------------------------------------------
# configure


def test_configure_defaults_to_n_cameras_units(tmp_path: Path) -> None:
    factory = FakeFactory()
    backend = _backend(_sdk("1", "2"), factory)
    backend.configure({"n_cameras": 2}, [])
    results = backend.start(_target(tmp_path))
    assert [r.name for r in results] == ["cam0", "cam1"]
    assert factory.runners["cam1"].kwargs["cam_basename"] == f"{_BASENAME}.msw.cam1"
    assert factory.runners["cam0"].kwargs["workflow"] == "run-flir-flycap-1cam"


@pytest.mark.parametrize(
    ("settings", "match"),
    [
        ({"driver": "usb3"}, "driver"),
        ({"n_cameras": 0}, "n_cameras"),
        ({"n_cameras": True}, "n_cameras"),
        ({"workflow": "no-such-workflow"}, "no-such-workflow"),
        ({"fps": 30, "colour": "red"}, r"\['colour', 'fps'\]"),
    ],
)
def test_configure_rejects_bad_settings(settings: dict, match: str) -> None:
    with pytest.raises(CameraConfigError, match=match):
        FlirBonsaiBackend().configure(settings, [])


@pytest.mark.parametrize(
    ("units", "match"),
    [
        (_units(("top", -1, "")), "index"),
        ([CameraUnitSpec("top", settings={"index": "0"})], "index"),
        ([CameraUnitSpec("top", settings={"idx": 0})], r"\['idx'\]"),
        (_units(("top", 0, ""), ("top", 1, "")), "name"),
        (_units(("top", 0, ""), ("back", 0, "")), "index"),
        (_units(("top", 0, "7"), ("back", 1, "7")), "serial"),
        ([CameraUnitSpec("")], "no name"),
    ],
)
def test_configure_rejects_bad_units(units: list, match: str) -> None:
    with pytest.raises(CameraConfigError, match=match):
        FlirBonsaiBackend().configure({}, units)


# ---------------------------------------------------------------------------
# preflight


def test_preflight_resolves_serial_to_index() -> None:
    backend = _backend(_sdk("200", "100"))
    backend.configure({}, _units(("top", 0, "100"), ("back", 1, "200")))
    res = _by_name(backend.preflight())
    assert res["top"].state == "ready"
    assert res["top"].extra == {"probe": "sdk", "serial": "100", "index": 1}
    assert res["back"].extra["index"] == 0


def test_preflight_missing_serial_keeps_the_other() -> None:
    backend = _backend(_sdk("100"))
    backend.configure({}, _units(("top", 0, "100"), ("back", 1, "200")))
    res = _by_name(backend.preflight())
    assert res["top"].ok and res["top"].state == "ready"
    assert not res["back"].ok and res["back"].state == "missing"
    assert "200" in res["back"].error
    assert res["back"].error_code == "not_connected"


def test_preflight_fewer_cameras_than_units() -> None:
    backend = _backend(_sdk("100"))
    backend.configure({}, _units(("top", 0, ""), ("back", 1, "")))
    res = _by_name(backend.preflight())
    assert res["top"].state == "ready"
    assert res["back"].state == "missing"
    assert "index 1" in res["back"].error


@pytest.mark.parametrize("raises", [False, True])
def test_preflight_probe_unavailable_launches_by_index(raises: bool) -> None:
    def probe(driver: str) -> ProbeResult:
        if raises:
            raise RuntimeError("boom")
        return ProbeResult(driver, error="no SDK, no device list")

    backend = FlirBonsaiBackend(probe=probe, runner_factory=FakeFactory())
    backend.configure({}, _units(("top", 3, "100")))
    (res,) = backend.preflight()
    assert res.state == "ready"
    assert res.extra["probe"] == "unavailable"
    assert res.extra["index"] == 3


def test_spinnaker_launches_by_serial(tmp_path: Path) -> None:
    factory = FakeFactory()
    backend = _backend(_sdk("100", driver="spinnaker"), factory)
    backend.configure({"driver": "spinnaker"}, _units(("top", 0, "100")))
    backend.start(_target(tmp_path))
    kw = factory.runners["top"].kwargs
    assert kw["cam_serial"] == "100"
    assert kw["workflow"] == "run-flir-spinnaker-1cam"


# ---------------------------------------------------------------------------
# start


def test_start_failure_of_one_camera_keeps_the_other(tmp_path: Path) -> None:
    factory = FakeFactory(back="no_frame")
    backend = _backend(_sdk("100", "200"), factory)
    backend.configure({}, _units(("top", 0, "100"), ("back", 1, "200")))
    res = _by_name(backend.start(_target(tmp_path)))
    assert res["top"].state == "recording" and res["top"].ok
    assert res["top"].files == [
        f"{_BASENAME}.msw.top.0.avi",
        f"{_BASENAME}.msw.top.0.timestamps.csv",
    ]
    assert res["back"].state == "failed"
    assert res["back"].error_code == "no_first_frame"
    assert "without a frame" in res["back"].error


def test_start_launch_error_is_a_failed_unit(tmp_path: Path) -> None:
    factory = FakeFactory(top="launch_error")
    backend = _backend(_sdk("100", "200"), factory)
    backend.configure({}, _units(("top", 0, ""), ("back", 1, "")))
    res = _by_name(backend.start(_target(tmp_path)))
    assert res["top"].state == "failed"
    assert res["top"].error_code == "launch_failed"
    assert res["back"].state == "recording"


def test_start_does_not_launch_missing_camera(tmp_path: Path) -> None:
    factory = FakeFactory()
    backend = _backend(_sdk("100"), factory)
    backend.configure({}, _units(("top", 0, "100"), ("back", 1, "200")))
    res = _by_name(backend.start(_target(tmp_path)))
    assert res["back"].state == "missing"
    assert set(factory.runners) == {"top"}


# ---------------------------------------------------------------------------
# stop / close


def test_stop_states(tmp_path: Path) -> None:
    factory = FakeFactory(back="empty_ts", side="early_exit", front="no_frame")
    backend = _backend(_sdk("1", "2", "3", "4"), factory)
    backend.configure(
        {},
        _units(("top", 0, ""), ("back", 1, ""), ("side", 2, ""), ("front", 3, "")),
    )
    backend.start(_target(tmp_path))
    res = _by_name(backend.stop("session end"))
    assert res["top"].state == "stopped" and res["top"].ok
    assert res["top"].extra["returncode"] == 0
    assert len(res["top"].files) == 2
    assert res["back"].state == "failed"
    assert res["back"].error_code == "empty_timestamps"
    assert res["side"].error_code == "exited_early"
    assert res["front"].state == "failed"
    assert res["front"].error_code == "no_first_frame"


def test_stop_reports_a_camera_that_does_not_exit(tmp_path: Path) -> None:
    factory = FakeFactory(top="hang")
    backend = _backend(_sdk("1"), factory)
    backend.configure({}, _units(("top", 0, "")))
    backend.start(_target(tmp_path))
    (res,) = backend.stop("end")
    assert res.state == "failed"
    assert res.error_code == "no_exit"
    assert res.extra["returncode"] is None
    backend.close()
    assert factory.runners["top"].stop_calls[-1] == 0.0
    assert not factory.runners["top"].is_running


def test_stop_and_close_are_idempotent(tmp_path: Path) -> None:
    factory = FakeFactory()
    backend = _backend(_sdk("1"), factory)
    backend.configure({}, _units(("top", 0, "")))
    backend.start(_target(tmp_path))
    first = backend.stop("end")
    assert backend.stop("again") == first
    assert len(factory.runners["top"].stop_calls) == 1
    backend.close()
    backend.close()


def test_stop_without_start() -> None:
    backend = _backend(_sdk("1"))
    backend.configure({}, _units(("top", 0, "")))
    (res,) = backend.stop("abort")
    assert res.state == "stopped" and res.ok
    backend.close()
