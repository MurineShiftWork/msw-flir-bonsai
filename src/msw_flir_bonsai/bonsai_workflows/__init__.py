"""Bundled Bonsai workflow XMLs and path resolution utilities.

Four workflows are shipped as package data:
    run-flir-flycap-1cam       -- FlyCapture2, single camera (standard)
    run-flir-spinnaker-1cam    -- Spinnaker, single camera (standard)
    run-flir-flycap-2cam       -- FlyCapture2, two cameras in one process (legacy)
    run-flir-spinnaker-2cam    -- Spinnaker, two cameras in one process (legacy)

Use ``workflow_path(stem)`` to get an absolute filesystem path to any of these.
``BonsaiCameraRunner`` calls this internally; direct use is rarely needed.
"""

from pathlib import Path

WORKFLOW_DIR = Path(__file__).parent


def workflow_path(name: str) -> Path:
    """Return absolute path to a bundled .bonsai workflow file by stem name."""
    p = WORKFLOW_DIR / f"{name}.bonsai"
    if not p.exists():
        available = sorted(f.stem for f in WORKFLOW_DIR.glob("*.bonsai"))
        raise FileNotFoundError(f"Workflow '{name}' not found. Available: {available}")
    return p
