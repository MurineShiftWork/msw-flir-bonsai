"""Unit tests for msw_flir_bonsai.probe: fake DLLs and subprocess results only."""

from __future__ import annotations

import ctypes
import json
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from msw_flir_bonsai import probe
from msw_flir_bonsai.probe import (
    CameraInfo,
    CameraMismatchError,
    ProbeResult,
    parse_usb_ids,
    probe_cameras,
    probe_sdk,
    resolve_camera_units,
)

# ---------------------------------------------------------------------------
# Fake vendor libraries


def _out(arg: Any) -> Any:
    """The ctypes object behind a ``byref()`` argument."""
    return arg._obj


class FakeLib:
    """Records calls; every attribute is a plain function, like a CDLL symbol."""

    def __init__(self, **functions: Any) -> None:
        self.calls: list[str] = []
        for name, fn in functions.items():
            setattr(self, name, self._wrap(name, fn))

    def _wrap(self, name: str, fn: Any) -> Any:
        def call(*args: Any) -> int:
            self.calls.append(name)
            return int(fn(*args))

        call.argtypes = None  # type: ignore[attr-defined]
        call.restype = None  # type: ignore[attr-defined]
        return call


def _fc2_lib(serials: list[int], fail: str = "") -> FakeLib:
    def create(ctx: Any) -> int:
        _out(ctx).value = 1234
        return 0

    def num(ctx: Any, n: Any) -> int:
        _out(n).value = len(serials)
        return 7 if fail == "num" else 0

    def serial_at(ctx: Any, i: int, sn: Any) -> int:
        _out(sn).value = serials[i]
        return 0

    return FakeLib(
        fc2CreateContext=create,
        fc2GetNumOfCameras=num,
        fc2GetCameraSerialNumberFromIndex=serial_at,
        fc2DestroyContext=lambda ctx: 0,
    )


def _spin_lib(serials: list[str], unreadable: int = -1) -> FakeLib:
    state: dict[str, int] = {}

    def set_handle(p: Any, value: int) -> int:
        _out(p).value = value
        return 0

    def list_get(cam_list: Any, i: int, cam: Any) -> int:
        state["cam"] = i
        return set_handle(cam, 100 + i)

    def get_string(node: Any, buf: Any, length: Any) -> int:
        if state["cam"] == unreadable:
            return -1002
        buf.value = serials[state["cam"]].encode()
        _out(length).value = len(serials[state["cam"]]) + 1
        return 0

    def get_size(cam_list: Any, n: Any) -> int:
        _out(n).value = len(serials)
        return 0

    return FakeLib(
        spinSystemGetInstance=lambda p: set_handle(p, 1),
        spinCameraListCreateEmpty=lambda p: set_handle(p, 2),
        spinSystemGetCameras=lambda system, cam_list: 0,
        spinCameraListGetSize=get_size,
        spinCameraListGet=list_get,
        spinCameraGetTLDeviceNodeMap=lambda cam, p: set_handle(p, 3),
        spinNodeMapGetNode=lambda nodemap, name, p: set_handle(p, 4),
        spinStringGetValue=get_string,
        spinCameraRelease=lambda cam: 0,
        spinCameraListClear=lambda cam_list: 0,
        spinCameraListDestroy=lambda cam_list: 0,
        spinSystemReleaseInstance=lambda system: 0,
    )


def _loader(lib: Any) -> Any:
    return lambda driver: (lib, "")


# ---------------------------------------------------------------------------
# probe_sdk


