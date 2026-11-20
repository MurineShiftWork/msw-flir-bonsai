"""Barcode and TTL alignment between camera frames and behaviour timestamps.

Two alignment channels are available:

1. **TTL barcodes**: the ttl-barcoder barcodes Bpod sends on a BNC output, recorded
   on a camera GPIO input. They decode to the values Bpod logged, given the settings
   they were sent with (init and bit lengths, bit count).

2. **Trial TTL edges**: the sequence task (and others) pulse a BNC line at trial
   start/end.  The GPIO state column in the camera CSV captures these edges at
   frame resolution, providing a per-frame trial tag without needing a barcode.

The GPIO column is the camera's line status bitmask; both channels read one
line from it, selected by ``line_bit`` (default 31, the FlyCapture barcode
input). For Spinnaker recordings pass the bit of the wired line.

Typical workflow::

    from msw_flir_bonsai.alignment import align_barcodes, align_ttl_edges

    # --- barcode alignment ---
    cam_df = preprocess_camera_csv("cam1.csv")
    bpod_barcodes = [...]  # list of (time_s, barcode_int) from Bpod events
    cam_barcodes = extract_camera_barcodes(cam_df, barcode_config, line_bit=31)
    offset_s = align_barcodes(bpod_barcodes, cam_barcodes)

    # --- TTL-edge alignment (when barcode is absent/incomplete) ---
    bpod_trial_times = [...]  # list of trial-start times in Bpod clock
    offset_s = align_ttl_edges(cam_df, bpod_trial_times)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import numpy as np

from msw_flir_bonsai.timestamps import FLYCAP_BARCODE_LINE_BIT, line_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pandas as pd
    from ttl_barcoder.core.config import BarcodeConfig

    from msw_flir_bonsai.readers.session import FlirCamera

# ---------------------------------------------------------------------------
# Barcode extraction from camera GPIO


def _frames_of(obj: Any) -> pd.DataFrame:
    """``obj`` itself if it is a DataFrame, else its ``frames`` (a ``FlirCamera``)."""
    import pandas as pd

    if isinstance(obj, pd.DataFrame):
        return obj
    frames = getattr(obj, "frames", None)
    if frames is None:
        raise ValueError(
            "no frames loaded: pass a DataFrame, or a camera from "
            "load_session(..., load=True)"
        )
    return frames


def extract_camera_barcodes(
    df: pd.DataFrame | FlirCamera,
    barcode_config: BarcodeConfig | None = None,
    *,
    gpio_col: str = "gpio_state",
    line_bit: int = FLYCAP_BARCODE_LINE_BIT,
    inverted: bool = False,
) -> list[tuple[float, int]]:
    """Decode the barcodes on one camera GPIO line to the values Bpod sent.

    The line is sampled once per frame, so its edges are the frames where its level
    changes. Barcodes are found by their configured lengths (ttl-barcoder's stream
    decoder) with the frame interval as the sampling interval: a barcode starts at its
    first high frame, and the init/end framing is checked only when the init pulses
    span at least two frames. Without framing, a long high pulse can decode as a
    barcode; matching the values to Bpod's rejects it.

    Args:
        df: Preprocessed camera DataFrame with ``timestamp_s`` and *gpio_col*, or a
            ``FlirCamera`` from ``load_session(..., load=True)``.
        barcode_config: The settings the barcodes were sent with (the session's
            ``barcodes:``, or the task's barcode settings). None uses the ttl-barcoder
            defaults, with a warning.
        gpio_col: Column name holding the line status bitmask.
        line_bit: Bit of the barcode line in *gpio_col* (31 on FlyCapture; the
            Spinnaker bit depends on the wiring).
        inverted: The line reads low while the Bpod output is high.

    Returns:
        ``(time_s, value)`` per barcode: half a frame before its first high frame.
        Empty, with a warning, when a bit is shorter than three frames: over random
        frame phases most such barcodes decode to wrong values (35 ms bits at 60 fps
        decode exactly about a quarter of the time).
    """
    from ttl_barcoder.core.barcode_ttl import BarcodeTTL
    from ttl_barcoder.core.config import BarcodeConfig

    df = _frames_of(df)
    if gpio_col not in df.columns:
        raise KeyError(
            f"GPIO column '{gpio_col}' not found. Available: {list(df.columns)}"
        )
    if barcode_config is None:
        barcode_config = BarcodeConfig.default()
        logging.warning(
            "No barcode settings given: decoding camera barcodes with the "
            "ttl-barcoder defaults (%s)",
            barcode_config,
        )

    level = line_state(df[gpio_col].to_numpy(), line_bit).astype(bool)
    if inverted:
        level = ~level
    ts = df["timestamp_s"].to_numpy(dtype=float)
    if len(ts) < 2:
        return []
    frame_s = float(np.median(np.diff(ts)))
    if barcode_config.bit_duration_ms / 1000 < 3 * frame_s:
        logging.warning(
            "Camera barcodes not decoded: %.0f ms bits are shorter than three frames "
            "(%.1f fps). Send longer barcodes to this camera.",
            barcode_config.bit_duration_ms,
            1 / frame_s,
        )
        return []

    # Only level changes are edges: a line already high at the first frame has no
    # rising edge there, so no barcode can start at it.
    changes = np.flatnonzero(np.diff(level)) + 1
    edge_times = ts[changes].tolist()
    edge_levels = level[changes].tolist()
    found = BarcodeTTL(barcode_config).find_barcodes(
        edge_times, edge_levels, sample_interval_s=frame_s
    )
    return [(b.start_s, b.value) for b in found]


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
    line_bit: int = FLYCAP_BARCODE_LINE_BIT,
) -> float:
    """Estimate camera-to-Bpod offset by matching TTL rising edges.

    The sequence task and others pulse a Bpod BNC line at trial start/end.
    The camera records these as GPIO state changes.  This function cross-correlates
    the two edge sequences to find the best time offset.

    Args:
        cam_df: Preprocessed camera DataFrame.
        bpod_event_times_s: Trial event times (e.g. trial-start) in Bpod clock.
        gpio_col: Camera GPIO column name (line status bitmask).
        max_offset_search_s: Maximum |offset| to consider during search.
        min_matches: Minimum number of matching edges required.
        line_bit: Bit of the TTL line in *gpio_col* (31 on FlyCapture).

    Returns:
        ``offset_s`` such that ``bpod_time = camera_time + offset_s``.
    """
    gpio = line_state(cam_df[gpio_col].to_numpy(), line_bit)
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
