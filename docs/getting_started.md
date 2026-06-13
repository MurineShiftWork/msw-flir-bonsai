# Getting started

## Requirements

- Python 3.10 or later
- Windows 10/11 (the Bonsai workflows and camera SDKs are Windows-only)
- [Bonsai](https://bonsai-rx.org/) installed (the `.exe` is called by the runner)
- A FLIR camera with either the **FlyCapture2** or **Spinnaker** SDK installed

## Install the package

```sh
pip install msw_flir_bonsai
```

This installs the `msw-flir` console script and the Python API.  It does **not**
install the camera SDKs - those must be obtained from FLIR/Teledyne separately.

## Camera SDK notes

### FlyCapture2

- Download from the FLIR/Teledyne support page (search "FlyCapture2 SDK").
- Install the SDK, then install the Python wrapper: `PyCapture2` (a wheel is
  bundled in the SDK installer on recent versions, or available via the Teledyne
  download portal).
- Cameras that use FlyCapture2: Chameleon3, Grasshopper3 (USB3 Gen1 models).

### Spinnaker

- Download the Spinnaker SDK from the Teledyne FLIR support page.
- Install the SDK, then install `PySpin` (bundled in the Spinnaker Python
  installer).
- Cameras that use Spinnaker: Blackfly S, Oryx, and current-generation USB3 /
  GigE models.

Verify SDK installation with:

```sh
msw-flir list-cameras --driver flycap
# or
msw-flir list-cameras --driver spinnaker
```

## Locate Bonsai.exe

The runner needs an absolute path to `Bonsai.exe`.  Run:

```sh
msw-flir find-bonsai
```

This scans common install locations and prints the path.  Either set the
`BONSAI_EXE` environment variable to this path, or pass it explicitly when
creating a `BonsaiCameraRunner`.

```sh
# Windows PowerShell (per-session)
$Env:BONSAI_EXE = "C:\Users\lab\AppData\Local\Bonsai\Bonsai.exe"

# or add it to your system environment variables for a permanent setting
```

## Verify with a test recording

Connect a camera, then run a 5-second test to confirm everything works:

```sh
msw-flir test-record --cam-index 0 --driver flycap --fps 30 --duration 5
```

Output is written to `~/msw_flir_test/` by default.  You should see a `.avi`
video file and a `.csv` metadata file (frame counter, timestamp, GPIO state).

```sh
# Check the CSV has the expected columns
python - <<'EOF'
from msw_flir_bonsai.timestamps import load_camera_csv
df = load_camera_csv("~/msw_flir_test/test/test__<YYYYMMDD_HHMMSS>/test__<YYYYMMDD_HHMMSS>__cam0.csv")
print(df.head())
print(df.columns.tolist())
EOF
```

Expected columns: `frame_index`, `frame_counter`, `timestamp_raw`, `gpio_state`
(the last column is present only when a GPIO line is connected).

## First camera connection from Python

```python
from msw_flir_bonsai.runner import BonsaiCameraRunner

runner = BonsaiCameraRunner(
    workflow="run-flir-flycap-1cam",    # or run-flir-spinnaker-1cam
    output_dir=r"D:\DATA\video",
    session="mouse001__20260524",
    cam_index=0,
    fps=60,                              # FlyCapture only; ignored for Spinnaker
    driver="flycap",                     # "flycap" or "spinnaker"
    # bonsai_exe not needed if BONSAI_EXE env var is set
)

runner.start()        # launches Bonsai subprocess, begins writing frames immediately
print(runner.pid)     # OS process ID for the Bonsai process

# ... wait, run behaviour task, etc. ...

runner.stop()         # sends SIGTERM; Bonsai flushes open files before exiting
runner.wait(timeout=15)
```

This is the full acquisition lifecycle.  For multi-camera setups see
[Usage](usage.md).
