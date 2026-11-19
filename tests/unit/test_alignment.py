"""Unit tests for msw_flir_bonsai.alignment: pure numpy/pandas, no hardware needed."""

from __future__ import annotations

import pandas as pd
import pytest
from ttl_barcoder.core.config import BarcodeConfig

from msw_flir_bonsai.alignment import (
    align_barcodes,
    align_ttl_edges,
    extract_camera_barcodes,
)

# ---------------------------------------------------------------------------
# Helpers


# FlyCapture line status words: other bits always set, barcode on bit 31.
_LOW = 0x70000000
_HIGH = 0xF0000000


def _make_df(timestamps: list[float], gpio: list[int]) -> pd.DataFrame:
    """Build a camera frame table; *gpio* is the 0/1 level of the bit-31 line."""
    return pd.DataFrame(
        {
            "frame_index": range(len(timestamps)),
            "timestamp_s": timestamps,
            "gpio_state": [_HIGH if g else _LOW for g in gpio],
        }
    )


# ---------------------------------------------------------------------------
# extract_camera_barcodes


def _sampled(config, values, starts, fps, n_frames, offset=0.004):
    """Bpod barcodes as a camera sees them: the line level at each frame."""
    from ttl_barcoder.core.barcode_ttl import BarcodeTTL

    ttl = BarcodeTTL(config)
    edges: list[tuple[float, bool]] = []
    for value, t in zip(values, starts, strict=True):
        for level, ms in ttl.get_sequence(value):
            edges.append((t, level))
            t += ms / 1000
        edges.append((t, False))
    ts = [offset + i / fps for i in range(n_frames)]
    gpio, k, level = [], 0, False
    for t in ts:
        while k < len(edges) and edges[k][0] <= t:
            level = edges[k][1]
            k += 1
        gpio.append(int(level))
    return _make_df(ts, gpio)


_VALUES = [0b1111111111 << 13, 2**37 - 1, 1, 123456789, 2**36 + 5]


class TestExtractCameraBarcodes:
    def test_missing_column_raises(self) -> None:
        df = pd.DataFrame({"timestamp_s": [0.0, 1.0], "frame_index": [0, 1]})
        with pytest.raises(KeyError, match="GPIO column"):
            extract_camera_barcodes(df, gpio_col="missing")

    def test_flat_line_has_no_barcodes(self) -> None:
        ts = [i * 0.016 for i in range(100)]
        assert extract_camera_barcodes(_make_df(ts, [0] * 100), BarcodeConfig()) == []
        assert extract_camera_barcodes(_make_df(ts, [1] * 100), BarcodeConfig()) == []

    @pytest.mark.parametrize(
        ("config", "fps"),
        [
            (BarcodeConfig(bit_duration_ms=100.0, init_duration_ms=100.0), 60.0),
            (BarcodeConfig(bit_duration_ms=100.0, init_duration_ms=100.0), 66.2),
            (BarcodeConfig(bit_duration_ms=50.0, init_duration_ms=50.0), 60.0),
            (BarcodeConfig(), 120.0),
        ],
        ids=["100ms-60fps", "100ms-66fps", "50ms-60fps", "35ms-120fps"],
    )
    def test_values_match_what_bpod_sent(self, config, fps) -> None:
        gap = config.total_duration_ms / 1000 + 1.0
        starts = [2.0 + i * gap for i in range(len(_VALUES))]
        df = _sampled(config, _VALUES, starts, fps, int((starts[-1] + gap) * fps))
        found = extract_camera_barcodes(df, config)
        assert [v for _, v in found] == _VALUES
        # half a frame before the first high frame: within half a frame of the start
        for (t, _), start in zip(found, starts, strict=True):
            assert abs(t - start) <= 0.5 / fps

    def test_trial_pulses_between_barcodes_are_ignored(self) -> None:
        config = BarcodeConfig(bit_duration_ms=100.0, init_duration_ms=100.0)
        df = _sampled(config, _VALUES[:2], [2.0, 12.0], 60.0, 1200)
        level = (df["gpio_state"] == _HIGH).astype(int).tolist()
        for f in (420, 421, 422, 900, 901):  # 50 ms trial pulses
            level[f] = 1
        df = _make_df(df["timestamp_s"].tolist(), level)
        assert [v for _, v in extract_camera_barcodes(df, config)] == _VALUES[:2]

    def test_inverted_line(self) -> None:
        config = BarcodeConfig(bit_duration_ms=100.0, init_duration_ms=100.0)
        df = _sampled(config, _VALUES[:2], [2.0, 12.0], 60.0, 1200)
        flipped = _make_df(
            df["timestamp_s"].tolist(), (df["gpio_state"] == _LOW).astype(int).tolist()
        )
        found = extract_camera_barcodes(flipped, config, inverted=True)
        assert [v for _, v in found] == _VALUES[:2]

    def test_bits_shorter_than_three_frames_are_refused(self, caplog) -> None:
        config = BarcodeConfig()  # 35 ms bits: 2.1 frames at 60 fps
        df = _sampled(config, _VALUES[:1], [1.0], 60.0, 240)
        assert extract_camera_barcodes(df, config) == []
        assert "shorter than three frames" in caplog.text

    def test_a_line_high_from_the_first_frame_is_not_a_barcode(self) -> None:
        ts = [i / 120 for i in range(600)]
        assert extract_camera_barcodes(_make_df(ts, [1] * 600), BarcodeConfig()) == []

    def test_without_settings_warns_and_uses_defaults(self, caplog) -> None:
        df = _sampled(BarcodeConfig(), _VALUES[:1], [1.0], 120.0, 480)
        assert [v for _, v in extract_camera_barcodes(df)] == _VALUES[:1]
        assert "No barcode settings" in caplog.text

    def test_other_line_bit_sees_no_edges(self) -> None:
        df = _sampled(BarcodeConfig(), _VALUES[:1], [1.0], 120.0, 480)
        assert extract_camera_barcodes(df, BarcodeConfig(), line_bit=30) == []


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
