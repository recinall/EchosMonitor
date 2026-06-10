"""Archive read-back tests (M9 Stage C).

Write a synthetic SDS archive, then read windows back through
:class:`ArchiveReader`: correct trim, gap handling (gaps stay explicit,
never filled), the ``files`` index path, and the SDS-scan fallback.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from obspy import Stream, Trace, UTCDateTime

from echosmonitor.core.models import StreamID
from echosmonitor.storage.archive_reader import ArchiveReader
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.sds import sds_path

_SID = StreamID("IU", "ANMO", "00", "BHZ")
_FS = 100.0
# A fixed instant well inside one UTC day so no midnight split is involved.
_T0 = UTCDateTime("2026-05-10T12:00:00")


def _write_trace(root: Path, sid: StreamID, t0: UTCDateTime, n: int, value_start: int) -> Path:
    """Write one MiniSEED day-file at the canonical SDS path; return it."""
    data = np.arange(value_start, value_start + n, dtype=np.int32)
    tr = Trace(data=data)
    tr.stats.network, tr.stats.station = sid.network, sid.station
    tr.stats.location, tr.stats.channel = sid.location, sid.channel
    tr.stats.sampling_rate = _FS
    tr.stats.starttime = t0
    path = sds_path(root, t0, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tr.write(str(path), format="MSEED")
    return path


def test_read_window_trims_to_request(tmp_path) -> None:
    root = tmp_path / "sds"
    _write_trace(root, _SID, _T0, n=int(_FS * 60), value_start=0)  # 60 s

    reader = ArchiveReader(root)  # no DAO → SDS-scan fallback
    st = reader.read_window(_SID, _T0 + 10, _T0 + 20)

    assert len(st) == 1
    tr = st[0]
    # ~10 s of data, trimmed to the request (inclusive endpoints → 1001).
    assert 1000 <= tr.stats.npts <= 1001
    assert abs(tr.stats.starttime - (_T0 + 10)) < 1.0 / _FS
    # The data is the contiguous integer ramp we wrote (samples 1000..).
    assert int(tr.data[0]) == 1000


def test_files_in_range_index_lookup(tmp_path) -> None:
    root = tmp_path / "sds"
    path = _write_trace(root, _SID, _T0, n=int(_FS * 60), value_start=0)

    dao = ArchiveDao(tmp_path / "archive.db", batch_window_s=0.1)
    dev = dao.upsert_device("dev", "h", 18000, {})
    sid_row = dao.upsert_stream(dev, (_SID.network, _SID.station, _SID.location, _SID.channel), _FS)
    dao.record_file(sid_row, path, _T0, _T0 + 60, 1024)

    # In-range hits; out-of-range misses.
    assert dao.files_in_range(sid_row, _T0 + 10, _T0 + 20) == [path]
    assert dao.files_in_range(sid_row, _T0 + 120, _T0 + 180) == []
    assert dao.find_stream_id("dev", _SID.nslc) == sid_row
    assert dao.find_stream_id("dev", "ZZ.NONE..XXX") is None

    reader = ArchiveReader(root, dao=dao)
    st = reader.read_window(_SID, _T0 + 5, _T0 + 15, device_name="dev")
    assert len(st) == 1
    assert st[0].stats.npts > 0


def test_gap_is_preserved_not_filled(tmp_path) -> None:
    root = tmp_path / "sds"
    # Two segments in the same UTC day with a 5 s gap, written to the one
    # day-file as two records.
    seg_a = Trace(data=np.arange(0, int(_FS * 10), dtype=np.int32))
    seg_b = Trace(data=np.arange(0, int(_FS * 10), dtype=np.int32))
    for tr, start in ((seg_a, _T0), (seg_b, _T0 + 15)):
        tr.stats.network, tr.stats.station = _SID.network, _SID.station
        tr.stats.location, tr.stats.channel = _SID.location, _SID.channel
        tr.stats.sampling_rate = _FS
        tr.stats.starttime = start
    path = sds_path(root, _T0, _SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    Stream([seg_a, seg_b]).write(str(path), format="MSEED")

    reader = ArchiveReader(root)
    st = reader.read_window(_SID, _T0, _T0 + 25)
    # Merged into one masked trace across the gap (no fill_value), or two
    # separate segments — either way the gap must NOT be silently filled.
    if len(st) == 1:
        assert np.ma.isMaskedArray(st[0].data) and bool(st[0].data.mask.any())
    else:
        assert len(st) == 2


def test_missing_archive_returns_empty_stream(tmp_path) -> None:
    reader = ArchiveReader(tmp_path / "empty")
    st = reader.read_window(_SID, _T0, _T0 + 10)
    assert len(st) == 0
