"""Unit tests for the session model (M2-B, rule 14).

Pure half (``core/session.py``): name sanitisation delegates to the
device sanitiser, path grammar. Disk half (``storage/sessions.py``):
the project-name injectivity guard against existing session roots.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from echosmonitor.core.session import (
    SessionInfo,
    sanitize_project_name,
    session_archive_root,
)
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.sds import sanitize_device_name
from echosmonitor.storage.sessions import (
    ProjectNameCollisionError,
    ensure_project_root,
    stored_project_name,
)

# ---------------------------------------------------------------------------
# Pure: sanitisation + path grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["Survey 2026", "proj/A", "..", "   ", "Già_fatto", "plain"],
)
def test_sanitize_project_name_matches_device_rules(raw: str) -> None:
    """Rule 14: project names use the SAME sanitiser as device names."""
    assert sanitize_project_name(raw) == sanitize_device_name(raw)


def test_session_archive_root_is_base_slash_sanitized(tmp_path: Path) -> None:
    assert session_archive_root(tmp_path, "Survey 2026") == tmp_path / "Survey_2026"


def test_session_info_is_frozen() -> None:
    info = SessionInfo(
        session_id=1,
        project_name="p",
        sanitized_name="p",
        started_at="2026-06-11T00:00:00.000000Z",
        devices=(),
        db_root=Path("/tmp/p"),
    )
    with pytest.raises(AttributeError):
        info.project_name = "q"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Guard: injectivity against existing session roots
# ---------------------------------------------------------------------------


def _make_project_db(root: Path, raw_name: str) -> None:
    """Create ``root/archive.db`` recording ``raw_name`` as its project."""
    root.mkdir(parents=True, exist_ok=True)
    dao = ArchiveDao(root / "archive.db")
    sid = dao.start_session("host", "0.0.0", "hash", project_name=raw_name)
    dao.end_session(sid)
    dao.close()


def test_guard_passes_for_fresh_name(tmp_path: Path) -> None:
    assert ensure_project_root(tmp_path, "fresh") == tmp_path / "fresh"


def test_guard_passes_when_resuming_same_project(tmp_path: Path) -> None:
    _make_project_db(tmp_path / "Survey_2026", "Survey 2026")
    root = ensure_project_root(tmp_path, "Survey 2026")
    assert root == tmp_path / "Survey_2026"


def test_guard_rejects_colliding_different_name(tmp_path: Path) -> None:
    """'Survey 2026' and 'Survey_2026' sanitise to one segment; recording
    the second over the first would merge two projects' archives."""
    _make_project_db(tmp_path / "Survey_2026", "Survey 2026")
    with pytest.raises(ProjectNameCollisionError, match="Survey 2026"):
        ensure_project_root(tmp_path, "Survey_2026")


def test_guard_allows_unverifiable_existing_dir(
    tmp_path: Path, capture_structlog
) -> None:
    """An existing dir with no readable recorded name cannot be proven a
    collision — allow, but loudly."""
    (tmp_path / "mystery").mkdir()
    root = ensure_project_root(tmp_path, "mystery")
    assert root == tmp_path / "mystery"
    assert any(
        r.get("event") == "session_project_root_unverified" for r in capture_structlog
    )


def test_stored_project_name_reads_latest_named_row(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    dao = ArchiveDao(root / "archive.db")
    s1 = dao.start_session("h", "v", "c", project_name="p")
    dao.end_session(s1)
    dao.close()
    assert stored_project_name(root) == "p"


def test_stored_project_name_none_without_db(tmp_path: Path) -> None:
    assert stored_project_name(tmp_path / "nope") is None


def test_stored_project_name_none_for_corrupt_db(
    tmp_path: Path, capture_structlog
) -> None:
    root = tmp_path / "bad"
    root.mkdir()
    (root / "archive.db").write_bytes(b"not a sqlite file at all")
    assert stored_project_name(root) is None
    assert any(
        r.get("event") == "session_project_name_unreadable" for r in capture_structlog
    )
