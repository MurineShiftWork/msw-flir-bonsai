# Usage

Common patterns for running cameras, preprocessing data, and aligning timestamps.
For installation and first-run verification, see [Getting started](getting_started.md).

---

## Recording as part of an MSW session

The usual path: instead of driving the runner yourself, declare the camera in
the rig's setup YAML under `cameras:` and run any camera-aware MSW task. MSW then
launches Bonsai and records a `video_flir` acquisition (one `.avi` +
`.timestamps.csv` per camera) alongside the behavioural data.

One FlyCapture camera on a setup:

```yaml
cameras:
  backend: flir_bonsai
  driver: flycap                  # default
  bonsai_exe: C:\...\Bonsai.exe   # or set the BONSAI_EXE env var
  cameras:
    - index: 0                    # from `msw flir list-cameras`
      name: top                   # optional label; appears in the artifact name
```

The full key reference and the per-backend bring-up checklist live in the main
MSW docs (Setup config -> Cameras). The rest of this page covers driving the
runner directly, which the MSW path does for you.

---

## Running cameras

### Single camera

```python
from msw_flir_bonsai.runner import BonsaiCameraRunner

runner = BonsaiCameraRunner(
    workflow="run-flir-flycap-1cam",
    output_dir=r"D:\DATA\video",
    session="mouse001__20260524",
    cam_index=0,
    fps=60,
    driver="flycap",
)

runner.start()
# ... behavioural task runs ...
runner.stop()
runner.wait(timeout=15)
```

`bonsai_exe` is optional when `BONSAI_EXE` is set in the environment.

### Multiple cameras

Each camera runs in its own subprocess.  A crash in one does not affect the
others.

```python
from msw_flir_bonsai.runner import MultiCameraRunner

multi = MultiCameraRunner.from_config(
    n_cameras=2,
    driver="flycap",
    output_dir=r"D:\DATA\video",
    session="mouse001__20260524",
    fps=60,
)

multi.start()

# Poll until all processes exit (or add your own condition)
import time
while multi.any_running:
    time.sleep(0.5)

multi.stop()
```

`from_config` spawns one process per camera index (0, 1, ...) using the 1-cam
workflow.  The 2-cam workflow variants are legacy and not used here.

### Monitoring subprocess health

```python
print(runner.is_running)   # False after crash or stop
print(runner.pid)          # OS process ID while running

print(multi.all_running)   # True if every camera is still active
print(multi.any_running)   # True if at least one is active
print(len(multi))          # number of runners
```

### Spinnaker cameras

Switch `driver` and `workflow`; everything else is the same.  Do not pass `fps`
for Spinnaker - set the frame rate directly in the workflow XML.

```python
runner = BonsaiCameraRunner(
    workflow="run-flir-spinnaker-1cam",
    output_dir=r"D:\DATA\video",
    session="mouse001__20260524",
    cam_index=0,
    driver="spinnaker",      # fps not applicable here
)
```

---

## Preprocessing camera timestamps

The Bonsai workflow writes a headerless CSV with columns:
`frame_counter`, `timestamp_raw`, `gpio_state` (optional).

FlyCapture timestamps are cyclic (128 s period) and may contain frame-counter
rollovers.  `preprocess_camera_csv` corrects both.

```python
from msw_flir_bonsai.timestamps import preprocess_camera_csv

df = preprocess_camera_csv(
    "cam0__20260524_120000.csv",
    ts_cycle_s=128.0,    # FlyCapture rollover period; use np.inf for Spinnaker
    counter_bits=32,
)
# Columns: frame_index, timestamp_raw, timestamp_s, frame_counter, [gpio_state]
```

To zero timestamps relative to the first behavioural event:

```python
df = preprocess_camera_csv(
    "cam0.csv",
    session_start_s=bpod_session_start_time,  # subtract this from all timestamps
)
```

### Detecting dropped frames

```python
from msw_flir_bonsai.timestamps import detect_dropped_frames

dropped = detect_dropped_frames(df, expected_fps=60.0)
print(f"{dropped.sum()} dropped frames detected")

# View the frames where drops occurred
print(df.loc[dropped, ["frame_index", "timestamp_s"]])
```

`detect_dropped_frames` flags any inter-frame gap larger than 1.5 times the
nominal inter-frame interval.

---

## Aligning camera time to Bpod

Two methods are available depending on what GPIO data was recorded.

### Barcode alignment (preferred)

Use this when both the camera GPIO and Bpod BNC recorded the same periodic
barcode pulses.

```python
from msw_flir_bonsai.alignment import extract_camera_barcodes, align_barcodes

# Extract barcodes from the camera GPIO column
cam_barcodes = extract_camera_barcodes(
    df,
    gpio_col="gpio_state",
    fps=60.0,
    barcode_bit_duration_s=0.1,
)

# bpod_barcodes: list of (time_s, barcode_int) decoded from Bpod events
offset_s = align_barcodes(bpod_barcodes, cam_barcodes, max_hamming=2)

# Apply the offset
df["bpod_time"] = df["timestamp_s"] + offset_s
```

`offset_s` satisfies `bpod_time = camera_time + offset_s`.  Up to
`max_hamming` bit errors per barcode are tolerated; the median offset across
all matched pairs is used for robustness.

### TTL-edge alignment (fallback)

Use this when barcodes are absent or too sparse but Bpod pulsed a BNC line
(e.g. at trial start/end) that the camera GPIO recorded.

```python
from msw_flir_bonsai.alignment import align_ttl_edges

# bpod_trial_times: list of trial-start times (seconds) in Bpod clock
offset_s = align_ttl_edges(
    df,
    bpod_trial_times,
    gpio_col="gpio_state",
    max_offset_search_s=60.0,
    min_matches=5,
)

df["bpod_time"] = df["timestamp_s"] + offset_s
```

This method cross-correlates rising GPIO edges with Bpod event times via a
histogram search.  It requires at least `min_matches` agreeing edge pairs.

---

## CLI quick reference

```sh
# Locate Bonsai.exe
msw-flir find-bonsai

# List cameras
msw-flir list-cameras --driver flycap
msw-flir list-cameras --driver spinnaker

# Run session (blocks until Ctrl+C)
msw-flir run D:\DATA\video mouse001__20260524 --n-cameras 2 --fps 60

# Short test recording
msw-flir test-record --cam-index 0 --duration 5

# All subcommands also available via msw flir when murineshiftwork is installed
msw flir run D:\DATA\video mouse001__20260524 --n-cameras 2
```

See [CLI reference](usage/cli.md) for full option tables.
