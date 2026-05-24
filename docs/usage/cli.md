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

```
msw-flir test-record [options]
```

**Options:**

| Option | Default | Description |
|---|---|---|
| `--output-dir` | `~/msw_flir_test` | Output directory |
| `--session` | `test` | Session name |
| `--cam-index`, `-c` | `0` | Camera index |
| `--driver` | `flycap` | Camera driver |
| `--fps` | `30` | Frame rate |
| `--duration` | `5.0` | Recording duration in seconds |
| `--bonsai-exe` | `$BONSAI_EXE` | Path to `Bonsai.exe` |
| `--workflow` | auto | Override workflow stem name |

**Example:**

```
msw-flir test-record --cam-index 1 --driver spinnaker --duration 10
```
