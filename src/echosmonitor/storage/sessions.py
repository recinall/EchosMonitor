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
    from pathlib import Path

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
