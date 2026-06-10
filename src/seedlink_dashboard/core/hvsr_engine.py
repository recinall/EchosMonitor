"""Live HVSR measurement engine (best-effort consumer, rule 11).

:class:`HvsrEngine` is a *peer* of :class:`~seedlink_dashboard.core.
streaming_engine.StreamingEngine`, owned by the main window and living on
the GUI thread. It drives an HVSR measurement over a chosen device's 3
components and runs the (heavy, numba-JIT-bearing) ``hvsrpy`` re-compute
**off** the GUI thread *and* **off** the data-path threads, on a dedicated
``_hvsr_thread`` (the standard parentless-worker + ``moveToThread``
pattern, rule 1).

Best-effort consumer (CLAUDE.md rule 11). The engine *pulls* successive
non-overlapping windows from the ring buffers via
:meth:`StreamingEngine.read_recent` (a cheap, lock-protected read) on its
own ``QTimer``; it never sits on, and so can never back-pressure,
acquisition / DSP / detection / storage. A re-compute runs on the worker
thread over a *snapshot* of the accumulator; while it runs (the first one
JIT-compiles ``hvsrpy``'s smoothing and can take several seconds) the live
accumulator keeps growing on the GUI thread. If a re-compute is still in
flight when the next window arrives the engine **skips** that cycle (one
log per 5 s, ``hvsrBackpressure`` emitted) — it never queues unboundedly
and never blocks the data path. A lagging compute only coarsens the
re-compute rate.

Wait observability (rule 7). Each compute logs start / done / elapsed on
the worker thread (see :meth:`HvsrAccumulator.compute`). Stop sets a
cooperative flag and joins the thread with a bounded ``wait``.

This module imports no ``hvsrpy`` — the boundary lives in
:mod:`seedlink_dashboard.core.hvsr`; the engine only ever sees the frozen
:class:`~seedlink_dashboard.core.hvsr.HvsrResult`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np
import structlog
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from seedlink_dashboard.core.hvsr import (
    HvsrAccumulator,
    HvsrResult,
    HvsrSettings,
    responses_identical,
)

if TYPE_CHECKING:
    from seedlink_dashboard.core.response import ResponseProvider
    from seedlink_dashboard.core.streaming_engine import StreamingEngine
    from seedlink_dashboard.storage.archive_reader import ArchiveReader

_log = structlog.get_logger(__name__)

# Bounded join wait on stop (rule 7) — generous: the in-flight compute may be
# the first one (numba JIT, several seconds). Never an unbounded join.
_THREAD_JOIN_MS = 8000

# Throttle the skip log to one line per this many seconds (rule 5).
_SKIP_LOG_INTERVAL_S = 5.0

# Each present component must hold at least this fraction of the requested
# window before we capture it (the 3 components' tails differ by a sample
# or two).
_MIN_WINDOW_FILL = 0.9

# Start computing (and showing a curve) once this many windows exist; the
# measurement then refines as N grows. SESAME criteria may fail early and
# pass as windows accumulate — the intended "refines over time" UX.
_MIN_WINDOWS = 3


class HvsrState(StrEnum):
    """Lifecycle of a measurement, surfaced to the UI."""

    IDLE = "idle"
    ACCUMULATING = "accumulating"
    COMPUTING = "computing"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass(slots=True)
class HvsrSummary:
    """Snapshot handed to the UI on start / state change."""

    measurement_id: str
    device: str
    nslc_by_component: dict[str, str]
    live: bool
    state: HvsrState = HvsrState.IDLE
    n_windows_total: int = 0
    n_windows_valid: int = 0
    last_compute_ms: float = 0.0
    same_response: bool = False
    last_error: str = ""


@dataclass(slots=True)
class _ComputeRequest:
    """Engine → worker (type-erased through the Qt signal)."""

    measurement_id: str
    accumulator: HvsrAccumulator


@dataclass(slots=True)
class _ComputeResult:
    """Worker → engine (type-erased through the Qt signal)."""

    measurement_id: str
    result: HvsrResult
    elapsed_ms: float


@dataclass(slots=True)
class _Measurement:
    """Engine-side bookkeeping for the single active measurement."""

    measurement_id: str
    device: str
    group: dict[str, str]  # component letter -> nslc
    settings: HvsrSettings
    accumulator: HvsrAccumulator
    live: bool
    same_response: bool
    state: HvsrState = HvsrState.IDLE
    pending: int = 0
    skipped: int = 0
    last_skip_log: float = 0.0
    last_compute_ms: float = 0.0
    n_windows_valid: int = 0
    last_error: str = ""
    # Whether the first full HVSR compute has landed. Until then the engine
    # emits a cheap raw 3-channel PSD per window so the PSD panel populates
    # early (FIX 3); after, the result's smoothed PSD owns it.
    first_compute_done: bool = False
    # End time of the most-recently captured window; the next window is only
    # captured once ``latest`` has advanced a full window length past it, so
    # captured windows are disjoint (non-overlapping).
    last_window_end: UTCDateTime | None = None


class _HvsrWorker(QObject):
    """Lives on ``_hvsr_thread``. Runs one hvsrpy re-compute per request.

    Parentless ``QObject`` moved to the worker thread; slots invoked via
    ``QueuedConnection``. Never raises across the thread boundary — a failed
    compute becomes a ``failed`` signal, not a crashed thread.
    """

    computed = Signal(object)  # _ComputeResult
    failed = Signal(str, str)  # measurement_id, message

    def __init__(self) -> None:
        super().__init__()
        self._stop = False

    @Slot(object)
    def compute(self, request: object) -> None:
        if not isinstance(request, _ComputeRequest):  # defensive (type-erased)
            return
        if self._stop:
            return
        t0 = time.monotonic()
        try:
            result = request.accumulator.compute()
        except Exception as exc:  # never crash the worker thread
            _log.error(
                "hvsr_worker_compute_failed", measurement=request.measurement_id, error=str(exc)
            )
            self.failed.emit(request.measurement_id, str(exc))
            return
        if self._stop:
            return  # disengaged mid-compute — do not announce
        elapsed = (time.monotonic() - t0) * 1000.0
        self.computed.emit(_ComputeResult(request.measurement_id, result, elapsed))

    @Slot()
    def request_stop(self) -> None:
        self._stop = True

    @Slot()
    def clear_stop(self) -> None:
        self._stop = False


class HvsrEngine(QObject):
    """Owns one active HVSR measurement and its dedicated compute thread."""

    hvsrMeasurementStarted = Signal(str, object)  # id, HvsrSummary  # noqa: N815
    hvsrMeasurementStopped = Signal(str)  # id  # noqa: N815
    hvsrUpdated = Signal(object)  # HvsrResult  # noqa: N815
    # Early raw 3-channel PSD ({comp: (freqs, db)}) before the first full
    # compute, so the PSD panel populates from the first window (FIX 3).
    hvsrPsdReady = Signal(object)  # noqa: N815
    hvsrWindowCount = Signal(int, int)  # n_valid, n_total  # noqa: N815
    hvsrStateChanged = Signal(str, str)  # id, state.value  # noqa: N815
    hvsrBackpressure = Signal(str, int)  # id, skipped  # noqa: N815

    # Engine → worker (QueuedConnection → runs on ``_hvsr_thread``).
    _computeRequested = Signal(object)  # _ComputeRequest  # noqa: N815
    _stopRequested = Signal()  # noqa: N815
    _clearStopRequested = Signal()  # noqa: N815

    def __init__(
        self,
        engine: StreamingEngine,
        provider: ResponseProvider | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._provider = provider
        self._measurement: _Measurement | None = None
        self._seq = 0

        self._worker = _HvsrWorker()
        self._hvsr_thread = QThread()
        self._hvsr_thread.setObjectName("hvsr-worker")
        self._worker.moveToThread(self._hvsr_thread)

        # Worker → engine: QueuedConnection so these slots run on the GUI thread.
        self._worker.computed.connect(self._on_computed, Qt.ConnectionType.QueuedConnection)
        self._worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        # Engine → worker: QueuedConnection so the slot body runs on the worker.
        self._computeRequested.connect(self._worker.compute, Qt.ConnectionType.QueuedConnection)
        self._stopRequested.connect(self._worker.request_stop, Qt.ConnectionType.QueuedConnection)
        self._clearStopRequested.connect(
            self._worker.clear_stop, Qt.ConnectionType.QueuedConnection
        )

        # GUI-thread pull timer (live mode). Interval set per measurement.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start_measurement(self, device: str, group: dict[str, str], settings: HvsrSettings) -> str:
        """Begin a LIVE HVSR measurement on ``device``'s 3-component ``group``.

        ``group`` maps component letters to NSLCs (``{"Z": .., "N": .., "E": ..}``).
        Stops any prior measurement first. Returns the new measurement id.
        Windows accumulate on a timer and the curve refines as N grows.
        """
        self.stop_measurement()
        self._seq += 1
        measurement_id = f"hvsr-{self._seq}"
        same_response, detail = responses_identical(self._provider, device, group, UTCDateTime())
        nslc_ref = group.get("Z") or next(iter(group.values()))
        station_key = ".".join(nslc_ref.split(".")[:2])
        accumulator = HvsrAccumulator(
            settings,
            same_response=same_response,
            same_response_detail=detail,
            device=device,
            station_key=station_key,
            provenance="live",
        )
        m = _Measurement(
            measurement_id=measurement_id,
            device=device,
            group=dict(group),
            settings=settings,
            accumulator=accumulator,
            live=True,
            same_response=same_response,
        )
        self._measurement = m
        if not self._hvsr_thread.isRunning():
            self._hvsr_thread.start()
        self._clearStopRequested.emit()  # reset a stop flag from a prior run
        self._worker._stop = False
        self._set_state(m, HvsrState.ACCUMULATING)
        self.hvsrMeasurementStarted.emit(measurement_id, self._summary(m))
        # Tick fast enough to notice a full fresh window promptly; the capture
        # cadence itself is gated on data availability, not the timer rate.
        interval_ms = max(100, int(settings.window_length_s / 4.0 * 1000.0))
        self._timer.setInterval(interval_ms)
        self._timer.start()
        return measurement_id

    def start_archive_measurement(
        self,
        device: str,
        group: dict[str, str],
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        settings: HvsrSettings,
        reader: ArchiveReader,
    ) -> str:
        """Run HVSR over an archived ``[t_start, t_end]`` window (Stage C).

        Slices non-overlapping 3C windows from the archive (a deliberate
        one-shot inline read, bounded by the reader's day-scan cap — this is a
        user action over a bounded range, not the live data path, so rule 11's
        back-pressure concern does not apply), feeds them all into a fresh
        accumulator (``provenance="archive"``), and dispatches ONE off-thread
        compute. Returns the measurement id, or ``""`` if the range holds no
        gap-free 3-component window (the caller surfaces "no data").
        """
        from seedlink_dashboard.core.hvsr import slice_archive_windows

        self.stop_measurement()
        windows = slice_archive_windows(reader, device, group, t_start, t_end, settings)
        if not windows:
            _log.info("hvsr_archive_no_windows", device=device)
            return ""
        self._seq += 1
        measurement_id = f"hvsr-{self._seq}"
        same_response, detail = responses_identical(self._provider, device, group, t_start)
        nslc_ref = group.get("Z") or next(iter(group.values()))
        station_key = ".".join(nslc_ref.split(".")[:2])
        accumulator = HvsrAccumulator(
            settings,
            same_response=same_response,
            same_response_detail=detail,
            device=device,
            station_key=station_key,
            provenance="archive",
        )
        for z, n, e, ws, fs in windows:
            try:
                accumulator.add_window(z, n, e, ws, fs)
            except Exception as exc:
                _log.warning("hvsr_archive_add_window_failed", error=str(exc))
        if accumulator.n_windows == 0:
            return ""
        m = _Measurement(
            measurement_id=measurement_id,
            device=device,
            group=dict(group),
            settings=settings,
            accumulator=accumulator,
            live=False,
            same_response=same_response,
        )
        self._measurement = m
        if not self._hvsr_thread.isRunning():
            self._hvsr_thread.start()
        self._worker._stop = False
        self._set_state(m, HvsrState.ACCUMULATING)
        self.hvsrMeasurementStarted.emit(measurement_id, self._summary(m))
        self._request_recompute(m, force=True)  # single off-thread compute
        return measurement_id

    def set_window_override(self, measurement_id: str, window_id: int, accepted: bool) -> None:
        """Manually include/exclude one window; triggers an immediate recompute."""
        m = self._measurement
        if m is None or m.measurement_id != measurement_id:
            return
        m.accumulator.set_window_override(window_id, accepted)
        self._request_recompute(m, force=True)

    def stop_measurement(self, measurement_id: str | None = None) -> None:
        """Stop the active measurement (or the named one). Idempotent.

        Stops the pull timer, sets a cooperative stop flag (observed within
        one polling period — rule 7), and joins the worker thread with a
        bounded wait.
        """
        m = self._measurement
        if m is None:
            return
        if measurement_id is not None and measurement_id != m.measurement_id:
            return
        self._timer.stop()
        self._set_state(m, HvsrState.STOPPING)
        # Synchronous flag write (GIL-atomic) so a long in-flight compute is
        # interrupted within one poll; the queued request_stop is a
        # belt-and-suspenders for the idle-worker case.
        self._worker._stop = True
        self._stopRequested.emit()
        if self._hvsr_thread.isRunning():
            self._hvsr_thread.quit()
            if not self._hvsr_thread.wait(_THREAD_JOIN_MS):
                _log.warning("hvsr_thread_join_timeout", measurement=m.measurement_id)
        self._set_state(m, HvsrState.IDLE)
        stopped_id = m.measurement_id
        self._measurement = None
        self.hvsrMeasurementStopped.emit(stopped_id)

    def active_measurement(self) -> HvsrSummary | None:
        """Snapshot of the current measurement for the UI, or ``None``."""
        m = self._measurement
        return None if m is None else self._summary(m)

    def shutdown(self) -> None:
        """Tear down for app exit — stop the measurement and the thread."""
        self.stop_measurement()
        if self._hvsr_thread.isRunning():
            self._hvsr_thread.quit()
            self._hvsr_thread.wait(_THREAD_JOIN_MS)

    # ------------------------------------------------------------------
    # Internal — live accumulation
    # ------------------------------------------------------------------
    @Slot()
    def _tick(self) -> None:
        """Live pull tick — best-effort, never blocks the data path (rule 11)."""
        m = self._measurement
        if m is None or not m.live or m.state not in (HvsrState.ACCUMULATING, HvsrState.COMPUTING):
            return
        captured = self._capture_disjoint_window(m)
        if captured is None:
            return
        try:
            m.accumulator.add_window(*captured)
        except Exception as exc:  # inconsistent window — drop it, keep running
            _log.warning("hvsr_add_window_failed", measurement=m.measurement_id, error=str(exc))
            return
        self.hvsrWindowCount.emit(m.n_windows_valid, m.accumulator.n_windows)
        # Early PSD (FIX 3): until the first full compute lands, emit a cheap
        # raw 3-channel PSD each window so the panel populates from window 1.
        # This is a few-ms Welch (no hvsrpy/JIT) and touches no data-path
        # thread, so it does not back-pressure science (rule 11).
        if not m.first_compute_done:
            psds = m.accumulator.raw_channel_psds()
            if psds:
                self.hvsrPsdReady.emit(psds)
        self._request_recompute(m, force=False)

    def _capture_disjoint_window(
        self, m: _Measurement
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, UTCDateTime, float] | None:
        """Capture the next non-overlapping window, or ``None`` if not ready.

        ``read_recent`` only ever returns the LAST ``window_length_s`` of each
        component (ending at that component's ``latest``). We make successive
        windows disjoint by only capturing once the common ``latest`` has
        advanced a full window length past the previous window's end.
        """
        wl = m.settings.window_length_s
        samples: dict[str, np.ndarray] = {}
        fs_ref = 0.0
        latests: list[UTCDateTime] = []
        for comp, nslc in m.group.items():
            arr, fs, latest = self._engine.read_recent(m.device, nslc, wl)
            if arr.size == 0 or fs <= 0 or latest is None:
                return None  # a component is not yet streaming — wait
            samples[comp] = arr
            fs_ref = fs
            latests.append(latest)
        if len(samples) != 3 or fs_ref <= 0:
            return None
        common_latest = min(latests)
        min_len = min(int(a.shape[0]) for a in samples.values())
        need = int(wl * fs_ref * _MIN_WINDOW_FILL)
        if min_len < max(1, need):
            return None  # not enough samples for a full window yet

        if m.last_window_end is None:
            # First window: capture as soon as a full window exists.
            pass
        elif float(common_latest - m.last_window_end) < wl:
            return None  # no full fresh disjoint window yet
        elif float(common_latest - m.last_window_end) > 2.0 * wl:
            # Fell behind / ring scrolled: we cannot recover the missed span
            # from a read_recent-only ring. We capture the freshest full
            # window [common_latest-wl, common_latest] (real, disjoint samples)
            # and the ``m.last_window_end = common_latest`` below IS the
            # resync — the missed interval is dropped, never overlapped or
            # fabricated (rule 7 honesty). HVSR windows need not be contiguous,
            # only disjoint, so the dropped span is sound; the warning is the
            # observable trace that a gap occurred.
            _log.warning(
                "hvsr_window_gap",
                measurement=m.measurement_id,
                gap_s=round(float(common_latest - m.last_window_end), 1),
            )

        za = samples["Z"][-min_len:].astype(np.float64, copy=False)
        na = samples["N"][-min_len:].astype(np.float64, copy=False)
        ea = samples["E"][-min_len:].astype(np.float64, copy=False)
        t_start = common_latest - (min_len - 1) / fs_ref
        m.last_window_end = common_latest
        return za, na, ea, t_start, fs_ref

    def _request_recompute(self, m: _Measurement, *, force: bool) -> None:
        """Dispatch a recompute, or skip if one is in flight (rule 11)."""
        if not force and m.accumulator.n_windows < _MIN_WINDOWS:
            return
        if m.accumulator.n_windows == 0:
            return
        if m.pending >= 1:
            m.skipped += 1
            now = time.monotonic()
            if now - m.last_skip_log >= _SKIP_LOG_INTERVAL_S:
                _log.warning(
                    "hvsr_recompute_skipped", measurement=m.measurement_id, skipped_total=m.skipped
                )
                m.last_skip_log = now
            self.hvsrBackpressure.emit(m.measurement_id, m.skipped)
            return
        m.pending += 1
        self._set_state(m, HvsrState.COMPUTING)
        self._computeRequested.emit(_ComputeRequest(m.measurement_id, m.accumulator.snapshot()))

    @Slot(object)
    def _on_computed(self, payload: object) -> None:
        if not isinstance(payload, _ComputeResult):
            return
        m = self._measurement
        if m is None or m.measurement_id != payload.measurement_id:
            return  # stale result from a stopped measurement
        m.pending = max(0, m.pending - 1)
        m.first_compute_done = True
        m.last_compute_ms = payload.elapsed_ms
        m.n_windows_valid = payload.result.n_windows_valid
        self.hvsrWindowCount.emit(payload.result.n_windows_valid, payload.result.n_windows_total)
        self.hvsrUpdated.emit(payload.result)
        if not m.live:
            # Archive run is one-shot: the single compute is done. Stay
            # selectable for manual override (which re-dispatches a compute),
            # but the measurement is otherwise idle.
            self._set_state(m, HvsrState.IDLE)
        elif m.state is HvsrState.COMPUTING:
            self._set_state(m, HvsrState.ACCUMULATING)

    @Slot(str, str)
    def _on_failed(self, measurement_id: str, message: str) -> None:
        m = self._measurement
        if m is None or m.measurement_id != measurement_id:
            return
        m.pending = max(0, m.pending - 1)
        # A single bad compute must not kill a live measurement; keep going.
        m.last_error = message
        if m.state is HvsrState.COMPUTING:
            self._set_state(m, HvsrState.ACCUMULATING)

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------
    def _set_state(self, m: _Measurement, state: HvsrState) -> None:
        m.state = state
        self.hvsrStateChanged.emit(m.measurement_id, state.value)

    def _summary(self, m: _Measurement) -> HvsrSummary:
        return HvsrSummary(
            measurement_id=m.measurement_id,
            device=m.device,
            nslc_by_component=dict(m.group),
            live=m.live,
            state=m.state,
            n_windows_total=m.accumulator.n_windows,
            n_windows_valid=m.n_windows_valid,
            last_compute_ms=m.last_compute_ms,
            same_response=m.same_response,
            last_error=m.last_error,
        )
