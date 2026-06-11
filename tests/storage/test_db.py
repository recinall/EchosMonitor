"""Tests for ``storage/db.py`` — schema, PRAGMAs, _upgrade contract."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from echosmonitor.storage import db as db_mod
from echosmonitor.storage.db import SCHEMA_VERSION, connect


def test_connect_creates_schema_on_empty_db(tmp_path: Path) -> None:
    p = tmp_path / "archive.db"
    conn = connect(p)
    try:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"_meta", "sessions", "devices", "streams", "gaps", "files"} <= tables
    finally:
        conn.close()


def test_connect_records_schema_version(tmp_path: Path) -> None:
    conn = connect(tmp_path / "archive.db")
    try:
        row = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()
        assert row is not None
        assert int(row["value"]) == SCHEMA_VERSION
    finally:
        conn.close()


def test_connect_applies_required_pragmas(tmp_path: Path) -> None:
    conn = connect(tmp_path / "archive.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()["journal_mode"] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"] == 1
        assert conn.execute("PRAGMA synchronous").fetchone()["synchronous"] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()["timeout"] == 5000
    finally:
        conn.close()


def test_connect_calls_upgrade_on_every_open(tmp_path: Path) -> None:
    """Regression: ``_upgrade`` is always called so a future schema bump
    cannot quietly skip migrations on existing databases."""
    p = tmp_path / "archive.db"
    with patch.object(db_mod, "_upgrade", wraps=db_mod._upgrade) as upgrade_spy:
        conn = connect(p)
        conn.close()
        # Re-open: still must be called.
        conn = connect(p)
        conn.close()
    assert upgrade_spy.call_count == 2
    # Both calls must pass the current SCHEMA_VERSION on a fresh + reopened db.
    for call in upgrade_spy.call_args_list:
        args = call.args
        assert args[1] == SCHEMA_VERSION


# A faithful copy of the M5 (schema v1) DDL — no ``detections`` table.
# Used to materialise a genuine v1 database the migration must upgrade.
_V1_SCHEMA_SQL = """
CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
    ended_at TEXT, host TEXT NOT NULL, version TEXT NOT NULL,
    config_hash TEXT NOT NULL
);
CREATE TABLE devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    host TEXT NOT NULL, port INTEGER NOT NULL, config_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
);
CREATE TABLE streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    network TEXT NOT NULL, station TEXT NOT NULL, location TEXT NOT NULL,
    channel TEXT NOT NULL, first_packet_at TEXT, last_packet_at TEXT,
    sample_rate REAL NOT NULL, total_packets INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    UNIQUE(device_id, network, station, location, channel)
);
"""


def _make_v1_db(path: Path) -> int:
    """Materialise a schema-v1 DB with one device + stream. Returns stream id."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_V1_SCHEMA_SQL)
        conn.execute("INSERT INTO _meta(key, value) VALUES ('schema_version', '1')")
        conn.execute(
            "INSERT INTO devices(name, host, port, config_json, first_seen_at,"
            "                    last_seen_at)"
            " VALUES ('legacy', 'h', 1, '{}', 'now', 'now')"
        )
        dev = conn.execute("SELECT id FROM devices WHERE name='legacy'").fetchone()["id"]
        conn.execute(
            "INSERT INTO streams(device_id, network, station, location, channel,"
            "                    sample_rate)"
            " VALUES (?, 'IU', 'ANMO', '00', 'BHZ', 100.0)",
            (dev,),
        )
        sid = conn.execute("SELECT id FROM streams WHERE device_id=?", (dev,)).fetchone()["id"]
        conn.commit()
        return int(sid)
    finally:
        conn.close()


def test_detections_table_present_on_fresh_db(tmp_path: Path) -> None:
    conn = connect(tmp_path / "archive.db")
    try:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "detections" in tables
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(detections)")}
        assert {"stream_id", "kind", "t_on", "t_off", "score", "detected_at", "meta_json"} <= cols
    finally:
        conn.close()


def test_v1_to_current_migration_on_existing_v1_db(tmp_path: Path) -> None:
    """A real v1 DB upgrades cleanly through the whole ladder: the
    detections table (v2) appears, the version reaches the current
    SCHEMA_VERSION, pre-existing v1 rows survive, and — post rule 12 —
    no ``events`` table is ever created."""
    p = tmp_path / "archive.db"
    stream_id = _make_v1_db(p)

    conn = connect(p)  # triggers v1 → 2 → 3 in one open
    try:
        version = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[
            "value"
        ]
        assert int(version) == SCHEMA_VERSION

        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "detections" in tables
        assert "events" not in tables  # AI events table removed (rule 12)

        # Legacy data preserved across the migration.
        survived = conn.execute("SELECT COUNT(*) AS n FROM streams WHERE id=?", (stream_id,))
        assert survived.fetchone()["n"] == 1

        # The migrated detections table accepts a FK-valid insert.
        conn.execute(
            "INSERT INTO detections(stream_id, kind, t_on, score, detected_at)"
            " VALUES (?, 'sta_lta', '2026-06-01T00:00:00', 7.5, '2026-06-01T00:00:01')",
            (stream_id,),
        )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) AS n FROM detections").fetchone()["n"] == 1
    finally:
        conn.close()


