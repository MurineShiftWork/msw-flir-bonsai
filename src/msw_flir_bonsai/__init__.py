"""msw-flir-bonsai: FLIR camera control via Bonsai for murine shift work experiments.

Provides a Python runner that launches Bonsai camera workflows as subprocesses,
plus timestamp preprocessing and barcode/TTL alignment utilities for
post-session data alignment to Bpod behavioural clocks.

Sub-modules:
    runner     -- BonsaiCameraRunner and MultiCameraRunner subprocess management
    timestamps -- CSV loading, timestamp unwrapping, dropped-frame detection
    alignment  -- barcode and TTL-edge alignment to Bpod clocks
    cli        -- msw-flir command-line interface (Typer/argparse)
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("msw_flir_bonsai")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"
