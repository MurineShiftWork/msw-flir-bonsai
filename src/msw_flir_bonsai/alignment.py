"""Barcode and TTL alignment between camera frames and behaviour timestamps.

Two alignment channels are available:

1. **TTL barcodes**: periodic binary barcode pulses recorded both on the camera
   GPIO input and on the Bpod BNC output.  A barcode sequence can be matched even
   when partial (some bits missing) using Hamming-distance matching.

2. **Trial TTL edges**: the sequence task (and others) pulse a BNC line at trial
   start/end.  The GPIO state column in the camera CSV captures these edges at
   frame resolution, providing a per-frame trial tag without needing a barcode.

Typical workflow::

    from msw_flir_bonsai.alignment import align_barcodes, align_ttl_edges

    # --- barcode alignment ---
    cam_df = preprocess_camera_csv("cam1.csv")
    bpod_barcodes = [...]  # list of (time_s, barcode_int) from Bpod events
    cam_barcodes = extract_camera_barcodes(cam_df, gpio_col="gpio_state")
    offset_s = align_barcodes(bpod_barcodes, cam_barcodes)

    # --- TTL-edge alignment (when barcode is absent/incomplete) ---
    bpod_trial_times = [...]  # list of trial-start times in Bpod clock
    offset_s = align_ttl_edges(cam_df, bpod_trial_times)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd

# ---------------------------------------------------------------------------
# Barcode extraction from camera GPIO


def extract_camera_barcodes(
    df: pd.DataFrame,
    gpio_col: str = "gpio_state",
    fps: float = 60.0,
    barcode_bit_duration_s: float = 0.1,
) -> list[tuple[float, int]]:
    """Extract binary barcodes from the GPIO state column.

    A barcode is a fixed-length burst of high/low GPIO transitions.  This
    function detects rising edges and groups subsequent bits into integer
    barcode values.

    Args:
        df: Preprocessed camera DataFrame with ``timestamp_s`` and *gpio_col*.
        gpio_col: Column name holding the binary GPIO state (0/1 or 0/255).
        fps: Camera frame rate used to compute bit window sizes.
        barcode_bit_duration_s: Duration of each barcode bit in seconds.

    Returns:
        List of ``(time_s, barcode_int)`` tuples, one per detected barcode.
    """
    if gpio_col not in df.columns:
        raise KeyError(
            f"GPIO column '{gpio_col}' not found. Available: {list(df.columns)}"
        )

    gpio = (df[gpio_col].to_numpy() > 0).astype(int)
    ts = df["timestamp_s"].to_numpy()
    frames_per_bit = max(1, int(round(fps * barcode_bit_duration_s)))

    rising = np.where((gpio[1:] == 1) & (gpio[:-1] == 0))[0] + 1
    barcodes: list[tuple[float, int]] = []
    used: set[int] = set()

    for edge in rising:
        if edge in used:
            continue
        bits: list[int] = []
        pos = edge
        while pos < len(gpio):
            window = gpio[pos : pos + frames_per_bit]
            if len(window) < frames_per_bit // 2:
                break
            bits.append(int(window.mean() > 0.5))
            used.update(range(pos, pos + frames_per_bit))
            pos += frames_per_bit
            if len(bits) >= 32:
                break
        if bits:
            value = int("".join(str(b) for b in bits), 2)
            barcodes.append((float(ts[edge]), value))

    return barcodes


# ---------------------------------------------------------------------------
# Barcode alignment (handles incomplete sequences)


def align_barcodes(
    bpod_barcodes: Sequence[tuple[float, int]],
    camera_barcodes: Sequence[tuple[float, int]],
    max_hamming: int = 2,
) -> float:
    """Compute the camera-to-Bpod time offset using matched barcode pairs.

    Matching is done by Hamming distance on the barcode integer values, tolerating
    up to *max_hamming* bit errors (handles partial/corrupted barcodes).  The
    median offset across all matched pairs is returned for robustness.

    Args:
        bpod_barcodes: ``(time_s, value)`` pairs from Bpod.
        camera_barcodes: ``(time_s, value)`` pairs from camera GPIO.
        max_hamming: Maximum bit-error count to accept as a match.

    Returns:
        ``offset_s`` such that ``bpod_time = camera_time + offset_s``.

    Raises:
        ValueError: If fewer than 2 barcode pairs can be matched.
    """

    def hamming(a: int, b: int) -> int:
        return bin(a ^ b).count("1")

    offsets = []
    for cam_t, cam_val in camera_barcodes:
        best = min(bpod_barcodes, key=lambda bc: hamming(bc[1], cam_val))
        if hamming(best[1], cam_val) <= max_hamming:
            offsets.append(best[0] - cam_t)

    if len(offsets) < 2:
        raise ValueError(
            f"Too few barcode matches ({len(offsets)}) to compute a reliable offset. "
            "Check GPIO recording and barcode timing."
        )

    offset = float(np.median(offsets))
    jitter = float(np.std(offsets))
    logging.info(
        f"Barcode alignment: {len(offsets)} matches, "
        f"offset={offset:.4f}s, jitter={jitter:.4f}s"
    )
    return offset


# ---------------------------------------------------------------------------
# TTL-edge alignment (fallback when barcodes are incomplete)


def align_ttl_edges(
    cam_df: pd.DataFrame,
    bpod_event_times_s: Sequence[float],
    gpio_col: str = "gpio_state",
    max_offset_search_s: float = 60.0,
    min_matches: int = 5,
) -> float:
    """Estimate camera-to-Bpod offset by matching TTL rising edges.

    The sequence task and others pulse a Bpod BNC line at trial start/end.
    The camera records these as GPIO state changes.  This function cross-correlates
    the two edge sequences to find the best time offset.

    Args:
        cam_df: Preprocessed camera DataFrame.
        bpod_event_times_s: Trial event times (e.g. trial-start) in Bpod clock.
        gpio_col: Camera GPIO column name.
        max_offset_search_s: Maximum |offset| to consider during search.
        min_matches: Minimum number of matching edges required.

    Returns:
        ``offset_s`` such that ``bpod_time = camera_time + offset_s``.
    """
    gpio = (cam_df[gpio_col].to_numpy() > 0).astype(int)
    ts = cam_df["timestamp_s"].to_numpy()
    rising_idx = np.where((gpio[1:] == 1) & (gpio[:-1] == 0))[0] + 1
    cam_edge_times = ts[rising_idx]

    bpod_arr = np.asarray(bpod_event_times_s)

    # Search over candidate offsets = differences between all pairs of edges
    candidates = []
    for ce in cam_edge_times:
        for be in bpod_arr:
            candidates.append(be - ce)

    candidates_arr = np.asarray(candidates)
    candidates_arr = candidates_arr[np.abs(candidates_arr) <= max_offset_search_s]

    if len(candidates_arr) == 0:
        raise ValueError(
            "No candidate offsets within search range: check GPIO and event times."
        )

    # Histogram to find consensus offset
    hist, edges = np.histogram(candidates_arr, bins=int(max_offset_search_s * 20))
    best_bin = int(np.argmax(hist))
    offset = float((edges[best_bin] + edges[best_bin + 1]) / 2)

    matches = int(hist[best_bin])
    if matches < min_matches:
        raise ValueError(
            f"TTL alignment found only {matches} matching edges (need {min_matches}). "
            "Consider increasing max_offset_search_s or checking GPIO."
        )

    logging.info(f"TTL-edge alignment: {matches} matching edges, offset={offset:.4f}s")
    return offset
