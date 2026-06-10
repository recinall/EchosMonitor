"""Off-GUI-thread loader for a detection's archived 3-component waveform.

When a detection has scrolled out of the live ring buffer, the Detections
detail pane shows the event by reading its Z/N/E components back from the
SDS archive (static — no playback). The read itself (SDS file discovery,
``obspy.read``, merge/trim, and the NaN-gap array build) is heavy enough
that doing it on the GUI thread would hold the GIL and starve the SeedLink
worker — the exact failure that got the first "Archive Replay" attempt
reverted (``docs/POSTMORTEMS.md``). The recorded lesson: *isolation from
buffers is not isolation from the GUI thread.*

So this module runs the read on a dedicated worker thread, mirroring the
proven-correct :class:`~echosmonitor.core.hvsr_engine.HvsrEngine`
pattern exactly:

* a parentless ``_ArchiveDetailWorker`` ``moveToThread``-ed onto a
  dedicated ``QThread``,
* all cross-thread signals connected with ``Qt.ConnectionType.
  QueuedConnection`` (worker slots run on the worker; result slots run on
  the GUI thread),
* the worker never raises across the boundary — a failed read becomes a
  ``failed`` signal, not a crashed thread,
* a cooperative ``_stop`` flag plus a latest-wins ``_active_token`` make an
  in-flight load interruptible at component granularity (rule 7),
* type-erased dataclasses pass through ``Signal(object)`` with ``isinstance``
  guards.

The loader shares **nothing** with the live engine's data path: it never
touches a ring buffer, coalescer, or live queue. Its only inputs are an
``archive_root`` path (snapshotted on the GUI thread into the request) and a
thread-safe :class:`~echosmonitor.storage.dao.ArchiveDao` (per-thread
sqlite via ``threading.local``), both consulted **read-only** (rule 8). A
fresh :class:`~echosmonitor.storage.archive_reader.ArchiveReader` is
built per call on the worker thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import structlog
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from echosmonitor.core.models import StreamID
from echosmonitor.storage.archive_reader import ArchiveReader

if TYPE_CHECKING:
    from obspy import Stream

    from echosmonitor.storage.dao import ArchiveDao

_log = structlog.get_logger(__name__)

# Bounded join wait on shutdown (rule 7). A bounded archive read is
# sub-second; a cold day-by-day SDS scan (capped at the reader's
# ``_MAX_SCAN_DAYS``) is the worst case. Never an unbounded join.
_THREAD_JOIN_MS = 4000


@dataclass(slots=True)
class ArchiveDetailRequest:
    """GUI -> worker (type-erased through the Qt signal).

    ``components`` maps component letters to NSLCs
    (``{"Z": .., "N": .., "E": ..}``). ``archive_root`` is snapshotted on
    the GUI thread (``str(engine.archive_root(device))``) so the worker
    never touches the live engine.
    """

    token: int
    device: str
    trigger_nslc: str
    components: dict[str, str]
    t_start_epoch: float
    t_end_epoch: float
    archive_root: str


@dataclass(slots=True)
class ComponentTrace:
    """One prepared component, ready for ``setData`` on the GUI thread.

    ``x`` is wall-clock POSIX epochs on a regular ``1/fs`` grid across the
    requested window; ``y`` is float64 counts with ``np.nan`` wherever the
    archive had a gap (masked / missing). pyqtgraph renders NaN as a line
    break, so gaps are honest and never interpolated.
    """

    comp: str
    nslc: str
    x: np.ndarray
    y: np.ndarray
    fs: float
    start_epoch: float


@dataclass(slots=True)
class ArchiveDetailResult:
    """Worker -> GUI (type-erased through the Qt signal).

    ``traces`` holds only the components that had archived data (Z first);
    ``trigger_comp`` is the component letter of the detection's own NSLC.
    """

    token: int
    trigger_comp: str
    traces: list[ComponentTrace]
    elapsed_ms: float


def _stream_to_xy(
    stream: Stream, t_start: float, t_end: float
) -> tuple[np.ndarray, np.ndarray, float] | None:
    """Project a merged Stream onto a regular grid with NaN gap-breaks.

    The reader merges with ``method=0`` (gaps left explicit as masked
    arrays / separate sub-traces, no fill). Build a single ``1/fs`` grid
    spanning ``[t_start, t_end]`` and scatter each sub-trace's samples onto
    it; everything unfilled (inter-trace gaps, masked positions) stays
    ``np.nan``. Pure / worker-thread-safe — no Qt, no I/O.

    Returns ``(x_epochs, y_counts, fs)`` or ``None`` when the stream is
    empty (component absent from the archive).
    """
    if len(stream) == 0:
        return None
    # Use the first sub-trace's sample rate as the grid reference; same-id
    # merged traces share it.
    fs = float(stream[0].stats.sampling_rate)
    if fs <= 0.0:
        return None
    n = round((t_end - t_start) * fs) + 1
    if n <= 0:
        return None
    x = t_start + np.arange(n, dtype=np.float64) / fs
    y = np.full(n, np.nan, dtype=np.float64)
    for trace in stream:
        tr_fs = float(trace.stats.sampling_rate)
        if tr_fs != fs:
            # A same-id sub-trace at a different declared rate would be
            # mis-placed on the shared grid; skip it rather than scatter it
            # wrong (an archive-corruption edge case, not a normal path).
            continue
        data = np.ma.getdata(trace.data).astype(np.float64, copy=False)
        mask = (
            np.ma.getmaskarray(trace.data)
            if np.ma.isMaskedArray(trace.data)
            else np.zeros(data.shape[0], dtype=bool)
        )
        # Vectorised scatter onto the shared grid. Masked / out-of-window
        # samples are dropped (those grid positions stay NaN → line breaks).
        offset = round((float(trace.stats.starttime) - t_start) * fs)
        dst = offset + np.arange(data.shape[0])
        keep = (dst >= 0) & (dst < n) & ~mask
        y[dst[keep]] = data[keep]
    return x, y, fs


class _ArchiveDetailWorker(QObject):
    """Lives on the loader's dedicated thread. Reads one detection's 3C
    window per request; never raises across the thread boundary.

    Parentless ``QObject`` moved to the worker thread; ``load`` is invoked
    via ``QueuedConnection``. A failed read becomes a ``failed`` signal; a
    window with no archived data for any component becomes ``empty``.
    """

    loaded = Signal(object)  # ArchiveDetailResult
    failed = Signal(int, str)  # token, message
    empty = Signal(int)  # token

    def __init__(self, dao: ArchiveDao | None) -> None:
        super().__init__()  # parentless — moveToThread requires no parent
        self._dao = dao
        self._stop = False
        # Latest-wins gate, written GIL-atomically from the GUI thread: a
        # load already inside ``load`` notices it was superseded and aborts.
        self._active_token = -1

    @Slot(object)
    def load(self, request: object) -> None:
        if not isinstance(request, ArchiveDetailRequest):  # defensive (type-erased)
            return
        token = request.token
        if self._stop or token != self._active_token:
            return  # superseded before we started
        t0 = time.monotonic()
        _log.info(
            "archive_detail_load_start",
            token=token,
            device=request.device,
            nslc=request.trigger_nslc,
            t_start=request.t_start_epoch,
            t_end=request.t_end_epoch,
            n_components=len(request.components),
        )
        try:
            reader = ArchiveReader(Path(request.archive_root), self._dao)
            t_start_u = UTCDateTime(request.t_start_epoch)
            t_end_u = UTCDateTime(request.t_end_epoch)
            traces: list[ComponentTrace] = []
            # Z first, then N, then E, then anything else — stable order for
            # the stacked view.
            for comp in self._ordered_components(request.components):
                if self._stop or token != self._active_token:
                    return  # cooperative cancel between component reads
                nslc = request.components[comp]
                stream = reader.read_window(
                    StreamID.from_trace_id(nslc),
                    t_start_u,
                    t_end_u,
                    device_name=request.device,
                )
                xy = _stream_to_xy(stream, request.t_start_epoch, request.t_end_epoch)
                if xy is None:
                    continue
                x, y, fs = xy
                traces.append(
                    ComponentTrace(
                        comp=comp,
                        nslc=nslc,
                        x=x,
                        y=y,
                        fs=fs,
                        start_epoch=request.t_start_epoch,
                    )
                )
        except Exception as exc:  # never crash the worker thread
            _log.error("archive_detail_load_failed", token=token, error=str(exc))
            self.failed.emit(token, str(exc))
            return
        if self._stop or token != self._active_token:
            return  # disengaged mid-read — do not announce
        elapsed = (time.monotonic() - t0) * 1000.0
        if not traces:
            _log.info("archive_detail_load_empty", token=token, elapsed_ms=elapsed)
            self.empty.emit(token)
            return
        trigger_comp = self._trigger_component(request)
        _log.info(
            "archive_detail_load_done",
            token=token,
            n_traces=len(traces),
            elapsed_ms=elapsed,
        )
        self.loaded.emit(ArchiveDetailResult(token, trigger_comp, traces, elapsed))

    @staticmethod
    def _ordered_components(components: dict[str, str]) -> list[str]:
        order = {"Z": 0, "N": 1, "E": 2}
        return sorted(components, key=lambda c: order.get(c, 99))

    @staticmethod
    def _trigger_component(request: ArchiveDetailRequest) -> str:
        for comp, nslc in request.components.items():
            if nslc == request.trigger_nslc:
                return comp
        # Fall back to Z, or the first available component.
        if "Z" in request.components:
            return "Z"
        return next(iter(request.components), "Z")

    @Slot()
    def request_stop(self) -> None:
        self._stop = True

    @Slot()
    def clear_stop(self) -> None:
        self._stop = False


class ArchiveDetailLoader(QObject):
    """Owns the archive-detail worker and its dedicated thread.

    Created and owned by the main window (like the deconvolution worker).
    Re-emits the worker's results on the GUI thread. ``request`` is
    latest-wins: each call supersedes any in-flight load.
    """

    loaded = Signal(object)  # ArchiveDetailResult (GUI thread)
    failed = Signal(int, str)  # token, message (GUI thread)
    empty = Signal(int)  # token (GUI thread)

    # GUI -> worker (QueuedConnection → runs on ``_thread``).
    _loadRequested = Signal(object)  # ArchiveDetailRequest  # noqa: N815
    _stopRequested = Signal()  # noqa: N815
    _clearStopRequested = Signal()  # noqa: N815

    def __init__(self, dao: ArchiveDao | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._token = 0

        self._worker = _ArchiveDetailWorker(dao)
        self._thread = QThread()
        self._thread.setObjectName("archive-detail-loader")
        self._worker.moveToThread(self._thread)

        # Worker → loader: QueuedConnection so these re-emit on the GUI thread.
        self._worker.loaded.connect(self._on_loaded, Qt.ConnectionType.QueuedConnection)
        self._worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        self._worker.empty.connect(self._on_empty, Qt.ConnectionType.QueuedConnection)
        # Loader → worker: QueuedConnection so the slot bodies run on the worker.
        self._loadRequested.connect(self._worker.load, Qt.ConnectionType.QueuedConnection)
        self._stopRequested.connect(self._worker.request_stop, Qt.ConnectionType.QueuedConnection)
        self._clearStopRequested.connect(
            self._worker.clear_stop, Qt.ConnectionType.QueuedConnection
        )

    # ------------------------------------------------------------------
    def request(
        self,
        device: str,
        trigger_nslc: str,
        components: dict[str, str],
        t_start_epoch: float,
        t_end_epoch: float,
        archive_root: str,
    ) -> int:
        """Dispatch a 3C archive read off the GUI thread; return its token.

        Latest-wins: bumps the token and writes it to the worker's
        ``_active_token`` GIL-atomically so a load already in flight notices
        it was superseded and aborts; the new load runs with a cleared stop
        flag. The thread is started lazily on first use.
        """
        self._token += 1
        token = self._token
        # GIL-atomic supersede + re-arm for the new load (belt-and-suspenders
        # with the queued clear_stop for the idle-worker case).
        self._worker._active_token = token
        self._worker._stop = False
        self._clearStopRequested.emit()
        if not self._thread.isRunning():
            self._thread.start()
        self._loadRequested.emit(
            ArchiveDetailRequest(
                token=token,
                device=device,
                trigger_nslc=trigger_nslc,
                components=dict(components),
                t_start_epoch=t_start_epoch,
                t_end_epoch=t_end_epoch,
                archive_root=archive_root,
            )
        )
        return token

    def cancel(self) -> None:
        """Drop any in-flight load (its result will be ignored downstream).

        Bumps the token so a delivered result is stale, and sets the
        cooperative stop flag so a multi-component read aborts between
        components. Does not join — cheap cancel.
        """
        self._token += 1
        self._worker._active_token = self._token
        self._worker._stop = True
        self._stopRequested.emit()

    def shutdown(self) -> None:
        """Tear down for app exit — stop the worker and join the thread."""
        self._worker._stop = True
        self._stopRequested.emit()
        if self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(_THREAD_JOIN_MS):
                _log.warning("archive_detail_thread_join_timeout")

    # ------------------------------------------------------------------
    @Slot(object)
    def _on_loaded(self, payload: object) -> None:
        if isinstance(payload, ArchiveDetailResult):
            self.loaded.emit(payload)

    @Slot(int, str)
    def _on_failed(self, token: int, message: str) -> None:
        self.failed.emit(token, message)

    @Slot(int)
    def _on_empty(self, token: int) -> None:
        self.empty.emit(token)
