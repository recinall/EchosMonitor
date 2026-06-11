"""Archive browser loader — session discovery + per-session detail off-thread.

The M3-A worker behind the Archive tab's session browser. Properties that
matter (skill ``qt-worker-threading`` §7: every new worker pins a
start→stop→start cycle and a stop-during-busy case):

* discovery + detail results arrive via queued signals with the right
  shapes (3C grouping, coverage clipped to the session span, extent);
* a missing/corrupt session DB becomes ``detailFailed``, never a crashed
  thread;
* requests are latest-wins within their own token stream;
* ``shutdown()`` during a busy scan returns within the join bound and the
  loader still serves requests after a restart.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from obspy import UTCDateTime
from PySide6.QtCore import QObject, Slot

from echosmonitor.core import archive_browser_loader as abl_mod
from echosmonitor.core.archive_browser_loader import (
    ArchiveBrowserLoader,
    SessionDetailResult,
    SessionListResult,
)
from echosmonitor.core.models import SessionEntry, SessionRecord
from echosmonitor.core.session import session_archive_root
from echosmonitor.storage.dao import ArchiveDao

_T0 = UTCDateTime("2026-05-10T12:00:00")
_FS = 100.0
_DEVICE = "echos-1"
_STA = "XX.STA.00.HH"


def _iso(t: UTCDateTime) -> str:
    return str(t)


def _seed_project(
    base: Path,
    project: str,
    *,
    started: UTCDateTime,
    ended: UTCDateTime | None,
    with_streams: bool = True,
) -> Path:
    """A closed session with two coverage segments and a gap between them."""
    root = session_archive_root(base, project)
    dao = ArchiveDao(root / "archive.db")
    sid = dao.start_session("host", "v", "hash", project_name=project, devices=(_DEVICE,))
    if with_streams:
        dev_id = dao.upsert_device(_DEVICE, "h", 18000, {})
        for comp in ("Z", "N", "E"):
            stream_id = dao.upsert_stream(dev_id, ("XX", "STA", "00", f"HH{comp}"), _FS)
            dao.record_file(stream_id, root / f"a-{comp}.mseed", _T0, _T0 + 30, 1024)
            dao.record_file(stream_id, root / f"b-{comp}.mseed", _T0 + 60, _T0 + 90, 1024)
    if ended is not None:
        dao.end_session(sid)
    dao.close()
    conn = sqlite3.connect(root / "archive.db")
    conn.execute(
        "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
        (_iso(started), _iso(ended) if ended is not None else None, sid),
    )
    conn.commit()
    conn.close()
    return root


class _Collector(QObject):
    def __init__(self, loader: ArchiveBrowserLoader) -> None:
        super().__init__()
        self.lists: list[SessionListResult] = []
        self.details: list[SessionDetailResult] = []
        self.list_failed: list[tuple[int, str]] = []
        self.detail_failed: list[tuple[int, str]] = []
        loader.sessionsListed.connect(self._on_list)
        loader.detailLoaded.connect(self._on_detail)
        loader.listFailed.connect(lambda tok, msg: self.list_failed.append((tok, msg)))
        loader.detailFailed.connect(lambda tok, msg: self.detail_failed.append((tok, msg)))

    @Slot(object)
    def _on_list(self, payload: object) -> None:
        if isinstance(payload, SessionListResult):
            self.lists.append(payload)

    @Slot(object)
    def _on_detail(self, payload: object) -> None:
        if isinstance(payload, SessionDetailResult):
            self.details.append(payload)


def test_list_then_detail_builds_grouped_coverage(qtbot, tmp_path: Path) -> None:
    base = tmp_path / "archive"
    _seed_project(base, "proj", started=_T0 - 10, ended=_T0 + 120)
    loader = ArchiveBrowserLoader()
    col = _Collector(loader)
    try:
        loader.request_sessions(str(base))
        qtbot.waitUntil(lambda: bool(col.lists) or bool(col.list_failed), timeout=10_000)
        assert not col.list_failed
        (entry,) = col.lists[-1].entries
        assert entry.record.project_name == "proj"

        loader.request_detail(entry)
        qtbot.waitUntil(lambda: bool(col.details) or bool(col.detail_failed), timeout=10_000)
        assert not col.detail_failed
        detail = col.details[-1]
        (station,) = detail.stations
        assert station.device == _DEVICE
        # Same contract the live grouping pins: Z exact; the two horizontals
        # both present (their N/E labelling is the helper's sorted order —
        # orientation-agnostic so Z/1/2 stations work, hvsrpy ns/ew symmetric).
        assert station.group["Z"] == f"{_STA}Z"
        assert set(station.group.values()) == {f"{_STA}Z", f"{_STA}N", f"{_STA}E"}
        # Two covered segments with the gap between them, inside the span.
        assert len(station.intervals) == 2
        span_start, span_end = detail.span
        assert span_start == float((_T0 - 10).timestamp)
        assert span_end == float((_T0 + 120).timestamp)
        for seg_start, seg_end in station.intervals:
            assert span_start <= seg_start < seg_end <= span_end
        assert station.extent is not None
    finally:
        loader.shutdown()


def test_membership_filters_foreign_devices(qtbot, tmp_path: Path) -> None:
    """Streams of devices that are NOT session members stay out of the tree."""
    base = tmp_path / "archive"
    root = _seed_project(base, "proj", started=_T0 - 10, ended=_T0 + 120)
    dao = ArchiveDao(root / "archive.db")
    ghost = dao.upsert_device("ghost", "h", 18000, {})
    for comp in ("Z", "N", "E"):
        dao.upsert_stream(ghost, ("YY", "GHO", "00", f"HH{comp}"), _FS)
    dao.close()

    loader = ArchiveBrowserLoader()
    col = _Collector(loader)
    try:
        loader.request_sessions(str(base))
        qtbot.waitUntil(lambda: bool(col.lists), timeout=10_000)
        loader.request_detail(col.lists[-1].entries[0])
        qtbot.waitUntil(lambda: bool(col.details), timeout=10_000)
        assert {s.device for s in col.details[-1].stations} == {_DEVICE}
    finally:
        loader.shutdown()


def test_missing_db_emits_detail_failed_thread_survives(qtbot, tmp_path: Path) -> None:
    base = tmp_path / "archive"
    _seed_project(base, "proj", started=_T0 - 10, ended=_T0 + 120)
    bogus = SessionEntry(
        record=SessionRecord(
            id=1,
            project_name="gone",
            started_at=_iso(_T0),
            ended_at=_iso(_T0 + 1),
            closed_dirty=False,
            host="h",
            devices=(),
        ),
        session_root=str(tmp_path / "gone"),
        db_path=str(tmp_path / "gone" / "archive.db"),
    )
    loader = ArchiveBrowserLoader()
    col = _Collector(loader)
    try:
        loader.request_detail(bogus)
        qtbot.waitUntil(lambda: bool(col.detail_failed), timeout=10_000)
        # The worker thread survives — a list request still completes.
        loader.request_sessions(str(base))
        qtbot.waitUntil(lambda: bool(col.lists), timeout=10_000)
        assert col.lists[-1].entries
    finally:
        loader.shutdown()


def test_list_requests_are_latest_wins(qtbot, tmp_path: Path, monkeypatch) -> None:
    base = tmp_path / "archive"
    _seed_project(base, "proj", started=_T0 - 10, ended=_T0 + 120)

    real = abl_mod.discover_sessions
    calls = {"n": 0}

    def _slow_first(root: Path, limit_per_db: int = 200, should_stop=None):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(0.5)  # the first scan straddles the second request
        return real(root, limit_per_db, should_stop)

    monkeypatch.setattr(abl_mod, "discover_sessions", _slow_first)

    loader = ArchiveBrowserLoader()
    col = _Collector(loader)
    try:
        tok1 = loader.request_sessions(str(base))
        tok2 = loader.request_sessions(str(base))
        assert tok2 > tok1
        qtbot.waitUntil(lambda: bool(col.lists), timeout=10_000)
        qtbot.wait(200)  # give a stale emit the chance to (wrongly) arrive
        assert all(r.token == tok2 for r in col.lists)
    finally:
        loader.shutdown()


def test_shutdown_during_busy_scan_is_bounded_then_restartable(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    base = tmp_path / "archive"
    _seed_project(base, "proj", started=_T0 - 10, ended=_T0 + 120)

    real = abl_mod.discover_sessions
    calls = {"n": 0}

    def _slow_first(root: Path, limit_per_db: int = 200, should_stop=None):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            time.sleep(1.0)
        return real(root, limit_per_db, should_stop)

    monkeypatch.setattr(abl_mod, "discover_sessions", _slow_first)

    loader = ArchiveBrowserLoader()
    col = _Collector(loader)
    loader.request_sessions(str(base))
    qtbot.waitUntil(lambda: calls["n"] >= 1, timeout=10_000)

    t0 = time.monotonic()
    loader.shutdown()
    assert time.monotonic() - t0 < 4.5  # the join bound, not a hang (rule 7)
    qtbot.wait(100)
    assert not col.lists  # the superseded scan never announced

    # start → stop → start (skill §7): the loader serves again after shutdown.
    try:
        loader.request_sessions(str(base))
        qtbot.waitUntil(lambda: bool(col.lists), timeout=10_000)
        assert col.lists[-1].entries
    finally:
        loader.shutdown()
