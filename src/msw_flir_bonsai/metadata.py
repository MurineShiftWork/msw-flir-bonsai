"""Per-camera metadata file written next to each recording.

The runner writes ``{cam_basename}.camera.json`` into the acquisition directory when a
camera starts, before Bonsai opens its writers. It records what a reader needs to decode
the files without being told: the driver, the workflow, the CSV column order and the
timestamp encoding. Writing it is best-effort and never stops a recording.

Recordings without the file predate it; they were all made with FlyCapture.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from msw_flir_bonsai.timestamps import CSV_COLUMNS

FORMAT_VERSION = 1
SUFFIX = ".camera.json"

#: Frame rate of the FlyCapture workflow when no ``cam1fps`` is passed.
FLYCAP_WORKFLOW_FPS = 60.0

#: Timestamp encoding of the CSV's ``timestamp_raw`` column, per driver.
TIMESTAMP_ENCODING = {
    "flycap": "flycap_cycle_timer",  # packed uint32, 128 s period
    "spinnaker": "nanoseconds",
}

log = logging.getLogger(__name__)


def camera_metadata(
    *, driver: str, workflow: str, cam_index: int, fps: float | None
) -> dict[str, Any]:
    """The metadata document for one camera."""
    from msw_flir_bonsai import __version__

    return {
        "format_version": FORMAT_VERSION,
        "writer": f"msw-flir-bonsai {__version__}",
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "driver": driver,
        "workflow": workflow,
        "camera_index": cam_index,
        "fps_requested": fps,
        **_fps_in_effect(driver, fps),
        "csv_columns": list(CSV_COLUMNS),
        "timestamp_encoding": TIMESTAMP_ENCODING.get(driver, "unknown"),
    }


def write_camera_metadata(
    acqdir: str | Path, cam_basename: str, meta: dict[str, Any]
) -> Path | None:
    """Write ``{cam_basename}.camera.json`` into ``acqdir``; None if it fails."""
    path = Path(acqdir) / f"{cam_basename}{SUFFIX}"
    try:
        path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning(f"could not write camera metadata {path}: {exc}")
        return None
    return path


def read_camera_metadata(path: str | Path) -> dict[str, Any] | None:
    """Read a camera metadata file; None if missing or unreadable."""
    try:
        meta = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug(f"camera metadata {path}: {exc}")
        return None
    return meta if isinstance(meta, dict) else None


def _fps_in_effect(driver: str, fps: float | None) -> dict[str, Any]:
    """The frame rate the camera runs at and where it comes from.

    FlyCapture takes ``cam1fps`` or the workflow default. Spinnaker cannot be set from
    the workflow: the camera's default user set decides, so the rate is not known here.
    """
    if driver == "flycap":
        if fps is not None:
            return {"fps": float(fps), "fps_source": "cam1fps"}
        return {"fps": FLYCAP_WORKFLOW_FPS, "fps_source": "workflow default"}
    return {"fps": None, "fps_source": "camera user set"}