class TestProbeSdk:
    def test_flycap_index_and_serial(self) -> None:
        lib = _fc2_lib([12345678, 87654321])
        result = probe_sdk("flycap", loader=_loader(lib))
        assert result.source == "sdk"
        assert result.count == 2
        assert result.cameras == [
            CameraInfo(0, "12345678"),
            CameraInfo(1, "87654321"),
        ]
        assert lib.calls[-1] == "fc2DestroyContext"

    def test_flycap_signatures_declared(self) -> None:
        lib: Any = _fc2_lib([12345678])
        probe_sdk("flycap", loader=_loader(lib))
        assert lib.fc2CreateContext.restype is ctypes.c_int
        assert lib.fc2GetCameraSerialNumberFromIndex.argtypes[1] is ctypes.c_uint

    def test_flycap_error_code_is_unavailable_and_context_destroyed(self) -> None:
        lib = _fc2_lib([12345678], fail="num")
        result = probe_sdk("flycap", loader=_loader(lib))
        assert result.source == "none"
        assert "fc2GetNumOfCameras returned error 7" in result.error
        assert lib.calls[-1] == "fc2DestroyContext"

    def test_spinnaker_serials_without_init(self) -> None:
        lib = _spin_lib(["12345678", "87654321"])
        result = probe_sdk("spinnaker", loader=_loader(lib))
        assert result.source == "sdk"
        assert [c.serial for c in result.cameras] == ["12345678", "87654321"]
        assert "spinCameraInit" not in lib.calls
        # every function has an explicit signature (handles are pointers)
        for name in vars(lib):
            if name.startswith("spin"):
                assert getattr(lib, name).restype is ctypes.c_int, name
                assert getattr(lib, name).argtypes is not None, name

    def test_spinnaker_releases_in_reverse_order(self) -> None:
        lib = _spin_lib(["12345678"])
        probe_sdk("spinnaker", loader=_loader(lib))
        assert lib.calls[-4:] == [
            "spinCameraRelease",
            "spinCameraListClear",
            "spinCameraListDestroy",
            "spinSystemReleaseInstance",
        ]

    def test_spinnaker_unreadable_serial_still_counted(self) -> None:
        lib = _spin_lib(["12345678", "87654321"], unreadable=1)
        result = probe_sdk("spinnaker", loader=_loader(lib))
        assert result.count == 2
        assert result.cameras[1] == CameraInfo(1, "")

    def test_missing_symbol_is_unavailable(self) -> None:
        result = probe_sdk("flycap", loader=_loader(SimpleNamespace()))
        assert result.source == "none"
        assert "AttributeError" in result.error

    def test_library_not_found(self) -> None:
        result = probe_sdk("flycap", loader=lambda d: (None, "not found"))
        assert result == ProbeResult("flycap", error="not found")

    def test_unknown_driver(self) -> None:
        assert "unknown driver" in probe_sdk("pylon").error


