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
msw-flir test-record --cam-index 0 --fps 30  # 5-second test recording
msw-flir run D:\DATA\video mouse001 --n-cameras 2 --fps 60
```

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
