"""Off-GUI-thread loader for the Archive tab's session browser (M3-A).

Session discovery scans the base archive root for project DBs and the
per-session detail pass opens one session's ``archive.db`` to build the
device/station tree with coverage — both are file + sqlite I/O and must
never run on the GUI thread (rule 1). This loader mirrors the
proven-correct :class:`~echosmonitor.core.archive_window_loader.
ArchiveWindowLoader` threading skeleton exactly (skill:
``qt-worker-threading`` §1-§2):

* a parentless ``_ArchiveBrowserWorker`` ``moveToThread``-ed onto a
  dedicated ``QThread``,
* cross-thread signals connected with ``QueuedConnection`` both ways,
* the worker never raises across the boundary (→ ``failed`` signal),
* cooperative ``_stop`` + latest-wins tokens (rule 7) — one token
  stream PER REQUEST KIND, because a detail request must not silently
  supersede an independent list refresh still queued behind it,
* type-erased frozen dataclasses through ``Signal(object)`` with
  ``isinstance`` guards.

Every DB the worker touches is opened **read-only** and closed before
the slot returns (``ArchiveDao.close`` only closes the calling thread's
connection — a per-request open/close on the worker thread is the leak-
proof discipline the M2-B DAO-lifetime note mandates). Browsing never
migrates or rewrites a DB (rule 8).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from echosmonitor.core.models import SessionEntry, three_component_groups_from_pairs
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.sessions import discover_sessions

if TYPE_CHECKING:
    from echosmonitor.core.models import SessionRecord

_log = structlog.get_logger(__name__)

# Bounded join wait on shutdown (rule 7); same reasoning as the archive
# window loader — a discovery pass is N bounded sqlite opens, a detail
# pass a handful of indexed queries.
_THREAD_JOIN_MS = 4000


@dataclass(frozen=True, slots=True)
class SessionListRequest:
    """GUI -> worker. ``base_root`` snapshotted on the GUI thread."""

    token: int
    base_root: str


@dataclass(frozen=True, slots=True)
class SessionListResult:
    """Worker -> GUI: every browsable session, newest first."""

    token: int
    entries: tuple[SessionEntry, ...]
    elapsed_ms: float


@dataclass(frozen=True, slots=True)
class SessionDetailRequest:
    """GUI -> worker: build one session's device/station tree."""

    token: int
    entry: SessionEntry


@dataclass(frozen=True, slots=True)
class StationCoverage:
    """One 3C station of a session: its group, extent and coverage.

    ``extent`` is the stream's full archived ``(start, end)`` epoch span
    (``None`` when nothing is indexed); ``intervals`` are the covered
    epoch spans of the primary (Z) component clipped to the session
    span — the coverage strip renders them and the complement is gaps.
    """

    device: str
    station: str
    group: dict[str, str]
    extent: tuple[float, float] | None
    intervals: tuple[tuple[float, float], ...]


@dataclass(frozen=True, slots=True)
class SessionDetailResult:
    """Worker -> GUI: the per-session tree the browser renders.

    ``span`` is the session's ``(started, ended-or-now)`` epoch window —
    the coverage strips' reference frame.
    """

    token: int
    entry: SessionEntry
    span: tuple[float, float]
    stations: tuple[StationCoverage, ...]
    elapsed_ms: float


def _session_span(record: SessionRecord) -> tuple[float, float]:
    """The session's ``(started, ended)`` epochs; an open session ends now."""
    started = float(UTCDateTime(record.started_at).timestamp)
    if record.ended_at is not None:
        ended = float(UTCDateTime(record.ended_at).timestamp)
    else:
        ended = float(UTCDateTime().timestamp)
    return started, max(started, ended)


