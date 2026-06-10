"""Cross-session durability contract test (POSTMORTEMS 2026-05-10).

These tests bypass Qt machinery entirely: they drive ``MseedWriter`` and
``ArchiveDao`` directly. The point is to lock the contract that
``files.bytes`` in the metadata index always equals the post-fsync
durable size of the on-disk file, independently of how many fsync
windows or process restarts have happened — the property that the
flake of ``test_pipeline_e2e::test_restart_resumption_reuses_existing_db``
exposed.

Without the writer's ``file_size`` field on ``flushedFile``, the engine
piped per-fsync deltas into ``record_file`` (whose UPSERT is replace-
not-add), so the index lost session 1's contribution on session 2's
first re-touch of the path.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from obspy import Trace, UTCDateTime

from echosmonitor.config.schema import ArchiveConfig
from echosmonitor.core.models import StreamID
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.mseed_writer import MseedWriter
from echosmonitor.storage.sds import device_sds_root, sds_path

_DEVICE = "dev1"

_NSLC = "IU.ANMO.00.BHZ"
_SID = StreamID("IU", "ANMO", "00", "BHZ")
_STARTTIME = UTCDateTime("2026-05-09T12:00:00")


def _make_trace(*, npts: int, packet_index: int) -> Trace:
    """Build an int32 STEIM2-compatible trace at packet_index * dt."""
    sample_rate = 100.0
    dt_per_packet = npts / sample_rate
    return Trace(
        data=(np.arange(npts, dtype=np.int32) % 1000) - 500,
        header={
            "network": "IU",
            "station": "ANMO",
            "location": "00",
            "channel": "BHZ",
            "starttime": _STARTTIME + packet_index * dt_per_packet,
            "sampling_rate": sample_rate,
        },
    )


def _record_one_session(
    archive_root: Path,
    db_path: Path,
    n_packets: int,
    starting_packet_index: int,
) -> int:
    """Run one synchronous writer + DAO session. Returns the post-fsync
    file size that the DAO recorded for the path.

    Bypasses the engine: every flushedFile is fed directly into the
    DAO's ``record_file`` to mirror the engine's slot, so the DAO row
    is exactly what the engine would have produced — minus Qt timing.
    """
    cfg = ArchiveConfig(
        enabled=True,
        encoding="STEIM2",
        record_length=512,
    )
    writer = MseedWriter(_DEVICE, archive_root, cfg)

    dao = ArchiveDao(db_path)
    dao.start_session(host="test-host", version="0", config_hash="0" * 64)
    device_id = dao.upsert_device(_DEVICE, "h", 1, {})
    stream_id = dao.upsert_stream(device_id, ("IU", "ANMO", "00", "BHZ"), 100.0)

    captured: list[tuple[object, ...]] = []
    writer.flushedFile.connect(lambda *args: captured.append(args))

    try:
        for k in range(n_packets):
            writer.write_trace(
                _NSLC,
                _make_trace(npts=512, packet_index=starting_packet_index + k),
            )
        writer.close_all()  # final fsync + emit
        # Replay every captured flushedFile through the DAO exactly the
        # way the engine slot would.
        for ev in captured:
            _device, _nslc, path, t_start, t_end, _bytes_added, file_size = ev
            assert isinstance(path, Path)
            dao.record_file(stream_id, path, t_start, t_end, int(file_size))
        dao.flush_now()
        return int(file_size)  # last fsync's file size
    finally:
        dao.close()


def _read_dao_bytes(db_path: Path) -> int:
    """Read ``files.bytes`` for the single test path."""
    expected = sds_path(Path("/dummy"), _STARTTIME, _SID).name
    dao_ro = ArchiveDao(db_path)
    try:
        cur = dao_ro._conn().execute(
            "SELECT bytes FROM files WHERE path LIKE ?",
            (f"%{expected}",),
        )
        row = cur.fetchone()
        assert row is not None, "no files row recorded"
        return int(row["bytes"])
    finally:
        dao_ro.close()


def test_cross_session_files_bytes_equals_disk_size(tmp_path: Path) -> None:
    """One session: ``files.bytes`` == ``stat(path).st_size``."""
    archive_root = tmp_path / "archive"
    db_path = tmp_path / "archive.db"

    final_size = _record_one_session(archive_root, db_path, n_packets=3, starting_packet_index=0)
    on_disk_path = sds_path(device_sds_root(archive_root, _DEVICE), _STARTTIME, _SID)
    assert on_disk_path.exists()
    on_disk_size = on_disk_path.stat().st_size
    dao_bytes = _read_dao_bytes(db_path)

    assert dao_bytes == on_disk_size, f"DAO bytes {dao_bytes} != on-disk size {on_disk_size}"
    assert final_size == on_disk_size


@pytest.mark.parametrize("_iteration", range(10))
def test_cross_session_durability_monotonic(tmp_path: Path, _iteration: int) -> None:
    """Two sessions in sequence: ``files.bytes`` after session 2
    must be ≥ session 1's recorded value, AND must equal current
    on-disk file size after session 2.

    Parametrized 10x (10 distinct tmp_paths) so a single ``pytest -q``
    run already exercises the cross-session contract ten times. The
    outer 50-iter pytest loop in CI compounds that to 500 cycles.
    """
    archive_root = tmp_path / "archive"
    db_path = tmp_path / "archive.db"

    # Session 1: 3 packets.
    s1_final = _record_one_session(archive_root, db_path, n_packets=3, starting_packet_index=0)
    s1_disk = sds_path(device_sds_root(archive_root, _DEVICE), _STARTTIME, _SID).stat().st_size
    s1_dao = _read_dao_bytes(db_path)
    assert s1_dao == s1_disk

    # Session 2: 1 packet — INTENTIONALLY small. Under the pre-fix
    # bug, the DAO's UPSERT would replace s1's bytes with this
    # session's tiny last-fsync delta and the assertion below would
    # fail.
    s2_final = _record_one_session(archive_root, db_path, n_packets=1, starting_packet_index=3)
    s2_disk = sds_path(device_sds_root(archive_root, _DEVICE), _STARTTIME, _SID).stat().st_size
    s2_dao = _read_dao_bytes(db_path)

    assert s2_disk > s1_disk, "session 2 must have appended bytes"
    assert s2_dao == s2_disk, f"DAO bytes {s2_dao} != on-disk size {s2_disk} after session 2"
    assert s2_dao >= s1_dao, f"cross-session DAO went BACKWARDS: {s2_dao} < {s1_dao}"
    # Sanity: file_size returned by the writer equals the DAO record.
    assert s2_final == s2_dao
    assert s1_final == s1_dao


def test_cross_session_writer_resumes_existing_file(tmp_path: Path) -> None:
    """Session 2 must reopen an existing file (record-aligned tail) and
    append, not overwrite. ``_validate_or_truncate`` must be a no-op
    for a clean session-1 tail.
    """
    archive_root = tmp_path / "archive"
    db_path = tmp_path / "archive.db"

    _record_one_session(archive_root, db_path, n_packets=2, starting_packet_index=0)
    s1_size = sds_path(device_sds_root(archive_root, _DEVICE), _STARTTIME, _SID).stat().st_size
    assert s1_size % 512 == 0, "session 1 must leave the file aligned"

    _record_one_session(archive_root, db_path, n_packets=2, starting_packet_index=2)
    s2_size = sds_path(device_sds_root(archive_root, _DEVICE), _STARTTIME, _SID).stat().st_size
    assert s2_size > s1_size, "session 2 must append, not truncate or overwrite"
    assert s2_size % 512 == 0, "session 2 must end aligned too"
