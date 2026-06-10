"""Tests for the M8 detection DAO methods on ``storage/dao.py``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from obspy import UTCDateTime

from echosmonitor.core.models import Detection
from echosmonitor.storage.dao import ArchiveDao


@pytest.fixture
def dao(tmp_path: Path) -> ArchiveDao:
    return ArchiveDao(tmp_path / "archive.db", batch_window_s=0.1)


def _stream(dao: ArchiveDao) -> int:
    dev = dao.upsert_device("dev1", "h", 18000, {})
    return dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)


def _det(t_on: str, t_off: str | None, score: float, *, kind: str = "sta_lta") -> Detection:
    return Detection(
        device="dev1",
        nslc="IU.ANMO.00.BHZ",
        kind=kind,
        t_on=UTCDateTime(t_on),
        t_off=UTCDateTime(t_off) if t_off is not None else None,
        score=score,
        detected_at=UTCDateTime("2026-06-01T00:00:10"),
        meta={"sta_s": 1.0, "lta_s": 10.0, "on_thr": 3.5},
    )


def test_record_then_recent_round_trip(dao: ArchiveDao) -> None:
    sid = _stream(dao)
    det_id = dao.record_detection(sid, _det("2026-06-01T00:00:00", "2026-06-01T00:00:03", 7.2))
    assert det_id >= 1

    rows = dao.recent_detections(limit=10)
    assert len(rows) == 1
    got = rows[0]
    assert got.id == det_id
    assert got.device == "dev1"
    assert got.nslc == "IU.ANMO.00.BHZ"
    assert got.kind == "sta_lta"
    assert got.t_on == UTCDateTime("2026-06-01T00:00:00")
    assert got.t_off == UTCDateTime("2026-06-01T00:00:03")
    assert got.score == pytest.approx(7.2)
    # meta round-trips through meta_json.
    assert got.meta["on_thr"] == pytest.approx(3.5)


def test_open_then_close_updates_offtime_and_score(dao: ArchiveDao) -> None:
    sid = _stream(dao)
    # Open row: t_off NULL, peak-so-far score.
    det_id = dao.record_detection(sid, _det("2026-06-01T00:00:00", None, 4.0))
    rows = dao.recent_detections(limit=10)
    assert rows[0].t_off is None
    assert rows[0].score == pytest.approx(4.0)

    # Close it with the final peak ratio.
    dao.update_detection_offtime(det_id, UTCDateTime("2026-06-01T00:00:05"), score=9.5)
    rows = dao.recent_detections(limit=10)
    assert len(rows) == 1  # same row updated in place, not a new insert
    assert rows[0].id == det_id
    assert rows[0].t_off == UTCDateTime("2026-06-01T00:00:05")
    assert rows[0].score == pytest.approx(9.5)


def test_update_offtime_without_score_preserves_score(dao: ArchiveDao) -> None:
    sid = _stream(dao)
    det_id = dao.record_detection(sid, _det("2026-06-01T00:00:00", None, 4.0))
    dao.update_detection_offtime(det_id, UTCDateTime("2026-06-01T00:00:05"))
    rows = dao.recent_detections(limit=10)
    assert rows[0].score == pytest.approx(4.0)
    assert rows[0].t_off == UTCDateTime("2026-06-01T00:00:05")


def test_recent_detections_newest_first_and_limit(dao: ArchiveDao) -> None:
    sid = _stream(dao)
    for i in range(5):
        dao.record_detection(sid, _det(f"2026-06-01T00:00:0{i}", None, float(i)))
    rows = dao.recent_detections(limit=3)
    assert len(rows) == 3
    on_times = [r.t_on for r in rows]
    assert on_times == sorted(on_times, reverse=True)
    assert on_times[0] == UTCDateTime("2026-06-01T00:00:04")


def test_recent_detections_since_filter(dao: ArchiveDao) -> None:
    sid = _stream(dao)
    dao.record_detection(sid, _det("2026-06-01T00:00:00", None, 1.0))
    dao.record_detection(sid, _det("2026-06-01T12:00:00", None, 2.0))
    rows = dao.recent_detections(limit=10, since=UTCDateTime("2026-06-01T06:00:00"))
    assert len(rows) == 1
    assert rows[0].t_on == UTCDateTime("2026-06-01T12:00:00")


def test_count_detections_uses_count_star(dao: ArchiveDao) -> None:
    sid = _stream(dao)
    assert dao.count_detections() == 0
    for i in range(4):
        dao.record_detection(sid, _det(f"2026-06-01T00:00:0{i}", None, float(i)))
    assert dao.count_detections() == 4
    assert dao.count_detections(since=UTCDateTime("2026-06-01T00:00:02")) == 2


def test_detection_fk_integrity(dao: ArchiveDao) -> None:
    """A detection against a non-existent stream is rejected by the FK."""
    _stream(dao)  # ensures schema + a valid stream exists, but we use a bad id
    with pytest.raises(sqlite3.IntegrityError):
        dao.record_detection(99999, _det("2026-06-01T00:00:00", None, 1.0))
