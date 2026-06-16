"""Tests for the M6.6-B per-session StationXML persistence (schema v6)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from echosmonitor.storage.archive_reader import read_session_stationxml
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.db import SCHEMA_VERSION, connect

_XML = "<FDSNStationXML>fake</FDSNStationXML>"


def test_fresh_db_has_session_stationxml_table(tmp_path: Path) -> None:
    conn = connect(tmp_path / "archive.db")
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "session_stationxml" in tables
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_stationxml)")}
        assert {"session_id", "device_name", "xml_blob", "fetched_at"} <= cols
    finally:
        conn.close()


def test_upsert_and_read_round_trip(tmp_path: Path) -> None:
    dao = ArchiveDao(tmp_path / "archive.db")
    try:
        sid = dao.start_session("host", "1.0", "hash", project_name="proj", devices=["echos"])
        dao.upsert_session_stationxml(sid, "echos", _XML)
        assert dao.read_session_stationxml(sid, "echos") == _XML
        # Absent device / session → None, never raises.
        assert dao.read_session_stationxml(sid, "other") is None
        assert dao.read_session_stationxml(9999, "echos") is None
    finally:
        dao.close()


def test_upsert_replaces_in_place(tmp_path: Path) -> None:
    dao = ArchiveDao(tmp_path / "archive.db")
    try:
        sid = dao.start_session("host", "1.0", "hash", project_name="proj", devices=["echos"])
        dao.upsert_session_stationxml(sid, "echos", _XML)
        dao.upsert_session_stationxml(sid, "echos", "<FDSNStationXML>v2</FDSNStationXML>")
        assert dao.read_session_stationxml(sid, "echos") == "<FDSNStationXML>v2</FDSNStationXML>"
        # Exactly one row for the (session, device) pair (UNIQUE upsert).
        conn = sqlite3.connect(tmp_path / "archive.db")
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM session_stationxml WHERE session_id=? AND device_name=?",
                (sid, "echos"),
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 1
    finally:
        dao.close()


def test_archive_reader_read_session_stationxml(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    dao = ArchiveDao(db)
    try:
        sid = dao.start_session("host", "1.0", "hash", project_name="proj", devices=["echos"])
        dao.upsert_session_stationxml(sid, "echos", _XML)
    finally:
        dao.close()
    # Read-back with NO writer DAO, read-only open.
    assert read_session_stationxml(db, sid, "echos") == _XML
    assert read_session_stationxml(db, sid, "missing") is None
    # A non-existent DB degrades to None (graceful), never raises.
    assert read_session_stationxml(tmp_path / "nope.db", sid, "echos") is None


def test_pre_v6_db_migrates_and_reads_none_before(tmp_path: Path) -> None:
    """A v5 DB gains the table on connect (no-op CREATE); a read-only open
    of a pre-v6 DB returns None instead of raising on the missing table."""
    db = tmp_path / "archive.db"
    # Build a real DB, then simulate a pre-v6 state: drop the table + stamp v5.
    dao = ArchiveDao(db)
    sid = dao.start_session("host", "1.0", "hash", project_name="proj", devices=["echos"])
    dao.close()
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE session_stationxml")
    raw.execute("UPDATE _meta SET value='5' WHERE key='schema_version'")
    raw.commit()
    raw.close()

    # Read-only open never migrates (rule 8): the table is absent → None.
    assert read_session_stationxml(db, sid, "echos") is None

    # A normal connect migrates v5 → v6, recreating the table.
    conn = connect(db)
    try:
        version = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[
            "value"
        ]
        assert int(version) == SCHEMA_VERSION
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "session_stationxml" in tables
    finally:
        conn.close()
