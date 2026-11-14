# msw-flir-bonsai

FLIR camera acquisition for MurineShiftWork via Bonsai subprocesses.

Each camera runs in an **isolated Bonsai subprocess** — a crash in one camera does not
affect others or the main behaviour task.  Workflow XMLs for FlyCapture (PointGrey2) and
Spinnaker cameras are shipped as package data.

---

## Installation

```sh
pip install msw-flir-bonsai
```

Or with uv:

```sh
uv add msw-flir-bonsai
```

Runtime dependencies: `numpy`, `pandas`.  No FLIR Python SDK is required on the acquisition
machine — Bonsai handles all camera communication.

---

## Requirements

### Bonsai (Windows only)

Install [Bonsai](https://bonsai-rx.org/) and the following NuGet packages via the Bonsai
package manager (`Tools → Manage Packages`):

| Package | Purpose | Tested version |
|---|---|---|
| `Bonsai.Core` | Core reactive framework | 2.8.5 |
| `Bonsai.Design` | Editor UI | 2.8.5 |
| `Bonsai.Editor` | Editor shell | 2.8.5 |
| `Bonsai.System` | I/O operators (CsvWriter, etc.) | 2.8.5 |
| `Bonsai.Vision` | VideoWriter, image processing | 2.8.5 |
| `Bonsai.Vision.Design` | Vision editor support | 2.8.5 |
| `Bonsai.Scripting.IronPython` | IronPython inline scripts | 2.8.5 |
| `Bonsai.Scripting.IronPython.Design` | IronPython editor | 2.8.5 |
| `Bonsai.PointGrey2` | FlyCapture2 camera driver | 0.3.0 |
| `Bonsai.Spinnaker` | Spinnaker camera driver | 0.4.0 |

### Camera drivers (Windows)

| Driver | Version | Notes |
|---|---|---|
| FlyCapture2 SDK | 2.13.3 | Required for PointGrey / FLIR Grasshopper cameras |
| Spinnaker SDK | 3.x | Required for FLIR Blackfly S / BFS cameras |

> **Note:** FlyCapture2 and Spinnaker are mutually exclusive on the same machine in some
> versions. If using Spinnaker cameras, install only the Spinnaker SDK.

### Bonsai executable path

Set the `BONSAI_EXE` environment variable on the acquisition machine:

```bat
setx BONSAI_EXE "C:\Users\<user>\AppData\Local\Bonsai\Bonsai.exe"
```

Or pass `bonsai_exe=` directly to `BonsaiCameraRunner`.

---

## Quick start

```python
from msw_flir_bonsai.runner import BonsaiCameraRunner

runner = BonsaiCameraRunner(
    workflow="run-flir-flycap-1cam",   # or "run-flir-spinnaker-1cam"
    output_dir=r"D:\DATA\video",
    session="mouse001__20260518_120000",
    cam_index=0,
    fps=60,
    driver="flycap",
)
runner.start()

# ... main task runs on Linux, camera runs on Windows acquisition machine ...

runner.stop()
runner.wait(timeout=10)
```

### Multiple cameras

```python
from msw_flir_bonsai.runner import MultiCameraRunner

cameras = MultiCameraRunner.from_config(
    n_cameras=2,
    driver="flycap",
    output_dir=r"D:\DATA\video",
    session="mouse001__20260518_120000",
    fps=60,
)
cameras.start()
# each camera is an independent subprocess — one crash does not stop the other
cameras.stop()
```

### CLI

```sh
msw-flir find-bonsai                         # locate Bonsai.exe
msw-flir list-cameras --driver flycap        # enumerate connected cameras
msw-flir probe-cameras --driver flycap       # index and serial, no SDK wrapper
msw-flir test-record --cam-index 0 --fps 30  # 5-second test recording
msw-flir run D:\DATA\video mouse001 --n-cameras 2 --fps 60
```

### Camera identity

Bonsai selects a camera by SDK index, which shifts when a camera is missing.
`msw-flir probe-cameras` reads each camera's serial through the vendor C library
(in a child process, without opening the camera), falling back to the Windows USB
device list. With `serial` set per camera in the setup, MSW checks the connected
cameras before launching Bonsai and refuses to start on a mismatch. See
[CLI reference: probe-cameras](docs/usage/cli.md#probe-cameras) for the fields and
how to verify it on a rig.

### MSW camera backend (`flir_bonsai`)

The package registers `FlirBonsaiBackend` under the `msw.camera` entry point as
`flir_bonsai`, so an MSW setup selects it by name:

```yaml
cameras:
  - name: behaviour
    backend: flir_bonsai
    units:
      - {name: top, hw_id: "12345678", index: 0}   # hw_id: camera serial
      - {name: back, index: 1}
    settings: {driver: flycap, bonsai_exe: C:\Bonsai\Bonsai.exe}
```

- **Settings:** `driver` (`flycap` or `spinnaker`), `bonsai_exe` (else `BONSAI_EXE`),
  `workflow` (default `run-flir-<driver>-1cam`) and `n_cameras` (cameras `cam0..`
  when no units are listed). Per camera: `index` and the optional serial as `hw_id`.
  Unknown keys are a configuration error.
- **Preflight** probes the connected cameras. A camera whose serial is not connected,
  or which has no camera left, is `missing` and never launched; FlyCapture launches
  at the serial's SDK index, Spinnaker by serial. Without a probe answer every camera
  is launched by its configured index.
- **Start** runs one Bonsai per camera and reports each as `recording` once its first
  frame is written, else `failed`; the other cameras keep recording.
- **Stop** reports `stopped` when Bonsai exited after the stop request and the
  timestamps file has data, else `failed` with the exit code.
- Files are `<acq basename>.msw.<name>.<index>.avi` and `.timestamps.csv`.

---

## Output files

Each Bonsai workflow creates a session directory and writes:

```
<output_dir>/<session>/<session>__<datetime>/
    <session>__<datetime>__cam1.avi     # video
    <session>__<datetime>__cam1.csv     # per-frame metadata
```

**CSV columns (FlyCapture):**

| Column | Description |
|---|---|
| `frame_counter` | Hardware frame counter (rolls over at 32-bit) |
| `timestamp_raw` | Embedded hardware timestamp (seconds, cycles every 128 s) |
| `gpio_state` | GPIO input state (0/1) — records TTL barcode and trial pulses |

---

## Timestamp preprocessing

```python
from msw_flir_bonsai.timestamps import preprocess_camera_csv, detect_dropped_frames

df = preprocess_camera_csv(
    "cam1.csv",
    ts_cycle_s=128.0,       # FlyCapture rollover period; np.inf for Spinnaker
    session_start_s=None,   # set to subtract session t0 if known
)
# df["timestamp_s"]    — unwrapped monotonic timestamps
# df["frame_counter"]  — unwrapped frame counter
# df["gpio_state"]     — TTL/barcode input

drops = detect_dropped_frames(df, expected_fps=60.0)
```

---

## Alignment

Two channels link camera frames to Bpod behaviour timestamps:

### 1. TTL barcodes

Periodic binary barcode pulses recorded on both the camera GPIO and the Bpod BNC output.
Partial barcodes are handled via Hamming-distance matching (up to 2 bit errors tolerated).

```python
from msw_flir_bonsai.alignment import extract_camera_barcodes, align_barcodes

cam_barcodes = extract_camera_barcodes(df)          # [(time_s, value), ...]
bpod_barcodes = [...]                               # from Bpod session data
offset_s = align_barcodes(bpod_barcodes, cam_barcodes)
df["timestamp_bpod"] = df["timestamp_s"] + offset_s
```

### 2. Trial TTL edges (fallback)

The sequence task and others pulse a Bpod BNC line at trial start/end.  The camera GPIO
records these transitions at frame resolution, providing per-frame trial alignment even
when barcodes are absent or incomplete.

```python
from msw_flir_bonsai.alignment import align_ttl_edges

bpod_trial_starts = [...]    # trial-start times from Bpod session YAML
offset_s = align_ttl_edges(df, bpod_trial_starts)
```

---

## Development setup

```sh
git clone https://github.com/murineshiftwork/msw-flir-bonsai.git
cd msw-flir-bonsai
uv sync --extra dev
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

## Running tests

```sh
uv run pytest
```

Integration tests against a real Bonsai installation (Windows only):

```sh
set BONSAI_EXE=C:\Users\<user>\AppData\Local\Bonsai\Bonsai.exe
uv run pytest tests/integration/ -v
```

## Release workflow

1. Work on a `feature/` or `fix/` branch, committing with `cz commit`
2. Open a PR — CI (lint + tests + secrets scan) must pass before merge
3. Merge to main → version bump and tag are created automatically
4. Tag triggers release: GitHub release + PyPI publish

## License

See [LICENSE](LICENSE).
