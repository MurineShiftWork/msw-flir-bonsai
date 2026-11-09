"""Identify connected FLIR cameras by serial number before Bonsai starts.

Bonsai picks a camera by SDK enumeration index. When a camera is missing the
indices shift: the remaining camera records under the other camera's name, or
Bonsai exits 0 with an empty timestamps file. This module asks the vendor SDK
which serial sits at which index, so a setup can name its cameras by serial.

Mechanism, in order of preference (:func:`probe_cameras`):

1. ``sdk``: the vendor C library, through ctypes, in a child process
   (``python -I -m msw_flir_bonsai.probe --driver ...``) that prints one JSON
   line. FlyCapture2 (``FlyCapture2_C``) reports index and serial without
   connecting; Spinnaker (``SpinnakerC_v140`` and other suffixes) reads
   ``DeviceSerialNumber`` from the transport-layer node map without
   ``spinCameraInit``.
2. ``usb``: the Windows PnP list of present USB devices from vendor ``0x1E10``
   (FLIR / Point Grey). Gives the count and the USB serial strings, but not the
   SDK index order.
3. ``none``: neither worked; callers keep launching by configured index.

:func:`resolve_camera_units` turns a probe and the configured cameras into the
launch plan (index per camera, and the serial for Spinnaker, whose Bonsai node
selects by ``SerialNumber``).

Critical choices:

- The SDK runs in a child process. A vendor DLL can crash the interpreter with
  an access violation or hang on a busy bus; the parent only sees an exit code
  or a timeout and falls back to USB. The parent never loads a vendor DLL.
- Cameras are never opened (no ``fc2Connect``, no ``spinCameraInit``): Bonsai
  or another tool may hold them, and enumeration needs no exclusive access.
- Every ctypes function gets explicit ``argtypes`` and ``restype``. Handles are
  pointers; the default ``int`` restype would truncate them on 64-bit Windows.
- USB serial strings are not trusted to equal the SDK serial until checked on a
  rig, so with the ``usb`` source a serial mismatch warns and only a camera
  count shortfall refuses.

Where to start reading: :func:`probe_cameras`, :func:`probe_sdk`,
:func:`resolve_camera_units`.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

log = logging.getLogger(__name__)

DRIVERS = ("flycap", "spinnaker")

#: Env vars holding the full path of the C library, checked before the search.
FC2_DLL_ENV = "MSW_FLIR_FC2_DLL"
SPIN_DLL_ENV = "MSW_FLIR_SPIN_DLL"

_FC2_NAMES = ("FlyCapture2_C", "FlyCapture2_C_v140", "FlyCapture2_C_v100")
# Spinnaker suffixes the C library with the MSVC toolset. v140 is the name
# shipped through Spinnaker 3; the others are guesses: verify on a rig.
_SPIN_NAMES = ("SpinnakerC_v140", "SpinnakerC_v143", "SpinnakerC_v120", "SpinnakerC")

_USB_VENDOR = "1E10"  # FLIR Integrated Imaging Solutions / Point Grey
# Top-level device only (no &MI_ interface children). The last part is the
# USB serial string, or a Windows-made id containing '&' when there is none.
_USB_ID = re.compile(
    rf"^USB\\VID_{_USB_VENDOR}&PID_([0-9A-F]{{4}})\\([^\\]+)$", re.IGNORECASE
)
_USB_SCRIPT = (
    "try { Get-PnpDevice -PresentOnly -ErrorAction Stop |"
    " ForEach-Object { $_.InstanceId } }"
    " catch { Get-CimInstance Win32_PnPEntity |"
    " ForEach-Object { $_.PNPDeviceID } }"
)


def _install_dirs(driver: str) -> list[Path]:
    """Default vendor install directories holding the 64-bit C library."""
    pf = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
    if driver == "flycap":
        base = pf / "Point Grey Research" / "FlyCapture2" / "bin64"
        return [base, base / "vs2015"]
    return [
        pf / vendor / "Spinnaker" / "bin64" / sub
        for vendor in ("Teledyne", "FLIR Systems", "Point Grey Research")
        for sub in ("vs2015", "vs140")
    ]


@dataclass
class CameraInfo:
    """One enumerated camera. ``index`` is None when the source has no SDK order."""

    index: int | None
    serial: str


@dataclass
class ProbeResult:
    """What a probe found.

    Attributes:
        driver: ``"flycap"`` or ``"spinnaker"``.
        source: ``"sdk"``, ``"usb"`` or ``"none"`` (nothing could be read).
        count: Number of cameras, or None with source ``"none"``.
        cameras: One entry per camera, in SDK index order for source ``"sdk"``.
        error: Why a preferred source failed; empty when the SDK answered.
    """

    driver: str
    source: str = "none"
    count: int | None = None
    cameras: list[CameraInfo] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProbeResult:
        """Rebuild a result from :meth:`to_dict` output; raises on a bad shape."""
        cameras = [
            CameraInfo(
                index=None if c.get("index") is None else int(c["index"]),
                serial=str(c.get("serial", "")),
            )
            for c in data.get("cameras", [])
        ]
        count = data.get("count")
        return cls(
            driver=str(data["driver"]),
            source=str(data["source"]),
            count=None if count is None else int(count),
            cameras=cameras,
            error=str(data.get("error", "")),
        )


class CameraMismatchError(RuntimeError):
    """The connected cameras do not match the configured ones."""


class _SdkError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Vendor C library loading


def _load_library(driver: str) -> tuple[Any, str]:
    """Load the vendor C library for ``driver``.

    Order: the env var (full path), the plain names on the DLL search path,
    then the default install directories.

    Returns:
        ``(library, "")`` or ``(None, reason)``. Never raises.
    """
    env, names = (
        (FC2_DLL_ENV, _FC2_NAMES) if driver == "flycap" else (SPIN_DLL_ENV, _SPIN_NAMES)
    )
    candidates: list[str] = []
    if os.environ.get(env, "").strip():
        candidates.append(os.environ[env].strip())
    candidates += [p for n in names if (p := ctypes.util.find_library(n))]
    candidates += [
        str(d / f"{n}.dll")
        for d in _install_dirs(driver)
        for n in names
        if (d / f"{n}.dll").is_file()
    ]
    errors = []
    for path in candidates:
        try:
            add_dir = getattr(os, "add_dll_directory", None)
            if add_dir is not None and Path(path).parent.is_dir():
                # dependent DLLs (Spinnaker.dll, GenTL producers) sit beside it
                add_dir(str(Path(path).parent))
            # CDLL: x64 Windows has a single calling convention, so cdecl vs
            # stdcall (WinDLL) does not matter for the 64-bit libraries.
            return ctypes.CDLL(path), ""
        except OSError as e:
            errors.append(f"{path}: {e}")
    if not candidates:
        return None, f"{names[0]} library not found (set {env} to its full path)"
    return None, "; ".join(errors)


def _declare(lib: Any, name: str, argtypes: list[Any], restype: Any) -> Any:
    """Return ``lib.<name>`` with its ctypes signature set."""
    fn = getattr(lib, name)
    fn.argtypes = argtypes
    fn.restype = restype
    return fn


def _check(rc: int, name: str) -> None:
    # fc2Error and spinError are int enums; 0 is FC2_ERROR_OK / SPINNAKER_ERR_SUCCESS
    if rc != 0:
        raise _SdkError(f"{name} returned error {rc}")


_P_VOID = ctypes.POINTER(ctypes.c_void_p)


def _probe_flycap(lib: Any) -> list[CameraInfo]:
    """Enumerate FlyCapture2 cameras: index and serial, without connecting."""
    create = _declare(lib, "fc2CreateContext", [_P_VOID], ctypes.c_int)
    count = _declare(
        lib,
        "fc2GetNumOfCameras",
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)],
        ctypes.c_int,
    )
    serial_at = _declare(
        lib,
        "fc2GetCameraSerialNumberFromIndex",
        [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)],
        ctypes.c_int,
    )
    destroy = _declare(lib, "fc2DestroyContext", [ctypes.c_void_p], ctypes.c_int)

    ctx = ctypes.c_void_p()
    _check(create(ctypes.byref(ctx)), "fc2CreateContext")
    try:
        n = ctypes.c_uint()
        _check(count(ctx, ctypes.byref(n)), "fc2GetNumOfCameras")
        cameras = []
        for i in range(n.value):
            sn = ctypes.c_uint()
            _check(
                serial_at(ctx, i, ctypes.byref(sn)), "fc2GetCameraSerialNumberFromIndex"
            )
            cameras.append(CameraInfo(index=i, serial=str(sn.value)))
        return cameras
    finally:
        destroy(ctx)


def _probe_spinnaker(lib: Any) -> list[CameraInfo]:
    """Enumerate Spinnaker cameras: index and DeviceSerialNumber, without Init.

    The transport-layer device node map is readable before ``spinCameraInit``,
    so a camera streaming in another process can still be listed. Handles are
    released in reverse order of acquisition: Spinnaker refuses to release the
    system while camera references are alive.
    """
    vp, size_t = ctypes.c_void_p, ctypes.c_size_t
    err = ctypes.c_int
    get_system = _declare(lib, "spinSystemGetInstance", [_P_VOID], err)
    release_system = _declare(lib, "spinSystemReleaseInstance", [vp], err)
    list_create = _declare(lib, "spinCameraListCreateEmpty", [_P_VOID], err)
    list_fill = _declare(lib, "spinSystemGetCameras", [vp, vp], err)
    list_size = _declare(
        lib, "spinCameraListGetSize", [vp, ctypes.POINTER(size_t)], err
    )
    list_get = _declare(lib, "spinCameraListGet", [vp, size_t, _P_VOID], err)
    list_clear = _declare(lib, "spinCameraListClear", [vp], err)
    list_destroy = _declare(lib, "spinCameraListDestroy", [vp], err)
    camera_release = _declare(lib, "spinCameraRelease", [vp], err)
    tl_nodemap = _declare(lib, "spinCameraGetTLDeviceNodeMap", [vp, _P_VOID], err)
    get_node = _declare(lib, "spinNodeMapGetNode", [vp, ctypes.c_char_p, _P_VOID], err)
    get_string = _declare(
        lib,
        "spinStringGetValue",
        [vp, ctypes.POINTER(ctypes.c_char), ctypes.POINTER(size_t)],
        err,
    )

    def serial_of(cam: ctypes.c_void_p) -> str:
        nodemap, node = vp(), vp()
        _check(tl_nodemap(cam, ctypes.byref(nodemap)), "spinCameraGetTLDeviceNodeMap")
        _check(
            get_node(nodemap, b"DeviceSerialNumber", ctypes.byref(node)),
            "spinNodeMapGetNode",
        )
        buf = ctypes.create_string_buffer(256)
        length = size_t(len(buf))
        _check(get_string(node, buf, ctypes.byref(length)), "spinStringGetValue")
        return buf.value.decode("ascii", "replace").strip()

    system = vp()
    _check(get_system(ctypes.byref(system)), "spinSystemGetInstance")
    try:
        cam_list = vp()
        _check(list_create(ctypes.byref(cam_list)), "spinCameraListCreateEmpty")
        try:
            _check(list_fill(system, cam_list), "spinSystemGetCameras")
            try:
                n = size_t()
                _check(list_size(cam_list, ctypes.byref(n)), "spinCameraListGetSize")
                cameras = []
                for i in range(n.value):
                    cam = vp()
                    _check(
                        list_get(cam_list, i, ctypes.byref(cam)), "spinCameraListGet"
                    )
                    try:
                        serial = serial_of(cam)
                    except _SdkError as e:
                        # still count the camera; resolution reports it unnamed
                        log.warning(f"Spinnaker camera {i}: serial unreadable: {e}")
                        serial = ""
                    finally:
                        camera_release(cam)
                    cameras.append(CameraInfo(index=i, serial=serial))
                return cameras
            finally:
                list_clear(cam_list)
        finally:
            list_destroy(cam_list)
    finally:
        release_system(system)


def probe_sdk(
    driver: str, loader: Callable[[str], tuple[Any, str]] = _load_library
) -> ProbeResult:
    """Enumerate cameras through the vendor C library in this process.

    Loads a vendor DLL, so a vendor crash takes this process down: call it in
    a child process (:func:`probe_cameras` does).

    Returns:
        Source ``"sdk"`` with index and serial per camera, or source ``"none"``
        with the reason (library missing, symbol missing, SDK error code).
    """
    if driver not in DRIVERS:
        return ProbeResult(driver, error=f"unknown driver {driver!r}")
    try:
        lib, reason = loader(driver)
        if lib is None:
            return ProbeResult(driver, error=reason)
        cameras = _probe_flycap(lib) if driver == "flycap" else _probe_spinnaker(lib)
    except Exception as e:  # missing symbol, SDK error code, bad argument type
        return ProbeResult(driver, error=f"{type(e).__name__}: {e}")
    return ProbeResult(driver, "sdk", len(cameras), cameras)


# ---------------------------------------------------------------------------
# USB fallback (Windows, no SDK)


def parse_usb_ids(lines: Sequence[str]) -> list[CameraInfo]:
    """Pick FLIR / Point Grey cameras out of PnP instance ids.

    Interface children (``...&MI_00``) and duplicates are dropped. A device
    without a USB serial string gets ``serial=""``.
    """
    seen: set[str] = set()
    cameras = []
    for line in lines:
        m = _USB_ID.match(line.strip())
        if not m:
            continue
        key = m.group(0).upper()
        if key in seen:
            continue
        seen.add(key)
        serial = m.group(2)
        cameras.append(CameraInfo(index=None, serial="" if "&" in serial else serial))
    return cameras


def probe_usb(driver: str, timeout: float = 10.0) -> ProbeResult:
    """Count present FLIR / Point Grey USB devices without any vendor SDK.

    Runs PowerShell (``Get-PnpDevice -PresentOnly``, falling back to
    ``Win32_PnPEntity``). Counts every vendor-0x1E10 USB device regardless of
    driver.

    Returns:
        Source ``"usb"`` with count and USB serial strings (no index), or source
        ``"none"`` off Windows or when PowerShell fails.
    """
    if os.name != "nt":
        return ProbeResult(driver, error="USB enumeration is Windows-only")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _USB_SCRIPT],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return ProbeResult(driver, error=f"USB enumeration failed: {e}")
    if proc.returncode != 0:
        return ProbeResult(
            driver, error=f"USB enumeration exited with code {proc.returncode}"
        )
    cameras = parse_usb_ids(proc.stdout.splitlines())
    return ProbeResult(driver, "usb", len(cameras), cameras)


# ---------------------------------------------------------------------------
# Isolated probe with fallback


def _probe_in_child(driver: str, timeout: float) -> ProbeResult:
    """Run :func:`probe_sdk` in ``python -I -m msw_flir_bonsai.probe``."""
    cmd = [sys.executable, "-I", "-m", "msw_flir_bonsai.probe", "--driver", driver]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run has killed the child by the time this is raised
        return ProbeResult(driver, error=f"SDK probe timed out after {timeout:g} s")
    except OSError as e:
        return ProbeResult(driver, error=f"SDK probe could not start: {e}")
    if proc.returncode != 0:
        # 0xC0000005 is a Windows access violation inside the vendor DLL
        code = proc.returncode & 0xFFFFFFFF
        return ProbeResult(
            driver, error=f"SDK probe crashed (exit code {proc.returncode}, {code:#x})"
        )
    # vendor libraries may print to stdout; the result is the last JSON line
    lines = [x for x in proc.stdout.splitlines() if x.lstrip().startswith("{")]
    try:
        return ProbeResult.from_dict(json.loads(lines[-1]))
    except (IndexError, ValueError, KeyError, TypeError) as e:
        return ProbeResult(driver, error=f"SDK probe output unreadable: {e}")


def probe_cameras(driver: str, timeout: float = 10.0) -> ProbeResult:
    """Find the connected cameras for ``driver``, never loading a DLL here.

    Tries the SDK in a child process, then the USB device list, then gives up
    with source ``"none"``. Errors of the failed sources are joined in
    ``error``. Blocks for at most about ``2 * timeout`` seconds.

    Returns:
        The first source that answered. Never raises.
    """
    if driver not in DRIVERS:
        return ProbeResult(driver, error=f"unknown driver {driver!r}")
    sdk = _probe_in_child(driver, timeout)
    if sdk.source == "sdk":
        return sdk
    usb = probe_usb(driver, timeout)
    errors = [e for e in (sdk.error, usb.error) if e]
    usb.error = "; ".join(errors)
    return usb


# ---------------------------------------------------------------------------
# Launch plan


@dataclass
class CameraLaunch:
    """How to launch one configured camera.

    Attributes:
        name: The configured camera name.
        index: SDK index for the Bonsai ``cam1idx`` property.
        serial: Serial to pass as ``cam1serial`` (Spinnaker only), else empty.
    """

    name: str
    index: int
    serial: str = ""


def _norm(serial: object) -> str:
    return str(serial or "").strip()


def _usb_norm(serial: str) -> str:
    return serial.lstrip("0").upper()


def resolve_camera_units(
    units: Sequence[Any], driver: str, probe: ProbeResult
) -> list[CameraLaunch]:
    """Turn configured cameras and a probe into the launch plan.

    ``units`` are objects with ``index``, ``name`` and an optional ``serial``
    (msw-core ``CameraUnit``). The plan keeps their order.

    - Source ``"none"``: warn and launch by configured index (Spinnaker still
      gets configured serials).
    - No serials configured: require ``count >= max(index) + 1``.
    - Serials with source ``"sdk"``: every serial must be connected. FlyCapture
      launches at the serial's index; Spinnaker passes the serial to Bonsai.
      Cameras without a serial keep their index, which must exist and not
      collide with a serial-resolved one.
    - Serials with source ``"usb"``: require ``count >= len(units)``. A serial
      missing from the USB list only warns (the USB string is not verified to
      be the camera serial); FlyCapture keeps the configured index.

    Raises:
        CameraMismatchError: Cameras are missing, a serial is not connected,
            or two cameras resolve to the same index.
    """
    plan = [
        CameraLaunch(
            name=getattr(u, "name", "") or "",
            index=int(u.index),
            serial=_norm(getattr(u, "serial", "")),
        )
        for u in units
    ]
    pass_serial = driver == "spinnaker"
    if not plan:
        return plan
    if probe.source == "none":
        log.warning(
            f"Camera probe unavailable ({probe.error or 'no source'}): "
            "launching by configured index"
        )
        return _finish(plan, pass_serial)

    count = probe.count or 0
    label = f"{count} {driver} camera(s) found via {probe.source}"
    if not any(c.serial for c in plan):
        needed = max(c.index for c in plan) + 1
        if count < needed:
            raise CameraMismatchError(
                f"setup needs {needed} camera(s) (highest index {needed - 1}); {label}"
            )
        return _finish(plan, pass_serial)

    if probe.source == "usb":
        if count < len(plan):
            raise CameraMismatchError(f"setup needs {len(plan)} camera(s); {label}")
        found_usb = {_usb_norm(c.serial) for c in probe.cameras}
        missing = [
            c.serial for c in plan if c.serial and _usb_norm(c.serial) not in found_usb
        ]
        if missing:
            log.warning(f"Serials {missing} not in the USB device list ({label})")
        if not pass_serial:
            log.warning("No SDK index order via USB: FlyCapture uses configured index")
        return _finish(plan, pass_serial)

    by_serial = {
        c.serial: c.index for c in probe.cameras if c.serial and c.index is not None
    }
    missing = [c.serial for c in plan if c.serial and c.serial not in by_serial]
    if missing:
        raise CameraMismatchError(
            f"camera serial(s) {missing} not connected; {label}: {sorted(by_serial)}"
        )
    for c in plan:
        if c.serial:
            c.index = by_serial[c.serial]
        elif c.index >= count:
            raise CameraMismatchError(
                f"camera {c.name or c.index!r} has no serial and index {c.index} "
                f"is not connected; {label}"
            )
    indices = [c.index for c in plan]
    if len(set(indices)) != len(indices):
        raise CameraMismatchError(f"two cameras resolve to one index: {indices}")
    return _finish(plan, pass_serial)


def _finish(plan: list[CameraLaunch], pass_serial: bool) -> list[CameraLaunch]:
    if not pass_serial:
        for c in plan:
            c.serial = ""
    return plan


# ---------------------------------------------------------------------------
# Child-process entry point


def main(argv: Sequence[str] | None = None) -> None:
    """Print :func:`probe_sdk` as one JSON line (run by :func:`probe_cameras`)."""
    parser = argparse.ArgumentParser(prog="python -m msw_flir_bonsai.probe")
    parser.add_argument("--driver", choices=DRIVERS, required=True)
    args = parser.parse_args(argv)
    result = probe_sdk(args.driver)
    sys.stdout.write(json.dumps(result.to_dict()) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
