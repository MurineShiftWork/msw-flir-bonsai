"""FLIR video acquisition reader: per-camera discovery + completeness."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from msw_flir_bonsai.readers import FlirSession, load_session

if TYPE_CHECKING:
    from pathlib import Path

_BASENAME = "s1__20260620_120000_000000__video_flir__v1"


def _video(d: Path, label: str) -> Path:
    p = d / f"{_BASENAME}.msw.{label}.avi"
    p.write_bytes(b"\x00\x00")  # placeholder; reader checks presence, not content
    return p


def _timestamps(d: Path, label: str, rows: int = 3) -> Path:
    p = d / f"{_BASENAME}.msw.{label}.timestamps.csv"
    # Bonsai CSV: headerless, frame_counter,timestamp_raw[,gpio_state]
    lines = [f"{i},{i * 0.0167:.4f},0" for i in range(rows)]
    p.write_text("\n".join(lines) + "\n")
    return p


def test_load_returns_flir_session(tmp_path):
    _video(tmp_path, "top.0")
    _timestamps(tmp_path, "top.0")
    session = load_session(tmp_path)
    assert isinstance(session, FlirSession)
    assert session.directory == tmp_path


def test_complete_single_camera(tmp_path):
    _video(tmp_path, "top.0")
    _timestamps(tmp_path, "top.0")
    session = load_session(tmp_path)
    assert set(session.cameras) == {"top.0"}
    cam = session.cameras["top.0"]
    assert cam.video is not None and cam.timestamps is not None
    assert cam.complete is True
    assert session.is_complete is True


def test_dotted_label_parsed_whole(tmp_path):
    # cam_label may contain a dot (name.index); it must not be split.
    _video(tmp_path, "side.1")
    _timestamps(tmp_path, "side.1")
    session = load_session(tmp_path)
    assert "side.1" in session.cameras


def test_multi_camera_all_complete(tmp_path):
    for label in ("top.0", "side.1"):
        _video(tmp_path, label)
        _timestamps(tmp_path, label)
    session = load_session(tmp_path)
    assert set(session.cameras) == {"top.0", "side.1"}
    assert session.is_complete is True


def test_missing_timestamps_is_incomplete(tmp_path):
    _video(tmp_path, "top.0")  # video only, no .timestamps.csv
    session = load_session(tmp_path)
    cam = session.cameras["top.0"]
    assert cam.video is not None and cam.timestamps is None
    assert cam.complete is False
    assert session.is_complete is False


def test_one_incomplete_camera_fails_session(tmp_path):
    _video(tmp_path, "top.0")
    _timestamps(tmp_path, "top.0")
    _video(tmp_path, "side.1")  # missing its csv
    session = load_session(tmp_path)
    assert session.cameras["top.0"].complete is True
    assert session.cameras["side.1"].complete is False
    assert session.is_complete is False


def test_empty_dir_is_incomplete(tmp_path):
    session = load_session(tmp_path)
    assert session.cameras == {}
    assert session.is_complete is False


def test_load_parses_timestamps(tmp_path):
    _video(tmp_path, "top.0")
    _timestamps(tmp_path, "top.0", rows=5)
    session = load_session(tmp_path, load=True)
    frames = session.cameras["top.0"].frames
    assert frames is not None
    assert len(frames) == 5
    assert "timestamp_s" in frames.columns


def test_load_false_does_not_parse(tmp_path):
    _video(tmp_path, "top.0")
    _timestamps(tmp_path, "top.0")
    session = load_session(tmp_path)  # load=False default
    assert session.cameras["top.0"].frames is None


def test_summary_shape(tmp_path):
    _video(tmp_path, "top.0")
    _timestamps(tmp_path, "top.0")
    summary = load_session(tmp_path).summary()
    assert summary["top.0"] == {
        "video": True,
        "timestamps": True,
        "complete": True,
        "frames_loaded": False,
    }


def test_missing_directory_raises(tmp_path):
    with pytest.raises(NotADirectoryError):
        load_session(tmp_path / "does_not_exist")
