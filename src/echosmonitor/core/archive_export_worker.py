"""Off-GUI-thread waveform exports for the Archive tab (M3-C).

Exporting an interval re-reads it from the SDS archive (the MiniSEED
files are the source of truth — rule 8; the on-screen arrays are a
render, not an export format) and writes the destination file through
:mod:`echosmonitor.storage.exports`. Both halves are file I/O and run
on this loader's dedicated thread (rule 1; obspy reads on the GUI
thread starve the SeedLink worker — the recorded postmortem).

The threading skeleton mirrors :class:`~echosmonitor.core.
archive_window_loader.ArchiveWindowLoader` (skill ``qt-worker-threading``
§1-§2) with ONE deliberate deviation: exports are NOT latest-wins.
Every request is an explicit "save this file" — a second export must
not cancel the first, so requests queue and run serially on the worker
(queued slots dispatch in order). Tokens only route results back to
the caller. The cooperative ``_stop`` flag exists for shutdown alone:
it is polled between component reads AND inside the CSV row loop
(rule 7), and a stopped export removes its temp file — no partial
artifact (the atomic-write contract in ``storage/exports``).

Each request opens its metadata index READ-ONLY and closes it before
the slot returns (the M2-B per-context DAO lifetime note), exactly
like the other archive loaders.

Queue bound (rule 5): the request seam is the Qt event queue, uncapped
— deliberately, because drop-oldest is wrong for explicit saves. The
real bound is the UI: every request costs the user one application-
modal save dialog, so the queue depth is naturally click-limited.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import structlog
from obspy import Stream
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from echosmonitor.core.archive_detail_loader import _open_readonly_dao, _stream_to_xy
from echosmonitor.core.models import StreamID
from echosmonitor.storage.archive_reader import ArchiveReader
from echosmonitor.storage.exports import (
    ArchiveExportError,
    ExportCancelledError,
    write_window_csv,
    write_window_mseed,
)

_log = structlog.get_logger(__name__)

# Bounded join wait on shutdown (rule 7); reads are the same shape as the
# window loader's, the write is sequential file I/O with stop polls.
_THREAD_JOIN_MS = 4000

_COMPONENT_ORDER = {"Z": 0, "N": 1, "E": 2}


@dataclass(frozen=True, slots=True)
class ArchiveExportRequest:
    """GUI -> worker (type-erased through the Qt signal).

    ``fmt`` is ``"mseed"`` or ``"csv"``. ``archive_root``/``db_path``
    are the caller's per-request snapshot of the session context (the
    same contract as the window loader). ``dest_path`` is the
    user-chosen destination.
    """

    token: int
    fmt: str
    device: str
    group: dict[str, str]
    t_start_epoch: float
    t_end_epoch: float
    archive_root: str
    db_path: str | None
    dest_path: str


@dataclass(frozen=True, slots=True)
class ArchiveExportResult:
    """Worker -> GUI: one completed export."""

    token: int
    fmt: str
    dest_path: str
    n_bytes: int
    elapsed_ms: float


class _ArchiveExportWorker(QObject):
    """Lives on the loader's thread; one queued slot per export request."""

    exported = Signal(object)  # ArchiveExportResult
    failed = Signal(int, str)  # token, message
    empty = Signal(int)  # token

    def __init__(self) -> None:
        super().__init__()  # parentless — moveToThread requires no parent
        # Shutdown-only flag (NOT latest-wins — exports never supersede
        # each other); written GIL-atomically from the GUI thread.
        self._stop = False

    @Slot(object)
    def export(self, request: object) -> None:
        if not isinstance(request, ArchiveExportRequest):  # defensive (type-erased)
            return
        if self._stop:
            # A request queued behind shutdown is dropped, not half-run:
            # writing files while the app tears down is the worse bug.
            # Best-effort at exit (quit() can outrun queued slots); the
            # guaranteed drop is on the post-shutdown RESTART path, where
            # queue FIFO drains stale requests before the new clear_stop.
            _log.info("archive_export_dropped_on_stop", token=request.token)
            return
        token = request.token
        t0 = time.monotonic()
        _log.info(
            "archive_export_start",
            token=token,
            fmt=request.fmt,
            device=request.device,
            t_start=request.t_start_epoch,
            t_end=request.t_end_epoch,
            dest=request.dest_path,
        )
        dao = _open_readonly_dao(request.db_path)
        try:
            reader = ArchiveReader(Path(request.archive_root), dao)
            streams = self._read_components(reader, request)
            if streams is None:
                return  # stopped mid-read — no announcement, no file
            if not streams:
                _log.info("archive_export_empty", token=token)
                self.empty.emit(token)
                return
            if request.fmt == "mseed":
                n_bytes = self._export_mseed(streams, request)
            elif request.fmt == "csv":
                n_bytes = self._export_csv(streams, request)
            else:
                raise ArchiveExportError(f"unknown export format {request.fmt!r}")
        except ExportCancelledError:
            _log.info("archive_export_cancelled", token=token)
            return  # shutdown path — temp already removed, say nothing
        except Exception as exc:  # never crash the worker thread
            _log.error("archive_export_failed", token=token, error=str(exc))
            self.failed.emit(token, str(exc))
            return
        finally:
            if dao is not None:
                dao.close()  # this thread's connection — per-request lifetime
        elapsed = (time.monotonic() - t0) * 1000.0
        _log.info(
            "archive_export_done",
            token=token,
            fmt=request.fmt,
            dest=request.dest_path,
            n_bytes=n_bytes,
            elapsed_ms=elapsed,
        )
        self.exported.emit(
            ArchiveExportResult(
                token=token,
                fmt=request.fmt,
                dest_path=request.dest_path,
                n_bytes=n_bytes,
                elapsed_ms=elapsed,
            )
        )

    def _read_components(
        self, reader: ArchiveReader, request: ArchiveExportRequest
    ) -> dict[str, Stream] | None:
        """Read each component's window; ``None`` = cooperative stop."""
        t_start = UTCDateTime(request.t_start_epoch)
        t_end = UTCDateTime(request.t_end_epoch)
        streams: dict[str, Stream] = {}
        for comp in sorted(request.group, key=lambda c: _COMPONENT_ORDER.get(c, 99)):
            if self._stop:
                return None  # cooperative cancel between component reads
            stream = reader.read_window(
                StreamID.from_trace_id(request.group[comp]),
                t_start,
                t_end,
                device_name=request.device,
            )
            if len(stream):
                streams[comp] = stream
        return streams

    @staticmethod
    def _export_mseed(streams: dict[str, Stream], request: ArchiveExportRequest) -> int:
        merged = Stream()
        for stream in streams.values():
            merged += stream
        return write_window_mseed(merged, Path(request.dest_path))

    def _export_csv(self, streams: dict[str, Stream], request: ArchiveExportRequest) -> int:
        """All components on ONE shared grid; mixed rates cannot share it."""
        epochs = None
        grid_fs: float | None = None
        columns: dict[str, np.ndarray] = {}
        for comp, stream in streams.items():
            xy = _stream_to_xy(stream, request.t_start_epoch, request.t_end_epoch)
            if xy is None:
                continue
            x, y, fs = xy
            if grid_fs is None:
                epochs, grid_fs = x, fs
            elif fs != grid_fs:
                raise ArchiveExportError(
                    f"components have different sample rates "
                    f"({grid_fs:g} vs {fs:g} Hz) — export them separately"
                )
            columns[request.group[comp]] = y
        if epochs is None or not columns:
            raise ArchiveExportError("nothing archived in the selected interval")
        meta = {
            "device": request.device,
            "window_start": str(UTCDateTime(request.t_start_epoch)),
            "window_end": str(UTCDateTime(request.t_end_epoch)),
            "sample_rate_hz": f"{grid_fs:g}",
            "units": "counts",
            "gaps": "empty cells (never interpolated)",
        }
        return write_window_csv(
            Path(request.dest_path),
            epochs,
            columns,
            header_meta=meta,
            should_stop=lambda: self._stop,
        )

    @Slot()
    def request_stop(self) -> None:
        self._stop = True

    @Slot()
    def clear_stop(self) -> None:
        self._stop = False


