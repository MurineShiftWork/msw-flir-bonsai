# msw-flir-bonsai

FLIR camera control via Bonsai for murine shift work experiments

## Installation

```sh
pip install msw_flir_bonsai
```

## Usage

_Add usage examples here._

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
