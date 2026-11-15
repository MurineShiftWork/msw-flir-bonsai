"""Camera timestamp preprocessing utilities.

The Bonsai workflows write one headerless CSV per camera with three columns,
in the order of the workflow's ``MemberSelector``:

- ``gpio_state``: line status bitmask. FlyCapture
  ``Metadata.embeddedGPIOPinState``, Spinnaker
  ``ChunkData.ExposureEndLineStatusAll``.
- ``timestamp_raw``: camera timestamp as written. FlyCapture
  ``Metadata.embeddedTimeStamp``, Spinnaker ``ChunkData.Timestamp``.
- ``frame_counter``: FlyCapture ``Metadata.embeddedFrameCounter``, Spinnaker
  ``ChunkData.FrameID``.

``gpio_state`` is a bitmask of all camera lines, not a 0/1 level: select one
line with ``line_state`` (FlyCapture barcode input: bit 31).

``timestamp_raw`` is decoded per driver into ``timestamp_s``:

- FlyCapture packs the cycle timer into a uint32: bits 31-25 seconds (mod 128),
  bits 24-12 cycle count (8000 Hz), bits 11-0 cycle offset (1/3072 of a cycle).
  The decoded seconds roll over every 128 s and are unwrapped.
- Spinnaker gives nanoseconds as a uint64 that does not wrap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

Driver = Literal["flycap", "spinnaker"]
DRIVERS: tuple[str, ...] = ("flycap", "spinnaker")

#: Column names of the workflow CSV, in file order.
CSV_COLUMNS: tuple[str, str, str] = ("gpio_state", "timestamp_raw", "frame_counter")

#: Line bit carrying the barcode/TTL input on the FlyCapture rigs.
FLYCAP_BARCODE_LINE_BIT: int = 31

# FlyCapture embedded timestamp: seconds field rolls over every 128 s.
_FLYCAP_TS_CYCLE_S: float = 128.0
_FLYCAP_CYCLE_HZ: float = 8000.0
_FLYCAP_OFFSET_PER_CYCLE: float = 3072.0


def _check_driver(driver: str) -> None:
    if driver not in DRIVERS:
        raise ValueError(f"unknown driver {driver!r}; use one of {DRIVERS}")


def unwrap_cyclic(values: np.ndarray, period: float) -> np.ndarray:
    """Unwrap a cyclic sequence of values by detecting rollovers.

    Args:
        values: 1-D array of values that cycle in [0, period).
        period: The period at which the counter resets.

    Returns:
        Monotonically increasing float array.
    """
    values = np.asarray(values, dtype=float)
    if len(values) == 0:
        return values
    rollovers = np.diff(values) < -period / 2
    offsets = np.concatenate([[0.0], np.cumsum(rollovers) * period])
    return values + offsets


def unwrap_counter(values: np.ndarray, bits: int = 32) -> np.ndarray:
    """Unwrap an integer frame counter that rolls over at 2**bits.

    Args:
        values: 1-D integer array of counter values.
        bits: Bit width of the counter (16 or 32).

    Returns:
        Monotonically increasing integer array.
    """
    period = 2**bits
    return unwrap_cyclic(np.asarray(values, dtype=float), period=float(period)).astype(
        np.int64
    )


def decode_flycap_timestamp(raw: np.ndarray) -> np.ndarray:
    """Decode packed FlyCapture ``embeddedTimeStamp`` words to seconds in [0, 128).

    Args:
        raw: 1-D array of uint32 register values.

    Returns:
        Float seconds, still cyclic with a 128 s period (see ``unwrap_cyclic``).
    """
    raw = np.asarray(raw, dtype=np.uint64)
    sec = (raw >> np.uint64(25)) & np.uint64(0x7F)
    count = (raw >> np.uint64(12)) & np.uint64(0x1FFF)
    offset = raw & np.uint64(0xFFF)
    return (
        sec.astype(float)
        + count.astype(float) / _FLYCAP_CYCLE_HZ
        + offset.astype(float) / (_FLYCAP_CYCLE_HZ * _FLYCAP_OFFSET_PER_CYCLE)
    )


def decode_timestamps(raw: np.ndarray, driver: Driver = "flycap") -> np.ndarray:
    """Convert a raw camera timestamp column to monotonic seconds.

    Args:
        raw: The ``timestamp_raw`` column.
        driver: ``"flycap"`` (packed cycle timer, unwrapped over 128 s) or
            ``"spinnaker"`` (nanoseconds).

    Returns:
        Float seconds on the camera clock.
    """
    _check_driver(driver)
    if driver == "flycap":
        return unwrap_cyclic(decode_flycap_timestamp(raw), period=_FLYCAP_TS_CYCLE_S)
    return np.asarray(raw, dtype=np.uint64).astype(float) / 1e9


def line_state(gpio: np.ndarray | pd.Series, bit: int) -> np.ndarray:
    """Return the 0/1 level of one line from the ``gpio_state`` bitmask.

    Args:
        gpio: Raw line status words.
        bit: Line bit to extract (FlyCapture barcode input: 31). The Spinnaker
            ``ExposureEndLineStatusAll`` bit depends on the wiring and must be
            confirmed on a recording.

    Returns:
        Integer array of 0/1.
    """
    words = np.asarray(gpio, dtype=np.uint64)
    return ((words >> np.uint64(bit)) & np.uint64(1)).astype(int)


def load_camera_csv(path: str | Path, driver: Driver = "flycap") -> pd.DataFrame:
    """Load a Bonsai camera metadata CSV into a DataFrame.

    The CSV has no header and three columns: ``gpio_state`` (line status
    bitmask), ``timestamp_raw`` (camera timestamp as written) and
    ``frame_counter``. See the module docstring for the per-driver fields.

    Args:
        path: Path to the ``.timestamps.csv`` file written by Bonsai.
        driver: ``"flycap"`` or ``"spinnaker"``; both write the same column
            order, the driver is checked here and used for decoding.

    Returns:
        DataFrame with ``frame_index`` followed by the three CSV columns.

    Raises:
        ValueError: If the file does not have three columns or the driver is
            unknown.
    """
    _check_driver(driver)
    path = Path(path)
    raw = pd.read_csv(path, header=None, dtype=np.uint64)
    if raw.shape[1] != len(CSV_COLUMNS):
        raise ValueError(
            f"{path.name}: expected {len(CSV_COLUMNS)} columns "
            f"{CSV_COLUMNS}, found {raw.shape[1]}"
        )
    raw.columns = pd.Index(CSV_COLUMNS)
    raw.insert(0, "frame_index", np.arange(len(raw)))
    return raw


def preprocess_camera_csv(
    path: str | Path,
    driver: Driver = "flycap",
    counter_bits: int | None = None,
    session_start_s: float | None = None,
) -> pd.DataFrame:
    """Load a Bonsai camera CSV and decode its timestamps.

    Steps:
      1. Load the CSV (``load_camera_csv``).
      2. Decode ``timestamp_raw`` per driver into ``timestamp_s``.
      3. Unwrap the FlyCapture 32-bit frame counter.
      4. Optionally shift timestamps so t=0 is ``session_start_s``.

    Args:
        path: Path to Bonsai camera CSV.
        driver: ``"flycap"`` (default) or ``"spinnaker"``.
        counter_bits: Frame counter width to unwrap. Default: 32 for FlyCapture,
            no unwrapping for the 64-bit Spinnaker ``FrameID``.
        session_start_s: If given, subtract this from all timestamps so session t=0
            is the first behavioural event rather than camera power-on.

    Returns:
        DataFrame with ``frame_index``, ``gpio_state``, ``timestamp_raw``,
        ``frame_counter`` and ``timestamp_s`` (seconds, monotonic, optionally
        shifted).
    """
    df = load_camera_csv(path, driver=driver)
    df["timestamp_s"] = decode_timestamps(df["timestamp_raw"].to_numpy(), driver)

    if counter_bits is None and driver == "flycap":
        counter_bits = 32
    if counter_bits is not None:
        df["frame_counter"] = unwrap_counter(
            df["frame_counter"].to_numpy(), bits=counter_bits
        )

    if session_start_s is not None:
        df["timestamp_s"] = df["timestamp_s"] - session_start_s

    return df


def detect_dropped_frames(df: pd.DataFrame, expected_fps: float) -> pd.Series:
    """Detect dropped frames: return True where gap to next frame exceeds 1.5× the IFI.

    Args:
        df: Preprocessed camera DataFrame with ``timestamp_s`` column.
        expected_fps: Nominal camera frame rate in Hz.

    Returns:
        Boolean Series aligned to df.index, True where a drop is suspected
        after that frame.
    """
    ifi = 1.0 / expected_fps
    dt = df["timestamp_s"].diff().abs()
    return dt > 1.5 * ifi