class ArchiveExportLoader(QObject):
    """Owns the export worker and its dedicated thread.

    Created by the main window (shutdown joins the thread with the
    other archive loaders). UNLIKE the read loaders this is a serial
    queue: requests never supersede each other — see the module
    docstring.
    """

    exported = Signal(object)  # ArchiveExportResult (GUI thread)
    failed = Signal(int, str)  # token, message (GUI thread)
    empty = Signal(int)  # token (GUI thread)

    # GUI -> worker (QueuedConnection → runs on ``_thread``).
    _exportRequested = Signal(object)  # ArchiveExportRequest  # noqa: N815
    _stopRequested = Signal()  # noqa: N815
    _clearStopRequested = Signal()  # noqa: N815

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._token = 0

        self._worker = _ArchiveExportWorker()
        self._thread = QThread()
        self._thread.setObjectName("archive-export-worker")
        self._worker.moveToThread(self._thread)

        self._worker.exported.connect(self._on_exported, Qt.ConnectionType.QueuedConnection)
        self._worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        self._worker.empty.connect(self._on_empty, Qt.ConnectionType.QueuedConnection)
        self._exportRequested.connect(self._worker.export, Qt.ConnectionType.QueuedConnection)
        self._stopRequested.connect(self._worker.request_stop, Qt.ConnectionType.QueuedConnection)
        self._clearStopRequested.connect(
            self._worker.clear_stop, Qt.ConnectionType.QueuedConnection
        )

    # ------------------------------------------------------------------
    def request(
        self,
        fmt: str,
        device: str,
        group: dict[str, str],
        t_start_epoch: float,
        t_end_epoch: float,
        archive_root: str,
        db_path: str | None,
        dest_path: str,
    ) -> int:
        """Queue one export; returns its routing token.

        No supersede: an in-flight export keeps running; this one waits
        its turn on the worker's queue. The thread starts lazily. NOTE
        the stop flag is re-armed ONLY via the queued ``clear_stop``
        (never synchronously, unlike the latest-wins loaders): without a
        token supersede, a synchronous clear would resurrect an export
        still queued behind a shutdown — queue FIFO guarantees the stale
        request drains (and drops on the still-set flag) BEFORE the
        clear lands (qt-concurrency-auditor, M3-C).
        """
        self._token += 1
        token = self._token
        self._clearStopRequested.emit()
        if not self._thread.isRunning():
            self._thread.start()
        self._exportRequested.emit(
            ArchiveExportRequest(
                token=token,
                fmt=fmt,
                device=device,
                group=dict(group),
                t_start_epoch=t_start_epoch,
                t_end_epoch=t_end_epoch,
                archive_root=archive_root,
                db_path=db_path,
                dest_path=dest_path,
            )
        )
        return token

    def shutdown(self) -> None:
        """Tear down for app exit — stop the worker and join the thread.

        An export in flight aborts cooperatively (its temp file is
        removed; the destination is never half-written); exports still
        queued are dropped with a log line.
        """
        self._worker._stop = True
        self._stopRequested.emit()
        if self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(_THREAD_JOIN_MS):
                _log.warning("archive_export_thread_join_timeout")

    # ------------------------------------------------------------------
    @Slot(object)
    def _on_exported(self, payload: object) -> None:
        if isinstance(payload, ArchiveExportResult):
            self.exported.emit(payload)

    @Slot(int, str)
    def _on_failed(self, token: int, message: str) -> None:
        self.failed.emit(token, message)

    @Slot(int)
    def _on_empty(self, token: int) -> None:
        self.empty.emit(token)
