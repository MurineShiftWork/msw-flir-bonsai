"""Tests for timestamp unwrapping and camera CSV preprocessing."""

import numpy as np

from msw_flir_bonsai.timestamps import (
    detect_dropped_frames,
    load_camera_csv,
    preprocess_camera_csv,
    unwrap_counter,
    unwrap_cyclic,
)


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


def test_load_camera_csv_2col(tmp_path):
    csv = tmp_path / "cam.csv"
    csv.write_text("100,10.5\n101,10.516\n102,10.533\n")
    df = load_camera_csv(csv)
    assert "frame_index" in df.columns
    assert "frame_counter" in df.columns
    assert "timestamp_raw" in df.columns
    assert len(df) == 3


def test_load_camera_csv_3col(tmp_path):
    csv = tmp_path / "cam.csv"
    csv.write_text("100,10.5,0\n101,10.516,1\n102,10.533,0\n")
    df = load_camera_csv(csv)
    assert "gpio_state" in df.columns


def test_load_camera_csv_unknown_cols(tmp_path):
    csv = tmp_path / "cam.csv"
    csv.write_text("1,2.0,3,4\n5,6.0,7,8\n")
    df = load_camera_csv(csv)
    assert "col0" in df.columns
    assert len(df) == 2


def test_preprocess_no_rollover(tmp_path):
    rows = "\n".join(f"{i},{i * 0.0167},0" for i in range(100))
    csv = tmp_path / "cam.csv"
    csv.write_text(rows)
    df = preprocess_camera_csv(csv, ts_cycle_s=128.0)
    assert "timestamp_s" in df.columns
    assert df["timestamp_s"].is_monotonic_increasing


def test_preprocess_session_start_shift(tmp_path):
    rows = "\n".join(f"{i},{i * 0.0167},0" for i in range(60))
    csv = tmp_path / "cam.csv"
    csv.write_text(rows)
    df = preprocess_camera_csv(csv, session_start_s=0.5)
    assert df["timestamp_s"].iloc[0] < 0  # frames before session start are negative


def test_detect_dropped_frames(tmp_path):
    # Insert a 100ms gap (2× expected 16ms IFI at 60fps) to simulate a drop
    ts = list(np.arange(0, 1.0, 1 / 60))
    ts[30] = ts[29] + 0.1  # simulate drop at frame 30
    rows = "\n".join(f"{i},{t:.6f},0" for i, t in enumerate(ts))
    csv = tmp_path / "cam.csv"
    csv.write_text(rows)
    df = preprocess_camera_csv(csv)
    drops = detect_dropped_frames(df, expected_fps=60.0)
    assert drops.iloc[30], "Frame 30 should be flagged as dropped"
