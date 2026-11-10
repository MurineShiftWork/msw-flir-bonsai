# CLI reference

`msw-flir` is the standalone command installed by this package.
When `msw-flir-bonsai` is installed alongside `murineshiftwork`, the same commands
are also available as `msw flir <subcommand>`.

## find-bonsai

Scan known Windows install paths and print the `Bonsai.exe` location.

```
msw-flir find-bonsai
```

Copy the printed path into your setup YAML under `cameras.bonsai_exe`, or export
it as the `BONSAI_EXE` environment variable.

Searched locations (in order):

1. `%USERPROFILE%\AppData\Local\Bonsai\Bonsai.exe`
2. `C:\Program Files\Bonsai\Bonsai.exe`
3. `C:\Program Files (x86)\Bonsai\Bonsai.exe`

## list-cameras

Enumerate connected FLIR cameras and print their index and serial number.

```
msw-flir list-cameras --driver flycap
msw-flir list-cameras --driver spinnaker
```

Requires the FlyCapture2 SDK (`PyCapture2`) or Spinnaker SDK (`PySpin`) Python
wrapper to be installed.  These are Windows-only native libraries.

Example output:
```
FlyCapture2: 2 camera(s) found
  [0]  serial=12345678  model=Chameleon3 CM3-U3-13Y3M
  [1]  serial=87654321  model=Chameleon3 CM3-U3-13Y3M
```

The index shown is the value to pass as `cameras.cam_index` or `--cam-index`.

## probe-cameras

Print the index and serial of each connected camera without opening it, so a
setup can name its cameras by serial instead of by enumeration index.

```
msw-flir probe-cameras --driver flycap
msw-flir probe-cameras --driver spinnaker --json
```

| Option | Default | Description |
|---|---|---|
| `--driver`, `-d` | `flycap` | `flycap` or `spinnaker` |
| `--json` | off | Print `{driver, source, count, cameras: [{index, serial}], error}` |
| `--timeout` | `10` | Seconds before the SDK probe is killed |

`source` says where the answer came from:

- `sdk`: the FlyCapture2 or Spinnaker C library (`FlyCapture2_C.dll`,
  `SpinnakerC_v140.dll`), loaded through ctypes in a child process. No Python
  SDK wrapper is needed. Index and serial per camera.
- `usb`: the SDK was missing, crashed or hung. Windows PnP devices from vendor
  `0x1E10`: count and serials, no index order. Only the parent "USB Composite
  Device" (`USB\VID_1E10&PID_xxxx\<serial>`) counts; its serial is hex and is
  reported in decimal, like the SDK (hex `00BC614E` is serial `12345678`).
- `none`: neither worked; exits 1.

The C library is searched in `MSW_FLIR_FC2_DLL` / `MSW_FLIR_SPIN_DLL` (full
path), then on the DLL search path, then in the default `Program Files`
install directories.

!!! note "Spinnaker probe untested"
    The FlyCapture2 probe is checked on Flea3 USB3 cameras. The Spinnaker probe
    (DLL names, install folders, function calls) has not run on hardware yet.

### Naming cameras by serial

Add `serial` to each camera in the setup YAML (msw-core `CameraUnit`):

```yaml
cameras:
  backend: flir_bonsai
  driver: flycap
  cameras:
    - {index: 0, name: top, serial: "12345678"}
    - {index: 1, name: side, serial: "87654321"}
```

Before Bonsai starts, `resolve_camera_units` checks the probe against the
setup. FlyCapture launches each camera at the index the SDK reports for its
serial; Spinnaker passes the serial to Bonsai (`cam1serial`). A serial that is
not connected, or fewer cameras than the setup needs, refuses the start. With
no serials configured only the count is checked. With source `none` it warns
and launches by configured index. With source `usb` serials are checked too,
but FlyCapture keeps the configured index.

A Bonsai process that exits within 5 s of launch has not recorded. Bonsai
returns 0 in that case, so the exit is judged by time, not by return code. The
camera can stay held for a few seconds after such an exit, so Bonsai is
relaunched once after 3 s (`retry_delay`); a second early exit fails the start
(`BonsaiStartError`).

### Verifying on a rig

1. `msw flir probe-cameras -d flycap` (and `-d spinnaker`): `source` should be
   `sdk` and the serials should match the camera labels.
2. Unplug one camera and run it again: the count drops and the remaining
   serial keeps its identity even when its index changes.
3. List the USB devices with
   `Get-PnpDevice -PresentOnly | ? InstanceId -like 'USB\VID_1E10*'` in
   PowerShell: the entry without `MI_` ends in the hex serial.

## run

Launch N independent Bonsai camera subprocesses (one per camera index).

```
msw-flir run <output_dir> <session> [options]
```

**Arguments:**

| Argument | Description |
|---|---|
| `output_dir` | Root directory where video output is written |
| `session` | Session base name used for folder and file naming |

**Options:**

| Option | Default | Description |
|---|---|---|
| `--n-cameras`, `-n` | `1` | Number of camera subprocesses to launch |
| `--driver`, `-d` | `flycap` | Camera driver: `flycap` or `spinnaker` |
| `--fps` | `60` | Target frame rate (FlyCapture only; ignored for Spinnaker) |
| `--bonsai-exe` | `$BONSAI_EXE` | Path to `Bonsai.exe` |
| `--workflow` | auto | Override the workflow stem name |

Each camera uses the 1-cam Bonsai workflow with its own `cam_index`.  Press
`Ctrl+C` to stop all cameras cleanly.

**Example:**

```
msw-flir run D:\DATA\video mouse001__20260524 --n-cameras 2 --fps 60
```

## test-record

Run a short test recording from a single camera to verify the setup.
Bonsai exits 0 even when it could not record, so the command judges the
outcome itself: it exits 1 when Bonsai exited before the stop was requested
(at any time) or when `<session>.timestamps.csv` is missing or empty.

```
msw-flir test-record [options]
```

**Options:**

| Option | Default | Description |
|---|---|---|
| `--output-dir` | `~/msw_flir_test` | Output directory |
| `--session` | `test` | Session name |
| `--cam-index`, `-c` | `0` | Camera index |
| `--driver`, `-d` | `flycap` | Camera driver |
| `--fps` | `30` | Frame rate |
| `--duration` | `5.0` | Recording duration in seconds |
| `--bonsai-exe` | `$BONSAI_EXE`, then the `find-bonsai` locations | Path to `Bonsai.exe` |
| `--workflow` | auto | Override workflow stem name |

**Example:**

```
msw-flir test-record --cam-index 1 --driver spinnaker --duration 10
```
