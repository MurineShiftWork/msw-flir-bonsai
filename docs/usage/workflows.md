# Bonsai workflows

## Bundled workflows

Four Bonsai workflow XMLs are shipped as package data and located at runtime
via `msw_flir_bonsai.bonsai_workflows.workflow_path(name)`.

| Stem name | Driver | Cameras |
|---|---|---|
| `run-flir-flycap-1cam` | FlyCapture2 | 1 |
| `run-flir-spinnaker-1cam` | Spinnaker | 1 |
| `run-flir-flycap-2cam` | FlyCapture2 | 2 (legacy single-process) |
| `run-flir-spinnaker-2cam` | Spinnaker | 2 (legacy single-process) |

**The 1-cam workflows are the standard path.**  `MultiCameraRunner.from_config`
always selects the 1-cam variant and launches N independent subprocesses: one
per camera index.  The 2-cam workflows are retained for reference but are not
used by the Python runner.

## External properties (Bonsai CLI `-p` flags)

Each 1-cam workflow exposes these properties, settable via `Bonsai.exe --start -p key=value`:

| Property | Type | Workflows | Description |
|---|---|---|---|
| `basepath` | string | all | Root output directory |
| `session` | string | all | Session base name |
| `cam1idx` | int | all | Camera index (0-based) |
| `cam1fps` | int | **flycap only** | Target frame rate: sets `FramesPerSecond` on the FlyCapture node |
| `cam1ts` | bool | flycap only | Enable embedded timestamp |
| `cam1framecounter` | bool | flycap only | Enable frame counter |

!!! note "Spinnaker frame rate"
    The Spinnaker workflow has no `cam1fps` externalized property: the frame rate
    is not settable via CLI `-p`.  Set it directly in the Bonsai workflow XML
    (open in the Bonsai editor, find the SpinnakerCapture node, set `AcquisitionFrameRate`).

The `BonsaiCameraRunner._build_cmd()` method assembles these into the subprocess
command automatically.

## Control model

Bonsai is launched with `--start --no-editor` so the workflow begins
immediately without user interaction:

```
Bonsai.exe workflow.bonsai --start --no-editor -p basepath="D:\..." -p session="..." ...
```

- **Start**: the `--start` flag; no keyboard or socket needed.
- **Stop**: `stop()` creates a stop file whose path is passed to Bonsai in
  `MSW_BONSAI_STOP_FILE`. `Extensions\key-ctrl-stop.bonsai` checks for it every
  0.2 s (and for the caller's process, `MSW_BONSAI_PARENT_PID`, having exited)
  and then completes the stream, so the video and CSV writers flush and close
  and Bonsai exits on its own. If that does not happen within the timeout, the
  process is killed; on Windows the whole process tree (`taskkill /T`), because
  `Bonsai.exe` relaunches itself as a child worker.
- Bonsai runs in its own process group, so a Ctrl+C in the calling console does
  not reach it before the caller has shut down and requested the stop.

### Bundled extension workflows

Every bundled workflow includes three extension workflows from `Extensions\`,
shipped with the package:

| file | role |
|---|---|
| `key-ctrl-stop.bonsai` | completes the stream on the stop file (see above) |
| `key-ctrl-start.bonsai` | pass-through: start is handled by `--start` |
| `measure-rate.bonsai` | pass-through: no rate display in `--no-editor` mode |

### Live preview

If a workflow has a `<workflow>.bonsai.layout` next to it (bundled for
`run-flir-flycap-1cam`), each camera starts with `--visualizer-layout` pointing
at its own copy of that layout, with the preview window shifted right by camera
index so several cameras sit side by side. The copy is deleted on `stop()`.

### Alternative control mechanisms

If per-trial start/stop (without relaunching Bonsai) is ever needed:

| Mechanism | Notes |
|---|---|
| **TCP `NetworkCommand`** | Add a `NetworkCommand` source to the workflow; Python sends a string over TCP to trigger transitions |
| **File-system sentinel** | Bonsai `FileSystemWatcher` watches for a file; Python creates/deletes it |
| **OSC** | Low-latency via Bonsai OSC package; common in neuroscience tooling |

For the standard MSW use case (one Bonsai process per session), `--start` plus
the stop file is sufficient.

## Output structure

The IronPython transform inside the workflow creates:

```
<basepath>/
  <session>/
    <session>__<YYYYMMDD_HHMMSS>/
      <session>__<YYYYMMDD_HHMMSS>__cam0.avi   # video
      <session>__<YYYYMMDD_HHMMSS>__cam0.csv   # metadata (counter, timestamp, gpio)
```

The `preprocess_camera_csv` function loads and cleans the CSV;
see [Python API](python.md) for details.

## Locating bundled workflows

```python
from msw_flir_bonsai.bonsai_workflows import workflow_path

p = workflow_path("run-flir-flycap-1cam")
print(p)  # absolute path inside the installed package
```

This is what `BonsaiCameraRunner` calls internally.
