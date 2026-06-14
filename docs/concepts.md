# Concepts

## Bonsai subprocess isolation

Each camera runs in a dedicated Bonsai subprocess launched by `BonsaiCameraRunner`.
The subprocess receives all parameters (workflow path, output directory, camera serial
number, frame rate) as command-line arguments and writes frame timestamps to a CSV file.

Isolation means a crash in one camera's subprocess does not terminate the others or the
main behaviour task. The Python layer monitors subprocess health via poll() and logs
failures without raising into the task.

## FlyCapture vs Spinnaker backends

The backend is determined by camera hardware, not software configuration:

- **FlyCapture2** (`Bonsai.PointGrey2` package): Chameleon3, Grasshopper3 (older USB3
  Gen 1 models). Requires the FlyCapture2 SDK from Teledyne FLIR.
- **Spinnaker** (`Bonsai.Spinnaker` package): Blackfly S, Oryx, and current-generation
  USB3/GigE models. Requires the Spinnaker SDK.

`make_camera_client(config)` reads the `backend` field of `CameraConfig` to select which
`BonsaiCameraRunner` subclass to instantiate. Multi-camera rigs with mixed hardware use
`MultiCameraRunner`, which fans commands out to one runner per camera.

## Workflow XML structure

Bonsai workflows are XML files shipped as package data under `bonsai_workflows/`. Each
workflow defines a reactive pipeline: camera source (FlyCapture or Spinnaker node) →
frame sink (VideoWriter) → timestamp sink (CsvWriter). Parameters exposed to the
command line are declared as `ExternalizedMapping` nodes in the XML, which `BonsaiCameraRunner`
populates when building the subprocess command.

To add a camera parameter (frame rate, resolution): add an `ExternalizedMapping` node
in the Bonsai editor, update `_build_cmd()` in the corresponding runner class to pass
the value as a named argument.

## Timestamp alignment

`timestamps.py` provides utilities for working with camera frame timestamps:

- `load_camera_csv(path)`: reads the Bonsai-generated CSV with a `Timestamp` column
- `unwrap_cyclic(timestamps)` / `unwrap_counter(timestamps)`: corrects for hardware
  counter rollover in raw timestamp streams
- `preprocess_camera_csv(path)`: convenience wrapper that loads, unwraps, and returns
  a clean timestamp array
- `detect_dropped_frames(timestamps, fps)`: identifies gaps larger than 1.5 frame
  periods, indicating dropped frames

`alignment.py` provides barcode and TTL-based alignment between camera timestamps and
behaviour event times. `extract_camera_barcodes()` decodes TTL barcode pulses from a
camera GPIO channel; `align_barcodes()` maps camera frame indices to behaviour trial
timestamps via matched barcode events.

## MSW integration

In MSW tasks, camera acquisition is managed by the task framework:

```python
from murineshiftwork.hardware.camera.client import make_camera_client
from murineshiftwork.logic.config import SetupConfig

config = SetupConfig.from_yaml("setups/rig-01.yaml")
camera = make_camera_client(config.cameras)
camera.start_recording(output_dir=session_dir)
# ... run task ...
camera.stop_recording()
```

The `FlirBonsaiClient` returned by `make_camera_client` wraps `BonsaiCameraRunner`
and implements the `CameraClientProtocol` expected by `TaskProcess`.
