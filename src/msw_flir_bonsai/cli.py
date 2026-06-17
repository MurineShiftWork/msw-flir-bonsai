"""msw-flir CLI: standalone tool and MSW plugin subcommands.

Standalone usage (after pip install msw_flir_bonsai):
    msw-flir find-bonsai
    msw-flir list-cameras --driver flycap
    msw-flir run D:\\DATA\\video mouse001__20260524 --n-cameras 2 --fps 60
    msw-flir test-record --cam-index 0 --duration 5

Plugin usage (when registered as msw.cli entry-point and MSW is installed):
    msw flir find-bonsai
    msw flir list-cameras
    msw flir run ...
"""

from __future__ import annotations

import time
from pathlib import Path

import typer

app = typer.Typer(
    name="flir",
    help="FLIR camera tools: launch Bonsai workflows, inspect hardware.",
    no_args_is_help=True,
)

# Known Bonsai.exe install paths on Windows (searched in order).
_BONSAI_SEARCH_PATHS = [
    Path.home() / "AppData" / "Local" / "Bonsai" / "Bonsai.exe",
    Path("C:/Program Files/Bonsai/Bonsai.exe"),
    Path("C:/Program Files (x86)/Bonsai/Bonsai.exe"),
]


# ---------------------------------------------------------------------------
# find-bonsai


@app.command("find-bonsai")
def find_bonsai() -> None:
    """Scan known install locations and print the Bonsai.exe path.

    Copy the printed path into your setup YAML under cameras.bonsai_exe,
    or set the BONSAI_EXE environment variable.
    """
    for p in _BONSAI_SEARCH_PATHS:
        if p.exists():
            typer.echo(str(p))
            raise typer.Exit(0)
    lines = "\n".join(f"  {p}" for p in _BONSAI_SEARCH_PATHS)
    typer.echo(
        f"Bonsai.exe not found in known locations:\n{lines}\n\n"
        "Set BONSAI_EXE env var or pass --bonsai-exe explicitly.",
        err=True,
    )
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# list-cameras


@app.command("list-cameras")
def list_cameras(
    driver: str = typer.Option(
        "flycap",
        "--driver",
        "-d",
        help="Camera driver: flycap (FlyCapture2) or spinnaker (Spinnaker SDK)",
    ),
) -> None:
    """List connected FLIR cameras with their index and serial number.

    Requires the FlyCapture2 (PyCapture2) or Spinnaker (PySpin) SDK to be
    installed.  These are Windows-only SDKs; this command will fail on Linux.
    """
    if driver == "flycap":
        _list_flycap()
    elif driver == "spinnaker":
        _list_spinnaker()
    else:
        typer.echo(f"Unknown driver '{driver}'. Use flycap or spinnaker.", err=True)
        raise typer.Exit(1)


def _list_flycap() -> None:
    """Print connected FlyCapture2 cameras using PyCapture2."""
    try:
        import PyCapture2
    except ImportError as e:
        typer.echo(
            "PyCapture2 not installed. Install the FlyCapture2 SDK and Python wrapper.",
            err=True,
        )
        raise typer.Exit(1) from e

    bus = PyCapture2.BusManager()
    n = bus.getNumOfCameras()
    typer.echo(f"FlyCapture2: {n} camera(s) found")
    for i in range(n):
        cam = PyCapture2.Camera()
        cam.connect(bus.getCameraFromIndex(i))
        info = cam.getCameraInfo()
        model = (
            info.modelName.decode()
            if isinstance(info.modelName, bytes)
            else info.modelName
        )
        typer.echo(f"  [{i}]  serial={info.serialNumber}  model={model}")
        cam.disconnect()


def _list_spinnaker() -> None:
    """Print connected Spinnaker cameras using PySpin."""
    try:
        import PySpin
    except ImportError as e:
        typer.echo(
            "PySpin not installed. Install the Spinnaker SDK and Python wrapper.",
            err=True,
        )
        raise typer.Exit(1) from e

    system = PySpin.System.GetInstance()
    cam_list = system.GetCameras()
    n = cam_list.GetSize()
    typer.echo(f"Spinnaker: {n} camera(s) found")
    for i in range(n):
        cam = cam_list.GetByIndex(i)
        cam.Init()
        nodemap = cam.GetNodeMap()
        sn = PySpin.CStringPtr(nodemap.GetNode("DeviceSerialNumber")).GetValue()
        model = PySpin.CStringPtr(nodemap.GetNode("DeviceModelName")).GetValue()
        typer.echo(f"  [{i}]  serial={sn}  model={model}")
        cam.DeInit()
    cam_list.Clear()
    system.ReleaseInstance()


# ---------------------------------------------------------------------------
# run  (multi-camera launcher: blocks until Ctrl+C or all processes exit)


