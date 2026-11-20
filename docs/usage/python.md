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

Bonsai writes a headerless CSV per camera with three columns, in this order:

| Column | FlyCapture | Spinnaker |
|---|---|---|
| `gpio_state` | `embeddedGPIOPinState` | `ExposureEndLineStatusAll` |
| `timestamp_raw` | `embeddedTimeStamp` (packed cycle timer) | `Timestamp` (ns) |
| `frame_counter` | `embeddedFrameCounter` | `FrameID` |

`preprocess_camera_csv` decodes `timestamp_raw` per driver into `timestamp_s`.
FlyCapture packs seconds mod 128 (bits 31-25), 8 kHz cycles (bits 24-12) and
1/3072 cycle offsets (bits 11-0); the 128 s rollover is unwrapped. Spinnaker
nanoseconds are divided by 1e9.

```python
from msw_flir_bonsai.timestamps import preprocess_camera_csv

df = preprocess_camera_csv(
    "cam0.timestamps.csv",
    driver="flycap",        # or "spinnaker"
    session_start_s=None,   # or a Bpod event time to zero the clock
)
# df columns: frame_index, gpio_state, timestamp_raw, frame_counter, timestamp_s
```

`gpio_state` is the bitmask of all camera lines. Read one line with
`line_state(df["gpio_state"], bit)`; the FlyCapture barcode input is bit 31.

### Detect dropped frames

```python
from msw_flir_bonsai.timestamps import detect_dropped_frames

dropped = detect_dropped_frames(df, expected_fps=60.0)
n_dropped = dropped.sum()
print(f"{n_dropped} dropped frames")
```

---

## Aligning camera timestamps to Bpod

Both functions read one line of `gpio_state`, chosen by `line_bit` (default 31,
the FlyCapture barcode input). The Spinnaker line bit depends on the wiring:
confirm it on a recording and pass it explicitly.

### Barcode alignment

The camera must decode with the settings Bpod sent the barcodes with (the session's
`barcodes:`, or the task's barcode settings). Bits need about three frames: 100 ms bits
for a 60 fps camera. Shorter bits are refused with a warning.

```python
from ttl_barcoder import BarcodeConfig
from msw_flir_bonsai.alignment import extract_camera_barcodes, align_barcodes

config = BarcodeConfig(bit_duration_ms=100, init_duration_ms=100)
cam_barcodes = extract_camera_barcodes(df, config, line_bit=31)
# bpod_barcodes: list of (time_s, barcode_int) from Bpod BNC events
offset_s = align_barcodes(bpod_barcodes, cam_barcodes, max_hamming=2)

df["bpod_time"] = df["timestamp_s"] + offset_s
```

### TTL-edge alignment (fallback)

```python
from msw_flir_bonsai.alignment import align_ttl_edges

# bpod_trial_times: list of trial-start times in Bpod clock
offset_s = align_ttl_edges(df, bpod_trial_times, line_bit=31)
df["bpod_time"] = df["timestamp_s"] + offset_s
```