class TestLoadLibrary:
    def test_nothing_found_names_env_var(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv(probe.FC2_DLL_ENV, raising=False)
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
        with patch("ctypes.util.find_library", return_value=None):
            lib, reason = probe._load_library("flycap")
        assert lib is None
        assert probe.FC2_DLL_ENV in reason

    def test_bad_env_path_is_reported_not_raised(self, monkeypatch, tmp_path) -> None:
        bad = tmp_path / "SpinnakerC_v140.dll"
        bad.write_bytes(b"not a dll")
        monkeypatch.setenv(probe.SPIN_DLL_ENV, str(bad))
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
        with patch("ctypes.util.find_library", return_value=None):
            lib, reason = probe._load_library("spinnaker")
        assert lib is None
        assert str(bad) in reason

    def test_install_dir_searched(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv(probe.FC2_DLL_ENV, raising=False)
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
        dll = tmp_path / "Point Grey Research" / "FlyCapture2" / "bin64"
        dll.mkdir(parents=True)
        (dll / "FlyCapture2_C.dll").touch()
        with (
            patch("ctypes.util.find_library", return_value=None),
            patch("ctypes.CDLL", return_value="lib") as cdll,
        ):
            assert probe._load_library("flycap") == ("lib", "")
        assert cdll.call_args.args[0] == str(dll / "FlyCapture2_C.dll")


# ---------------------------------------------------------------------------
# USB fallback


# parent composite devices carry the hex serial; MI_ children a Windows id
_PNP = [
    r"USB\VID_1E10&PID_3300\00BC614E",
    r"USB\VID_1E10&PID_3300&MI_00\6&1A2B3C4D&0&0000",
    r"USB\VID_1E10&PID_3300\00bc614e",
    r"USB\VID_1E10&PID_3300\05397FB1",
    r"USB\VID_1E10&PID_4000\5&2A3B4C&0&3",
    r"USB\VID_046D&PID_C077\00000001",
    "",
]


def test_parse_usb_ids_hex_to_decimal() -> None:
    assert parse_usb_ids(_PNP) == [
        CameraInfo(None, "12345678"),
        CameraInfo(None, "87654321"),
    ]


class TestProbeUsb:
    def test_off_windows_is_unavailable(self) -> None:
        with patch("msw_flir_bonsai.probe.os.name", "posix"):
            assert probe.probe_usb("flycap").source == "none"

    def test_windows_counts_devices(self) -> None:
        done = subprocess.CompletedProcess([], 0, stdout="\n".join(_PNP), stderr="")
        with (
            patch("msw_flir_bonsai.probe.os.name", "nt"),
            patch("subprocess.run", return_value=done),
        ):
            result = probe.probe_usb("flycap")
        assert (result.source, result.count) == ("usb", 2)

    def test_powershell_failure_is_unavailable(self) -> None:
        with (
            patch("msw_flir_bonsai.probe.os.name", "nt"),
            patch("subprocess.run", side_effect=FileNotFoundError("powershell")),
        ):
            assert probe.probe_usb("flycap").source == "none"


# ---------------------------------------------------------------------------
# Child process and fallback chain


def _completed(rc: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr="")


_SDK_JSON = json.dumps(
    ProbeResult("flycap", "sdk", 1, [CameraInfo(0, "12345678")]).to_dict()
)


class TestProbeInChild:
    def test_parses_last_json_line(self) -> None:
        out = "vendor banner\n" + _SDK_JSON + "\n"
        with patch("subprocess.run", return_value=_completed(stdout=out)) as run:
            result = probe._probe_in_child("flycap", 5.0)
        assert result.cameras == [CameraInfo(0, "12345678")]
        assert run.call_args.args[0][1:4] == ["-I", "-m", "msw_flir_bonsai.probe"]

    def test_access_violation(self) -> None:
        with patch("subprocess.run", return_value=_completed(rc=3221225477)):
            result = probe._probe_in_child("flycap", 5.0)
        assert result.source == "none"
        assert "0xc0000005" in result.error

    def test_negative_exit(self) -> None:
        with patch("subprocess.run", return_value=_completed(rc=-11)):
            assert "crashed" in probe._probe_in_child("flycap", 5.0).error

    def test_timeout(self) -> None:
        err = subprocess.TimeoutExpired(cmd="python", timeout=5.0)
        with patch("subprocess.run", side_effect=err):
            assert "timed out" in probe._probe_in_child("flycap", 5.0).error

    def test_bad_output(self) -> None:
        with patch("subprocess.run", return_value=_completed(stdout="{oops")):
            assert "unreadable" in probe._probe_in_child("flycap", 5.0).error

    def test_real_child_without_sdk(self, monkeypatch, tmp_path) -> None:
        # nothing on CI provides the vendor DLL: the child reports it, no crash
        monkeypatch.setenv(probe.FC2_DLL_ENV, str(tmp_path / "missing.dll"))
        result = probe._probe_in_child("flycap", 60.0)
        assert result.driver == "flycap"
        assert result.source in ("none", "sdk")


class TestProbeCameras:
    def test_sdk_answer_wins(self) -> None:
        sdk = ProbeResult("flycap", "sdk", 0)
        with (
            patch.object(probe, "_probe_in_child", return_value=sdk),
            patch.object(probe, "probe_usb") as usb,
        ):
            assert probe_cameras("flycap") is sdk
        usb.assert_not_called()

    def test_falls_back_to_usb(self) -> None:
        sdk = ProbeResult("flycap", error="SDK probe crashed")
        usb = ProbeResult("flycap", "usb", 2, [CameraInfo(None, "12345678")])
        with (
            patch.object(probe, "_probe_in_child", return_value=sdk),
            patch.object(probe, "probe_usb", return_value=usb),
        ):
            result = probe_cameras("flycap")
        assert (result.source, result.count) == ("usb", 2)
        assert "crashed" in result.error

    def test_none_when_both_fail(self) -> None:
        with (
            patch.object(
                probe, "_probe_in_child", return_value=ProbeResult("flycap", error="a")
            ),
            patch.object(
                probe, "probe_usb", return_value=ProbeResult("flycap", error="b")
            ),
        ):
            result = probe_cameras("flycap")
        assert (result.source, result.error) == ("none", "a; b")

    def test_unknown_driver_skips_child(self) -> None:
        with patch.object(probe, "_probe_in_child") as child:
            assert probe_cameras("pylon").source == "none"
        child.assert_not_called()


def test_main_prints_json(capsys) -> None:
    with patch.object(probe, "probe_sdk", return_value=ProbeResult("spinnaker")):
        probe.main(["--driver", "spinnaker"])
    assert json.loads(capsys.readouterr().out)["source"] == "none"


# ---------------------------------------------------------------------------
# resolve_camera_units


def _unit(index: int, name: str = "", serial: str = "") -> SimpleNamespace:
    return SimpleNamespace(index=index, name=name, serial=serial)


def _sdk(*serials: str) -> ProbeResult:
    cams = [CameraInfo(i, s) for i, s in enumerate(serials)]
    return ProbeResult("flycap", "sdk", len(cams), cams)


class TestResolve:
    def test_flycap_index_from_serial(self) -> None:
        units = [_unit(0, "top", "87654321"), _unit(1, "side", "12345678")]
        plan = resolve_camera_units(units, "flycap", _sdk("12345678", "87654321"))
        assert [(c.name, c.index, c.serial) for c in plan] == [
            ("top", 1, ""),
            ("side", 0, ""),
        ]

    def test_spinnaker_passes_serial(self) -> None:
        units = [_unit(0, "top", "87654321")]
        plan = resolve_camera_units(units, "spinnaker", _sdk("12345678", "87654321"))
        assert (plan[0].index, plan[0].serial) == (1, "87654321")

    def test_missing_camera_named(self) -> None:
        units = [_unit(0, "top", "12345678"), _unit(1, "side", "87654321")]
        with pytest.raises(CameraMismatchError, match="87654321"):
            resolve_camera_units(units, "flycap", _sdk("12345678"))

    def test_no_serials_count_check(self) -> None:
        units = [_unit(0, "top"), _unit(1, "side")]
        assert len(resolve_camera_units(units, "flycap", _sdk("12345678", "1"))) == 2
        with pytest.raises(CameraMismatchError, match="needs 2 camera"):
            resolve_camera_units(units, "flycap", _sdk("12345678"))

    def test_unit_without_serial_must_exist(self) -> None:
        units = [_unit(0, "top", "12345678"), _unit(1, "side")]
        with pytest.raises(CameraMismatchError, match="side"):
            resolve_camera_units(units, "flycap", _sdk("12345678"))

    def test_index_collision(self) -> None:
        units = [_unit(0, "top"), _unit(1, "side", "12345678")]
        with pytest.raises(CameraMismatchError, match="one index"):
            resolve_camera_units(units, "flycap", _sdk("12345678", "87654321"))

    def test_none_source_keeps_configured(self, caplog) -> None:
        units = [_unit(1, "top", "12345678")]
        plan = resolve_camera_units(units, "spinnaker", ProbeResult("spinnaker"))
        assert (plan[0].index, plan[0].serial) == (1, "12345678")
        assert "probe unavailable" in caplog.text

    def test_usb_count_short(self) -> None:
        usb = ProbeResult("flycap", "usb", 1, [CameraInfo(None, "12345678")])
        units = [_unit(0, "top", "12345678"), _unit(1, "side", "87654321")]
        with pytest.raises(CameraMismatchError, match="via usb"):
            resolve_camera_units(units, "flycap", usb)

    def test_usb_serial_missing_refuses(self) -> None:
        usb = ProbeResult("flycap", "usb", 2, parse_usb_ids(_PNP))
        units = [_unit(0, "top", "12345678"), _unit(1, "side", "11223344")]
        with pytest.raises(CameraMismatchError, match=r"11223344.*source=usb"):
            resolve_camera_units(units, "flycap", usb)

    def test_usb_serials_keep_configured_index(self, caplog) -> None:
        usb = ProbeResult("flycap", "usb", 2, parse_usb_ids(_PNP))
        units = [_unit(1, "top", "12345678"), _unit(0, "side", "087654321")]
        plan = resolve_camera_units(units, "flycap", usb)
        assert [c.index for c in plan] == [1, 0]
        assert "configured index" in caplog.text

    def test_no_units(self) -> None:
        assert resolve_camera_units([], "flycap", _sdk()) == []