@app.command()
def run(
    output_dir: Path = typer.Argument(..., help="Root directory for video output"),
    session: str = typer.Argument(
        ..., help="Session name (used for folder/file naming)"
    ),
    n_cameras: int = typer.Option(
        1, "--n-cameras", "-n", help="Number of cameras to launch"
    ),
    driver: str = typer.Option(
        "flycap", "--driver", "-d", help="Camera driver: flycap or spinnaker"
    ),
    fps: int = typer.Option(60, "--fps", help="Target frame rate (FlyCapture only)"),
    bonsai_exe: Path | None = typer.Option(
        None,
        "--bonsai-exe",
        envvar="BONSAI_EXE",
        help="Path to Bonsai.exe (falls back to BONSAI_EXE env var)",
    ),
    workflow: str = typer.Option(
        "",
        "--workflow",
        help="Override workflow stem name (default: run-flir-{driver}-1cam)",
    ),
) -> None:
    """Launch N camera processes (one per camera index) and block until stopped.

    Each camera runs an independent Bonsai subprocess using the 1-cam workflow.
    Press Ctrl+C to stop all cameras cleanly.

    Examples:
        msw-flir run D:\\DATA\\video mouse001 --n-cameras 2 --fps 60
        msw-flir run D:\\DATA\\video test --driver spinnaker
    """
    from msw_flir_bonsai.runner import MultiCameraRunner

    runner = MultiCameraRunner.from_config(
        n_cameras=n_cameras,
        driver=driver,
        output_dir=output_dir,
        session=session,
        fps=fps,
        bonsai_exe=str(bonsai_exe) if bonsai_exe else None,
        workflow=workflow,
    )

    runner.start()
    typer.echo(
        f"Started {n_cameras} camera process(es)  driver={driver}  fps={fps}\n"
        "Press Ctrl+C to stop."
    )

    try:
        while runner.any_running:
            time.sleep(0.5)
        typer.echo("All camera processes exited.")
    except KeyboardInterrupt:
        typer.echo("\nStopping cameras...")
        runner.stop()


# ---------------------------------------------------------------------------
# test-record  (quick sanity check: one camera, fixed duration)


@app.command("test-record")
def test_record(
    output_dir: Path = typer.Option(
        Path.home() / "msw_flir_test",
        "--output-dir",
        help="Output directory for the test recording",
    ),
    session: str = typer.Option("test", "--session", help="Session name"),
    cam_index: int = typer.Option(
        0, "--cam-index", "-c", help="Camera index (0-based)"
    ),
    driver: str = typer.Option(
        "flycap", "--driver", help="Camera driver: flycap or spinnaker"
    ),
    fps: int = typer.Option(30, "--fps", help="Target frame rate"),
    duration: float = typer.Option(
        5.0, "--duration", help="Recording duration in seconds"
    ),
    bonsai_exe: Path | None = typer.Option(None, "--bonsai-exe", envvar="BONSAI_EXE"),
    workflow: str = typer.Option(
        "",
        "--workflow",
        help="Override workflow stem (default: run-flir-{driver}-1cam)",
    ),
) -> None:
    """Run a short test recording from a single camera to verify the setup.

    Useful for checking camera index, frame rate, and output path before
    a real session.  Duration defaults to 5 seconds.
    """
    from msw_flir_bonsai.runner import BonsaiCameraRunner

    resolved_workflow = workflow or f"run-flir-{driver}-1cam"
    output_dir.mkdir(parents=True, exist_ok=True)
    runner = BonsaiCameraRunner(
        workflow=resolved_workflow,
        acqdir=str(output_dir),
        cam_basename=session,
        cam_index=cam_index,
        fps=fps,
        driver=driver,
        bonsai_exe=str(bonsai_exe) if bonsai_exe else None,
    )

    typer.echo(
        f"Test recording: cam_index={cam_index}  driver={driver}  fps={fps}  "
        f"duration={duration}s"
    )
    runner.start()
    try:
        time.sleep(duration)
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        runner.wait(timeout=10)

    typer.echo(f"Done. Output written to: {output_dir}")


# ---------------------------------------------------------------------------
# Entry point for the standalone console script


def main() -> None:
    """Entry point for the ``msw-flir`` console script."""
    app()


# ---------------------------------------------------------------------------
# MSW argparse plugin registration
#
# The MSW CLI plugin loader calls ep.load()(sub_parsers) where sub_parsers is
# an argparse _SubParsersAction.  This register() function adds a "flir"
# subparser that passes all remaining args through to the Typer app, avoiding
# duplication of argument definitions.


def _dispatch_flir(args: dict) -> None:
    """Forward remaining CLI args to the Typer app when invoked via ``msw flir``."""
    import sys

    flir_args: list[str] = args.get("flir_args", []) or ["--help"]
    old_argv = sys.argv[:]
    sys.argv = ["msw-flir"] + flir_args
    try:
        main()
    finally:
        sys.argv = old_argv


def register(sub_parsers: object) -> None:
    """Register flir subcommands with the MSW argparse parser.

    Called by the MSW CLI plugin loader at startup.  Usage after registration:
        msw flir find-bonsai
        msw flir list-cameras --driver flycap
        msw flir run <output_dir> <session> --n-cameras 2
        msw flir test-record --cam-index 0 --duration 5
    """
    import argparse

    p = sub_parsers.add_parser(  # type: ignore[attr-defined]
        "flir",
        help="FLIR camera tools (msw-flir-bonsai): run 'msw flir --help'",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("flir_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p.set_defaults(func=_dispatch_flir)
