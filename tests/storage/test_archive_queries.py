"""Read-only archive browse queries (Archive tab, Stage A).

Seed the ``files`` index (the same shape the M5 writer produces via
``record_file``) and exercise :meth:`ArchiveDao.archive_extent` and
:meth:`ArchiveDao.archive_coverage`: the extent spans the recorded files, the
coverage merges contiguous files and exposes a deliberate gap, and an
un-archived stream returns the honest empty result (``None`` / ``[]``) rather
than a placeholder span.

These queries are DB-only (they never touch the filesystem), so the rows are
seeded with distinct synthetic paths — the ``files.path`` UNIQUE constraint
means one MiniSEED day-file is one row, and distinct paths model the
file-level coverage the Archive tab's strip renders.
"""

from __future__ import annotations

from pathlib import Path

from obspy import UTCDateTime

from echosmonitor.core.models import StreamID
from echosmonitor.storage.dao import ArchiveDao

_SID = StreamID("IU", "ANMO", "00", "BHZ")
_FS = 100.0
_T0 = UTCDateTime("2026-05-10T12:00:00")


def _seed_dao(tmp_path: Path) -> tuple[ArchiveDao, int]:
    dao = ArchiveDao(tmp_path / "archive.db", batch_window_s=0.1)
    dev = dao.upsert_device("dev", "h", 18000, {})
    sid_row = dao.upsert_stream(dev, (_SID.network, _SID.station, _SID.location, _SID.channel), _FS)
    return dao, sid_row


def _record(
    dao: ArchiveDao, sid_row: int, name: str, t_start: UTCDateTime, t_end: UTCDateTime
) -> None:
    dao.record_file(sid_row, Path(f"/sds/{name}.mseed"), t_start, t_end, 1024)


def test_extent_spans_recorded_files(tmp_path: Path) -> None:
    dao, sid_row = _seed_dao(tmp_path)
    _record(dao, sid_row, "a", _T0, _T0 + 60)
    _record(dao, sid_row, "b", _T0 + 120, _T0 + 180)

    extent = dao.archive_extent("dev", _SID.nslc)
    assert extent is not None
    t_min, t_max = extent
    assert abs(t_min - _T0) < 1e-6
    assert abs(t_max - (_T0 + 180)) < 1e-6


def test_coverage_exposes_gap(tmp_path: Path) -> None:
    dao, sid_row = _seed_dao(tmp_path)
    # Covered: [T0, T0+60] and [T0+120, T0+180]; gap is (T0+60, T0+120).
    _record(dao, sid_row, "a", _T0, _T0 + 60)
    _record(dao, sid_row, "b", _T0 + 120, _T0 + 180)

    cov = dao.archive_coverage("dev", _SID.nslc, _T0, _T0 + 180)
    assert len(cov) == 2
    (a_start, a_end), (b_start, b_end) = cov
    assert abs(a_start - _T0) < 1e-6
    assert abs(a_end - (_T0 + 60)) < 1e-6
    assert abs(b_start - (_T0 + 120)) < 1e-6
    assert abs(b_end - (_T0 + 180)) < 1e-6
    # The gap is the complement between the two covered intervals.
    assert a_end < b_start


def test_coverage_clips_to_window(tmp_path: Path) -> None:
    dao, sid_row = _seed_dao(tmp_path)
    _record(dao, sid_row, "a", _T0, _T0 + 180)

    # Window is strictly inside the single covered file → one clipped interval.
    cov = dao.archive_coverage("dev", _SID.nslc, _T0 + 30, _T0 + 90)
    assert len(cov) == 1
    (start, end) = cov[0]
    assert abs(start - (_T0 + 30)) < 1e-6
    assert abs(end - (_T0 + 90)) < 1e-6


def test_adjacent_files_merge(tmp_path: Path) -> None:
    dao, sid_row = _seed_dao(tmp_path)
    # Two abutting files: [T0, T0+60] and [T0+60, T0+120] → one interval.
    _record(dao, sid_row, "a", _T0, _T0 + 60)
    _record(dao, sid_row, "b", _T0 + 60, _T0 + 120)

    cov = dao.archive_coverage("dev", _SID.nslc, _T0, _T0 + 120)
    assert len(cov) == 1
    (start, end) = cov[0]
    assert abs(start - _T0) < 1e-6
    assert abs(end - (_T0 + 120)) < 1e-6


def test_empty_archive_returns_none_and_empty(tmp_path: Path) -> None:
    dao, _sid_row = _seed_dao(tmp_path)
    # Stream row exists but no files recorded.
    assert dao.archive_extent("dev", _SID.nslc) is None
    assert dao.archive_coverage("dev", _SID.nslc, _T0, _T0 + 60) == []
    # Unknown stream entirely.
    assert dao.archive_extent("dev", "ZZ.NONE..XXX") is None
    assert dao.archive_coverage("dev", "ZZ.NONE..XXX", _T0, _T0 + 60) == []
