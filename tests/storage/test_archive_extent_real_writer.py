"""Archive extent/coverage against the REAL write path (regression).

The Stage A `test_archive_queries.py` seeded the `files` index with hand-built
`record_file` calls and synthetic paths. That proved the merge logic but NOT
that the queries match what the *live engine actually writes*. This test closes
that gap: it drives the real :class:`MseedWriter` to produce real SDS day-files,
then records them through `ArchiveDao.record_file` **exactly as the engine's
`_on_archive_flushed_file` does** (resolve stream id via `upsert_stream`, store
the writer's own `flushedFile` `t_start`/`t_end`/`file_size`). It then asserts
that `archive_extent` returns the real recorded span for a 3-component station,
that the Archive tab's default interval falls inside it, and that the window
reads back actual samples.

This is the lesson the field bug taught: the real DB had an empty `files` index
(archiving was disabled), and a hand-seeded test cannot catch a format mismatch
between the writer and the query — only a real-write-path test can.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from obspy import Trace, UTCDateTime

from echosmonitor.config.schema import ArchiveConfig
from echosmonitor.core.models import StreamID, device_stream_key
from echosmonitor.storage.archive_reader import ArchiveReader
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.mseed_writer import MseedWriter

_DEVICE = "Echos"
_FS = 500.0
_T0 = UTCDateTime("2026-06-05T12:00:00")
_DUR = 30.0
# Full NSLCs, exactly how the live engine names a 3C station's streams.
_GROUP = {
    "Z": "XX.ECHOS.00.HHZ",
    "N": "XX.ECHOS.00.HHN",
    "E": "XX.ECHOS.00.HHE",
}


def _trace(nslc: str) -> Trace:
    net, sta, loc, cha = nslc.split(".")
    data = np.random.default_rng(abs(hash(nslc)) % 2**32).standard_normal(int(_FS * _DUR)) * 1e3
    tr = Trace(data=data.astype(np.int32))
    tr.stats.network, tr.stats.station = net, sta
    tr.stats.location, tr.stats.channel = loc, cha
    tr.stats.sampling_rate = _FS
    tr.stats.starttime = _T0
    return tr


def _archive_via_real_writer(qapp: Any, tmp_path: Path) -> tuple[ArchiveDao, Path]:
    """Write the 3C station with the real MseedWriter and index it the same way
    the engine does. Returns ``(dao, sds_root)``."""
    root = tmp_path / "archive"
    root.mkdir(parents=True, exist_ok=True)
    writer = MseedWriter(_DEVICE, root, ArchiveConfig(enabled=True))
    flushed: list[tuple[Any, ...]] = []
    writer.flushedFile.connect(lambda *a: flushed.append(a))
    for nslc in _GROUP.values():
        writer.write_trace(nslc, _trace(nslc))
    writer.close_all()  # final fsync → emits flushedFile per path

    assert flushed, "the real writer emitted no flushedFile — nothing to index"

    dao = ArchiveDao(root / "archive.db", batch_window_s=0.1)
    dev_id = dao.upsert_device(_DEVICE, "echos.local", 18000, {})
    # Replicate streaming_engine._on_archive_flushed_file (DB-after-fsync).
    for device, nslc, path, t_start, t_end, _bytes_added, file_size in flushed:
        assert device == _DEVICE
        net, sta, loc, cha = nslc.split(".")
        sid = dao.upsert_stream(dev_id, (net, sta, loc, cha), _FS)
        dao.record_file(sid, path, t_start, t_end, int(file_size))
    dao.flush_now()
    return dao, root


def test_extent_matches_real_writer_output(qapp: Any, tmp_path: Path) -> None:
    dao, _root = _archive_via_real_writer(qapp, tmp_path)

    # The query the Archive browser runs for the selected station's Z stream.
    extent = dao.archive_extent(_DEVICE, _GROUP["Z"])
    assert extent is not None, "archive_extent returned None for real archived data"
    t_min, t_max = extent
    # The writer's flushedFile span is exactly the data span (± one sample).
    assert abs(float(t_min.timestamp) - float(_T0.timestamp)) < 1.0
    assert abs(float(t_max.timestamp) - float((_T0 + _DUR).timestamp)) < 1.0

    # Coverage over the extent is a single contiguous interval.
    cov = dao.archive_coverage(_DEVICE, _GROUP["Z"], t_min, t_max)
    assert len(cov) == 1
    assert abs(float(cov[0][0].timestamp) - float(t_min.timestamp)) < 1.0


def test_window_reads_back_real_samples(qapp: Any, tmp_path: Path) -> None:
    dao, root = _archive_via_real_writer(qapp, tmp_path)
    reader = ArchiveReader(root, dao=dao)
    st = reader.read_window(
        StreamID.from_trace_id(_GROUP["Z"]), _T0 + 5, _T0 + 15, device_name=_DEVICE
    )
    assert len(st) >= 1
    assert sum(tr.stats.npts for tr in st) > 0  # actual samples, not "no data"


def test_archive_tab_shows_real_extent_and_default_within(qtbot, qapp: Any, tmp_path: Path) -> None:
    from PySide6.QtCore import QObject, Signal

    from echosmonitor.gui.widgets.archive_tab import ArchiveTab

    dao, _root = _archive_via_real_writer(qapp, tmp_path)

    class _FakeEngine(QObject):
        newStreamSeen = Signal(str, str)  # noqa: N815
        devicesChanged = Signal()  # noqa: N815

        def __init__(self) -> None:
            super().__init__()
            self._buffers: dict[str, object] = {}

    engine = _FakeEngine()
    for nslc in _GROUP.values():
        engine._buffers[device_stream_key(_DEVICE, nslc)] = object()

    tab = ArchiveTab(engine, dao)  # type: ignore[arg-type]
    qtbot.addWidget(tab)

    # The extent label shows the REAL recorded span, and Load is enabled.
    assert "Archived:" in tab.extent_text_for_test()
    assert "No archived data" not in tab.extent_text_for_test()
    assert tab.load_enabled_for_test()

    # The default interval falls strictly WITHIN the real extent (not a
    # now→now+1year fallback).
    extent = dao.archive_extent(_DEVICE, _GROUP["Z"])
    assert extent is not None
    t_min, t_max = float(extent[0].timestamp), float(extent[1].timestamp)
    ds, de = tab.interval_for_test()
    assert t_min <= ds < de <= t_max + 1.0
