"""Off-GUI-thread loader for an Archive-tab window (3C waveform + spectrogram).

The central Archive tab lets the user browse the SDS archive and load an
arbitrary ``device`` + 3-component ``group`` over an arbitrary interval,
**statically** (no playback). The read (SDS discovery, ``obspy.read``,
merge/trim, NaN-gap array build) *and* the spectrogram STFT are heavy enough
that running them on the GUI thread would hold the GIL and starve the live
SeedLink worker — the failure that reverted the first "Archive Replay" attempt
(``docs/POSTMORTEMS.md``). So this loader mirrors the proven-correct
:class:`~echosmonitor.core.archive_detail_loader.ArchiveDetailLoader`
threading skeleton exactly:

* a parentless ``_ArchiveWindowWorker`` ``moveToThread``-ed onto a dedicated
  ``QThread``,
* all cross-thread signals connected with ``Qt.ConnectionType.QueuedConnection``,
* the worker never raises across the boundary (a failed read becomes a
  ``failed`` signal),
* a cooperative ``_stop`` flag plus a latest-wins ``_active_token`` make an
  in-flight load interruptible at component granularity (rule 7),
* type-erased dataclasses pass through ``Signal(object)`` with ``isinstance``
  guards.

It differs from the detection-detail loader in two ways: there is no
``trigger_nslc`` (the Archive tab loads an explicit ``{"Z","N","E": nslc}``
group), and it **also builds the spectrogram off the worker thread** for the
primary component, handing the finished raw-power image to the GUI. The GUI
does only the cheap display-domain colorize + ``setImage`` (a UI concern; core
never imports gui). The pure ``_stream_to_xy`` array-build helper and the
``ComponentTrace`` dataclass are reused from the detection-detail loader
**without modifying it** (it stays byte-identical).

The loader shares **nothing** with the live engine's data path: read-only
``archive_root`` + thread-safe DAO (rule 8); never touches a ring buffer,
coalescer, or live queue (rule 11).
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

from echosmonitor.core.archive_detail_loader import ComponentTrace, _stream_to_xy
from echosmonitor.core.models import StreamID
from echosmonitor.dsp.spectrogram import RollingSpectrogram
from echosmonitor.storage.archive_reader import ArchiveReader

if TYPE_CHECKING:
    from echosmonitor.storage.dao import ArchiveDao

_log = structlog.get_logger(__name__)

# Bounded join wait on shutdown (rule 7); same reasoning as the detail loader.
_THREAD_JOIN_MS = 4000

# Component preference order for the spectrogram's primary channel.
_COMPONENT_ORDER = {"Z": 0, "N": 1, "E": 2}


@dataclass(slots=True)
class ArchiveWindowRequest:
    """GUI -> worker (type-erased through the Qt signal).

    ``group`` maps component letters to NSLCs (``{"Z","N","E": nslc}``).
    ``primary_comp`` selects which component's samples drive the spectrogram
    (default ``"Z"``). ``archive_root`` is snapshotted on the GUI thread so
    the worker never touches the live engine.
    """

    token: int
    device: str
    group: dict[str, str]
    primary_comp: str
    t_start_epoch: float
    t_end_epoch: float
    archive_root: str


@dataclass(slots=True)
class ArchiveWindowResult:
    """Worker -> GUI (type-erased through the Qt signal).

    ``traces`` holds the components that had archived data (Z first). The
    spectrogram fields describe the primary component's STFT image as raw
    linear power (``spec_power`` shape ``(n_freq, n_cols)``); they are all
    ``None`` when the primary component is absent or too short for one STFT
    window. ``spec_t_start`` / ``spec_t_end`` are the wall-clock epochs the
    image's X extent maps to.
    """

    token: int
    traces: list[ComponentTrace]
    primary_comp: str
    spec_power: np.ndarray | None
    spec_freqs: np.ndarray | None
    spec_t_start: float
    spec_t_end: float
    elapsed_ms: float


def _build_spectrogram(
    trace: ComponentTrace,
) -> tuple[np.ndarray, np.ndarray, float, float] | None:
    """Compute a static raw-power spectrogram image from one component.

    Gaps (``np.nan`` in ``trace.y``) are zero-filled **for the spectrogram
    only** — ``np.fft.rfft`` cannot take NaN, and a static overview spectrogram
    of a gappy record is inherently approximate; the trace plot keeps its NaN
    line-breaks. Returns ``(power, freqs, t_start, t_end)`` with ``power`` of
    shape ``(n_freq, n_cols)``, or ``None`` when the window is too short for a
    single STFT segment.
    """
    fs = float(trace.fs)
    if fs <= 0.0 or trace.y.size == 0:
        return None
    samples = np.nan_to_num(trace.y.astype(np.float64, copy=False), nan=0.0)
    spec = RollingSpectrogram(fs)
    columns = spec.add_samples(samples)
    if not columns:
        return None
    power = np.stack(columns, axis=1).astype(np.float32)  # (n_freq, n_cols)
    freqs = spec.freqs()
    t_start = float(trace.start_epoch)
    t_end = t_start + power.shape[1] * spec.column_dt
    return power, freqs, t_start, t_end


class _ArchiveWindowWorker(QObject):
    """Lives on the loader's dedicated thread. Reads one 3C window + builds the
    primary component's spectrogram per request; never raises across the
    thread boundary."""

    loaded = Signal(object)  # ArchiveWindowResult
    failed = Signal(int, str)  # token, message
    empty = Signal(int)  # token

    def __init__(self, dao: ArchiveDao | None) -> None:
        super().__init__()  # parentless — moveToThread requires no parent
        self._dao = dao
        self._stop = False
        # Latest-wins gate, written GIL-atomically from the GUI thread.
        self._active_token = -1

    @Slot(object)
    def load(self, request: object) -> None:
        if not isinstance(request, ArchiveWindowRequest):  # defensive (type-erased)
            return
        token = request.token
        if self._stop or token != self._active_token:
            return  # superseded before we started
        t0 = time.monotonic()
        _log.info(
            "archive_window_load_start",
            token=token,
            device=request.device,
            t_start=request.t_start_epoch,
            t_end=request.t_end_epoch,
            n_components=len(request.group),
            primary=request.primary_comp,
        )
        try:
            reader = ArchiveReader(Path(request.archive_root), self._dao)
            t_start_u = UTCDateTime(request.t_start_epoch)
            t_end_u = UTCDateTime(request.t_end_epoch)
            traces: list[ComponentTrace] = []
            for comp in sorted(request.group, key=lambda c: _COMPONENT_ORDER.get(c, 99)):
                if self._stop or token != self._active_token:
                    return  # cooperative cancel between component reads
                nslc = request.group[comp]
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
            if self._stop or token != self._active_token:
                return  # disengaged mid-read — do not announce
            spec = self._spectrogram_for(traces, request.primary_comp)
        except Exception as exc:  # never crash the worker thread
            _log.error("archive_window_load_failed", token=token, error=str(exc))
            self.failed.emit(token, str(exc))
            return
        if self._stop or token != self._active_token:
            return
        elapsed = (time.monotonic() - t0) * 1000.0
        if not traces:
            _log.info("archive_window_load_empty", token=token, elapsed_ms=elapsed)
            self.empty.emit(token)
            return
        if spec is None:
            spec_power = spec_freqs = None
            spec_t_start = request.t_start_epoch
            spec_t_end = request.t_end_epoch
        else:
            spec_power, spec_freqs, spec_t_start, spec_t_end = spec
        _log.info(
            "archive_window_load_done",
            token=token,
            n_traces=len(traces),
            has_spectrogram=spec is not None,
            elapsed_ms=elapsed,
        )
        self.loaded.emit(
            ArchiveWindowResult(
                token=token,
                traces=traces,
                primary_comp=request.primary_comp,
                spec_power=spec_power,
                spec_freqs=spec_freqs,
                spec_t_start=spec_t_start,
                spec_t_end=spec_t_end,
                elapsed_ms=elapsed,
            )
        )

    @staticmethod
    def _spectrogram_for(
        traces: list[ComponentTrace], primary_comp: str
    ) -> tuple[np.ndarray, np.ndarray, float, float] | None:
        """Pick the primary component (falling back to the first present) and
        build its spectrogram off-thread."""
        if not traces:
            return None
        primary = next((t for t in traces if t.comp == primary_comp), traces[0])
        return _build_spectrogram(primary)

    @Slot()
    def request_stop(self) -> None:
        self._stop = True

    @Slot()
    def clear_stop(self) -> None:
        self._stop = False


class ArchiveWindowLoader(QObject):
    """Owns the archive-window worker and its dedicated thread.

    Created and owned by the main window (like the detail loader). Re-emits the
    worker's results on the GUI thread. ``request`` is latest-wins: each call
    supersedes any in-flight load.
    """

    loaded = Signal(object)  # ArchiveWindowResult (GUI thread)
    failed = Signal(int, str)  # token, message (GUI thread)
    empty = Signal(int)  # token (GUI thread)

    # GUI -> worker (QueuedConnection → runs on ``_thread``).
    _loadRequested = Signal(object)  # ArchiveWindowRequest  # noqa: N815
    _stopRequested = Signal()  # noqa: N815
    _clearStopRequested = Signal()  # noqa: N815

    def __init__(self, dao: ArchiveDao | None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._token = 0

        self._worker = _ArchiveWindowWorker(dao)
        self._thread = QThread()
        self._thread.setObjectName("archive-window-loader")
        self._worker.moveToThread(self._thread)

        self._worker.loaded.connect(self._on_loaded, Qt.ConnectionType.QueuedConnection)
        self._worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        self._worker.empty.connect(self._on_empty, Qt.ConnectionType.QueuedConnection)
        self._loadRequested.connect(self._worker.load, Qt.ConnectionType.QueuedConnection)
        self._stopRequested.connect(self._worker.request_stop, Qt.ConnectionType.QueuedConnection)
        self._clearStopRequested.connect(
            self._worker.clear_stop, Qt.ConnectionType.QueuedConnection
        )

    # ------------------------------------------------------------------
    def request(
        self,
        device: str,
        group: dict[str, str],
        t_start_epoch: float,
        t_end_epoch: float,
        archive_root: str,
        primary_comp: str = "Z",
    ) -> int:
        """Dispatch a 3C archive read + spectrogram build off the GUI thread.

        Latest-wins: bumps the token and writes it to the worker's
        ``_active_token`` GIL-atomically so a load already in flight notices it
        was superseded and aborts; the new load runs with a cleared stop flag.
        The thread is started lazily on first use. Returns the new token.
        """
        self._token += 1
        token = self._token
        self._worker._active_token = token
        self._worker._stop = False
        self._clearStopRequested.emit()
        if not self._thread.isRunning():
            self._thread.start()
        self._loadRequested.emit(
            ArchiveWindowRequest(
                token=token,
                device=device,
                group=dict(group),
                primary_comp=primary_comp,
                t_start_epoch=t_start_epoch,
                t_end_epoch=t_end_epoch,
                archive_root=archive_root,
            )
        )
        return token

    def cancel(self) -> None:
        """Drop any in-flight load (its result will be ignored downstream)."""
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
                _log.warning("archive_window_thread_join_timeout")

    # ------------------------------------------------------------------
    @Slot(object)
    def _on_loaded(self, payload: object) -> None:
        if isinstance(payload, ArchiveWindowResult):
            self.loaded.emit(payload)

    @Slot(int, str)
    def _on_failed(self, token: int, message: str) -> None:
        self.failed.emit(token, message)

    @Slot(int)
    def _on_empty(self, token: int) -> None:
        self.empty.emit(token)