class _ArchiveBrowserWorker(QObject):
    """Lives on the loader's dedicated thread; never raises across it."""

    sessionsListed = Signal(object)  # SessionListResult  # noqa: N815
    detailLoaded = Signal(object)  # SessionDetailResult  # noqa: N815
    listFailed = Signal(int, str)  # token, message  # noqa: N815
    detailFailed = Signal(int, str)  # token, message  # noqa: N815

    def __init__(self) -> None:
        super().__init__()  # parentless — moveToThread requires no parent
        self._stop = False
        # Latest-wins gates, written GIL-atomically from the GUI thread —
        # one per request kind (list refreshes and detail loads are
        # independent; neither supersedes the other).
        self._active_list_token = -1
        self._active_detail_token = -1

    @Slot(object)
    def list_sessions(self, request: object) -> None:
        if not isinstance(request, SessionListRequest):  # defensive (type-erased)
            return
        token = request.token
        if self._stop or token != self._active_list_token:
            return  # superseded before we started
        t0 = time.monotonic()
        try:
            entries = discover_sessions(
                Path(request.base_root),
                # Cooperative cancel between DB opens (rule 7): each open
                # can busy-wait up to sqlite's busy_timeout.
                should_stop=lambda: self._stop or token != self._active_list_token,
            )
        except Exception as exc:  # never crash the worker thread
            _log.error("session_list_failed", token=token, error=str(exc))
            self.listFailed.emit(token, str(exc))
            return
        if self._stop or token != self._active_list_token:
            return  # superseded mid-scan — do not announce
        elapsed = (time.monotonic() - t0) * 1000.0
        self.sessionsListed.emit(
            SessionListResult(token=token, entries=tuple(entries), elapsed_ms=elapsed)
        )

    @Slot(object)
    def load_detail(self, request: object) -> None:
        if not isinstance(request, SessionDetailRequest):  # defensive (type-erased)
            return
        token = request.token
        if self._stop or token != self._active_detail_token:
            return
        entry = request.entry
        t0 = time.monotonic()
        _log.info(
            "session_detail_load_start",
            token=token,
            session_id=entry.record.id,
            project=entry.record.project_name,
            db=entry.db_path,
        )
        try:
            stations, span = self._build_detail(entry, token)
        except Exception as exc:  # never crash the worker thread
            _log.error("session_detail_load_failed", token=token, error=str(exc))
            self.detailFailed.emit(token, str(exc))
            return
        if stations is None or self._stop or token != self._active_detail_token:
            return  # superseded mid-read — do not announce
        elapsed = (time.monotonic() - t0) * 1000.0
        _log.info(
            "session_detail_load_done",
            token=token,
            session_id=entry.record.id,
            n_stations=len(stations),
            elapsed_ms=elapsed,
        )
        self.detailLoaded.emit(
            SessionDetailResult(
                token=token,
                entry=entry,
                span=span,
                stations=tuple(stations),
                elapsed_ms=elapsed,
            )
        )

    def _build_detail(
        self, entry: SessionEntry, token: int
    ) -> tuple[list[StationCoverage] | None, tuple[float, float]]:
        """Read one session's tree from its DB (read-only, closed before
        return). ``None`` stations signal a cooperative cancel."""
        record = entry.record
        span = _session_span(record)
        span_u = (UTCDateTime(span[0]), UTCDateTime(span[1]))
        dao = ArchiveDao(Path(entry.db_path), read_only=True)
        try:
            pairs = dao.list_streams()
            # Member devices when recorded (rule 14); a membership-less
            # row (e.g. the sessionless monitoring index) falls back to
            # every device in the DB — honest superset, coverage decides.
            if record.devices:
                members = set(record.devices)
                pairs = [(d, n) for d, n in pairs if d in members]
            groups = three_component_groups_from_pairs(pairs)
            stations: list[StationCoverage] = []
            for device in sorted(groups):
                for station in sorted(groups[device]):
                    if self._stop or token != self._active_detail_token:
                        return None, span  # cooperative cancel between streams
                    group = groups[device][station]
                    extent_u = dao.archive_extent(device, group["Z"])
                    extent = (
                        (float(extent_u[0].timestamp), float(extent_u[1].timestamp))
                        if extent_u is not None
                        else None
                    )
                    intervals = dao.archive_coverage(
                        device, group["Z"], span_u[0], span_u[1]
                    )
                    stations.append(
                        StationCoverage(
                            device=device,
                            station=station,
                            group=dict(group),
                            extent=extent,
                            intervals=tuple(
                                (float(s.timestamp), float(e.timestamp))
                                for s, e in intervals
                            ),
                        )
                    )
            return stations, span
        finally:
            dao.close()  # this thread's connection — never leaks (M2-B note)

    @Slot()
    def request_stop(self) -> None:
        self._stop = True

    @Slot()
    def clear_stop(self) -> None:
        self._stop = False


