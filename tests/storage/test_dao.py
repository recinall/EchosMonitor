"""Tests for ``storage/dao.py`` — round-trip + retry + batch flush."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from obspy import UTCDateTime

from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.db import connect


@pytest.fixture
def dao(tmp_path: Path) -> ArchiveDao:
    return ArchiveDao(tmp_path / "archive.db", batch_window_s=0.1)


def test_session_lifecycle_round_trip(dao: ArchiveDao) -> None:
    sid = dao.start_session("test-host", "0.1.0", "deadbeef")
    assert sid >= 1
    dao.end_session(sid)

    conn = dao._conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row is not None
    assert row["host"] == "test-host"
    assert row["version"] == "0.1.0"
    assert row["config_hash"] == "deadbeef"
    assert row["started_at"] is not None
    assert row["ended_at"] is not None


def test_upsert_device_returns_stable_id(dao: ArchiveDao) -> None:
    a = dao.upsert_device("dev1", "host1", 18000, {"a": 1})
    b = dao.upsert_device("dev1", "host1-renamed", 18001, {"a": 2})
    assert a == b  # same primary key — UPSERT, not INSERT-then-INSERT
    conn = dao._conn()
    row = conn.execute("SELECT * FROM devices WHERE id=?", (a,)).fetchone()
    assert row["host"] == "host1-renamed"
    assert row["port"] == 18001


def test_upsert_stream_unique_per_device(dao: ArchiveDao) -> None:
    dev = dao.upsert_device("dev1", "h", 1, {})
    s1 = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    s2 = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    assert s1 == s2  # same NSLC + device → same row
    s3 = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHN"), 100.0)
    assert s3 != s1  # different channel → different row


def test_record_packet_advances_counters(dao: ArchiveDao) -> None:
    dev = dao.upsert_device("dev1", "h", 1, {})
    sid = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    t1 = UTCDateTime("2026-05-09T12:00:00")
    t2 = UTCDateTime("2026-05-09T12:00:05")
    dao.record_packet(sid, t1, 100)
    dao.record_packet(sid, t2, 100)
    dao.flush_now()

    row = (
        dao._conn()
        .execute(
            "SELECT first_packet_at, last_packet_at, total_packets, total_bytes"
            " FROM streams WHERE id=?",
            (sid,),
        )
        .fetchone()
    )
    assert row["total_packets"] == 2
    assert row["total_bytes"] == 200
    assert row["first_packet_at"] == str(t1)
    assert row["last_packet_at"] == str(t2)


def test_record_file_upserts_by_path(dao: ArchiveDao) -> None:
    dev = dao.upsert_device("dev1", "h", 1, {})
    sid = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    p = Path("/archive/2026/IU/ANMO/BHZ.D/IU.ANMO.00.BHZ.D.2026.130")
    t_start = UTCDateTime("2026-05-09T00:00:00")
    t_end = UTCDateTime("2026-05-09T01:00:00")
    dao.record_file(sid, p, t_start, t_end, 1024)
    dao.flush_now()

    # UPDATE on the same path: t_end advances, t_start preserved.
    later_end = UTCDateTime("2026-05-09T02:00:00")
    dao.record_file(sid, p, t_start, later_end, 2048)
    dao.flush_now()

    rows = dao._conn().execute("SELECT * FROM files WHERE path=?", (str(p),)).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["t_start"] == str(t_start)
    assert row["t_end"] == str(later_end)
    assert row["bytes"] == 2048


def test_record_gap_inserts_distinct_rows(dao: ArchiveDao) -> None:
    dev = dao.upsert_device("dev1", "h", 1, {})
    sid = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    dao.record_gap(
        sid,
        UTCDateTime("2026-05-09T12:00:00"),
        UTCDateTime("2026-05-09T12:00:01"),
        100,
        "gap",
    )
    dao.record_gap(
        sid,
        UTCDateTime("2026-05-09T13:00:00"),
        UTCDateTime("2026-05-09T13:00:01"),
        -50,
        "overlap",
    )
    dao.flush_now()

    rows = (
        dao._conn()
        .execute("SELECT kind, samples_missing FROM gaps WHERE stream_id=?", (sid,))
        .fetchall()
    )
    assert len(rows) == 2
    kinds = {r["kind"] for r in rows}
    assert kinds == {"gap", "overlap"}


def test_last_packet_time_returns_none_when_unset(dao: ArchiveDao) -> None:
    dev = dao.upsert_device("dev1", "h", 1, {})
    sid = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    assert dao.last_packet_time(sid) is None


def test_last_packet_time_round_trips(dao: ArchiveDao) -> None:
    dev = dao.upsert_device("dev1", "h", 1, {})
    sid = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    t = UTCDateTime("2026-05-09T12:00:00")
    dao.record_packet(sid, t, 100)
    dao.flush_now()
    got = dao.last_packet_time(sid)
    assert got is not None
    assert abs(got - t) < 1e-3


# ---------------------------------------------------------------------------
# Tumbling commit window
# ---------------------------------------------------------------------------


def test_record_packet_does_not_commit_within_window(
    tmp_path: Path,
) -> None:
    dao = ArchiveDao(tmp_path / "archive.db", batch_window_s=10.0)
    dev = dao.upsert_device("dev1", "h", 1, {})
    sid = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    t = UTCDateTime("2026-05-09T12:00:00")
    dao.record_packet(sid, t, 100)
    # ``flush_now`` was NOT called; a fresh connection from another
    # thread must NOT see the row yet (writes are uncommitted).
    other_conn = connect(tmp_path / "archive.db")
    try:
        row = other_conn.execute("SELECT total_packets FROM streams WHERE id=?", (sid,)).fetchone()
        # WAL mode: snapshots may differ between connections. The
        # uncommitted update is invisible from another connection.
        assert row["total_packets"] == 0
    finally:
        other_conn.close()


def test_commit_if_due_after_window_elapses(tmp_path: Path) -> None:
    dao = ArchiveDao(tmp_path / "archive.db", batch_window_s=0.05)
    dev = dao.upsert_device("dev1", "h", 1, {})
    sid = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    t = UTCDateTime("2026-05-09T12:00:00")
    dao.record_packet(sid, t, 100)
    time.sleep(0.10)
    dao.record_packet(sid, t, 100)
    # Second record_packet falls outside the 0.05s window from the
    # first commit, so commit_if_due fires.
    other_conn = connect(tmp_path / "archive.db")
    try:
        row = other_conn.execute("SELECT total_packets FROM streams WHERE id=?", (sid,)).fetchone()
        assert row["total_packets"] >= 1
    finally:
        other_conn.close()


# ---------------------------------------------------------------------------
# SQLITE_BUSY retry path
# ---------------------------------------------------------------------------


def test_transactional_retries_on_sqlite_busy(tmp_path: Path) -> None:
    """A transient ``database is locked`` error triggers the retry loop.

    The action raises ``OperationalError`` flagged ``locked`` on its
    first invocation and succeeds on the second; ``transactional``
    must catch the first, sleep (patched to a no-op), and call the
    action again.
    """
    dao = ArchiveDao(tmp_path / "archive.db", batch_window_s=10.0)
    attempts = {"n": 0}

    def _action(cur: sqlite3.Cursor) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        cur.execute(
            "INSERT INTO devices(name, host, port, config_json,"
            "                    first_seen_at, last_seen_at)"
            " VALUES ('dev1', 'h', 1, '{}', 'now', 'now')"
        )

    with patch("time.sleep", lambda _s: None):
        dao.transactional(_action)
    dao.flush_now()
    assert attempts["n"] == 2
    row = dao._conn().execute("SELECT name FROM devices").fetchone()
    assert row["name"] == "dev1"


def test_transactional_propagates_non_busy_error(tmp_path: Path) -> None:
    """A non-busy OperationalError must NOT be retried — it surfaces."""
    dao = ArchiveDao(tmp_path / "archive.db", batch_window_s=10.0)
    attempts = {"n": 0}

    def _action(_cur: sqlite3.Cursor) -> None:
        attempts["n"] += 1
        raise sqlite3.OperationalError("near 'BOGUS': syntax error")

    with pytest.raises(sqlite3.OperationalError, match="syntax"):
        dao.transactional(_action)
    assert attempts["n"] == 1  # no retry


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


def test_close_is_idempotent(tmp_path: Path) -> None:
    dao = ArchiveDao(tmp_path / "archive.db")
    dao.upsert_device("dev1", "h", 1, {})
    dao.close()
    dao.close()  # must not raise


def test_close_flushes_pending_writes(tmp_path: Path) -> None:
    """``close`` must commit any dirty work before disconnecting."""
    dao = ArchiveDao(tmp_path / "archive.db", batch_window_s=10.0)
    dev = dao.upsert_device("dev1", "h", 1, {})
    sid = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    dao.record_packet(sid, UTCDateTime("2026-05-09T12:00:00"), 100)
    dao.close()

    other_conn = connect(tmp_path / "archive.db")
    try:
        row = other_conn.execute("SELECT total_packets FROM streams WHERE id=?", (sid,)).fetchone()
        assert row["total_packets"] == 1
    finally:
        other_conn.close()


# ---------------------------------------------------------------------------
# M2-B sessions: project name, membership, dirty close, listing
# ---------------------------------------------------------------------------


def test_start_session_records_project_and_devices(dao: ArchiveDao) -> None:
    sid = dao.start_session("h", "0.0.0", "c", project_name="Survey 2026", devices=("a", "b"))
    sessions = dao.list_sessions()
    assert len(sessions) == 1
    rec = sessions[0]
    assert rec.id == sid
    assert rec.project_name == "Survey 2026"
    assert rec.devices == ("a", "b")
    assert rec.ended_at is None
    assert rec.closed_dirty is False


def test_add_session_device_is_idempotent(dao: ArchiveDao) -> None:
    sid = dao.start_session("h", "v", "c", project_name="p")
    dao.add_session_device(sid, "dev")
    dao.add_session_device(sid, "dev")  # rejoin: still one member
    assert dao.list_sessions()[0].devices == ("dev",)


def test_end_session_dirty_flag(dao: ArchiveDao) -> None:
    sid = dao.start_session("h", "v", "c", project_name="p")
    dao.end_session(sid, dirty=True)
    rec = dao.list_sessions()[0]
    assert rec.ended_at is not None
    assert rec.closed_dirty is True


def test_close_dirty_sessions_sweeps_only_open_rows(dao: ArchiveDao) -> None:
    closed = dao.start_session("h", "v", "c", project_name="p")
    dao.end_session(closed)
    dao.start_session("h", "v", "c", project_name="p")  # left open (crash)
    swept = dao.close_dirty_sessions()
    assert swept == 1
    by_id = {rec.id: rec for rec in dao.list_sessions()}
    assert by_id[closed].closed_dirty is False  # clean close untouched
    open_recs = [r for r in by_id.values() if r.id != closed]
    assert all(r.closed_dirty and r.ended_at is not None for r in open_recs)


def test_sessionless_row_has_null_project(dao: ArchiveDao) -> None:
    """The monitoring index path (no project) stays representable."""
    dao.start_session("h", "v", "c")
    rec = dao.list_sessions()[0]
    assert rec.project_name is None
    assert rec.devices == ()


def test_session_started_at_fetches_by_id(dao: ArchiveDao) -> None:
    """Provenance by row id (rule 9): a later row — or a future-dated
    crash row — must not be able to answer for the one we hold."""
    first = dao.start_session("h", "v", "c", project_name="p")
    second = dao.start_session("h", "v", "c", project_name="p")
    recs = {r.id: r for r in dao.list_sessions()}
    assert dao.session_started_at(first) == recs[first].started_at
    assert dao.session_started_at(second) == recs[second].started_at
    with pytest.raises(KeyError):
        dao.session_started_at(99999)
