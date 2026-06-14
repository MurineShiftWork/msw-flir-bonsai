"""Camera timestamp preprocessing utilities.

FlyCapture and Spinnaker cameras embed hardware timestamps in each frame.
FlyCapture timestamps use a 128-second cycle counter that rolls over;
frame counters may also roll over at 16-bit or 32-bit boundaries.

This module provides:
  - ``unwrap_timestamps`` : remove rollovers from a cyclic timestamp array
  - ``load_camera_csv``   : load the Bonsai-written CSV into a DataFrame
  - ``preprocess_camera_csv``: full pipeline: load → unwrap → compute absolute times

The output is a DataFrame with columns:
  ``frame_index`` (int), ``timestamp_raw``, ``timestamp_s`` (float, unwrapped),
  ``frame_counter`` (int, unwrapped), ``gpio_state`` (int, optional)

These can be directly matched to Bpod trial events via barcode alignment
(see ``alignment.py``) or via TTL state changes.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

# FlyCapture embedded timestamp rolls over every 128 seconds.
_FLYCAP_TS_CYCLE_S: float = 128.0


def unwrap_cyclic(values: np.ndarray, period: float) -> np.ndarray:
    """Unwrap a cyclic sequence of values by detecting rollovers.

    Args:
        values: 1-D array of values that cycle in [0, period).
        period: The period at which the counter resets.

    Returns:
        Monotonically increasing array with the same dtype as *values*.
    """
    values = np.asarray(values, dtype=float)
    diff = np.diff(values)
    rollovers = np.where(diff < -period / 2)[0] + 1
    offsets = np.zeros(len(values), dtype=float)
    for idx in rollovers:
        offsets[idx:] += period
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


def load_camera_csv(path: str | Path) -> pd.DataFrame:
    """Load a Bonsai camera metadata CSV into a DataFrame.

    The CSV written by the Bonsai workflow has no header; columns are:
      frame_counter (int), timestamp_raw (float, seconds), gpio_state (int, optional)

    Args:
        path: Path to the ``.csv`` file written by Bonsai.

    Returns:
        DataFrame with columns inferred from column count.
    """
    path = Path(path)
    raw = pd.read_csv(path, header=None)

    if raw.shape[1] == 2:
        raw.columns = pd.Index(["frame_counter", "timestamp_raw"])
    elif raw.shape[1] == 3:
        raw.columns = pd.Index(["frame_counter", "timestamp_raw", "gpio_state"])
    else:
        raw.columns = pd.Index([f"col{i}" for i in range(raw.shape[1])])

    raw.insert(0, "frame_index", np.arange(len(raw)))
    return raw


def preprocess_camera_csv(
    path: str | Path,
    ts_cycle_s: float = _FLYCAP_TS_CYCLE_S,
    counter_bits: int = 32,
    session_start_s: float | None = None,
) -> pd.DataFrame:
    """Full preprocessing pipeline for a Bonsai camera CSV.

    Steps:
      1. Load CSV.
      2. Unwrap cyclic hardware timestamps (remove 128-s rollovers for FlyCapture).
      3. Unwrap frame counter rollovers.
      4. Optionally shift timestamps so t=0 is ``session_start_s``.

    Args:
        path: Path to Bonsai camera CSV.
        ts_cycle_s: Timestamp rollover period in seconds (128.0 for FlyCapture;
            set to ``np.inf`` for Spinnaker which does not cycle).
        counter_bits: Bit width of frame counter (32 for FlyCapture, 32 for Spinnaker).
        session_start_s: If given, subtract this from all timestamps so session t=0
            is the first behavioural event rather than camera power-on.

    Returns:
        DataFrame with columns: ``frame_index``, ``timestamp_raw``, ``timestamp_s``
        (unwrapped, optionally shifted), ``frame_counter`` (unwrapped), and any
        additional columns (e.g. ``gpio_state``) from the original CSV.
    """
    df = load_camera_csv(path)

    ts_unwrapped = unwrap_cyclic(df["timestamp_raw"].to_numpy(), period=ts_cycle_s)
    df["timestamp_s"] = ts_unwrapped

    if "frame_counter" in df.columns:
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