class ArchiveBrowserLoader(QObject):
    """Owns the session-browser worker and its dedicated thread.

    Created by the main window, driven by the Archive tab. Re-emits the
    worker's results on the GUI thread. Both request kinds are
    latest-wins within their own stream.
    """

    sessionsListed = Signal(object)  # SessionListResult (GUI thread)  # noqa: N815
    detailLoaded = Signal(object)  # SessionDetailResult (GUI thread)  # noqa: N815
    listFailed = Signal(int, str)  # token, message (GUI thread)  # noqa: N815
    detailFailed = Signal(int, str)  # token, message (GUI thread)  # noqa: N815

    # GUI -> worker (QueuedConnection → runs on ``_thread``).
    _listRequested = Signal(object)  # SessionListRequest  # noqa: N815
    _detailRequested = Signal(object)  # SessionDetailRequest  # noqa: N815
    _stopRequested = Signal()  # noqa: N815
    _clearStopRequested = Signal()  # noqa: N815

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._list_token = 0
        self._detail_token = 0

        self._worker = _ArchiveBrowserWorker()
        self._thread = QThread()
        self._thread.setObjectName("archive-browser-loader")
        self._worker.moveToThread(self._thread)

        self._worker.sessionsListed.connect(
            self._on_sessions_listed, Qt.ConnectionType.QueuedConnection
        )
        self._worker.detailLoaded.connect(
            self._on_detail_loaded, Qt.ConnectionType.QueuedConnection
        )
        self._worker.listFailed.connect(self._on_list_failed, Qt.ConnectionType.QueuedConnection)
        self._worker.detailFailed.connect(
            self._on_detail_failed, Qt.ConnectionType.QueuedConnection
        )
        self._listRequested.connect(self._worker.list_sessions, Qt.ConnectionType.QueuedConnection)
        self._detailRequested.connect(
            self._worker.load_detail, Qt.ConnectionType.QueuedConnection
        )
        self._stopRequested.connect(self._worker.request_stop, Qt.ConnectionType.QueuedConnection)
        self._clearStopRequested.connect(
            self._worker.clear_stop, Qt.ConnectionType.QueuedConnection
        )

    # ------------------------------------------------------------------
    def request_sessions(self, base_root: str) -> int:
        """Dispatch a session-discovery scan off the GUI thread."""
        self._list_token += 1
        token = self._list_token
        self._worker._active_list_token = token
        self._arm()
        self._listRequested.emit(SessionListRequest(token=token, base_root=base_root))
        return token

    def request_detail(self, entry: SessionEntry) -> int:
        """Dispatch one session's tree/coverage build off the GUI thread."""
        self._detail_token += 1
        token = self._detail_token
        self._worker._active_detail_token = token
        self._arm()
        self._detailRequested.emit(SessionDetailRequest(token=token, entry=entry))
        return token

    def _arm(self) -> None:
        """Re-arm the stop flag and lazily start the thread (skill §2)."""
        self._worker._stop = False
        self._clearStopRequested.emit()
        if not self._thread.isRunning():
            self._thread.start()

    def cancel(self) -> None:
        """Drop any in-flight work (results are stale downstream)."""
        self._list_token += 1
        self._detail_token += 1
        self._worker._active_list_token = self._list_token
        self._worker._active_detail_token = self._detail_token
        self._worker._stop = True
        self._stopRequested.emit()

    def shutdown(self) -> None:
        """Tear down for app exit — stop the worker and join the thread."""
        self._worker._stop = True
        self._stopRequested.emit()
        if self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(_THREAD_JOIN_MS):
                _log.warning("archive_browser_thread_join_timeout")

    # ------------------------------------------------------------------
    @Slot(object)
    def _on_sessions_listed(self, payload: object) -> None:
        if isinstance(payload, SessionListResult):
            self.sessionsListed.emit(payload)

    @Slot(object)
    def _on_detail_loaded(self, payload: object) -> None:
        if isinstance(payload, SessionDetailResult):
            self.detailLoaded.emit(payload)

    @Slot(int, str)
    def _on_list_failed(self, token: int, message: str) -> None:
        self.listFailed.emit(token, message)

    @Slot(int, str)
    def _on_detail_failed(self, token: int, message: str) -> None:
        self.detailFailed.emit(token, message)
