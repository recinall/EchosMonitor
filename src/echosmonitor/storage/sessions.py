"""Project-name injectivity guard for session-rooted archives (rule 14).

``sanitize_project_name`` is not injective: distinct raw names can
collapse to one directory segment ("proj A" / "proj_A" → "proj_A").
Two *different* projects sharing one physical session root would merge
their SDS trees and their ``archive.db`` — exactly the collision the
session layout exists to prevent. The config schema enforces the same
property for device names at load time; for projects the collision
domain is "whatever already exists on disk", so the check runs at
session-creation time against the target directory.

The raw name a session root belongs to is recorded in that root's own
``archive.db`` (``sessions.project_name``, schema v4) — the DB is the
only durable place the *raw* (pre-sanitisation) name survives.

Read-only module: it never creates directories or rows (the engine's
DAO does that after the guard passes).
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import structlog

from echosmonitor.core.session import sanitize_project_name, session_archive_root

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from echosmonitor.core.models import SessionEntry

_log = structlog.get_logger(__name__)

_DB_FILENAME = "archive.db"


class ProjectNameCollisionError(ValueError):
    """A new project name sanitises onto an existing, different project."""


def stored_project_name(session_root: Path) -> str | None:
    """The raw project name recorded in ``session_root/archive.db``.

    Returns ``None`` when the DB does not exist, cannot be read, or has
    no session row carrying a project name — all of which mean "nothing
    durable contradicts the caller's name". Read-only; opens its own
    short-lived connection rather than going through ``connect()`` so a
    foreign/corrupt file is reported as ``None`` instead of being
    migrated or rewritten as a side effect of a *check*.
    """
    db_path = session_root / _DB_FILENAME
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT project_name FROM sessions"
                " WHERE project_name IS NOT NULL"
                " ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _log.warning(
            "session_project_name_unreadable",
            db=str(db_path),
            error=str(exc),
        )
        return None
    return str(row[0]) if row is not None else None


def sweep_dirty_sessions(base_root: Path) -> dict[str, int]:
    """Close-as-dirty every unclosed session under ``base_root``.

    Launch-time crash recovery (ROADMAP M2-C): visits the base
    monitoring index (``base_root/archive.db``) and every immediate
    project dir's ``archive.db``, runs the DAO's ``close_dirty_sessions``
    on each, and returns ``{db_path: swept_count}`` for the DBs where
    anything was swept. Opening through the DAO also brings each DB to
    the current schema, so old project DBs migrate at launch rather
    than on first recording.

    Per-DB errors are logged and skipped — a corrupt or foreign file
    must not block launch (the engine's own per-DB sweep on open is the
    second line of defence).
    """
    import time

    from echosmonitor.storage.dao import ArchiveDao

    try:
        if not base_root.is_dir():
            return {}
        candidates = [base_root / _DB_FILENAME]
        candidates.extend(
            sorted(
                child / _DB_FILENAME
                for child in base_root.iterdir()
                if child.is_dir()
            )
        )
    except OSError as exc:
        # An unreadable archive root (permissions, stale network mount)
        # must not block launch — the engine's per-DB sweep on open is
        # the second line of defence.
        _log.warning(
            "session_sweep_root_unreadable",
            root=str(base_root),
            error=str(exc),
        )
        return {}
    swept: dict[str, int] = {}
    for db_path in candidates:
        try:
            if not db_path.is_file():
                continue
            t0 = time.monotonic()
            dao = ArchiveDao(db_path)
            try:
                count = dao.close_dirty_sessions()
            finally:
                dao.close()
        except (sqlite3.Error, OSError) as exc:
            _log.warning(
                "session_sweep_db_failed",
                db=str(db_path),
                error=str(exc),
            )
            continue
        # Per-DB observability (rule 7): each open runs migration + an
        # UPDATE + fsync, and sqlite's busy_timeout means a locked DB
        # can cost seconds — make the per-DB cost attributable.
        _log.debug(
            "session_sweep_db_done",
            db=str(db_path),
            swept=count,
            elapsed_s=round(time.monotonic() - t0, 3),
        )
        if count:
            swept[str(db_path)] = count
    return swept


def discover_sessions(
    base_root: Path,
    limit_per_db: int = 200,
    should_stop: Callable[[], bool] | None = None,
) -> list[SessionEntry]:
    """Every browsable session under ``base_root``, most-recent-first.

    The M3-A session browser's discovery pass (ROADMAP NOTE on M2-B):
    between sessions the engine exposes only the bare base root, so data
    recorded in CLOSED sessions — living under ``<base>/<project>/`` —
    is reachable only by opening the project dirs' ``archive.db``s
    explicitly. This visits the base monitoring index plus every
    immediate project dir's DB (the same candidate set as
    :func:`sweep_dirty_sessions`), reads their session rows
    **read-only** (a browse must never migrate or rewrite a DB as a
    side effect — rule 8), and tags each row with the session root +
    DB path a reader needs.

    Per-DB errors are logged and skipped: one corrupt or foreign file
    must not blank the whole browser. Rows are merged across DBs and
    sorted by ``started_at`` (ISO-8601 — lexicographic == chronological)
    newest first.
    """
    import time

    from echosmonitor.core.models import SessionEntry
    from echosmonitor.storage.dao import ArchiveDao

    t0 = time.monotonic()
    try:
        if not base_root.is_dir():
            return []
        roots = [base_root]
        roots.extend(
            sorted(child for child in base_root.iterdir() if child.is_dir())
        )
    except OSError as exc:
        _log.warning(
            "session_discovery_root_unreadable",
            root=str(base_root),
            error=str(exc),
        )
        return []
    entries: list[SessionEntry] = []
    scanned = 0
    for root in roots:
        # Cooperative cancel between DBs (rule 7): each open can busy-wait
        # up to sqlite's busy_timeout, so a many-project scan must remain
        # interruptible inside the loop, not only at its edges. The
        # callable keeps this module Qt-free (rule 2) — the browser
        # worker passes its stop/token check.
        if should_stop is not None and should_stop():
            _log.info("session_discovery_cancelled", root=str(base_root), scanned=scanned)
            return entries
        db_path = root / _DB_FILENAME
        try:
            if not db_path.is_file():
                continue
            dao = ArchiveDao(db_path, read_only=True)
            try:
                records = dao.list_sessions(limit=limit_per_db)
            finally:
                dao.close()
        except (sqlite3.Error, OSError) as exc:
            _log.warning(
                "session_discovery_db_failed",
                db=str(db_path),
                error=str(exc),
            )
            continue
        scanned += 1
        entries.extend(
            SessionEntry(
                record=record,
                session_root=str(root),
                db_path=str(db_path),
            )
            for record in records
        )
    entries.sort(key=lambda e: (e.record.started_at, e.record.id), reverse=True)
    _log.info(
        "session_discovery_done",
        root=str(base_root),
        dbs_scanned=scanned,
        sessions=len(entries),
        elapsed_s=round(time.monotonic() - t0, 3),
    )
    return entries


def ensure_project_root(base_root: Path, project_name: str) -> Path:
    """Validate ``project_name`` against disk; return its session root.

    Raises:
        ProjectNameCollisionError: the sanitized segment already belongs
            to a project whose recorded raw name differs — recording
            would merge two projects' archives (rule 14).

    An existing root with the SAME raw name is fine (a new session in
    an existing project); an existing root with no readable recorded
    name is allowed but logged, since we cannot prove the collision
    either way and the files win over the index (rule 8).
    """
    root = session_archive_root(base_root, project_name)
    if not root.exists():
        return root
    recorded = stored_project_name(root)
    if recorded is None:
        _log.warning(
            "session_project_root_unverified",
            project=project_name,
            root=str(root),
            note="existing directory has no recorded project name; resuming into it",
        )
        return root
    if recorded != project_name:
        raise ProjectNameCollisionError(
            f"project {project_name!r} and existing project {recorded!r} map to "
            f"the same archive directory {sanitize_project_name(project_name)!r}; "
            f"rename one so their session archives stay separate"
        )
    return root
