"""Tests for ``storage/mseed_writer.py`` — file-level behaviour.

The writer is constructed directly (not on a separate QThread) and
``write_trace`` is invoked as a plain method call. This exercises the
encoding pipeline, LRU file-handle cache, midnight split, crash
recovery, and ``writeOk`` / ``writeFailed`` signal payloads without
introducing thread-crossing complexity. Threaded behaviour
(timer-driven fsync, BlockingQueuedConnection close) lives in
``test_mseed_writer_threaded.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from obspy import Trace, UTCDateTime, read

from echosmonitor.config.schema import ArchiveConfig
from echosmonitor.core.models import StreamID
from echosmonitor.storage.mseed_writer import MseedWriter
from echosmonitor.storage.sds import device_sds_root, sds_path

# Device name used by ``writer_factory`` below. The writer namespaces the
# SDS tree per device, so expected paths are rooted at
# ``device_sds_root(tmp_path, _DEVICE)``, not ``tmp_path`` directly.
_DEVICE = "dev1"


def _expected_root(tmp_path: Path) -> Path:
    return device_sds_root(tmp_path, _DEVICE)


def _make_int32_trace(
    *,
    starttime: UTCDateTime,
    npts: int = 512,
    sampling_rate: float = 100.0,
    nslc: str = "IU.ANMO.00.BHZ",
) -> Trace:
    net, sta, loc, cha = nslc.split(".")
    return Trace(
        # Use varying values so STEIM2 has something to compress; pure
        # zeros would be encoded but yield a less-realistic round-trip.
        data=(np.arange(npts, dtype=np.int32) % 1000) - 500,
        header={
            "network": net,
            "station": sta,
            "location": loc,
            "channel": cha,
            "starttime": starttime,
            "sampling_rate": sampling_rate,
        },
    )


def _make_float32_trace(
    *,
    starttime: UTCDateTime,
    npts: int = 256,
    sampling_rate: float = 100.0,
    nslc: str = "IU.ANMO.00.BHZ",
) -> Trace:
    net, sta, loc, cha = nslc.split(".")
    return Trace(
        data=np.linspace(-1.0, 1.0, npts, dtype=np.float32),
        header={
            "network": net,
            "station": sta,
            "location": loc,
            "channel": cha,
            "starttime": starttime,
            "sampling_rate": sampling_rate,
        },
    )


@pytest.fixture
def writer_factory(qapp: Any, tmp_path: Path) -> Iterator[Any]:
    """Build a ``MseedWriter`` with sensible test defaults.

    The factory captures emitted ``writeOk`` / ``writeFailed`` /
    ``flushedFile`` signals into lists exposed on the returned writer so
    tests can assert on them without pytest-qt-specific machinery.
    """
    writers: list[MseedWriter] = []

    def make(**overrides: Any) -> MseedWriter:
        cfg_kwargs: dict[str, Any] = {"enabled": True}
        cfg_kwargs.update(overrides)
        cfg = ArchiveConfig(**cfg_kwargs)
        writer = MseedWriter(_DEVICE, tmp_path, cfg)

        ok: list[tuple[Any, ...]] = []
        failed: list[tuple[Any, ...]] = []
        flushed: list[tuple[Any, ...]] = []
        writer.writeOk.connect(lambda *args: ok.append(args))
        writer.writeFailed.connect(lambda *args: failed.append(args))
        writer.flushedFile.connect(lambda *args: flushed.append(args))
        # Stash on the instance for test assertions. Allowed because
        # MseedWriter is not slotted (QObject is dynamic).
        writer._test_ok = ok  # type: ignore[attr-defined]
        writer._test_failed = failed  # type: ignore[attr-defined]
        writer._test_flushed = flushed  # type: ignore[attr-defined]
        writers.append(writer)
        return writer

    yield make
    for w in writers:
        w.close_all()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_int32_steim2(writer_factory: Any, tmp_path: Path) -> None:
    writer = writer_factory()
    starttime = UTCDateTime("2026-05-09T12:00:00")
    nslc = "IU.ANMO.00.BHZ"
    trace = _make_int32_trace(starttime=starttime, npts=1024, nslc=nslc)

    writer.write_trace(nslc, trace)
    writer.close_all()

    expected = sds_path(_expected_root(tmp_path), starttime, StreamID("IU", "ANMO", "00", "BHZ"))
    assert expected.exists()
    assert expected.stat().st_size > 0
    assert expected.stat().st_size % 512 == 0  # record-aligned

    st = read(str(expected))
    assert len(st) == 1
    rt = st[0]
    assert rt.stats.network == "IU"
    assert rt.stats.station == "ANMO"
    assert rt.stats.location == "00"
    assert rt.stats.channel == "BHZ"
    assert rt.stats.sampling_rate == pytest.approx(100.0)
    assert abs(rt.stats.starttime - starttime) < trace.stats.delta / 2
    np.testing.assert_array_equal(rt.data.astype(np.int32), trace.data)

    # writeOk signal payload
    ok_records: list[tuple[Any, ...]] = writer._test_ok
    assert len(ok_records) == 1
    device, nslc_em, n_bytes, path_em, split, enc = ok_records[0]
    assert device == "dev1"
    assert nslc_em == nslc
    assert n_bytes == expected.stat().st_size
    assert path_em == expected
    assert split is False
    assert enc == "STEIM2"


def test_writes_record_aligned(writer_factory: Any, tmp_path: Path) -> None:
    """Every os.write must produce a multiple of record_length bytes."""
    writer = writer_factory(record_length=512)
    starttime = UTCDateTime("2026-05-09T12:00:00")
    trace = _make_int32_trace(starttime=starttime, npts=4096)
    writer.write_trace("IU.ANMO.00.BHZ", trace)
    writer.close_all()

    p = sds_path(_expected_root(tmp_path), starttime, StreamID("IU", "ANMO", "00", "BHZ"))
    assert p.stat().st_size % 512 == 0


# ---------------------------------------------------------------------------
# Day rollover
# ---------------------------------------------------------------------------


def test_midnight_split_creates_two_files(writer_factory: Any, tmp_path: Path) -> None:
    writer = writer_factory()
    nslc = "IU.ANMO.00.BHZ"
    starttime = UTCDateTime("2026-05-09T23:59:59.500")
    trace = _make_int32_trace(starttime=starttime, npts=200)
    assert trace.stats.endtime > UTCDateTime("2026-05-10T00:00:00")

    writer.write_trace(nslc, trace)
    writer.close_all()

    sid = StreamID("IU", "ANMO", "00", "BHZ")
    pre_path = sds_path(_expected_root(tmp_path), starttime, sid)
    post_path = sds_path(_expected_root(tmp_path), UTCDateTime("2026-05-10T00:00:00.001"), sid)
    assert pre_path != post_path
    assert pre_path.exists()
    assert post_path.exists()

    pre_st = read(str(pre_path))
    post_st = read(str(post_path))
    assert pre_st[0].stats.endtime < UTCDateTime("2026-05-10T00:00:00")
    assert post_st[0].stats.starttime >= UTCDateTime("2026-05-10T00:00:00")
    assert pre_st[0].stats.npts + post_st[0].stats.npts == 200

    ok_records: list[tuple[Any, ...]] = writer._test_ok
    assert len(ok_records) == 1
    _, _, _, path_em, split, _ = ok_records[0]
    assert split is True
    # The signal carries the LAST file written = the post-midnight one.
    assert path_em == post_path


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


def test_lru_eviction_keeps_evicted_files_readable(writer_factory: Any, tmp_path: Path) -> None:
    """``max_open_files=4`` forces eviction on the 5th distinct stream."""
    writer = writer_factory(max_open_files=4)
    starttime = UTCDateTime("2026-05-09T12:00:00")
    streams = [
        "IU.ANMO.00.BHZ",
        "IU.ANMO.00.BHN",
        "IU.ANMO.00.BHE",
        "IU.ANMO.00.LHZ",
        "IU.ANMO.00.LHN",
    ]
    for nslc in streams:
        trace = _make_int32_trace(starttime=starttime, nslc=nslc)
        writer.write_trace(nslc, trace)

    # With cap=4 and 5 distinct streams written, exactly one file has
    # been evicted + closed. The first stream is the LRU victim.
    assert len(writer._open_files) == 4  # type: ignore[attr-defined]

    writer.close_all()

    # All five files must be on disk and readable, regardless of which
    # one was evicted mid-flight.
    for nslc in streams:
        sid = StreamID(*nslc.split("."))
        p = sds_path(_expected_root(tmp_path), starttime, sid)
        assert p.exists()
        st = read(str(p))
        assert len(st) == 1
        assert st[0].stats.npts == 512


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


def test_crash_recovery_truncates_unaligned_tail(
    writer_factory: Any, tmp_path: Path, capture_structlog: list[dict[str, Any]]
) -> None:
    """Pre-existing file with unaligned tail is truncated on first touch.

    This simulates a crash mid-write that left a fractional record at
    the end of the file. The next write to the same path must restore
    record alignment by truncation BEFORE appending.
    """
    nslc = "IU.ANMO.00.BHZ"
    sid = StreamID("IU", "ANMO", "00", "BHZ")
    starttime = UTCDateTime("2026-05-09T12:00:00")

    # Phase 1: write some records; close to release fd.
    writer1 = writer_factory(record_length=512)
    trace = _make_int32_trace(starttime=starttime, npts=2048)
    writer1.write_trace(nslc, trace)
    writer1.close_all()

    p = sds_path(_expected_root(tmp_path), starttime, sid)
    clean_size = p.stat().st_size
    assert clean_size > 512
    assert clean_size % 512 == 0

    # Simulate a torn write: append 256 garbage bytes (half a record).
    with p.open("ab") as f:
        f.write(b"\x00" * 256)
    torn_size = p.stat().st_size
    assert torn_size == clean_size + 256

    # Phase 2: a fresh writer touches the same path. It must truncate
    # back to ``clean_size`` before appending.
    writer2 = writer_factory(record_length=512)
    next_trace = _make_int32_trace(starttime=starttime + 30, npts=512, nslc=nslc)
    writer2.write_trace(nslc, next_trace)
    writer2.close_all()

    # The truncation log line must have been emitted exactly once.
    truncate_logs = [
        rec
        for rec in capture_structlog
        if rec.get("event") == "mseed_writer_truncated_to_valid_record"
    ]
    assert len(truncate_logs) == 1
    assert truncate_logs[0]["kept_bytes"] == clean_size
    assert truncate_logs[0]["lost_bytes"] == 256

    # The file is now record-aligned and readable end-to-end.
    final_size = p.stat().st_size
    assert final_size % 512 == 0
    assert final_size > clean_size  # we appended new records


# ---------------------------------------------------------------------------
# Encoding fallback
# ---------------------------------------------------------------------------


def test_float32_with_steim2_falls_back_to_float32_and_logs_once(
    writer_factory: Any,
    tmp_path: Path,
    capture_structlog: list[dict[str, Any]],
) -> None:
    """STEIM2 cannot encode floats; writer must emit FLOAT32 + log once per stream."""
    writer = writer_factory(encoding="STEIM2")
    nslc = "IU.ANMO.00.BHZ"
    starttime = UTCDateTime("2026-05-09T12:00:00")

    # Write twice — the INFO log should fire only once.
    for offset in (0, 30):
        trace = _make_float32_trace(starttime=starttime + offset, npts=256, nslc=nslc)
        writer.write_trace(nslc, trace)
    writer.close_all()

    downgrade_logs = [
        rec for rec in capture_structlog if rec.get("event") == "mseed_writer_encoding_downgraded"
    ]
    assert len(downgrade_logs) == 1
    assert downgrade_logs[0]["nslc"] == nslc
    assert downgrade_logs[0]["to"] == "FLOAT32"

    ok_records: list[tuple[Any, ...]] = writer._test_ok
    assert len(ok_records) == 2
    for _, _, _, _, _, encoding_chosen in ok_records:
        assert encoding_chosen == "FLOAT32"

    # File is readable and dtype is float32.
    sid = StreamID("IU", "ANMO", "00", "BHZ")
    p = sds_path(_expected_root(tmp_path), starttime, sid)
    st = read(str(p))
    assert st[0].data.dtype == np.float32


# ---------------------------------------------------------------------------
# Int64 overflow detection
# ---------------------------------------------------------------------------


def test_int64_within_int32_range_is_cast(writer_factory: Any, tmp_path: Path) -> None:
    """int64 data within int32 range casts cleanly to int32 for STEIM2."""
    writer = writer_factory(encoding="STEIM2")
    starttime = UTCDateTime("2026-05-09T12:00:00")
    nslc = "IU.ANMO.00.BHZ"
    net, sta, loc, cha = nslc.split(".")
    trace = Trace(
        data=(np.arange(512, dtype=np.int64) * 10),
        header={
            "network": net,
            "station": sta,
            "location": loc,
            "channel": cha,
            "starttime": starttime,
            "sampling_rate": 100.0,
        },
    )
    writer.write_trace(nslc, trace)
    writer.close_all()

    failed: list[tuple[Any, ...]] = writer._test_failed
    ok: list[tuple[Any, ...]] = writer._test_ok
    assert failed == []
    assert len(ok) == 1
    assert ok[0][5] == "STEIM2"


def test_int64_overflow_emits_write_failed(writer_factory: Any, tmp_path: Path) -> None:
    """An int64 sample > int32.max raises ``_EncodingError`` and emits writeFailed."""
    writer = writer_factory(encoding="STEIM2")
    starttime = UTCDateTime("2026-05-09T12:00:00")
    nslc = "IU.ANMO.00.BHZ"
    net, sta, loc, cha = nslc.split(".")
    data = np.zeros(256, dtype=np.int64)
    data[0] = np.iinfo(np.int32).max + 100  # overflow
    trace = Trace(
        data=data,
        header={
            "network": net,
            "station": sta,
            "location": loc,
            "channel": cha,
            "starttime": starttime,
            "sampling_rate": 100.0,
        },
    )
    writer.write_trace(nslc, trace)

    failed: list[tuple[Any, ...]] = writer._test_failed
    assert len(failed) == 1
    device, nslc_em, reason = failed[0]
    assert device == "dev1"
    assert nslc_em == nslc
    assert "encoding error" in reason


# ---------------------------------------------------------------------------
# flushedFile signal (for stage B's DAO)
# ---------------------------------------------------------------------------


def test_flush_all_emits_flushed_file_after_fsync(writer_factory: Any, tmp_path: Path) -> None:
    writer = writer_factory()
    nslc = "IU.ANMO.00.BHZ"
    starttime = UTCDateTime("2026-05-09T12:00:00")
    trace = _make_int32_trace(starttime=starttime, npts=512, nslc=nslc)

    writer.write_trace(nslc, trace)

    flushed_pre: list[tuple[Any, ...]] = writer._test_flushed
    assert flushed_pre == []  # nothing fsynced yet

    writer.flush_all()
    flushed: list[tuple[Any, ...]] = writer._test_flushed
    assert len(flushed) == 1
    device, nslc_em, path, t_start, t_end, bytes_added, file_size = flushed[0]
    assert device == "dev1"
    assert nslc_em == nslc
    assert path == sds_path(
        _expected_root(tmp_path), starttime, StreamID("IU", "ANMO", "00", "BHZ")
    )
    assert t_start == starttime
    assert t_end == trace.stats.endtime
    # Single write before fsync: bytes_added (delta) equals file_size,
    # both equal the on-disk file size.
    assert bytes_added == path.stat().st_size
    assert file_size == path.stat().st_size

    # A second flush with no new data emits nothing.
    writer.flush_all()
    assert len(writer._test_flushed) == 1


def test_flushed_file_size_grows_across_writes(writer_factory: Any, tmp_path: Path) -> None:
    """Multi-write, multi-fsync: bytes_added is per-window delta but
    file_size is the cumulative durable size and grows monotonically.

    Locks the contract that protects cross-session UPSERT semantics for
    ``files.bytes`` (POSTMORTEMS 2026-05-10).
    """
    writer = writer_factory()
    nslc = "IU.ANMO.00.BHZ"
    starttime = UTCDateTime("2026-05-09T12:00:00")
    sid = StreamID("IU", "ANMO", "00", "BHZ")
    expected_path = sds_path(_expected_root(tmp_path), starttime, sid)

    # Three traces to the same SDS path, each followed by a fsync sweep.
    sizes: list[int] = []
    deltas: list[int] = []
    for k in range(3):
        trace = _make_int32_trace(
            starttime=starttime + k * 5.12,  # 512 samples / 100 Hz
            npts=512,
            nslc=nslc,
        )
        writer.write_trace(nslc, trace)
        writer.flush_all()
        flushed = writer._test_flushed[-1]
        deltas.append(flushed[5])
        sizes.append(flushed[6])

    # file_size is monotonic non-decreasing across fsyncs.
    assert sizes == sorted(sizes)
    assert sizes[0] < sizes[1] < sizes[2]
    # file_size at every fsync matches actual on-disk size at that
    # moment — which by the end equals the final file size.
    assert sizes[-1] == expected_path.stat().st_size
    # Per-fsync deltas sum to the final file size.
    assert sum(deltas) == sizes[-1]


def test_close_all_emits_final_flushed_file(writer_factory: Any, tmp_path: Path) -> None:
    """``close_all`` must fsync and emit ``flushedFile`` for any pending data."""
    writer = writer_factory()
    nslc = "IU.ANMO.00.BHZ"
    starttime = UTCDateTime("2026-05-09T12:00:00")
    trace = _make_int32_trace(starttime=starttime, npts=256, nslc=nslc)
    writer.write_trace(nslc, trace)
    writer.close_all()
    assert len(writer._test_flushed) == 1


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------


def test_non_trace_payload_emits_write_failed(
    writer_factory: Any,
) -> None:
    writer = writer_factory()
    writer.write_trace("IU.ANMO.00.BHZ", "not a trace")
    failed: list[tuple[Any, ...]] = writer._test_failed
    assert len(failed) == 1
    assert "non-trace" in failed[0][2]


def test_close_is_idempotent(writer_factory: Any) -> None:
    writer = writer_factory()
    writer.close_all()
    writer.close_all()  # must not raise


def test_exactly_one_terminal_signal_per_write_including_pause(
    writer_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Terminal-signal invariant (M6.5-A): every ``write_trace`` call
    emits exactly one ``writeOk`` XOR ``writeFailed`` — including the
    write that TRIPS the slow-IO pause (which must stay a success, not
    double-emit) and the subsequent writes dropped while paused (which
    must be ``writeFailed``-acknowledged, not silent). The engine's
    archive in-flight gauge counts these acks against packets sent; any
    deviation skews it.
    """
    import echosmonitor.storage.mseed_writer as mw

    writer = writer_factory()
    nslc = "IU.ANMO.00.BHZ"
    t0 = UTCDateTime("2026-05-09T12:00:00")

    # Force every write to register as "slow" so the third one trips
    # the pause; the writes themselves still succeed.
    monkeypatch.setattr(mw, "_SLOW_IO_WARN_MS", -1.0)

    ok: list[tuple[Any, ...]] = writer._test_ok
    failed: list[tuple[Any, ...]] = writer._test_failed
    for i in range(mw._SLOW_IO_THRESHOLD):
        writer.write_trace(nslc, _make_int32_trace(starttime=t0 + i * 5.12, npts=512))
    # All three wrote successfully; the third tripped the pause but must
    # NOT have emitted a second (writeFailed) terminal for itself.
    assert len(ok) == mw._SLOW_IO_THRESHOLD
    assert failed == []

    # While paused, each dropped trace gets exactly one writeFailed ack.
    for i in range(2):
        writer.write_trace(nslc, _make_int32_trace(starttime=t0 + 100 + i * 5.12, npts=512))
    assert len(ok) == mw._SLOW_IO_THRESHOLD
    assert len(failed) == 2
    assert all("paused" in f[2] for f in failed)
    # Invariant across the whole sequence: one terminal per call.
    assert len(ok) + len(failed) == mw._SLOW_IO_THRESHOLD + 2
