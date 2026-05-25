# Python API

## Running cameras from Python

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
    bonsai_exe=r"C:\Users\lab\AppData\Local\Bonsai\Bonsai.exe",
)

runner.start()
# ... behavioural task runs ...
runner.stop()
runner.wait(timeout=15)
```

`bonsai_exe` falls back to the `BONSAI_EXE` environment variable if not provided.

### Multiple cameras (one process per camera)

```python
from msw_flir_bonsai.runner import MultiCameraRunner

multi = MultiCameraRunner.from_config(
    n_cameras=2,
    driver="flycap",
    output_dir=r"D:\DATA\video",
    session="mouse001__20260524",
    fps=60,
    bonsai_exe=r"C:\...\Bonsai.exe",
)

multi.start()
# ... task runs ...
multi.stop()
```

`from_config` always uses the 1-cam workflow and spawns N independent processes
with consecutive `cam_index` values (0, 1, …).  A crash in one process does not
affect the others.

### MSW integration (via CameraClient protocol)

When used through `murineshiftwork`, the runner is wrapped by `FlirBonsaiClient`
and called via the four-method `CameraClient` protocol:

```python
from murineshiftwork.hardware.camera.client import make_camera_client

cameras_config = setup_config.cameras  # CameraConfig from setup YAML
client = make_camera_client(cameras_config)

if client:
    client.preflight()           # checks bonsai_exe path
    client.start_recording(output_path, session_name)
    # ... task ...
    client.stop_recording()
    client.teardown()
```

See the [murineshiftwork setup config docs](https://larsrollik.github.io/murineshiftwork/setup/) for the full YAML reference.

---

## Preprocessing camera timestamps

Bonsai writes a CSV per camera with frame counter, hardware timestamp, and
optionally a GPIO state column.  The timestamps module cleans up rollovers.

```python
from msw_flir_bonsai.timestamps import preprocess_camera_csv

df = preprocess_camera_csv(
    "cam0__20260524_120000.csv",
    ts_cycle_s=128.0,       # FlyCapture cycle period; np.inf for Spinnaker
    counter_bits=32,
    session_start_s=None,   # or a Bpod event time to zero the clock
)
# df columns: frame_index, timestamp_raw, timestamp_s, frame_counter, gpio_state
```

### Detect dropped frames

```python
from msw_flir_bonsai.timestamps import detect_dropped_frames

dropped = detect_dropped_frames(df, expected_fps=60.0)
n_dropped = dropped.sum()
print(f"{n_dropped} dropped frames")
```

---

## Aligning camera timestamps to Bpod

### Barcode alignment

```python
from msw_flir_bonsai.alignment import extract_camera_barcodes, align_barcodes

cam_barcodes = extract_camera_barcodes(df, gpio_col="gpio_state", fps=60.0)
# bpod_barcodes: list of (time_s, barcode_int) from Bpod BNC events
offset_s = align_barcodes(bpod_barcodes, cam_barcodes, max_hamming=2)

df["bpod_time"] = df["timestamp_s"] + offset_s
```

### TTL-edge alignment (fallback)

```python
from msw_flir_bonsai.alignment import align_ttl_edges

# bpod_trial_times: list of trial-start times in Bpod clock
offset_s = align_ttl_edges(df, bpod_trial_times, gpio_col="gpio_state")
df["bpod_time"] = df["timestamp_s"] + offset_s
```
