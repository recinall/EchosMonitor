"""Off-GUI-thread archive re-indexing for the Archive tab (M3-D).

Rebuilding a session root's ``archive.db`` from its SDS tree is file
walking + per-file obspy headonly reads + DB writes — all of it lives
in :func:`echosmonitor.storage.reindex.reindex_session_root` and runs
on this loader's dedicated thread (rules 1/8).

The threading skeleton is the M3-C export loader's (skill
``qt-worker-threading`` §1-§2) with the same deliberate deviation:
requests are a SERIAL QUEUE, never latest-wins. A re-index is an
explicit user action on a specific directory; a second request must
not cancel the first. The cooperative ``_stop`` flag is shutdown-only
and re-armed exclusively via the queued ``clear_stop`` (the M3-C
auditor finding: without a token supersede, a synchronous clear could
resurrect a request queued behind a shutdown — queue FIFO drains stale
requests against the still-set flag before the clear lands). The flag
is polled per file inside the storage function (rule 7); a stopped
re-index leaves a PARTIAL index, which is safe: the files win over the
index (rule 8) and a re-run converges.

Progress beats from the storage layer are throttled here (one signal
per ``_PROGRESS_MIN_INTERVAL_S``, plus the final beat) so a
many-thousand-file tree cannot flood the GUI queue (rule 5 in spirit:
the seam is bounded by time, excess beats are dropped-oldest by
construction).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import structlog
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from echosmonitor.storage.reindex import (
    ReindexProgress,
    ReindexReport,
    reindex_session_root,
)

_log = structlog.get_logger(__name__)

_THREAD_JOIN_MS = 4000
_PROGRESS_MIN_INTERVAL_S = 0.1


@dataclass(frozen=True, slots=True)
class ArchiveReindexRequest:
    """GUI -> worker (type-erased through the Qt signal)."""

    token: int
    session_root: str
    host: str
    version: str


@dataclass(frozen=True, slots=True)
class ArchiveReindexProgressEvent:
    """Worker -> GUI: one throttled progress beat."""

    token: int
    files_done: int
    files_total: int
    files_skipped: int


@dataclass(frozen=True, slots=True)
class ArchiveReindexResult:
    """Worker -> GUI: one completed re-index."""

    token: int
    report: ReindexReport


class _ArchiveReindexWorker(QObject):
    """Lives on the loader's thread; one queued slot per request."""

    progressed = Signal(object)  # ArchiveReindexProgressEvent
    finished = Signal(object)  # ArchiveReindexResult
    failed = Signal(int, str)  # token, message

    def __init__(self) -> None:
        super().__init__()  # parentless — moveToThread requires no parent
        # Shutdown-only flag (NOT latest-wins — re-indexes never supersede
        # each other); written GIL-atomically from the GUI thread.
        self._stop = False

    @Slot(object)
    def reindex(self, request: object) -> None:
        if not isinstance(request, ArchiveReindexRequest):  # defensive (type-erased)
            return
        if self._stop:
            # Queued behind shutdown: dropped, not half-run (export-worker
            # precedent — the post-shutdown RESTART path guarantees the
            # drop via queue FIFO before the queued clear lands).
            _log.info("archive_reindex_dropped_on_stop", token=request.token)
            return
        token = request.token
        last_beat = 0.0

        def _on_progress(p: ReindexProgress) -> None:
            nonlocal last_beat
            now = time.monotonic()
            if now - last_beat < _PROGRESS_MIN_INTERVAL_S and p.files_done < p.files_total:
                return
            last_beat = now
            self.progressed.emit(
                ArchiveReindexProgressEvent(
                    token=token,
                    files_done=p.files_done,
                    files_total=p.files_total,
                    files_skipped=p.files_skipped,
                )
            )

        try:
            report = reindex_session_root(
                Path(request.session_root),
                host=request.host,
                version=request.version,
                progress=_on_progress,
                should_stop=lambda: self._stop,
            )
        except Exception as exc:  # never crash the worker thread
            _log.error("archive_reindex_failed", token=token, error=str(exc))
            self.failed.emit(token, str(exc))
            return
        if report.cancelled:
            # Shutdown path — partial index is safe (re-run converges);
            # the GUI is going away, announce nothing.
            return
        self.finished.emit(ArchiveReindexResult(token=token, report=report))

    @Slot()
    def request_stop(self) -> None:
        self._stop = True

    @Slot()
    def clear_stop(self) -> None:
        self._stop = False


class ArchiveReindexLoader(QObject):
    """Owns the re-index worker and its dedicated thread.

    Created by the main window (shutdown joins the thread with the other
    archive loaders). Serial queue — see the module docstring. The
    ACTIVE-session guard does NOT live here: the main window refuses a
    request targeting ``engine.archive_db_path()`` before it ever
    reaches this loader (the loader cannot see the engine — rule 4
    keeps that knowledge in the GUI layer that owns both).
    """

    progressed = Signal(object)  # ArchiveReindexProgressEvent (GUI thread)
    finished = Signal(object)  # ArchiveReindexResult (GUI thread)
    failed = Signal(int, str)  # token, message (GUI thread)

    # GUI -> worker (QueuedConnection → runs on ``_thread``).
    _reindexRequested = Signal(object)  # ArchiveReindexRequest  # noqa: N815
    _stopRequested = Signal()  # noqa: N815
    _clearStopRequested = Signal()  # noqa: N815

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._token = 0

        self._worker = _ArchiveReindexWorker()
        self._thread = QThread()
        self._thread.setObjectName("archive-reindex-worker")
        self._worker.moveToThread(self._thread)

        self._worker.progressed.connect(self._on_progressed, Qt.ConnectionType.QueuedConnection)
        self._worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        self._worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        self._reindexRequested.connect(self._worker.reindex, Qt.ConnectionType.QueuedConnection)
        self._stopRequested.connect(self._worker.request_stop, Qt.ConnectionType.QueuedConnection)
        self._clearStopRequested.connect(
            self._worker.clear_stop, Qt.ConnectionType.QueuedConnection
        )

    # ------------------------------------------------------------------
    def request(self, session_root: str, host: str, version: str) -> int:
        """Queue one re-index; returns its routing token.

        No supersede: an in-flight re-index keeps running; this one
        waits its turn. The thread starts lazily. The stop flag is
        re-armed ONLY via the queued ``clear_stop`` (see module
        docstring).
        """
        self._token += 1
        token = self._token
        self._clearStopRequested.emit()
        if not self._thread.isRunning():
            self._thread.start()
        self._reindexRequested.emit(
            ArchiveReindexRequest(
                token=token,
                session_root=session_root,
                host=host,
                version=version,
            )
        )
        return token

    def shutdown(self) -> None:
        """Tear down for app exit — stop the worker and join the thread.

        A re-index in flight aborts cooperatively at the next per-file
        poll, leaving a partial (safe, re-runnable) index; requests
        still queued are dropped with a log line.
        """
        self._worker._stop = True
        self._stopRequested.emit()
        if self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(_THREAD_JOIN_MS):
                _log.warning("archive_reindex_thread_join_timeout")

    # ------------------------------------------------------------------
    @Slot(object)
    def _on_progressed(self, payload: object) -> None:
        if isinstance(payload, ArchiveReindexProgressEvent):
            self.progressed.emit(payload)

    @Slot(object)
    def _on_finished(self, payload: object) -> None:
        if isinstance(payload, ArchiveReindexResult):
            self.finished.emit(payload)

    @Slot(int, str)
    def _on_failed(self, token: int, message: str) -> None:
        self.failed.emit(token, message)
