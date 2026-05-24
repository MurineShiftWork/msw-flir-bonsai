from pathlib import Path

WORKFLOW_DIR = Path(__file__).parent


def workflow_path(name: str) -> Path:
    """Return absolute path to a bundled .bonsai workflow file by stem name."""
    p = WORKFLOW_DIR / f"{name}.bonsai"
    if not p.exists():
        available = sorted(f.stem for f in WORKFLOW_DIR.glob("*.bonsai"))
        raise FileNotFoundError(f"Workflow '{name}' not found. Available: {available}")
    return p
