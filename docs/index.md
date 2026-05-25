# msw-flir-bonsai

FLIR camera control via Bonsai for murine shift work experiments

## Installation

```sh
pip install msw_flir_bonsai
```

## Quick start

```sh
# Find Bonsai.exe path on the acquisition machine
msw-flir find-bonsai

# List connected FLIR cameras and their indices
msw-flir list-cameras --driver flycap

# Run a 5-second test recording from camera 0
msw-flir test-record --cam-index 0 --fps 30 --duration 5

# Launch two cameras for a full session (blocks until Ctrl+C)
msw-flir run D:\DATA\video mouse001__20260524 --n-cameras 2 --fps 60
```

For Python API usage, timestamp preprocessing, and barcode alignment, see the
[Usage](usage/python.md) section.  For MSW setup YAML configuration, see the
[murineshiftwork setup docs](https://larsrollik.github.io/murineshiftwork/setup/).

## Development

```sh
git clone https://github.com/larsrollik/msw-flir-bonsai.git
cd msw-flir-bonsai
uv sync --extra dev
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
uv run pytest
```

## Docs

```sh
uv sync --extra docs
uv run mkdocs serve
```
