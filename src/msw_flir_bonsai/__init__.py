from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("msw_flir_bonsai")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "unknown"
