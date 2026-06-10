"""Per-device SDS namespacing: two devices, same NSLC, distinct files.

The archive-corruption bug: two configured devices emitting the SAME SEED
NSLC (e.g. both ``XX.ECHOS.00.HHZ`` with ``archive.root_dir=null``) wrote
to the SAME physical SDS file (interleaved-record corruption) and collapsed
both devices' ``files`` rows under ``UNIQUE(path)`` (extent/coverage
collapse). The fix namespaces the SDS tree per device inside the writer and
reader.

These tests drive the REAL :class:`MseedWriter` write path (not hand-seeded
fixtures — that was the archive_extent lesson) so they assert the observable
outcome: no shared files, per-device reads return that device's data, and
two distinct DAO extents/coverages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from obspy import Trace, UTCDateTime

from echosmonitor.config.schema import ArchiveConfig
from echosmonitor.core.models import StreamID
from echosmonitor.storage.archive_reader import ArchiveReader
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.mseed_writer import MseedWriter
from echosmonitor.storage.sds import device_sds_root, sanitize_device_name

# Two distinct devices that emit the IDENTICAL SEED NSLC.
_DEVICE_A = "Echos"
_DEVICE_B = "Echos_WK"
_NSLC = "XX.ECHOS.00.HHZ"
_FS = 100.0
_T0 = UTCDateTime("2026-06-05T12:00:00")
_DUR = 20.0


# ---------------------------------------------------------------------------
# Pure-helper tests
# ---------------------------------------------------------------------------


def test_sanitize_spaces_to_underscore() -> None:
    assert sanitize_device_name("my device") == "my_device"


def test_sanitize_slash_and_backslash_to_underscore() -> None:
    # A single path segment is produced — no separator survives.
    out = sanitize_device_name("a/b\\c")
    assert "/" not in out
    assert "\\" not in out
    assert out == "a_b_c"


def test_sanitize_path_traversal_maps_to_safe_token() -> None:
    # ".." must never escape the base root: it reduces to a fallback token.
    out = sanitize_device_name("..")
    assert out not in (".", "..")
    assert out.startswith("device_")
    # "." likewise.
    out_dot = sanitize_device_name(".")
    assert out_dot.startswith("device_")


def test_sanitize_empty_result_falls_back_to_deterministic_token() -> None:
    # All-separator name reduces to empty → deterministic hash token.
    out = sanitize_device_name("///")
    assert out.startswith("device_")
    # Deterministic: same input → same token.
    assert out == sanitize_device_name("///")


def test_sanitize_already_safe_name_unchanged() -> None:
    assert sanitize_device_name("Echos_WK-1.2") == "Echos_WK-1.2"


def test_sanitize_collapses_and_strips() -> None:
    assert sanitize_device_name("__a  b__") == "a_b"


def test_device_sds_root_joins_sanitized_segment(tmp_path: Path) -> None:
    base = tmp_path / "archive"
    root = device_sds_root(base, "my device")
    assert root == base / "my_device"
    assert root.parent == base


def test_device_sds_root_distinct_for_distinct_devices(tmp_path: Path) -> None:
    base = tmp_path / "archive"
    assert device_sds_root(base, _DEVICE_A) != device_sds_root(base, _DEVICE_B)


# ---------------------------------------------------------------------------
# Real-write-path tests
# ---------------------------------------------------------------------------


def _trace(value: int) -> Trace:
    """A constant-value int32 trace so cross-contamination is detectable."""
    net, sta, loc, cha = _NSLC.split(".")
    data = np.full(int(_FS * _DUR), value, dtype=np.int32)
    tr = Trace(data=data)
    tr.stats.network, tr.stats.station = net, sta
    tr.stats.location, tr.stats.channel = loc, cha
    tr.stats.sampling_rate = _FS
    tr.stats.starttime = _T0
    return tr


def _write_one_device(
    base: Path, device: str, value: int
) -> tuple[list[tuple[Any, ...]], list[Path]]:
    """Write the SAME NSLC for ``device`` with a distinguishable constant
    sample value. Returns ``(flushed_events, written_paths)``."""
    writer = MseedWriter(device, base, ArchiveConfig(enabled=True))
    flushed: list[tuple[Any, ...]] = []
    paths: list[Path] = []
    writer.flushedFile.connect(lambda *a: flushed.append(a))
    writer.writeOk.connect(lambda *a: paths.append(a[3]))
    writer.write_trace(_NSLC, _trace(value))
    writer.close_all()
    return flushed, paths


def test_two_devices_same_nslc_write_distinct_files(qapp: Any, tmp_path: Path) -> None:
    base = tmp_path / "archive"
    base.mkdir(parents=True, exist_ok=True)

    _flushed_a, paths_a = _write_one_device(base, _DEVICE_A, value=111)
    _flushed_b, paths_b = _write_one_device(base, _DEVICE_B, value=222)

    assert paths_a and paths_b
    path_a, path_b = paths_a[-1], paths_b[-1]

    # (a) Two DIFFERENT physical files, both exist, NEITHER shared.
    assert path_a != path_b
    assert path_a.exists()
    assert path_b.exists()
    # Each lives under its OWN device_sds_root subtree.
    assert str(path_a).startswith(str(device_sds_root(base, _DEVICE_A)) + "/")
    assert str(path_b).startswith(str(device_sds_root(base, _DEVICE_B)) + "/")
    # The leaf SDS filenames are identical (same NSLC) — only the device
    # segment disambiguates them.
    assert path_a.name == path_b.name


def test_per_device_read_returns_that_devices_data(qapp: Any, tmp_path: Path) -> None:
    base = tmp_path / "archive"
    base.mkdir(parents=True, exist_ok=True)

    _write_one_device(base, _DEVICE_A, value=111)
    _write_one_device(base, _DEVICE_B, value=222)

    reader = ArchiveReader(base)  # no DAO → device-scoped SDS-scan fallback
    sid = StreamID.from_trace_id(_NSLC)

    st_a = reader.read_window(sid, _T0 + 5, _T0 + 15, device_name=_DEVICE_A)
    st_b = reader.read_window(sid, _T0 + 5, _T0 + 15, device_name=_DEVICE_B)

    assert len(st_a) >= 1
    assert len(st_b) >= 1
    # Each device reads back ITS OWN distinguishable constant value, proving
    # no cross-contamination into a shared file.
    assert int(st_a[0].data[0]) == 111
    assert int(st_b[0].data[0]) == 222
    assert int(st_a[0].data.max()) == 111
    assert int(st_b[0].data.max()) == 222


def _index_like_engine(base: Path, device: str, flushed: list[tuple[Any, ...]]) -> ArchiveDao:
    """Index a device's flushedFile events exactly as the engine does, into
    the SINGLE shared DAO at ``base/archive.db``."""
    dao = ArchiveDao(base / "archive.db", batch_window_s=0.1)
    dev_id = dao.upsert_device(device, "h", 18000, {})
    for _device, nslc, path, t_start, t_end, _bytes_added, file_size in flushed:
        net, sta, loc, cha = nslc.split(".")
        sid = dao.upsert_stream(dev_id, (net, sta, loc, cha), _FS)
        dao.record_file(sid, path, t_start, t_end, int(file_size))
    dao.flush_now()
    return dao


def test_distinct_extents_and_coverage_no_collapse(qapp: Any, tmp_path: Path) -> None:
    base = tmp_path / "archive"
    base.mkdir(parents=True, exist_ok=True)

    # Device A spans [_T0, _T0+_DUR]; device B spans a LATER, disjoint
    # window so a collapse (both rows under UNIQUE(path)) would be visible
    # as one merged extent rather than two.
    t0_b = _T0 + 3600.0
    writer_b = MseedWriter(_DEVICE_B, base, ArchiveConfig(enabled=True))
    flushed_b: list[tuple[Any, ...]] = []
    writer_b.flushedFile.connect(lambda *a: flushed_b.append(a))
    tr_b = _trace(222)
    tr_b.stats.starttime = t0_b
    writer_b.write_trace(_NSLC, tr_b)
    writer_b.close_all()

    flushed_a, _paths_a = _write_one_device(base, _DEVICE_A, value=111)
    assert flushed_a and flushed_b

    # One SHARED index (engine keeps a single archive.db at the base).
    dao = _index_like_engine(base, _DEVICE_A, flushed_a)
    # Re-open onto the same DB for device B (mirrors a second device row).
    dev_b = dao.upsert_device(_DEVICE_B, "h", 18000, {})
    for _device, nslc, path, t_start, t_end, _bytes_added, file_size in flushed_b:
        net, sta, loc, cha = nslc.split(".")
        sid = dao.upsert_stream(dev_b, (net, sta, loc, cha), _FS)
        dao.record_file(sid, path, t_start, t_end, int(file_size))
    dao.flush_now()

    ext_a = dao.archive_extent(_DEVICE_A, _NSLC)
    ext_b = dao.archive_extent(_DEVICE_B, _NSLC)
    assert ext_a is not None
    assert ext_b is not None

    # TWO DISTINCT extents — the collapse is gone.
    assert abs(float(ext_a[0].timestamp) - float(_T0.timestamp)) < 1.0
    assert abs(float(ext_b[0].timestamp) - float(t0_b.timestamp)) < 1.0
    assert float(ext_a[1].timestamp) < float(ext_b[0].timestamp)

    # Coverage is likewise per-device (A's coverage does not include B's
    # later window and vice versa).
    cov_a = dao.archive_coverage(_DEVICE_A, _NSLC, _T0, t0_b + _DUR)
    cov_b = dao.archive_coverage(_DEVICE_B, _NSLC, _T0, t0_b + _DUR)
    assert len(cov_a) == 1
    assert len(cov_b) == 1
    assert abs(float(cov_a[0][0].timestamp) - float(_T0.timestamp)) < 1.0
    assert abs(float(cov_b[0][0].timestamp) - float(t0_b.timestamp)) < 1.0
    dao.close()
