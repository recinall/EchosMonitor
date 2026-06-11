"""Session discovery for the Archive tab's browser (M3-A, rule 14).

Between sessions the engine exposes only the bare base root, so closed
sessions' data — under ``<base>/<project>/`` — is reachable only by
scanning the project dirs' ``archive.db``s plus the base monitoring
index. Discovery must be strictly READ-ONLY (a browse never migrates or
rewrites a DB — rule 8) and must survive a corrupt DB without blanking
the whole list.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from echosmonitor.core.session import session_archive_root
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.db import connect
from echosmonitor.storage.sessions import discover_sessions


def _set_session_times(db_path: Path, session_id: int, started: str, ended: str | None) -> None:
    """Pin a session row's span (the DAO stamps real 'now' on insert)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
        (started, ended, session_id),
    )
    conn.commit()
    conn.close()


def _seed_project(
    base: Path,
    project: str,
    *,
    started: str,
    ended: str | None,
    dirty: bool = False,
    devices: tuple[str, ...] = ("dev",),
) -> Path:
    root = session_archive_root(base, project)
    dao = ArchiveDao(root / "archive.db")
    sid = dao.start_session("host", "v", "hash", project_name=project, devices=devices)
    if ended is not None:
        dao.end_session(sid, dirty=dirty)
    dao.close()
    _set_session_times(root / "archive.db", sid, started, ended)
    return root


# ---------------------------------------------------------------------------
# Read-only connect semantics
# ---------------------------------------------------------------------------


def test_read_only_connect_refuses_writes(tmp_path: Path) -> None:
    db = tmp_path / "archive.db"
    connect(db).close()  # create a real schema
    ro = connect(db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO _meta(key, value) VALUES ('x', 'y')")
    finally:
        ro.close()


def test_read_only_connect_missing_file_raises_and_creates_nothing(tmp_path: Path) -> None:
    db = tmp_path / "nope" / "archive.db"
    with pytest.raises(sqlite3.OperationalError):
        connect(db, read_only=True)
    # No mkdir side effect either (the write path creates parents; ro must not).
    assert not db.parent.exists()


def test_read_only_browse_leaves_db_bytes_untouched(tmp_path: Path) -> None:
    """A browse never migrates/rewrites the file (rule 8)."""
    base = tmp_path / "archive"
    _seed_project(base, "proj", started="2026-06-01T10:00:00.000000Z", ended=None)
    db = session_archive_root(base, "proj") / "archive.db"
    before = hashlib.sha1(db.read_bytes()).hexdigest()

    entries = discover_sessions(base)

    assert len(entries) == 1
    assert hashlib.sha1(db.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discovers_across_project_dbs_and_base_index_newest_first(tmp_path: Path) -> None:
    base = tmp_path / "archive"
    # Base monitoring index (sessionless detections, project_name NULL).
    base_dao = ArchiveDao(base / "archive.db")
    base_sid = base_dao.start_session("host", "v", "hash")
    base_dao.end_session(base_sid)
    base_dao.close()
    _set_session_times(
        base / "archive.db", base_sid, "2026-06-02T09:00:00.000000Z", "2026-06-02T10:00:00.000000Z"
    )
    alpha = _seed_project(
        base,
        "Alpha Site",
        started="2026-06-03T08:00:00.000000Z",
        ended="2026-06-03T09:00:00.000000Z",
    )
    beta = _seed_project(
        base,
        "Beta",
        started="2026-06-01T08:00:00.000000Z",
        ended=None,  # still open
    )

    entries = discover_sessions(base)

    assert [e.record.project_name for e in entries] == ["Alpha Site", None, "Beta"]
    by_name = {e.record.project_name: e for e in entries}
    assert by_name["Alpha Site"].session_root == str(alpha)
    assert by_name["Alpha Site"].db_path == str(alpha / "archive.db")
    assert by_name[None].session_root == str(base)
    assert by_name["Beta"].record.ended_at is None
    assert by_name["Beta"].db_path == str(beta / "archive.db")


def test_dirty_flag_and_devices_survive_discovery(tmp_path: Path) -> None:
    base = tmp_path / "archive"
    _seed_project(
        base,
        "crashy",
        started="2026-06-01T08:00:00.000000Z",
        ended="2026-06-02T08:00:00.000000Z",
        dirty=True,
        devices=("echos-1", "echos-2"),
    )
    (entry,) = discover_sessions(base)
    assert entry.record.closed_dirty is True
    assert entry.record.devices == ("echos-1", "echos-2")


def test_corrupt_project_db_is_skipped_not_fatal(tmp_path: Path) -> None:
    base = tmp_path / "archive"
    _seed_project(base, "good", started="2026-06-01T08:00:00.000000Z", ended=None)
    bad = base / "bad_project"
    bad.mkdir(parents=True)
    (bad / "archive.db").write_bytes(b"this is not sqlite at all")

    entries = discover_sessions(base)

    assert [e.record.project_name for e in entries] == ["good"]


def test_missing_base_root_returns_empty(tmp_path: Path) -> None:
    assert discover_sessions(tmp_path / "never_created") == []
