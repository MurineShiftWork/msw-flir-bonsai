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
always selects the 1-cam variant and launches N independent subprocesses — one
per camera index.  The 2-cam workflows are retained for reference but are not
used by the Python runner.

## External properties (Bonsai CLI `-p` flags)

Each 1-cam workflow exposes these properties, settable via `Bonsai.exe --start -p key=value`:

| Property | Type | Workflows | Description |
|---|---|---|---|
| `basepath` | string | all | Root output directory |
| `session` | string | all | Session base name |
| `cam1idx` | int | all | Camera index (0-based) |
| `cam1fps` | int | **flycap only** | Target frame rate — sets `FramesPerSecond` on the FlyCapture node |
| `cam1ts` | bool | flycap only | Enable embedded timestamp |
| `cam1framecounter` | bool | flycap only | Enable frame counter |

!!! note "Spinnaker frame rate"
    The Spinnaker workflow has no `cam1fps` externalized property — the frame rate
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
- **Stop**: `process.terminate()` → Bonsai catches the signal, flushes open
  files, and exits cleanly.

### Keyboard shortcuts in the workflow

The workflow XML references `Extensions\key-ctrl-start.bonsai` and
`Extensions\key-ctrl-stop.bonsai`.  These are Bonsai editor UI bindings —
they are **inactive in `--no-editor` mode** and can be ignored.

### Alternative control mechanisms

If per-trial start/stop (without relaunching Bonsai) is ever needed:

| Mechanism | Notes |
|---|---|
| **TCP `NetworkCommand`** | Add a `NetworkCommand` source to the workflow; Python sends a string over TCP to trigger transitions |
| **File-system sentinel** | Bonsai `FileSystemWatcher` watches for a file; Python creates/deletes it |
| **OSC** | Low-latency via Bonsai OSC package; common in neuroscience tooling |

For the standard MSW use case (one Bonsai process per session), `--start` +
`terminate()` is complete and correct.

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
