"""Unit tests for msw_flir_bonsai.alignment: pure numpy/pandas, no hardware needed."""

from __future__ import annotations

import pandas as pd
import pytest

from msw_flir_bonsai.alignment import (
    align_barcodes,
    align_ttl_edges,
    extract_camera_barcodes,
)

# ---------------------------------------------------------------------------
# Helpers


def _make_df(timestamps: list[float], gpio: list[int]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "frame_index": range(len(timestamps)),
            "timestamp_s": timestamps,
            "gpio_state": gpio,
        }
    )


def _gpio_pulse(n_frames: int, pulse_start: int, pulse_len: int) -> list[int]:
    """Return a GPIO array with a single high pulse."""
    g = [0] * n_frames
    for i in range(pulse_start, min(pulse_start + pulse_len, n_frames)):
        g[i] = 1
    return g


# ---------------------------------------------------------------------------
# extract_camera_barcodes


class TestExtractCameraBarcodes:
    def test_missing_column_raises(self) -> None:
        df = pd.DataFrame({"timestamp_s": [0.0, 1.0], "frame_index": [0, 1]})
        with pytest.raises(KeyError, match="GPIO column"):
            extract_camera_barcodes(df, gpio_col="missing")

    def test_no_rising_edges_returns_empty(self) -> None:
        df = _make_df([i * 0.016 for i in range(100)], [0] * 100)
        result = extract_camera_barcodes(df)
        assert result == []

    def test_constant_high_no_barcode(self) -> None:
        df = _make_df([i * 0.016 for i in range(100)], [1] * 100)
        result = extract_camera_barcodes(df)
        assert result == []

    def test_single_pulse_produces_one_barcode(self) -> None:
        fps = 60.0
        bit_dur = 0.1
        frames_per_bit = int(round(fps * bit_dur))  # 6 frames per bit
        n = 300
        gpio = _gpio_pulse(n, pulse_start=20, pulse_len=frames_per_bit)
        ts = [i / fps for i in range(n)]
        df = _make_df(ts, gpio)
        result = extract_camera_barcodes(df, fps=fps, barcode_bit_duration_s=bit_dur)
        assert len(result) >= 1
        t, val = result[0]
        assert t == pytest.approx(ts[20], abs=0.05)

    def test_returns_list_of_tuples(self) -> None:
        fps = 60.0
        n = 200
        gpio = _gpio_pulse(n, 10, 6)
        ts = [i / fps for i in range(n)]
        df = _make_df(ts, gpio)
        result = extract_camera_barcodes(df, fps=fps)
        for item in result:
            assert isinstance(item, tuple)
            assert len(item) == 2


# ---------------------------------------------------------------------------
# align_barcodes


class TestAlignBarcodes:
    def _make_pairs(
        self, n: int, offset: float
    ) -> tuple[list[tuple[float, int]], list[tuple[float, int]]]:
        bpod = [(float(i), i * 100 + 1) for i in range(n)]
        camera = [(float(i) - offset, i * 100 + 1) for i in range(n)]
        return bpod, camera

    def test_exact_match_returns_correct_offset(self) -> None:
        bpod, camera = self._make_pairs(10, offset=1.5)
        result = align_barcodes(bpod, camera)
        assert result == pytest.approx(1.5, abs=0.001)

    def test_hamming_tolerance_handles_bit_errors(self) -> None:
        bpod = [(float(i), i * 100 + 1) for i in range(10)]
        # Flip one bit in some camera barcodes
        camera = [(float(i) - 2.0, (i * 100 + 1) ^ 1) for i in range(10)]
        result = align_barcodes(bpod, camera, max_hamming=2)
        assert result == pytest.approx(2.0, abs=0.01)

    def test_too_few_matches_raises(self) -> None:
        bpod = [(0.0, 1), (1.0, 2)]
        camera = [(0.0, 9999999)]  # no plausible match
        with pytest.raises(ValueError, match="Too few barcode matches"):
            align_barcodes(bpod, camera, max_hamming=0)

    def test_negative_offset(self) -> None:
        bpod, camera = self._make_pairs(8, offset=-3.0)
        result = align_barcodes(bpod, camera)
        assert result == pytest.approx(-3.0, abs=0.01)


# ---------------------------------------------------------------------------
# align_ttl_edges


class TestAlignTtlEdges:
    def _make_cam_df(
        self, n_frames: int, fps: float, edge_frames: list[int]
    ) -> pd.DataFrame:
        ts = [i / fps for i in range(n_frames)]
        gpio = [0] * n_frames
        for f in edge_frames:
            gpio[f] = 1
            if f + 1 < n_frames:
                gpio[f + 1] = 1
        return _make_df(ts, gpio)

    def test_matching_edges_returns_offset(self) -> None:
        fps = 60.0
        offset = 5.0
        edge_frames = [60, 180, 300, 420, 540, 660]
        df = self._make_cam_df(800, fps, edge_frames)
        bpod_times = [f / fps + offset for f in edge_frames]
        result = align_ttl_edges(df, bpod_times)
        assert result == pytest.approx(offset, abs=0.1)

    def test_no_candidates_raises(self) -> None:
        fps = 60.0
        edge_frames = [60, 120]
        df = self._make_cam_df(300, fps, edge_frames)
        # bpod times wildly outside max_offset_search_s
        bpod_times = [1000.0, 2000.0]
        with pytest.raises(ValueError, match="No candidate offsets"):
            align_ttl_edges(df, bpod_times, max_offset_search_s=10.0)

    def test_too_few_matching_edges_raises(self) -> None:
        fps = 60.0
        df = self._make_cam_df(300, fps, [60])
        bpod_times = [1.0]
        with pytest.raises(ValueError, match="only"):
            align_ttl_edges(df, bpod_times, min_matches=5)