def _make_v2_db(path: Path) -> int:
    """Materialise a schema-v2 DB (v1 + detections). Returns stream id.
    Built by migrating a v1 DB up to current then stamping the version
    back to 2 — dropping nothing — so the next connect exercises the
    v2 → v3 step exactly as a genuine v2 install would."""
    import sqlite3

    sid = _make_v1_db(path)
    connect(path).close()  # migrates 1 → SCHEMA_VERSION
    conn = sqlite3.connect(path)
    try:
        # Seed one detections row so the v2→v3 test can prove the no-op
        # bump preserves detection data, not just streams.
        conn.execute(
            "INSERT INTO detections(stream_id, kind, t_on, score, detected_at)"
            " VALUES (?, 'sta_lta', '2026-06-01T00:00:00', 7.5, '2026-06-01T00:00:01')",
            (sid,),
        )
        conn.execute("UPDATE _meta SET value='2' WHERE key='schema_version'")
        conn.commit()
    finally:
        conn.close()
    return sid


def test_v2_to_v3_migration_is_pure_version_bump(tmp_path: Path) -> None:
    """M0 regression: with the AI events subsystem removed (rule 12), the
    v2 → v3 step is a pure no-op bump — NO ``events`` table is created
    and detections rows survive. The ladder then continues to the
    current version (v4 added the session columns, M2-B)."""
    p = tmp_path / "archive.db"
    stream_id = _make_v2_db(p)

    conn = connect(p)  # triggers the v2 → v3 (→ current) upgrade
    try:
        version = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[
            "value"
        ]
        assert int(version) == SCHEMA_VERSION

        events_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchall()
        assert events_tables == []

        # Pre-existing rows survive the no-op bump untouched.
        survived = conn.execute("SELECT COUNT(*) AS n FROM streams WHERE id=?", (stream_id,))
        assert survived.fetchone()["n"] == 1
        detections = conn.execute(
            "SELECT COUNT(*) AS n FROM detections WHERE stream_id=?", (stream_id,)
        )
        assert detections.fetchone()["n"] == 1
    finally:
        conn.close()


def test_fresh_db_has_no_events_table(tmp_path: Path) -> None:
    """M0 regression: a fresh connect must not create the removed AI
    ``events`` table (rule 12)."""
    conn = connect(tmp_path / "archive.db")
    try:
        events_tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchall()
        assert events_tables == []
    finally:
        conn.close()


def test_migration_is_idempotent_on_reopen(tmp_path: Path) -> None:
    """Re-opening an already-current DB leaves the version at SCHEMA_VERSION
    and does not error (the upgrade ladder early-returns)."""
    p = tmp_path / "archive.db"
    _make_v1_db(p)
    connect(p).close()  # 1 → SCHEMA_VERSION
    conn = connect(p)  # current → current (no-op)
    try:
        version = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[
            "value"
        ]
        assert int(version) == SCHEMA_VERSION
    finally:
        conn.close()


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    """A bare ``stream`` insert with a non-existent device_id must fail."""
    import sqlite3

    conn = connect(tmp_path / "archive.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO streams(device_id, network, station, location, channel,"
                "                    sample_rate, total_packets, total_bytes)"
                " VALUES (999, 'IU', 'ANMO', '00', 'BHZ', 100.0, 0, 0)"
            )
            conn.commit()
    finally:
        conn.close()


def test_gaps_kind_check_constraint(tmp_path: Path) -> None:
    import sqlite3

    conn = connect(tmp_path / "archive.db")
    try:
        # Insert a session, device, stream so FK is satisfied.
        conn.execute(
            "INSERT INTO sessions(started_at, host, version, config_hash)"
            " VALUES ('2026-05-09T00:00:00', 'localhost', '0.1', 'abc')"
        )
        conn.execute(
            "INSERT INTO devices(name, host, port, config_json,"
            "                    first_seen_at, last_seen_at)"
            " VALUES ('dev', 'h', 1, '{}', '2026-05-09T00:00:00',"
            "         '2026-05-09T00:00:00')"
        )
        device_id = conn.execute("SELECT id FROM devices WHERE name='dev'").fetchone()["id"]
        conn.execute(
            "INSERT INTO streams(device_id, network, station, location, channel,"
            "                    sample_rate)"
            " VALUES (?, 'IU', 'ANMO', '00', 'BHZ', 100.0)",
            (device_id,),
        )
        stream_id = conn.execute(
            "SELECT id FROM streams WHERE device_id=?", (device_id,)
        ).fetchone()["id"]
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO gaps(stream_id, t_start, t_end, samples_missing,"
                "                 kind, detected_at)"
                " VALUES (?, '2026-05-09T00:00:00', '2026-05-09T00:00:01',"
                "         5, 'INVALID_KIND', '2026-05-09T00:00:01')",
                (stream_id,),
            )
            conn.commit()
    finally:
        conn.close()


