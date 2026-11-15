"""Tests for timestamp unwrapping and camera CSV preprocessing."""

import numpy as np
import pytest

from msw_flir_bonsai.timestamps import (
    CSV_COLUMNS,
    decode_flycap_timestamp,
    detect_dropped_frames,
    line_state,
    load_camera_csv,
    preprocess_camera_csv,
    unwrap_counter,
    unwrap_cyclic,
)

FLYCAP_GPIO_LOW = 0x30000000
FLYCAP_GPIO_HIGH = 0xB0000000  # bit 31 set


def _pack_flycap(t_s: float) -> int:
    """Pack seconds into the FlyCapture embeddedTimeStamp layout."""
    ticks = int(round(t_s * 8000 * 3072))  # whole cycle offsets since t=0
    sec, rem = divmod(ticks, 8000 * 3072)
    count, offset = divmod(rem, 3072)
    return ((sec % 128) << 25) | (count << 12) | offset


def _write_csv(path, rows):
    path.write_text("\n".join(",".join(str(v) for v in r) for r in rows) + "\n")
    return path


def test_unwrap_cyclic_no_rollover():
    ts = np.array([0.1, 5.0, 10.0, 50.0, 100.0])
    result = unwrap_cyclic(ts, period=128.0)
    np.testing.assert_allclose(result, ts)


def test_unwrap_cyclic_one_rollover():
    ts = np.array([120.0, 127.9, 0.1, 10.0])
    result = unwrap_cyclic(ts, period=128.0)
    assert result[2] > result[1]
    assert result[3] > result[2]
    np.testing.assert_allclose(result[2], 128.1, atol=0.01)


def test_unwrap_cyclic_multiple_rollovers():
    # 3 full cycles
    ts = np.concatenate(
        [
            np.linspace(0, 127, 50),
            np.linspace(0, 127, 50),
            np.linspace(0, 64, 25),
        ]
    )
    result = unwrap_cyclic(ts, period=128.0)
    assert np.all(np.diff(result) >= 0), "Result must be monotonically non-decreasing"
    assert result[-1] > 256  # at least 2 full cycles worth


def test_unwrap_counter_16bit():
    counts = np.array([65530, 65535, 0, 5, 10], dtype=np.int64)
    result = unwrap_counter(counts, bits=16)
    assert np.all(np.diff(result) > 0)
    assert result[2] == 65536


def test_decode_flycap_packed_fields():
    # 5 s, 4000 cycles (0.5 s), 1536 offsets (half a cycle)
    word = (5 << 25) | (4000 << 12) | 1536
    got = decode_flycap_timestamp(np.array([word]))
    np.testing.assert_allclose(got, [5.5 + 0.5 / 8000])


def test_load_camera_csv_column_order(tmp_path):
    csv = _write_csv(
        tmp_path / "cam.csv",
        [(FLYCAP_GPIO_LOW, _pack_flycap(1.0), 100), (FLYCAP_GPIO_HIGH, 0, 101)],
    )
    df = load_camera_csv(csv)
    assert list(df.columns) == ["frame_index", *CSV_COLUMNS]
    assert list(CSV_COLUMNS) == ["gpio_state", "timestamp_raw", "frame_counter"]
    assert df["gpio_state"].tolist() == [FLYCAP_GPIO_LOW, FLYCAP_GPIO_HIGH]
    assert df["frame_counter"].tolist() == [100, 101]


def test_load_camera_csv_rejects_other_widths(tmp_path):
    csv = _write_csv(tmp_path / "cam.csv", [(1, 2), (3, 4)])
    with pytest.raises(ValueError, match="expected 3 columns"):
        load_camera_csv(csv)


def test_unknown_driver_raises(tmp_path):
    csv = _write_csv(tmp_path / "cam.csv", [(0, 0, 0)])
    with pytest.raises(ValueError, match="unknown driver"):
        preprocess_camera_csv(csv, driver="basler")


def test_preprocess_flycap_unwraps_128s_rollover(tmp_path):
    fps = 60.0
    t = 120.0 + np.arange(900) / fps  # crosses 128 s after ~480 frames
    rows = [(FLYCAP_GPIO_LOW, _pack_flycap(x), 1000 + i) for i, x in enumerate(t)]
    df = preprocess_camera_csv(_write_csv(tmp_path / "cam.csv", rows))
    assert df["timestamp_raw"].iloc[0] == _pack_flycap(t[0])
    assert df["timestamp_s"].is_monotonic_increasing
    np.testing.assert_allclose(df["timestamp_s"], t, atol=1e-6)
    np.testing.assert_allclose(np.diff(df["timestamp_s"]), 1 / fps, atol=1e-6)


def test_preprocess_spinnaker_nanoseconds(tmp_path):
    ns = [3_000_000_000_000 + i * 16_666_667 for i in range(10)]
    rows = [(0, v, i) for i, v in enumerate(ns)]
    df = preprocess_camera_csv(
        _write_csv(tmp_path / "cam.csv", rows), driver="spinnaker"
    )
    assert df["timestamp_raw"].tolist() == ns
    np.testing.assert_allclose(df["timestamp_s"], np.array(ns) / 1e9)
    assert df["timestamp_s"].iloc[0] == pytest.approx(3000.0)


def test_line_state_selects_bit_31():
    words = np.array([FLYCAP_GPIO_LOW, FLYCAP_GPIO_HIGH, 0x70000000, 0xF0000000])
    assert line_state(words, 31).tolist() == [0, 1, 0, 1]
    assert line_state(words, 29).tolist() == [1, 1, 1, 1]


def test_preprocess_session_start_shift(tmp_path):
    rows = [(0, _pack_flycap(i / 60), i) for i in range(60)]
    df = preprocess_camera_csv(
        _write_csv(tmp_path / "cam.csv", rows), session_start_s=0.5
    )
    assert df["timestamp_s"].iloc[0] < 0  # frames before session start are negative


def test_preprocess_frame_counter_rollover(tmp_path):
    counters = [2**32 - 2, 2**32 - 1, 0, 1]
    rows = [(0, _pack_flycap(i / 60), c) for i, c in enumerate(counters)]
    df = preprocess_camera_csv(_write_csv(tmp_path / "cam.csv", rows))
    assert np.all(np.diff(df["frame_counter"]) == 1)


def test_detect_dropped_frames(tmp_path):
    # Insert a 100ms gap (2x expected 16ms IFI at 60fps) to simulate a drop
    ts = list(np.arange(0, 1.0, 1 / 60))
    ts[30] = ts[29] + 0.1  # simulate drop at frame 30
    rows = [(0, _pack_flycap(t), i) for i, t in enumerate(ts)]
    df = preprocess_camera_csv(_write_csv(tmp_path / "cam.csv", rows))
    drops = detect_dropped_frames(df, expected_fps=60.0)
    assert drops.iloc[30], "Frame 30 should be flagged as dropped"
