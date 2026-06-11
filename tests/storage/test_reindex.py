"""Re-indexer (M3-D) — rebuild ``archive.db`` from the SDS tree.

The MiniSEED files are the source of truth (rule 8); the re-indexer
re-derives the index from them: spans via obspy headonly reads, bytes
from ``stat`` (rule 9 — on-disk values at the call site), ``files``
rows upserted in place with stale rows pruned, and ONE synthesized
session row (``reindexed=1``) when the DB held no real sessions —
sessions cannot be reconstructed from the tree, so the span is the
data extent and the name is the directory name.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
from obspy import Trace, UTCDateTime

from echosmonitor.core.models import StreamID
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.reindex import ReindexProgress, reindex_session_root
from echosmonitor.storage.sds import device_sds_root, sds_path

_FS = 100.0
_T0 = UTCDateTime("2026-05-10T12:00:00")
_DEVICE = "echos-1"


def _write_trace(
    session_root: Path,
    comp: str,
    t0: UTCDateTime,
    npts: int,
    *,
    device: str = _DEVICE,
    header_channel: str | None = None,
) -> Path:
    """One MiniSEED day-file at the canonical SDS path under ``device``.

    ``header_channel`` forces a header NSLC that disagrees with the path
    (the mis-laid-file case the re-indexer must skip).
    """
    sid = StreamID("XX", "STA", "00", f"HH{comp}")
    path = sds_path(device_sds_root(session_root, device), t0, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(abs(hash(comp)) % (2**32))
    Trace(
        data=(rng.standard_normal(npts) * 1000.0).astype(np.int32),
        header={
            "network": "XX",
            "station": "STA",
            "location": "00",
            "channel": header_channel or f"HH{comp}",
            "starttime": t0,
            "sampling_rate": _FS,
        },
    ).write(str(path), format="MSEED")
    return path


def _db_rows(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Copied archive (no DB): full rebuild + synthesized session
# ---------------------------------------------------------------------------


def test_copied_archive_builds_db_from_tree(tmp_path: Path) -> None:
    root = tmp_path / "field_day"
    paths = {c: _write_trace(root, c, _T0, int(_FS * 30)) for c in ("Z", "N", "E")}
    assert not (root / "archive.db").exists()

    report = reindex_session_root(root, host="lab", version="1.0")

    assert report.files_indexed == 3
    assert report.files_skipped == 0
    assert report.files_pruned == 0
    assert report.devices == 1
    assert report.streams == 3
    assert report.synthesized_session
    assert not report.cancelled

    db = root / "archive.db"
    rows = _db_rows(db, "SELECT path, t_start, t_end, bytes FROM files ORDER BY path")
    assert len(rows) == 3
    by_path = {r[0]: r for r in rows}
    for comp, path in paths.items():
        row = by_path[str(path)]
        assert row[1] == str(_T0)
        # endtime of npts samples at fs: t0 + (npts-1)/fs
        assert row[2] == str(_T0 + (int(_FS * 30) - 1) / _FS)
        assert row[3] == path.stat().st_size, comp
    # rule 9: the per-stream counter is the SUM of the on-disk file bytes.
    totals = _db_rows(
        db,
        "SELECT s.channel, s.total_bytes FROM streams s ORDER BY s.channel",
    )
    for cha, total in totals:
        assert total == paths[cha[-1]].stat().st_size


def test_synthesized_session_row_is_honest(tmp_path: Path) -> None:
    root = tmp_path / "field_day"
    # Two segments → the session span is the DATA EXTENT across them.
    _write_trace(root, "Z", _T0, int(_FS * 30))
    p2 = _write_trace(root, "Z", _T0 + 86_400, int(_FS * 30))  # next day-file
    assert p2.name.endswith("131")  # really a second day

    reindex_session_root(root, host="lab", version="1.0")

    dao = ArchiveDao(root / "archive.db", read_only=True)
    try:
        records = dao.list_sessions()
    finally:
        dao.close()
    assert len(records) == 1
    rec = records[0]
    assert rec.reindexed
    assert rec.project_name == "field_day"  # the dir name — honest fallback
    assert rec.started_at == str(_T0)
    assert rec.ended_at == str(_T0 + 86_400 + (int(_FS * 30) - 1) / _FS)
    assert rec.devices == (_DEVICE,)
    assert rec.host == "lab"


def test_reindex_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    for c in ("Z", "N", "E"):
        _write_trace(root, c, _T0, int(_FS * 30))

    first = reindex_session_root(root)
    second = reindex_session_root(root)

    assert second.files_indexed == first.files_indexed == 3
    assert second.files_pruned == 0
    db = root / "archive.db"
    assert _db_rows(db, "SELECT COUNT(*) FROM files")[0][0] == 3
    assert _db_rows(db, "SELECT COUNT(*) FROM sessions")[0][0] == 1  # ONE synthesized row
    assert _db_rows(db, "SELECT COUNT(*) FROM devices")[0][0] == 1


# ---------------------------------------------------------------------------
# Stale DB: counts corrected from disk truth (rule 9)
# ---------------------------------------------------------------------------


def test_stale_db_counts_corrected_from_disk(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    real_path = _write_trace(root, "Z", _T0, int(_FS * 30))
    # A same-machine duplicate: the original tree EXISTS elsewhere on
    # disk — existence alone must not keep its rows in THIS root's index
    # (review major: prune by run membership, not disk existence).
    original_elsewhere = _write_trace(tmp_path / "original_proj", "Z", _T0, int(_FS * 30))

    # A stale DB as a copy would carry it: a real session row, the device
    # under its RAW name, a files row for the on-disk file with WRONG
    # span/bytes, a row for a path that only existed on the old machine,
    # and a row pointing into the duplicate's original tree.
    dao = ArchiveDao(root / "archive.db")
    sid = dao.start_session("oldhost", "v", "h", project_name="proj", devices=("Echos 1!",))
    dev_id = dao.upsert_device("Echos 1!", "10.0.0.9", 18000, {})
    dao.end_session(sid)
    dao.close()
    # NOTE: "Echos 1!" sanitises to "Echos_1", NOT this tree's dir
    # (echos-1) — so the stale device must stay untouched and the dir
    # gets its own row. A SAME-dir raw name is covered below.
    dao = ArchiveDao(root / "archive.db")
    stream_id = dao.upsert_stream(dev_id, ("XX", "STA", "00", "HHZ"), _FS)
    dao.record_file(
        stream_id,
        Path("/old/machine/archive/proj") / real_path.name,
        _T0 - 999,
        _T0 - 900,
        12,
    )
    dao.record_file(stream_id, original_elsewhere, _T0, _T0 + 30, 1234)
    dao.record_file(stream_id, real_path, _T0 - 5, _T0 + 5, 99)  # wrong span+bytes
    dao.close()

    report = reindex_session_root(root)

    assert report.files_indexed == 1
    # Pruned: the old machine's path AND the outside-root path that exists.
    assert report.files_pruned == 2
    assert not report.synthesized_session  # real session rows exist — never shadowed
    db = root / "archive.db"
    rows = _db_rows(db, "SELECT path, t_start, t_end, bytes FROM files")
    assert len(rows) == 1
    path, t_start, t_end, n_bytes = rows[0]
    assert path == str(real_path)
    assert t_start == str(_T0)  # disk truth replaced the stale span
    assert t_end == str(_T0 + (int(_FS * 30) - 1) / _FS)
    assert n_bytes == real_path.stat().st_size
    sessions = _db_rows(db, "SELECT project_name, reindexed FROM sessions")
    assert sessions == [("proj", 0)]
    # rule 9: only the in-root file's bytes count toward the stream total.
    totals = _db_rows(db, "SELECT SUM(total_bytes) FROM streams")
    assert totals[0][0] == real_path.stat().st_size


def test_device_dir_maps_to_existing_raw_named_device(tmp_path: Path) -> None:
    """A device dir must reuse the existing device row whose RAW name
    sanitises to it — never spawn a sanitized-name duplicate."""
    raw = "My Echos"  # sanitises to "My_Echos"
    root = tmp_path / "proj"
    _write_trace(root, "Z", _T0, int(_FS * 30), device=raw)

    dao = ArchiveDao(root / "archive.db")
    sid = dao.start_session("h", "v", "c", project_name="proj", devices=(raw,))
    dao.upsert_device(raw, "10.0.0.9", 18000, {})
    dao.end_session(sid)
    dao.close()

    report = reindex_session_root(root)

    assert report.files_indexed == 1
    names = _db_rows(root / "archive.db", "SELECT name FROM devices ORDER BY name")
    assert names == [(raw,)]  # no "My_Echos" duplicate


# ---------------------------------------------------------------------------
# Dirty / foreign files: skipped per file, never fatal
# ---------------------------------------------------------------------------


def test_dirty_and_foreign_files_skipped_never_fatal(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    good = _write_trace(root, "Z", _T0, int(_FS * 30))
    # Foreign: not SDS grammar.
    (root / _DEVICE / "README.txt").write_text("hello")
    # Dirty: SDS-named but not MiniSEED.
    corrupt = good.with_name(good.name.replace("HHZ", "HHN")).parent.parent / "HHN.D"
    corrupt.mkdir(parents=True, exist_ok=True)
    (corrupt / good.name.replace("HHZ", "HHN")).write_bytes(b"not miniseed at all")
    # Mis-laid: valid MiniSEED whose header NSLC disagrees with its path.
    _write_trace(root, "E", _T0, int(_FS * 30), header_channel="HHX")

    report = reindex_session_root(root)

    assert report.files_indexed == 1
    assert report.files_skipped == 3
    rows = _db_rows(root / "archive.db", "SELECT path FROM files")
    assert rows == [(str(good),)]


# ---------------------------------------------------------------------------
# Cancellation + progress (rule 7)
# ---------------------------------------------------------------------------


def test_cancel_returns_partial_and_rerun_converges(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    for c in ("Z", "N", "E"):
        _write_trace(root, c, _T0, int(_FS * 30))

    # Stop after the first file is INDEXED (the flag is also polled in the
    # scan phase, so a call-counting stop would fire before any work).
    beats: list[ReindexProgress] = []

    def _stop_after_first_indexed() -> bool:
        return bool(beats and beats[-1].files_done >= 1)

    report = reindex_session_root(
        root, progress=beats.append, should_stop=_stop_after_first_indexed
    )

    assert report.cancelled
    assert report.files_indexed == 1
    assert not report.synthesized_session  # the synthesis pass is skipped
    assert _db_rows(root / "archive.db", "SELECT COUNT(*) FROM sessions")[0][0] == 0

    rerun = reindex_session_root(root)
    assert not rerun.cancelled
    assert rerun.files_indexed == 3
    assert rerun.synthesized_session


def test_stop_during_scan_cancels_without_synthesis(tmp_path: Path) -> None:
    """A stop firing in the SCAN phase (auditor: it must be pollable too,
    rule 7) yields a cancelled run — a truncated candidate list must never
    feed the prune or the synthesized span."""
    root = tmp_path / "proj"
    for c in ("Z", "N", "E"):
        _write_trace(root, c, _T0, int(_FS * 30))

    report = reindex_session_root(root, should_stop=lambda: True)

    assert report.cancelled
    assert report.files_indexed == 0
    assert not report.synthesized_session
    assert report.files_pruned == 0


def test_progress_beats_count_up_to_total(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    for c in ("Z", "N", "E"):
        _write_trace(root, c, _T0, int(_FS * 30))
    beats: list[ReindexProgress] = []

    reindex_session_root(root, progress=beats.append)

    assert [b.files_done for b in beats] == [1, 2, 3]
    assert all(b.files_total == 3 for b in beats)


def test_list_sessions_reads_pre_v5_db_read_only_unmigrated(tmp_path: Path) -> None:
    """The browser opens foreign DBs read-only WITHOUT migration (rule 8):
    `list_sessions` must read a v4 DB (no `reindexed` column) as-is, rows
    reporting reindexed=False, and must not migrate it."""
    db = tmp_path / "archive.db"
    # A genuine v4 DB, built by hand: only what list_sessions touches.
    # (ALTER ... DROP COLUMN can't strip the column from a v5 DB — the
    # DDL's inline comments break SQLite's definition rewrite.)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO _meta VALUES ('schema_version', '4');
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL, ended_at TEXT, host TEXT NOT NULL,
            version TEXT NOT NULL, config_hash TEXT NOT NULL,
            project_name TEXT, closed_dirty INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE session_devices (
            session_id INTEGER NOT NULL,
            device_name TEXT NOT NULL,
            UNIQUE(session_id, device_name)
        );
        INSERT INTO sessions(started_at, ended_at, host, version, config_hash,
                             project_name)
        VALUES ('2026-05-10T12:00:00', '2026-05-10T13:00:00', 'h', 'v', 'c', 'p');
        """
    )
    conn.commit()
    conn.close()

    ro = ArchiveDao(db, read_only=True)
    try:
        records = ro.list_sessions()
    finally:
        ro.close()
    assert len(records) == 1
    assert records[0].project_name == "p"
    assert records[0].reindexed is False
    # Still v4, still column-less: the read-only open migrated nothing.
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    version = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[0]
    conn.close()
    assert "reindexed" not in cols
    assert version == "4"


def test_empty_tree_synthesizes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / _DEVICE).mkdir(parents=True)

    report = reindex_session_root(root)

    assert report.files_indexed == 0
    assert not report.synthesized_session
    assert _db_rows(root / "archive.db", "SELECT COUNT(*) FROM sessions")[0][0] == 0