def test_unique_streams_constraint(tmp_path: Path) -> None:
    import sqlite3

    conn = connect(tmp_path / "archive.db")
    try:
        conn.execute(
            "INSERT INTO devices(name, host, port, config_json,"
            "                    first_seen_at, last_seen_at)"
            " VALUES ('dev', 'h', 1, '{}', 'now', 'now')"
        )
        device_id = conn.execute("SELECT id FROM devices WHERE name='dev'").fetchone()["id"]
        conn.execute(
            "INSERT INTO streams(device_id, network, station, location, channel,"
            "                    sample_rate)"
            " VALUES (?, 'IU', 'ANMO', '00', 'BHZ', 100.0)",
            (device_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO streams(device_id, network, station, location, channel,"
                "                    sample_rate)"
                " VALUES (?, 'IU', 'ANMO', '00', 'BHZ', 100.0)",
                (device_id,),
            )
            conn.commit()
    finally:
        conn.close()


def test_unique_files_path_constraint(tmp_path: Path) -> None:
    """``files.path`` is UNIQUE: a re-touched file is UPDATEd, not duplicated."""
    import sqlite3

    conn = connect(tmp_path / "archive.db")
    try:
        conn.execute(
            "INSERT INTO devices(name, host, port, config_json, first_seen_at,"
            "                    last_seen_at)"
            " VALUES ('dev', 'h', 1, '{}', 'now', 'now')"
        )
        device_id = conn.execute("SELECT id FROM devices WHERE name='dev'").fetchone()["id"]
        conn.execute(
            "INSERT INTO streams(device_id, network, station, location, channel,"
            "                    sample_rate)"
            " VALUES (?, 'IU', 'ANMO', '00', 'BHZ', 100.0)",
            (device_id,),
        )
        stream_id = conn.execute(
            "SELECT id FROM streams WHERE device_id=?", (device_id,)
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO files(stream_id, path, t_start, t_end, bytes, last_modified_at)"
            " VALUES (?, '/a/b', 't1', 't2', 100, 't2')",
            (stream_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO files(stream_id, path, t_start, t_end, bytes,"
                "                  last_modified_at)"
                " VALUES (?, '/a/b', 't3', 't4', 200, 't4')",
                (stream_id,),
            )
            conn.commit()
    finally:
        conn.close()


def _make_v3_db(path: Path) -> int:
    """Materialise a schema-v3 DB with one open session row. Returns its id."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_V1_SCHEMA_SQL)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_id INTEGER NOT NULL,
                kind TEXT NOT NULL, t_on TEXT NOT NULL, t_off TEXT,
                score REAL NOT NULL, detected_at TEXT NOT NULL,
                meta_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        conn.execute("INSERT INTO _meta(key, value) VALUES ('schema_version', '3')")
        conn.execute(
            "INSERT INTO sessions(started_at, host, version, config_hash)"
            " VALUES ('2026-06-01T00:00:00Z', 'h', '0.0.0', 'c')"
        )
        sid = conn.execute("SELECT id FROM sessions").fetchone()["id"]
        conn.commit()
        return int(sid)
    finally:
        conn.close()


def test_v3_to_v4_migration_adds_session_columns_and_membership(tmp_path: Path) -> None:
    """v3 → v4 (M2-B, rule 14): sessions gain project_name + closed_dirty,
    session_devices appears, pre-existing rows survive with NULL project
    and a clean dirty flag."""
    db_path = tmp_path / "archive.db"
    old_id = _make_v3_db(db_path)

    conn = connect(db_path)
    try:
        version = conn.execute("SELECT value FROM _meta WHERE key='schema_version'").fetchone()[
            "value"
        ]
        assert int(version) == db_mod.SCHEMA_VERSION
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert {"project_name", "closed_dirty"} <= cols
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "session_devices" in tables
        row = conn.execute(
            "SELECT project_name, closed_dirty FROM sessions WHERE id=?", (old_id,)
        ).fetchone()
        assert row["project_name"] is None
        assert row["closed_dirty"] == 0
    finally:
        conn.close()


def test_v4_columns_present_on_fresh_db(tmp_path: Path) -> None:
    conn = connect(tmp_path / "archive.db")
    try:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert {"project_name", "closed_dirty"} <= cols
        sd_cols = {row["name"] for row in conn.execute("PRAGMA table_info(session_devices)")}
        assert sd_cols == {"session_id", "device_name"}
    finally:
        conn.close()
