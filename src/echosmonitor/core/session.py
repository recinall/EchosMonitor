"""Recording-session model (M2-B, CLAUDE.md rule 14).

A recording session is THE archive unit: a user-chosen project name
roots every archive write at ``<archive_root>/<sanitized_project>/``
with the per-device SDS tree below it (skill: ``miniseed-sds``), and
one ``archive.db`` sits at that session root.

This module is the pure half of the session model: name sanitisation,
path grammar, and the frozen :class:`SessionInfo` snapshot the engine
publishes. The disk-touching half (the project-name injectivity guard,
which must read an existing project dir's recorded raw name) lives in
``storage/sessions.py`` — rule 8 keeps file access out of ``core``.

Project names reuse the device-name sanitiser verbatim (rule 14: "the
same injectivity guard"): one filesystem segment, ``[A-Za-z0-9._-]``,
collapsed underscores, stripped ends, sha1 fallback. Like device names,
the mapping is NOT injective — distinct raw names can collapse to one
segment — so injectivity is enforced at session-creation time by
:func:`echosmonitor.storage.sessions.ensure_project_root` (reject
colliding names loudly), exactly as the config schema does for devices.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import platformdirs

from echosmonitor.storage.sds import sanitize_device_name

if TYPE_CHECKING:
    from echosmonitor.config import RootConfig


def resolve_base_archive_root(cfg: RootConfig) -> Path:
    """The app-level BASE archive root: config override else platformdirs.

    One definition shared by the engine (``_resolve_db_root``) and the
    launch-time crash-recovery sweep, so they can never disagree about
    where sessions live. Pure path computation; no I/O.
    """
    if cfg.app.archive_root is not None:
        return Path(cfg.app.archive_root)
    return Path(platformdirs.user_data_dir("echosmonitor", "EchosMonitor")) / "archive"


def sanitize_project_name(name: str) -> str:
    """Sanitise a project name into one SDS path segment.

    Identical rules to :func:`storage.sds.sanitize_device_name`
    (rule 14 mandates the same sanitiser + guard); the alias exists so
    call sites read as what they mean and so a future project-specific
    rule has exactly one place to land.
    """
    return sanitize_device_name(name)


def session_archive_root(base_root: Path, project_name: str) -> Path:
    """``<base_root>/<sanitized_project>`` — the session's archive root.

    The per-device SDS trees (``device_sds_root``) and the session's
    ``archive.db`` live directly below the returned path. Pure path
    arithmetic; no I/O, no existence check.
    """
    return base_root / sanitize_project_name(project_name)


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Immutable snapshot of the active recording session.

    Published by ``StreamingEngine.active_session()``; a new instance
    replaces the old one whenever membership changes (frozen dataclass,
    rule 4 — safe to hand across signal boundaries).

    ``devices`` is the set of devices that have recorded into this
    session so far (membership only grows; a device stopped mid-session
    stays a member — its files are part of the session's archive).
    """

    session_id: int
    project_name: str
    sanitized_name: str
    started_at: str  # ISO-8601 UTC (lexicographic == chronological)
    devices: tuple[str, ...]
    db_root: Path  # session root: holds archive.db + per-device trees
