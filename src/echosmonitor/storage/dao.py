"""Data-access layer for the M5 archive metadata index.

Owned by the storage QThread. Per-thread sqlite3 connections are
required by the standard library; :class:`ArchiveDao` enforces this
via :class:`threading.local`. The DAO is designed for one writer
thread (the engine's archive thread); other threads can hold their
own connections for read-only queries (future re-indexer / browse UI
in M6) but should not call mutation methods.

Commit cadence
--------------

Per-packet metadata writes batch into a 1 s tumbling window: after a
mutation, :meth:`commit_if_due` commits only when at least
``batch_window_s`` seconds have elapsed since the last commit. The
trade is documented in the module docstring of :mod:`storage.db`:

* SQLite at ``synchronous=NORMAL`` already collapses many writes into
  a single fsync per commit; batching at 1 s reduces the fsync rate
  by ~50x without affecting durability under normal shutdown.
* :meth:`flush_now` forces an immediate commit; the engine calls it
  on stop and on session boundaries.

Session-scope methods (:meth:`start_session` / :meth:`end_session`
/ :meth:`upsert_device`) commit immediately because callers depend on
the row being durable before subsequent FK-using writes can target it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import structlog
from obspy import UTCDateTime

from echosmonitor.core.models import Detection, SessionRecord
from echosmonitor.storage.db import connect

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


_T = TypeVar("_T")

# Maximum number of attempts to retry a SQLITE_BUSY-flagged statement
# before giving up. Backoff doubles each attempt: 50, 100, 200 ms.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S: tuple[float, ...] = (0.05, 0.10, 0.20)

_log = structlog.get_logger(__name__)


def _now_iso() -> str:
    return str(UTCDateTime())


class ArchiveDao:
    """Per-archive-root DAO. Holds one connection per accessing thread."""

    def __init__(
        self,
        db_path: Path,
        batch_window_s: float = 1.0,
        *,
        read_only: bool = False,
    ) -> None:
        self._db_path = db_path
        self._batch_window_s = batch_window_s
        self._read_only = read_only
        self._local = threading.local()
        # ``_dirty`` and ``_last_commit`` are only touched by the
        # storage thread (the only writer). Read-only readers don't
        # change them. No lock needed for the M5 single-writer
        # design — note in the docstring above.
        self._dirty = False
        self._last_commit: float = time.monotonic()

    # ------------------------------------------------------------------
    # Connection / lifecycle
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self._db_path, read_only=self._read_only)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Flush + close the current thread's connection.

        Idempotent. Other threads' connections (if any) outlive this
        call and must be closed by their own owners.
        """
        self.flush_now()
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    # Transaction primitives
    # ------------------------------------------------------------------

    def transactional(self, action: Callable[[sqlite3.Cursor], _T]) -> _T:
        """Run ``action`` against a fresh cursor with SQLITE_BUSY retry.

        Each attempt opens a new cursor and invokes ``action(cursor)``.
        On ``OperationalError`` flagged as locked/busy, the action is
        retried with exponential backoff up to :data:`_RETRY_ATTEMPTS`
        times. A successful return marks the connection dirty so
        :meth:`commit_if_due` can pick it up. Non-busy errors propagate
        immediately (no retry).

        Callers pass a small lambda that does the SQL work and returns
        whatever they need (e.g. ``cur.lastrowid`` or ``cur.fetchone``).
        Multiple statements per action are fine — they all run on the
        same cursor and either all succeed or the first failure rolls
        the retry counter forward together.
        """
        conn = self._conn()
        last_exc: sqlite3.OperationalError | None = None
        for attempt in range(_RETRY_ATTEMPTS):
            cur = conn.cursor()
            try:
                result = action(cur)
            except sqlite3.OperationalError as exc:
                msg = str(exc).lower()
                if any(s in msg for s in ("locked", "busy")) and attempt + 1 < _RETRY_ATTEMPTS:
                    last_exc = exc
                    backoff = _RETRY_BACKOFF_S[attempt]
                    _log.warning(
                        "archive_dao_busy_retry",
                        attempt=attempt + 1,
                        backoff_s=backoff,
                        error=str(exc),
                    )
                    time.sleep(backoff)
                    continue
                raise
            else:
                self._dirty = True
                return result
        # All retries exhausted — re-raise the last seen error.
        if last_exc is not None:
            raise last_exc
        # Unreachable: the loop either returns or raises.
        raise RuntimeError("transactional: unreachable")  # pragma: no cover

    def commit_if_due(self) -> None:
        """Commit if at least ``batch_window_s`` has elapsed since last commit.

        Trades up to ``batch_window_s`` of last-microsecond durability
        for a ~50x reduction in fsync rate. The MSEED file (not the
        DB) is the source of truth, so a crash in this window loses
        only the index row — re-derivable from the file.
        """
        if not self._dirty or self._read_only:
            return
        now = time.monotonic()
        if now - self._last_commit >= self._batch_window_s:
            self._conn().commit()
            self._last_commit = now
            self._dirty = False

    def flush_now(self) -> None:
        """Commit immediately if dirty. Called at session boundaries.

        A no-op on read-only DAOs: ``transactional`` marks the
        connection dirty even for SELECT-only actions, and a commit on
        a ``query_only`` connection has nothing to write.
        """
        if not self._dirty or self._read_only:
            return
        self._conn().commit()
        self._last_commit = time.monotonic()
        self._dirty = False

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start_session(
        self,
        host: str,
        version: str,
        config_hash: str,
        project_name: str | None = None,
        devices: Sequence[str] = (),
    ) -> int:
        """Insert a session row and commit immediately. Returns the new id.

        ``project_name`` is the RAW user-chosen name (rule 14 — the
        sanitized form is the directory this DB lives in; the raw name
        survives only here). ``None`` marks the sessionless monitoring
        index. ``devices`` seeds the membership table; later joiners go
        through :meth:`add_session_device`.
        """

        def _insert(cur: sqlite3.Cursor) -> int | None:
            cur.execute(
                "INSERT INTO sessions(started_at, host, version, config_hash,"
                "                     project_name)"
                " VALUES (?, ?, ?, ?, ?)",
                (_now_iso(), host, version, config_hash, project_name),
            )
            session_id = cur.lastrowid
            for device_name in devices:
                cur.execute(
                    "INSERT OR IGNORE INTO session_devices(session_id, device_name)"
                    " VALUES (?, ?)",
                    (session_id, device_name),
                )
            return session_id

        session_id = self.transactional(_insert)
        self.flush_now()
        if session_id is None:  # pragma: no cover - defensive
            raise RuntimeError("start_session: lastrowid was None")
        return int(session_id)

    def add_session_device(self, session_id: int, device_name: str) -> None:
        """Record that ``device_name`` recorded into the session.

        Idempotent (``INSERT OR IGNORE`` on the UNIQUE pair): a device
        that stops and rejoins the same session stays a single member.
        """

        def _insert(cur: sqlite3.Cursor) -> None:
            cur.execute(
                "INSERT OR IGNORE INTO session_devices(session_id, device_name)"
                " VALUES (?, ?)",
                (session_id, device_name),
            )

        self.transactional(_insert)
        self.flush_now()

    def end_session(self, session_id: int, *, dirty: bool = False) -> None:
        """Close a session row. ``dirty`` marks an administrative close
        (crash recovery), not a user-driven stop."""

        def _update(cur: sqlite3.Cursor) -> None:
            cur.execute(
                "UPDATE sessions SET ended_at=?, closed_dirty=? WHERE id=?",
                (_now_iso(), 1 if dirty else 0, session_id),
            )

        self.transactional(_update)
        self.flush_now()

    def session_started_at(self, session_id: int) -> str:
        """``started_at`` of exactly the given row (rule 9 provenance).

        Fetched by id — never by recency ordering, which a crash-dirty
        row with a skewed future timestamp could fool.
        """

        def _select(cur: sqlite3.Cursor) -> str:
            row = cur.execute(
                "SELECT started_at FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no session row with id {session_id}")
            return str(row["started_at"])

        return self.transactional(_select)

    def upsert_session_stationxml(
        self, session_id: int, device_name: str, xml_blob: str
    ) -> None:
        """Persist the device StationXML for a session (M6.6-B, rule 14).

        UPSERT on ``(session_id, device_name)``: a re-fetch replaces the
        prior blob. Committed immediately — a session-boundary write, like
        :meth:`start_session`. ``fetched_at`` is ISO-8601 UTC.
        """

        def _upsert(cur: sqlite3.Cursor) -> None:
            cur.execute(
                "INSERT INTO session_stationxml"
                "        (session_id, device_name, xml_blob, fetched_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(session_id, device_name)"
                " DO UPDATE SET xml_blob=excluded.xml_blob,"
                "               fetched_at=excluded.fetched_at",
                (session_id, device_name, xml_blob, _now_iso()),
            )

        self.transactional(_upsert)
        self.flush_now()

    def read_session_stationxml(self, session_id: int, device_name: str) -> str | None:
        """The persisted StationXML blob for a ``(session, device)``, or None.

        Guarded against a missing table so a browsed pre-v6 DB (read-only
        opens never migrate, rule 8) returns None instead of raising.
        """

        def _select(cur: sqlite3.Cursor) -> str | None:
            tables = {
                row[0]
                for row in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "session_stationxml" not in tables:
                return None
            row = cur.execute(
                "SELECT xml_blob FROM session_stationxml"
                " WHERE session_id=? AND device_name=?",
                (session_id, device_name),
            ).fetchone()
            return str(row["xml_blob"]) if row is not None else None

        return self.transactional(_select)

    def close_dirty_sessions(self) -> int:
        """Close every still-open session as dirty; return how many.

        Crash recovery (rule 14 / ROADMAP M2-C): a session left open by
        a crash is closed administratively on the next open of this DB,
        with ``closed_dirty=1`` and ``ended_at`` = now (the close time,
        NOT the real end of recording — the files carry the true
        extent, rule 8). Call BEFORE :meth:`start_session` so the new
        session can never be swept up by its own recovery pass.
        """

        def _update(cur: sqlite3.Cursor) -> int:
            cur.execute(
                "UPDATE sessions SET ended_at=?, closed_dirty=1 WHERE ended_at IS NULL",
                (_now_iso(),),
            )
            return cur.rowcount

        count = self.transactional(_update)
        self.flush_now()
        if count:
            _log.warning(
                "sessions_closed_dirty",
                db=str(self._db_path),
                count=count,
            )
        return int(count)

    def list_sessions(self, limit: int = 200) -> list[SessionRecord]:
        """Most-recent-first session rows with device membership.

        Bounded read for the Archive tab's session browser (M3-A) and
        tests; every field comes straight from the rows (rule 9). The
        v5 ``reindexed`` column is read via a ``pragma table_info``
        guard: read-only opens never migrate (rule 8), so a browsed
        pre-v5 DB simply has no such column — its rows read ``False``.
        """

        def _select(cur: sqlite3.Cursor) -> list[SessionRecord]:
            cols = {row[1] for row in cur.execute("PRAGMA table_info(sessions)")}
            reindexed_sql = "reindexed" if "reindexed" in cols else "0 AS reindexed"
            rows = cur.execute(
                "SELECT id, project_name, started_at, ended_at, closed_dirty, host,"
                f" {reindexed_sql}"
                " FROM sessions ORDER BY started_at DESC, id DESC LIMIT ?",
                (max(0, limit),),
            ).fetchall()
            records: list[SessionRecord] = []
            for row in rows:
                device_rows = cur.execute(
                    "SELECT device_name FROM session_devices"
                    " WHERE session_id=? ORDER BY device_name",
                    (row["id"],),
                ).fetchall()
                records.append(
                    SessionRecord(
                        id=int(row["id"]),
                        project_name=row["project_name"],
                        started_at=row["started_at"],
                        ended_at=row["ended_at"],
                        closed_dirty=bool(row["closed_dirty"]),
                        host=row["host"],
                        devices=tuple(r["device_name"] for r in device_rows),
                        reindexed=bool(row["reindexed"]),
                    )
                )
            return records

        return self.transactional(_select)

    # ------------------------------------------------------------------
    # Devices / streams (UPSERT)
    # ------------------------------------------------------------------

    def upsert_device(
        self,
        name: str,
        host: str,
        port: int,
        config_dict: dict[str, Any],
    ) -> int:
        """Insert or update a device row. Returns the device's id."""
        config_json = json.dumps(config_dict, sort_keys=True, default=str)
        now = _now_iso()

        def _upsert(cur: sqlite3.Cursor) -> Any:
            cur.execute(
                "INSERT INTO devices(name, host, port, config_json,"
                "                    first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET"
                "   host=excluded.host,"
                "   port=excluded.port,"
                "   config_json=excluded.config_json,"
                "   last_seen_at=excluded.last_seen_at",
                (name, host, port, config_json, now, now),
            )
            cur.execute("SELECT id FROM devices WHERE name=?", (name,))
            return cur.fetchone()

        row = self.transactional(_upsert)
        self.flush_now()
        if row is None:  # pragma: no cover - upsert above guarantees existence
            raise RuntimeError(f"upsert_device: row missing after insert: {name}")
        return int(row["id"])

    def upsert_stream(
        self,
        device_id: int,
        nslc: tuple[str, str, str, str],
        sample_rate: float,
    ) -> int:
        net, sta, loc, cha = nslc

        def _upsert(cur: sqlite3.Cursor) -> Any:
            cur.execute(
                "INSERT INTO streams(device_id, network, station, location,"
                "                    channel, sample_rate)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(device_id, network, station, location, channel)"
                "   DO UPDATE SET sample_rate=excluded.sample_rate",
                (device_id, net, sta, loc, cha, sample_rate),
            )
            cur.execute(
                "SELECT id FROM streams WHERE device_id=? AND network=?"
                "   AND station=? AND location=? AND channel=?",
                (device_id, net, sta, loc, cha),
            )
            return cur.fetchone()

        row = self.transactional(_upsert)
        self.flush_now()
        if row is None:  # pragma: no cover
            raise RuntimeError(f"upsert_stream: row missing after insert: {device_id}/{nslc}")
        return int(row["id"])

    # ------------------------------------------------------------------
    # Per-packet writes (batched)
    # ------------------------------------------------------------------

    def record_packet(self, stream_id: int, t_end: UTCDateTime, bytes_added: int) -> None:
        """Advance ``streams.last_packet_at`` and bump byte/packet counters."""
        t_end_iso = str(t_end)

        def _update(cur: sqlite3.Cursor) -> None:
            cur.execute(
                "UPDATE streams SET"
                "   first_packet_at=COALESCE(first_packet_at, ?),"
                "   last_packet_at=?,"
                "   total_packets=total_packets + 1,"
                "   total_bytes=total_bytes + ?"
                " WHERE id=?",
                (t_end_iso, t_end_iso, bytes_added, stream_id),
            )

        self.transactional(_update)
        self.commit_if_due()

    def record_file(
        self,
        stream_id: int,
        path: Path,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        n_bytes: int,
    ) -> None:
        """Insert or update one row in ``files``.

        ``files.path`` is UNIQUE. Re-touching an existing path UPDATEs
        the row in place: ``t_end`` and ``bytes`` advance; ``t_start``
        is preserved (the original first-write time).
        """
        path_str = str(path)
        now = _now_iso()

        def _upsert(cur: sqlite3.Cursor) -> None:
            cur.execute(
                "INSERT INTO files(stream_id, path, t_start, t_end, bytes,"
                "                  last_modified_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET"
                "   t_end=excluded.t_end,"
                "   bytes=excluded.bytes,"
                "   last_modified_at=excluded.last_modified_at",
                (stream_id, path_str, str(t_start), str(t_end), n_bytes, now),
            )

        self.transactional(_upsert)
        self.commit_if_due()

    # ------------------------------------------------------------------
    # Re-indexer writes (M3-D — storage/reindex.py is the only caller)
    # ------------------------------------------------------------------

    def replace_file(
        self,
        stream_id: int,
        path: Path,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        n_bytes: int,
    ) -> None:
        """Upsert one ``files`` row from DISK truth — ``t_start`` included.

        Unlike :meth:`record_file` (live writer: a re-touched file keeps
        its original first-write ``t_start``), the re-indexer's row must
        mirror what is actually in the file right now (rules 8/9) — a
        stale row's preserved ``t_start`` would be exactly the lie the
        re-index exists to correct.
        """

        def _upsert(cur: sqlite3.Cursor) -> None:
            cur.execute(
                "INSERT INTO files(stream_id, path, t_start, t_end, bytes,"
                "                  last_modified_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(path) DO UPDATE SET"
                "   stream_id=excluded.stream_id,"
                "   t_start=excluded.t_start,"
                "   t_end=excluded.t_end,"
                "   bytes=excluded.bytes,"
                "   last_modified_at=excluded.last_modified_at",
                (stream_id, str(path), str(t_start), str(t_end), n_bytes, _now_iso()),
            )

        self.transactional(_upsert)
        self.commit_if_due()

    def all_file_rows(self) -> list[tuple[int, str]]:
        """Every ``files`` row as ``(id, path)`` — the prune pass input."""

        def _select(cur: sqlite3.Cursor) -> list[tuple[int, str]]:
            rows = cur.execute("SELECT id, path FROM files ORDER BY id").fetchall()
            return [(int(r["id"]), str(r["path"])) for r in rows]

        return self.transactional(_select)

    def delete_files(self, ids: Sequence[int]) -> int:
        """Delete ``files`` rows by id; returns the number deleted."""
        if not ids:
            return 0

        def _delete(cur: sqlite3.Cursor) -> int:
            total = 0
            for file_id in ids:
                cur.execute("DELETE FROM files WHERE id=?", (file_id,))
                total += cur.rowcount
            return total

        deleted = self.transactional(_delete)
        self.flush_now()
        return int(deleted)

    def refresh_stream_byte_totals(self) -> None:
        """Reset every stream's ``total_bytes`` to the SUM of its file rows.

        Rule 9 at the call site: after a re-index the file rows mirror
        the on-disk bytes, so the per-stream counter is recomputed from
        them rather than from whatever a foreign/stale DB accumulated.
        ``total_packets`` is left alone — packet history is a live-write
        record the tree cannot reconstruct.
        """

        def _update(cur: sqlite3.Cursor) -> None:
            cur.execute(
                "UPDATE streams SET total_bytes ="
                " (SELECT COALESCE(SUM(bytes), 0) FROM files"
                "  WHERE files.stream_id = streams.id)"
            )

        self.transactional(_update)
        self.flush_now()

    def list_device_names(self) -> list[tuple[int, str]]:
        """Every device row as ``(id, name)`` (raw, pre-sanitisation names)."""

        def _select(cur: sqlite3.Cursor) -> list[tuple[int, str]]:
            rows = cur.execute("SELECT id, name FROM devices ORDER BY id").fetchall()
            return [(int(r["id"]), str(r["name"])) for r in rows]

        return self.transactional(_select)

    def upsert_reindexed_session(
        self,
        project_name: str,
        started_at: str,
        ended_at: str,
        host: str,
        version: str,
        devices: Sequence[str],
    ) -> int:
        """Insert-or-update THE synthesized session row (``reindexed=1``).

        At most one synthesized row per DB: a re-run updates it in place
        (span/membership track the tree) instead of stacking duplicates.
        The caller decides WHETHER to synthesize (only when the DB holds
        no real session rows — those are the durable session record and
        must never be shadowed).
        """

        def _upsert(cur: sqlite3.Cursor) -> int | None:
            row = cur.execute(
                "SELECT id FROM sessions WHERE reindexed=1 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO sessions(started_at, ended_at, host, version,"
                    "                     config_hash, project_name, reindexed)"
                    " VALUES (?, ?, ?, ?, '', ?, 1)",
                    (started_at, ended_at, host, version, project_name),
                )
                session_id = cur.lastrowid
            else:
                session_id = int(row["id"])
                cur.execute(
                    "UPDATE sessions SET started_at=?, ended_at=?, host=?,"
                    " version=?, project_name=? WHERE id=?",
                    (started_at, ended_at, host, version, project_name, session_id),
                )
                cur.execute(
                    "DELETE FROM session_devices WHERE session_id=?", (session_id,)
                )
            for device_name in devices:
                cur.execute(
                    "INSERT OR IGNORE INTO session_devices(session_id, device_name)"
                    " VALUES (?, ?)",
                    (session_id, device_name),
                )
            return session_id

        session_id = self.transactional(_upsert)
        self.flush_now()
        if session_id is None:  # pragma: no cover - defensive
            raise RuntimeError("upsert_reindexed_session: lastrowid was None")
        return int(session_id)

    def record_gap(
        self,
        stream_id: int,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        samples_missing: int,
        kind: str,
    ) -> None:
        def _insert(cur: sqlite3.Cursor) -> None:
            cur.execute(
                "INSERT INTO gaps(stream_id, t_start, t_end, samples_missing,"
                "                 kind, detected_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    stream_id,
                    str(t_start),
                    str(t_end),
                    samples_missing,
                    kind,
                    _now_iso(),
                ),
            )

        self.transactional(_insert)
        self.commit_if_due()

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def last_packet_time(self, stream_id: int) -> UTCDateTime | None:
        cur = self._conn().execute("SELECT last_packet_at FROM streams WHERE id=?", (stream_id,))
        row = cur.fetchone()
        if row is None or row["last_packet_at"] is None:
            return None
        return UTCDateTime(row["last_packet_at"])

    # ------------------------------------------------------------------
    # Detections (M8)
    # ------------------------------------------------------------------

    def record_detection(self, stream_id: int, detection: Detection) -> int:
        """Insert one ``detections`` row; return its new id.

        Commits immediately (``flush_now``) rather than batching: a
        detection is rare relative to per-packet metadata, and the
        engine announces it (``detectionRecorded``) only after this
        returns, so the row must be durable before the signal fires
        (CLAUDE.md rule 8 — persisted before announced).

        ``stream_id`` is resolved by the caller via ``upsert_stream``;
        the FK guarantees the parent stream exists.
        """
        meta_json = json.dumps(detection.meta, sort_keys=True, default=str)
        t_off = str(detection.t_off) if detection.t_off is not None else None

        def _insert(cur: sqlite3.Cursor) -> int | None:
            cur.execute(
                "INSERT INTO detections(stream_id, kind, t_on, t_off, score,"
                "                       detected_at, meta_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stream_id,
                    detection.kind,
                    str(detection.t_on),
                    t_off,
                    float(detection.score),
                    str(detection.detected_at),
                    meta_json,
                ),
            )
            return cur.lastrowid

        det_id = self.transactional(_insert)
        self.flush_now()
        if det_id is None:  # pragma: no cover - insert above guarantees a rowid
            raise RuntimeError("record_detection: lastrowid was None")
        return int(det_id)

    def update_detection_offtime(
        self,
        detection_id: int,
        t_off: UTCDateTime,
        score: float | None = None,
    ) -> None:
        """Close a previously-open detection by setting its ``t_off``.

        Called when a trigger that first surfaced as ``t_off=None``
        later drops below ``off_thr``. ``score`` (optional) overwrites
        the row's score with the trigger's final peak ratio — the open
        row only had the peak-so-far at onset time. Commits immediately
        so the close is durable before it is announced.
        """

        def _update(cur: sqlite3.Cursor) -> None:
            if score is None:
                cur.execute(
                    "UPDATE detections SET t_off=? WHERE id=?",
                    (str(t_off), detection_id),
                )
            else:
                cur.execute(
                    "UPDATE detections SET t_off=?, score=? WHERE id=?",
                    (str(t_off), float(score), detection_id),
                )

        self.transactional(_update)
        self.flush_now()

    def recent_detections(self, limit: int, since: UTCDateTime | None = None) -> list[Detection]:
        """Return the most-recent detections, newest first.

        Joined with ``streams`` + ``devices`` so each :class:`Detection`
        carries its device name and full NSLC without a second query.
        ``since`` (optional) filters to ``t_on >= since``. ``limit``
        bounds the result — the read used to pre-populate the table on
        startup is intentionally bounded and index-backed
        (``idx_detections_t_on``); it never touches waveform data.
        """
        params: tuple[object, ...]
        where = ""
        if since is not None:
            where = " WHERE d.t_on >= ?"
            params = (str(since), int(limit))
        else:
            params = (int(limit),)
        sql = (
            "SELECT d.id AS id, d.kind AS kind, d.t_on AS t_on, d.t_off AS t_off,"
            "       d.score AS score, d.detected_at AS detected_at, d.meta_json AS meta_json,"
            "       dev.name AS device,"
            "       s.network AS network, s.station AS station,"
            "       s.location AS location, s.channel AS channel"
            " FROM detections d"
            " JOIN streams s ON d.stream_id = s.id"
            " JOIN devices dev ON s.device_id = dev.id"
            f"{where}"
            " ORDER BY d.t_on DESC"
            " LIMIT ?"
        )
        rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_detection(row) for row in rows]

    def count_detections(self, since: UTCDateTime | None = None) -> int:
        """Count detections via ``COUNT(*)`` (CLAUDE.md rule 9), not an
        in-memory accumulator. ``since`` (optional) restricts to
        ``t_on >= since``."""
        if since is not None:
            cur = self._conn().execute(
                "SELECT COUNT(*) AS n FROM detections WHERE t_on >= ?",
                (str(since),),
            )
        else:
            cur = self._conn().execute("SELECT COUNT(*) AS n FROM detections")
        return int(cur.fetchone()["n"])

    def find_stream_id(self, device_name: str, nslc: str) -> int | None:
        """Resolve ``streams.id`` for ``(device_name, nslc)`` — read-only.

        Used by the archive reader to consult the ``files`` index without
        going through the engine's write-side caches. Returns ``None`` if
        the device/stream was never recorded or the NSLC is malformed.
        """
        parts = nslc.split(".")
        if len(parts) != 4:
            return None
        net, sta, loc, cha = parts
        cur = self._conn().execute(
            "SELECT s.id AS id FROM streams s"
            " JOIN devices dev ON s.device_id = dev.id"
            " WHERE dev.name=? AND s.network=? AND s.station=?"
            "   AND s.location=? AND s.channel=?",
            (device_name, net, sta, loc, cha),
        )
        row = cur.fetchone()
        return int(row["id"]) if row is not None else None

    def list_streams(self) -> list[tuple[str, str]]:
        """Every indexed ``(device_name, nslc)`` pair — read-only.

        Browse helper for the M3-A session browser: the per-session
        device/stream tree is derived from these pairs (filtered to the
        session's member devices by the caller). Ordered for stable UI.
        """
        rows = self._conn().execute(
            "SELECT dev.name AS device, s.network AS network, s.station AS station,"
            "       s.location AS location, s.channel AS channel"
            " FROM streams s JOIN devices dev ON s.device_id = dev.id"
            " ORDER BY dev.name, s.network, s.station, s.location, s.channel"
        ).fetchall()
        return [
            (
                str(row["device"]),
                f"{row['network']}.{row['station']}.{row['location']}.{row['channel']}",
            )
            for row in rows
        ]

    def files_in_range(
        self,
        stream_id: int,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
    ) -> list[Path]:
        """Return the archived file paths overlapping ``[t_start, t_end]``.

        Index-backed accelerator for the archive reader (M9 Stage C): a
        file overlaps the window iff ``t_start < window_end`` AND
        ``t_end > window_start``. Ordered by ``t_start``. Read-only — no
        commit. Returns ``[]`` when nothing overlaps. ISO-8601 timestamp
        strings sort lexicographically in the same order as time, so the
        string comparison is correct.
        """
        cur = self._conn().execute(
            "SELECT path FROM files"
            " WHERE stream_id=? AND t_start < ? AND t_end > ?"
            " ORDER BY t_start",
            (stream_id, str(t_end), str(t_start)),
        )
        return [Path(row["path"]) for row in cur.fetchall()]

    def archive_extent(self, device_name: str, nslc: str) -> tuple[UTCDateTime, UTCDateTime] | None:
        """Return the archived ``(earliest, latest)`` span for one stream.

        Read-only browse helper for the Archive tab (rule 8). Resolves
        ``streams.id`` via :meth:`find_stream_id`, then takes
        ``MIN(t_start)`` / ``MAX(t_end)`` over the ``files`` index. ISO-8601
        timestamps sort lexicographically in time order, so ``MIN``/``MAX``
        are correct. Returns ``None`` when the stream is unknown or nothing
        is archived — the caller shows an honest empty state, never a
        placeholder date.
        """
        stream_id = self.find_stream_id(device_name, nslc)
        if stream_id is None:
            return None
        cur = self._conn().execute(
            "SELECT MIN(t_start) AS t_min, MAX(t_end) AS t_max FROM files WHERE stream_id=?",
            (stream_id,),
        )
        row = cur.fetchone()
        if row is None or row["t_min"] is None or row["t_max"] is None:
            return None
        return UTCDateTime(row["t_min"]), UTCDateTime(row["t_max"])

    def archive_coverage(
        self,
        device_name: str,
        nslc: str,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
    ) -> list[tuple[UTCDateTime, UTCDateTime]]:
        """Return contiguous covered intervals within ``[t_start, t_end]``.

        Read-only browse helper for the Archive tab's coverage strip (rule
        8). Files overlapping the window (same predicate as
        :meth:`files_in_range`) are merged into maximal contiguous intervals
        and clipped to the window; the caller derives gaps as the complement.
        Adjacent or overlapping files coalesce into one interval. Returns
        ``[]`` when nothing is archived in the window.
        """
        stream_id = self.find_stream_id(device_name, nslc)
        if stream_id is None:
            return []
        rows = (
            self._conn()
            .execute(
                "SELECT t_start, t_end FROM files"
                " WHERE stream_id=? AND t_start < ? AND t_end > ?"
                " ORDER BY t_start",
                (stream_id, str(t_end), str(t_start)),
            )
            .fetchall()
        )
        merged: list[tuple[UTCDateTime, UTCDateTime]] = []
        for row in rows:
            seg_start = max(UTCDateTime(row["t_start"]), t_start)
            seg_end = min(UTCDateTime(row["t_end"]), t_end)
            if seg_end <= seg_start:
                continue
            if merged and seg_start <= merged[-1][1]:
                # Overlaps / abuts the previous interval — extend it.
                prev_start, prev_end = merged[-1]
                merged[-1] = (prev_start, max(prev_end, seg_end))
            else:
                merged.append((seg_start, seg_end))
        return merged

    @staticmethod
    def _row_to_detection(row: sqlite3.Row) -> Detection:
        nslc = f"{row['network']}.{row['station']}.{row['location']}.{row['channel']}"
        meta_raw = row["meta_json"]
        meta = json.loads(meta_raw) if meta_raw else {}
        return Detection(
            device=row["device"],
            nslc=nslc,
            kind=row["kind"],
            t_on=UTCDateTime(row["t_on"]),
            t_off=UTCDateTime(row["t_off"]) if row["t_off"] is not None else None,
            score=float(row["score"]),
            detected_at=UTCDateTime(row["detected_at"]),
            meta=meta,
            id=int(row["id"]),
        )
