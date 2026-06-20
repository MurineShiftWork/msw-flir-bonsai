"""Read a FLIR video acquisition directory written by the Bonsai runner.

Mirrors ``rpi_camera_ensemble.readers.load_session``: point at an acquisition
directory, get back a structured object reporting, per camera, whether the
video (``.avi``) and its frame/TTL timestamps (``.timestamps.csv``) are present
-- the completeness check a downstream loader needs before trusting a session.

File naming (written by the msw-core camera adapter + the Bonsai workflow):

    {acquisition_basename}.msw.{cam_label}.avi
    {acquisition_basename}.msw.{cam_label}.timestamps.csv

The ``.msw.`` separator and the per-camera ``cam_label`` (e.g. ``top.0``,
``cam0``) are produced by the namespace builder upstream; this module only
parses them back out. It does not assume one camera -- a multi-camera
acquisition yields one ``FlirCamera`` per label.

Usage:
    from msw_flir_bonsai.readers import load_session

    session = load_session("/path/to/subject__dt__video_flir__v1")
    session.is_complete                      # bool
    session.cameras["top.0"].video           # Path | None
    session.cameras["top.0"].frames          # DataFrame | None (load=True)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

log = logging.getLogger(__name__)

# A camera label may itself contain dots (``top.0``); the suffix is fixed, so
# anchor on it and let the label capture greedily up to the suffix.
_VIDEO_RE = re.compile(r"^.+\.msw\.(?P<label>.+)\.avi$")
_TS_RE = re.compile(r"^.+\.msw\.(?P<label>.+)\.timestamps\.csv$")


@dataclass
class FlirCamera:
    """One camera's artifacts within a video_flir acquisition."""

    label: str
    video: Path | None = None
    timestamps: Path | None = None
    frames: pd.DataFrame | None = None
    error: str | None = None

    @property
    def complete(self) -> bool:
        """True when both the video and its timestamps CSV are present."""
        return self.video is not None and self.timestamps is not None


@dataclass
class FlirSession:
    """A discovered FLIR video acquisition directory."""

    directory: Path
    cameras: dict[str, FlirCamera] = field(default_factory=dict)
    manifest: dict | None = None

    @property
    def is_complete(self) -> bool:
        """True when at least one camera was found and every camera is complete."""
        return bool(self.cameras) and all(c.complete for c in self.cameras.values())

    def summary(self) -> dict[str, dict[str, bool]]:
        """Per-camera present/complete flags for quick inspection."""
        return {
            label: {
                "video": cam.video is not None,
                "timestamps": cam.timestamps is not None,
                "complete": cam.complete,
                "frames_loaded": cam.frames is not None,
            }
            for label, cam in self.cameras.items()
        }

    @classmethod
    def from_directory(
        cls, directory: str | Path, *, load: bool = False
    ) -> FlirSession:
        """Discover (and optionally load) a video_flir acquisition directory."""
        directory = Path(directory)
        if not directory.is_dir():
            raise NotADirectoryError(f"not a directory: {directory}")

        cameras: dict[str, FlirCamera] = {}

        def _cam(label: str) -> FlirCamera:
            return cameras.setdefault(label, FlirCamera(label=label))

        for p in sorted(directory.iterdir()):
            if not p.is_file():
                continue
            m = _TS_RE.match(p.name)
            if m:
                _cam(m.group("label")).timestamps = p
                continue
            m = _VIDEO_RE.match(p.name)
            if m:
                _cam(m.group("label")).video = p

        session = cls(
            directory=directory,
            cameras=cameras,
            manifest=_read_manifest(directory),
        )
        if load:
            session._load_frames()

        n_complete = sum(c.complete for c in cameras.values())
        log.debug(
            "FlirSession %s: %d camera(s), %d complete",
            directory.name,
            len(cameras),
            n_complete,
        )
        return session

    def _load_frames(self) -> None:
        """Parse each present timestamps CSV; record (not raise) per-camera errors."""
        from msw_flir_bonsai.timestamps import preprocess_camera_csv

        for cam in self.cameras.values():
            if cam.timestamps is None:
                continue
            try:
                cam.frames = preprocess_camera_csv(cam.timestamps)
            except Exception as e:  # noqa: BLE001 - record, don't fail discovery
                cam.error = f"{type(e).__name__}: {e}"
                log.debug("frame load error %s: %s", cam.label, e)


def _read_manifest(directory: Path) -> dict | None:
    """Best-effort read of acquisition_manifest.yaml (None if absent/unreadable).

    YAML is optional for this package; if PyYAML is not installed the manifest
    is simply skipped -- completeness is determined from the files on disk, not
    the manifest.
    """
    p = directory / "acquisition_manifest.yaml"
    if not p.exists():
        return None
    try:
        import yaml
    except ImportError:  # pragma: no cover - yaml is optional for flir
        return None
    try:
        return yaml.safe_load(p.read_text()) or {}
    except Exception as e:  # noqa: BLE001
        log.debug("manifest read error %s: %s", p, e)
        return None


def load_session(directory: str | Path, *, load: bool = False) -> FlirSession:
    """Load one FLIR video acquisition directory and return a FlirSession.

    Canonical, discoverable entry point mirroring
    ``rpi_camera_ensemble.readers.load_session`` and
    ``murineshiftwork.readers.load_session``: pass the acquisition directory,
    get back a structured object reporting per-camera completeness.

    Args:
        directory: Path to the ``…__video_flir`` acquisition directory.
        load: If True, also parse each camera's timestamps CSV into a DataFrame
            (``FlirCamera.frames``). Default False (discovery only).

    Returns:
        A populated ``FlirSession``.

    Raises:
        NotADirectoryError: if ``directory`` does not exist or is not a directory.
    """
    return FlirSession.from_directory(directory, load=load)
