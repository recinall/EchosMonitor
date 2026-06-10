"""SQLite schema and connection helpers for the M5 metadata index.

The DB is an *index* over the SDS files on disk; if the two diverge,
the files win (rule 8 / plan ``Decisions taken``). Queries are
intentionally narrow: M5 is correctness-first; M6 will introduce a
browse UI on top, and any future re-indexer reads the SDS tree
directly via :func:`storage.sds.parse_sds_path`.

Schema invariants:

* All timestamps are ISO-8601 UTC strings (no native sqlite datetime).
  Use ``str(UTCDateTime(...))`` — its ``__str__`` is the canonical
  ISO form and round-trips through ``UTCDateTime("...")``.
* ``streams.last_packet_at`` is gated on the writer's fsync (the
  DB-after-fsync invariant). This means it lags real time by up to
  ``ArchiveConfig.fsync_interval_s`` (default 5 s). The contract is
  "the DB never claims more than what is on disk." Operators
  monitoring liveness via SQLite must account for this lag.
* ``files.path`` is UNIQUE: a file outlives a session. When a later
  session re-touches an existing file, the row is UPDATEd in place
  rather than INSERTed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import structlog

SCHEMA_VERSION = 3

# Connection-level PRAGMAs. ``WAL`` lets readers and writers proceed
# without blocking each other. ``synchronous=NORMAL`` is the standard
# WAL companion: trades a tiny window of "last microsecond" durability
# for ~5x throughput, acceptable because the MSEED file (not the DB)
# is the source of truth. ``busy_timeout`` covers transient lock
# contention from a concurrent reader.
_PRAGMAS: tuple[tuple[str, object], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("foreign_keys", "ON"),
    ("busy_timeout", 5000),
)

_CREATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    host         TEXT NOT NULL,
    version      TEXT NOT NULL,
    config_hash  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    host          TEXT NOT NULL,
    port          INTEGER NOT NULL,
    config_json   TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    network         TEXT NOT NULL,
    station         TEXT NOT NULL,
    location        TEXT NOT NULL,
    channel         TEXT NOT NULL,
    first_packet_at TEXT,
    last_packet_at  TEXT,
    sample_rate     REAL NOT NULL,
    total_packets   INTEGER NOT NULL DEFAULT 0,
    total_bytes     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(device_id, network, station, location, channel)
);

CREATE TABLE IF NOT EXISTS gaps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id       INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    t_start         TEXT NOT NULL,
    t_end           TEXT NOT NULL,
    samples_missing INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('gap', 'overlap', 'rate_change')),
    detected_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id        INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    path             TEXT NOT NULL UNIQUE,
    t_start          TEXT NOT NULL,
    t_end            TEXT NOT NULL,
    bytes            INTEGER NOT NULL,
    last_modified_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_streams_device ON streams(device_id);
CREATE INDEX IF NOT EXISTS idx_gaps_stream    ON gaps(stream_id);
CREATE INDEX IF NOT EXISTS idx_files_stream   ON files(stream_id);
"""

# Detection rows (schema v2). A ``detections`` row is the persisted,
# device-scoped form of a transient dsp ``Trigger`` (see
# :class:`core.models.Detection`). ``kind`` distinguishes detector
# families (``'sta_lta'`` today) so a single table can serve future
# detector kinds. ``score`` is a generic detector-agnostic magnitude
# (the peak STA/LTA ratio); ``t_off`` is NULL while a trigger is still
# open.
#
# This DDL lives in BOTH the base schema (fresh installs) and the
# v1→v2 migration (existing M5 databases). Both forms are idempotent
# (``IF NOT EXISTS``), so the migration recreates the table for an
# upgrading DB regardless of how the base schema evolves.
_DETECTIONS_DDL = """
CREATE TABLE IF NOT EXISTS detections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id   INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    t_on        TEXT NOT NULL,
    t_off       TEXT,
    score       REAL NOT NULL,
    detected_at TEXT NOT NULL,
    meta_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_detections_stream ON detections(stream_id);
CREATE INDEX IF NOT EXISTS idx_detections_t_on   ON detections(t_on);
"""

_CREATE_SCHEMA_SQL = _CREATE_SCHEMA_SQL + _DETECTIONS_DDL

# Schema v3 historically added the ``events`` table (the removed AI
# persist-on-detection feature, CLAUDE.md rule 12). The version number is
# retained so the migration ladder stays linear, but fresh installs no
# longer create the table and the v2→v3 step is a no-op stub. Old v3
# databases may still contain an orphaned ``events`` table with rows;
# nothing reads or writes it.

_log = structlog.get_logger(__name__)


def connect(db_path: Path) -> sqlite3.Connection:
    """Open or initialise the archive database at ``db_path``.

    Idempotent: existing databases keep their data; the schema
    statements all use ``IF NOT EXISTS``. The ``_meta.schema_version``
    row is created lazily on first connect; a future schema bump
    flows through :func:`_upgrade`.

    The connection is configured with thread-checks disabled
    (``check_same_thread=False``) so a single ``ArchiveDao`` can lazy-
    create connections per accessing thread without crashing the
    sqlite3 module's thread guard. Callers MUST still use one
    connection per thread (see :class:`storage.dao.ArchiveDao`).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for name, value in _PRAGMAS:
        conn.execute(f"PRAGMA {name}={value}")
    conn.executescript(_CREATE_SCHEMA_SQL)
    cur = conn.execute("SELECT value FROM _meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO _meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        from_version = SCHEMA_VERSION  # fresh DB
    else:
        from_version = int(row["value"])
    _upgrade(conn, from_version)
    return conn


def _upgrade(conn: sqlite3.Connection, from_version: int) -> None:
    """Apply schema migrations from ``from_version`` up to ``SCHEMA_VERSION``.

    Always called on connect so a regression test can assert the call
    site exists. A fresh DB (or an already-current one) passes
    ``from_version == SCHEMA_VERSION`` and returns immediately — the
    base schema (executed before this runs) already has every table.

    Existing databases step through the ladder one version at a time.
    Each step runs its DDL in-transaction and bumps the stored
    ``_meta.schema_version`` only after the DDL succeeds, so an
    interrupted upgrade re-runs cleanly on the next connect (every
    step is idempotent via ``IF NOT EXISTS``).

    v1 → v2 (M8): add the ``detections`` table + indexes. The M5 schema
    had no place for STA/LTA triggers; M8 persists them here. The
    table DDL is idempotent so it is safe even though the base schema
    of a fresh v2 install already created it.

    v2 → v3: historically added the ``events`` table for the removed AI
    persist-on-detection feature (rule 12). Now a no-op stub — only the
    version bump remains, so v2 databases still step to v3 and the
    ladder stays linear.
    """
    if from_version == SCHEMA_VERSION:
        return
    version = from_version
    if version == 1:
        conn.executescript(_DETECTIONS_DDL)
        version = 2
    if version == 2:
        version = 3
    if version != SCHEMA_VERSION:
        # Unknown / future version we don't know how to migrate. Leave
        # the DB untouched and surface it loudly rather than silently
        # claiming success.
        _log.warning(
            "archive_db_upgrade_unhandled",
            from_version=from_version,
            target_version=SCHEMA_VERSION,
        )
        return
    conn.execute(
        "UPDATE _meta SET value=? WHERE key='schema_version'",
        (str(version),),
    )
    conn.commit()
    _log.info(
        "archive_db_upgraded",
        from_version=from_version,
        to_version=version,
    )
