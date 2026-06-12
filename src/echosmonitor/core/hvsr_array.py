"""Multi-device (array) HVSR engine — synchronous per-station HVSR (M5).

Scope (skill ``hvsr-array``): with N independent Echos 3C stations the
honest computation is N independent H/V curves over the same interval and
settings, COMPARED spatially — never averaged across stations into one
"array curve" (f0 varies with the subsurface; a cross-station mean is not
a defined quantity). All per-station physics stays in
:class:`~echosmonitor.core.hvsr.HvsrAccumulator` / hvsrpy; this module is
N accumulators + orchestration.

Windowing decision (open question 5, recorded in ROADMAP): windows are
**per-device independent** — each device accumulates its own gap-free
disjoint windows (its own ``last_window_end`` cursor through the shared
:func:`~echosmonitor.core.hvsr_engine.capture_disjoint_window`); a device
with dropouts simply contributes fewer windows and can never stall the
others. Curves stay comparable because the interval and the settings are
shared (same-``fs`` across devices is NOT required — each accumulator
checks internal consistency only). The stricter common-window gate is a
deferred optional toggle, revisited on real field need.

Threading: the exact :class:`~echosmonitor.core.hvsr_engine.HvsrEngine`
skeleton (skill ``qt-worker-threading``). One dedicated worker thread; ONE
compute request per recompute cycle runs the eligible devices' snapshot
computes **serially** (seconds-scale each, N is small), checking the
cooperative stop flag between devices. Pending is bounded at 1 with
skip-and-throttled-log under load (rule 11 — a lagging compute only
coarsens the recompute rate, it never queues unboundedly and never touches
the data path). Stop joins the thread with a bounded wait (rule 7).

Geometry: a frozen :class:`~echosmonitor.core.positions.StationGeometry`
snapshot is taken by the CALLER at start (from the one shared
``PositionResolver``, rule 16) and carried verbatim on every
:class:`ArrayHvsrResult`; devices missing from it are discoverable via
:meth:`ArrayHvsrResult.unpositioned` — rendered as "no position", never
guessed.

This module imports no ``hvsrpy`` (the boundary lives in
:mod:`echosmonitor.core.hvsr`) and no ``gui`` (rule 4).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from echosmonitor.core.hvsr import (
    HvsrAccumulator,
    HvsrResult,
    HvsrSettings,
    Provenance,
    responses_identical,
)
from echosmonitor.core.hvsr_engine import HvsrState, capture_disjoint_window
from echosmonitor.core.positions import StationGeometry

if TYPE_CHECKING:
    from echosmonitor.core.response import ResponseProvider
    from echosmonitor.core.streaming_engine import StreamingEngine

_log = structlog.get_logger(__name__)

# Bounded join wait on stop (rule 7) — generous: after a synchronous stop
# the residual uninterruptible work is ONE device's compute (the stop flag
# is checked between devices), and that one compute may be the first of a
# run (numba JIT, several seconds). Never an unbounded join.
_THREAD_JOIN_MS = 8000

# Throttle the skip log to one line per this many seconds (rule 5).
_SKIP_LOG_INTERVAL_S = 5.0

# A device joins the compute set once it holds this many windows (live);
# below it the curve would be noise. Matches the single-station engine's
# "refines over time" UX.
_MIN_WINDOWS = 3


@dataclass(frozen=True, slots=True)
class ArrayHvsrResult:
    """One complete array recompute cycle — frozen, GUI-facing (rule 4).

    ``results`` holds the devices that were ELIGIBLE this cycle (enough
    windows) and computed successfully; ``errors`` the eligible devices
    whose compute failed (message verbatim). A selected device absent from
    both simply has not accumulated enough windows yet. Each cycle computes
    every eligible device, so the dict is self-contained — no cross-cycle
    merging on the consumer side.
    """

    measurement_id: str
    devices: tuple[str, ...]  # all selected devices, in start order
    # Mapping (not dict) so consumers cannot mutate a "frozen" cycle result.
    results: Mapping[str, HvsrResult]
    errors: Mapping[str, str]
    geometry: StationGeometry  # positioned subset, snapshotted at start
    settings: HvsrSettings
    provenance: Provenance
    elapsed_ms: float

    def unpositioned(self) -> tuple[str, ...]:
        """Selected devices with no resolved position (rule 16: say so)."""
        return tuple(d for d in self.devices if d not in self.geometry.positions)


@dataclass(slots=True)
class ArrayHvsrSummary:
    """Snapshot handed to the UI on start / state change."""

    measurement_id: str
    devices: tuple[str, ...]
    group_by_device: dict[str, dict[str, str]]
    live: bool
    state: HvsrState = HvsrState.IDLE
    # device -> (n_valid, n_total); n_valid lags by one compute.
    window_counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    last_compute_ms: float = 0.0
    same_response_by_device: dict[str, bool] = field(default_factory=dict)
    last_error: str = ""


@dataclass(slots=True)
class _ArrayComputeRequest:
    """Engine → worker (type-erased through the Qt signal)."""

    measurement_id: str
    # (device, accumulator SNAPSHOT) pairs — the live accumulators keep
    # growing on the GUI thread while the worker computes.
    accumulators: tuple[tuple[str, HvsrAccumulator], ...]


@dataclass(slots=True)
class _ArrayComputeResult:
    """Worker → engine (type-erased through the Qt signal)."""

    measurement_id: str
    results: dict[str, HvsrResult]
    errors: dict[str, str]
    elapsed_ms: float


@dataclass(slots=True)
class _Station:
    """Per-device bookkeeping inside the one active array measurement."""

    device: str
    group: dict[str, str]  # component letter -> nslc
    accumulator: HvsrAccumulator
    same_response: bool
    # Per-device disjoint-window cursor (the independent-windows decision).
    last_window_end: UTCDateTime | None = None
    n_windows_valid: int = 0


@dataclass(slots=True)
class _ArrayMeasurement:
    """Engine-side bookkeeping for the single active array measurement."""

    measurement_id: str
    stations: dict[str, _Station]  # insertion order = start order
    settings: HvsrSettings
    geometry: StationGeometry
    live: bool
    state: HvsrState = HvsrState.IDLE
    pending: int = 0
    skipped: int = 0
    last_skip_log: float = 0.0
    last_compute_ms: float = 0.0
    last_error: str = ""


class _ArrayWorker(QObject):
    """Lives on the array thread. Runs one serial N-device cycle per request.

    Parentless ``QObject`` moved to the worker thread; slots invoked via
    ``QueuedConnection``. Never raises across the thread boundary — a failed
    per-device compute becomes an entry in the result's ``errors`` (the
    other devices still compute), never a crashed thread.
    """

    computed = Signal(object)  # _ArrayComputeResult

    def __init__(self) -> None:
        super().__init__()
        self._stop = False

    @Slot(object)
    def compute(self, request: object) -> None:
        if not isinstance(request, _ArrayComputeRequest):  # defensive (type-erased)
            return
        if self._stop:
            return
        t0 = time.monotonic()
        results: dict[str, HvsrResult] = {}
        errors: dict[str, str] = {}
        for device, accumulator in request.accumulators:
            if self._stop:
                return  # stopped mid-cycle — do not announce a partial cycle
            try:
                results[device] = accumulator.compute()
            except Exception as exc:  # never crash the worker thread
                _log.error(
                    "hvsr_array_compute_failed",
                    measurement=request.measurement_id,
                    device=device,
                    error=str(exc),
                )
                errors[device] = str(exc)
        if self._stop:
            return  # stopped after the last device — do not announce
        elapsed = (time.monotonic() - t0) * 1000.0
        self.computed.emit(_ArrayComputeResult(request.measurement_id, results, errors, elapsed))

    @Slot()
    def request_stop(self) -> None:
        self._stop = True

    @Slot()
    def clear_stop(self) -> None:
        self._stop = False


class HvsrArrayEngine(QObject):
    """Owns one active multi-device HVSR measurement and its compute thread.

    A peer of :class:`~echosmonitor.core.hvsr_engine.HvsrEngine`, owned by
    the main window on the GUI thread. Best-effort consumer (rule 11): it
    pulls per-device windows from the ring buffers via ``read_recent`` on
    its own timer and can never back-pressure acquisition/DSP/storage.
    """

    arrayMeasurementStarted = Signal(str, object)  # id, ArrayHvsrSummary  # noqa: N815
    arrayMeasurementStopped = Signal(str)  # id  # noqa: N815
    arrayUpdated = Signal(object)  # ArrayHvsrResult  # noqa: N815
    # Live accumulation progress: {device: (n_valid, n_total)}.
    arrayWindowCounts = Signal(object)  # noqa: N815
    arrayStateChanged = Signal(str, str)  # id, state.value  # noqa: N815
    arrayBackpressure = Signal(str, int)  # id, skipped  # noqa: N815

    # Engine → worker (QueuedConnection → runs on the array thread).
    _computeRequested = Signal(object)  # _ArrayComputeRequest  # noqa: N815
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
        self._measurement: _ArrayMeasurement | None = None
        self._seq = 0

        self._worker = _ArrayWorker()
        self._array_thread = QThread()
        self._array_thread.setObjectName("hvsr-array-worker")
        self._worker.moveToThread(self._array_thread)

        # Worker → engine: QueuedConnection so the slot runs on the GUI thread.
        self._worker.computed.connect(self._on_computed, Qt.ConnectionType.QueuedConnection)
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
    def start_measurement(
        self,
        devices: Mapping[str, Mapping[str, str]],
        settings: HvsrSettings,
        geometry: StationGeometry,
    ) -> str:
        """Begin a LIVE array measurement over ``devices`` (device → Z/N/E NSLCs).

        ``geometry`` is the caller's position snapshot for the selection
        (``PositionResolver.geometry(devices)`` — rule 16); it rides every
        result unchanged. One shared ``settings`` drives all stations
        (skill ``hvsr-array``). Stops any prior measurement first; returns
        the new measurement id.

        Raises:
            ValueError: empty selection, or a group missing a Z/N/E entry —
                caller (UI) bugs, surfaced loudly on the GUI thread.
        """
        if not devices:
            raise ValueError("array HVSR needs at least one device")
        for device, group in devices.items():
            # Exactly Z/N/E: an extra component would make the shared capture
            # primitive (len==3 gate) silently never ready — reject loudly.
            if set(group) != {"Z", "N", "E"}:
                raise ValueError(
                    f"device {device!r} group must be exactly Z/N/E, got {sorted(group)}"
                )
        self.stop_measurement()
        self._seq += 1
        measurement_id = f"hvsr-array-{self._seq}"
        now = UTCDateTime()
        stations: dict[str, _Station] = {}
        for device, group in devices.items():
            # The counts-honesty layer runs PER DEVICE (skill hvsr-array);
            # each verdict rides that device's HvsrResult verbatim.
            same_response, detail = responses_identical(self._provider, device, dict(group), now)
            nslc_ref = group.get("Z") or next(iter(group.values()))
            station_key = ".".join(nslc_ref.split(".")[:2])
            stations[device] = _Station(
                device=device,
                group=dict(group),
                accumulator=HvsrAccumulator(
                    settings,
                    same_response=same_response,
                    same_response_detail=detail,
                    device=device,
                    station_key=station_key,
                    provenance="live",
                ),
                same_response=same_response,
            )
        m = _ArrayMeasurement(
            measurement_id=measurement_id,
            stations=stations,
            settings=settings,
            geometry=geometry,
            live=True,
        )
        self._measurement = m
        if not self._array_thread.isRunning():
            self._array_thread.start()
        self._clearStopRequested.emit()  # reset a stop flag from a prior run
        self._worker._stop = False
        self._set_state(m, HvsrState.ACCUMULATING)
        self.arrayMeasurementStarted.emit(measurement_id, self._summary(m))
        # Tick fast enough to notice a full fresh window promptly; the capture
        # cadence itself is gated on data availability, not the timer rate.
        interval_ms = max(100, int(settings.window_length_s / 4.0 * 1000.0))
        self._timer.setInterval(interval_ms)
        self._timer.start()
        return measurement_id

    def set_window_override(
        self, measurement_id: str, device: str, window_id: int, accepted: bool
    ) -> None:
        """Manually include/exclude one device's window; recomputes immediately."""
        m = self._measurement
        if m is None or m.measurement_id != measurement_id:
            return
        station = m.stations.get(device)
        if station is None:
            return
        station.accumulator.set_window_override(window_id, accepted)
        self._request_recompute(m, force=True)

    def stop_measurement(self, measurement_id: str | None = None) -> None:
        """Stop the active measurement (or the named one). Idempotent.

        Stops the pull timer, sets a cooperative stop flag (observed between
        per-device computes — rule 7), and joins the worker thread with a
        bounded wait.
        """
        m = self._measurement
        if m is None:
            return
        if measurement_id is not None and measurement_id != m.measurement_id:
            return
        self._timer.stop()
        self._set_state(m, HvsrState.STOPPING)
        # Synchronous flag write (GIL-atomic) so an in-flight cycle stops at
        # the next per-device boundary; the queued request_stop is a
        # belt-and-suspenders for the idle-worker case.
        self._worker._stop = True
        self._stopRequested.emit()
        if self._array_thread.isRunning():
            self._array_thread.quit()
            if not self._array_thread.wait(_THREAD_JOIN_MS):
                _log.warning("hvsr_array_thread_join_timeout", measurement=m.measurement_id)
        self._set_state(m, HvsrState.IDLE)
        stopped_id = m.measurement_id
        self._measurement = None
        self.arrayMeasurementStopped.emit(stopped_id)

    def active_measurement(self) -> ArrayHvsrSummary | None:
        """Snapshot of the current measurement for the UI, or ``None``."""
        m = self._measurement
        return None if m is None else self._summary(m)

    def shutdown(self) -> None:
        """Tear down for app exit — stop the measurement and the thread."""
        self.stop_measurement()
        if self._array_thread.isRunning():
            self._array_thread.quit()
            self._array_thread.wait(_THREAD_JOIN_MS)

    # ------------------------------------------------------------------
    # Internal — live accumulation
    # ------------------------------------------------------------------
    @Slot()
    def _tick(self) -> None:
        """Live pull tick — best-effort, never blocks the data path (rule 11).

        Each device is captured independently: one not-ready device (still
        buffering, dropped out, reconnecting) contributes nothing this tick
        while the others proceed — the per-device-independent-windows
        decision in action.
        """
        m = self._measurement
        if m is None or not m.live or m.state not in (HvsrState.ACCUMULATING, HvsrState.COMPUTING):
            return
        any_added = False
        for station in m.stations.values():
            captured = capture_disjoint_window(
                self._engine,
                station.device,
                station.group,
                m.settings.window_length_s,
                station.last_window_end,
                log_key=f"{m.measurement_id}/{station.device}",
            )
            if captured is None:
                continue
            window, station.last_window_end = captured
            try:
                station.accumulator.add_window(*window)
            except Exception as exc:  # inconsistent window — drop it, keep running
                _log.warning(
                    "hvsr_array_add_window_failed",
                    measurement=m.measurement_id,
                    device=station.device,
                    error=str(exc),
                )
                continue
            any_added = True
        if not any_added:
            return
        self.arrayWindowCounts.emit(self._window_counts(m))
        self._request_recompute(m, force=False)

    def _request_recompute(self, m: _ArrayMeasurement, *, force: bool) -> None:
        """Dispatch ONE cycle over the eligible devices, or skip (rule 11)."""
        threshold = 1 if force else _MIN_WINDOWS
        eligible = [s for s in m.stations.values() if s.accumulator.n_windows >= threshold]
        if not eligible:
            return
        if m.pending >= 1:
            m.skipped += 1
            now = time.monotonic()
            if now - m.last_skip_log >= _SKIP_LOG_INTERVAL_S:
                _log.warning(
                    "hvsr_array_recompute_skipped",
                    measurement=m.measurement_id,
                    skipped_total=m.skipped,
                )
                m.last_skip_log = now
            self.arrayBackpressure.emit(m.measurement_id, m.skipped)
            return
        m.pending += 1
        self._set_state(m, HvsrState.COMPUTING)
        self._computeRequested.emit(
            _ArrayComputeRequest(
                m.measurement_id,
                tuple((s.device, s.accumulator.snapshot()) for s in eligible),
            )
        )

    @Slot(object)
    def _on_computed(self, payload: object) -> None:
        if not isinstance(payload, _ArrayComputeResult):
            return
        m = self._measurement
        if m is None or m.measurement_id != payload.measurement_id:
            return  # stale cycle from a stopped measurement
        m.pending = max(0, m.pending - 1)
        m.last_compute_ms = payload.elapsed_ms
        m.last_error = "; ".join(f"{d}: {e}" for d, e in sorted(payload.errors.items()))
        for device, result in payload.results.items():
            station = m.stations.get(device)
            if station is not None:
                station.n_windows_valid = result.n_windows_valid
        self.arrayWindowCounts.emit(self._window_counts(m))
        self.arrayUpdated.emit(
            ArrayHvsrResult(
                measurement_id=m.measurement_id,
                devices=tuple(m.stations),
                results=payload.results,
                errors=payload.errors,
                geometry=m.geometry,
                settings=m.settings,
                provenance="live" if m.live else "archive",
                elapsed_ms=payload.elapsed_ms,
            )
        )
        if not m.live:
            # Stage-D forward reference: today the only constructor path is
            # start_measurement (live=True), so this branch is unreachable
            # until the archive mode lands. Archive runs are one-shot: the
            # single cycle is done; stay selectable for manual override
            # (which re-dispatches), but the measurement is otherwise idle.
            self._set_state(m, HvsrState.IDLE)
        elif m.state is HvsrState.COMPUTING:
            self._set_state(m, HvsrState.ACCUMULATING)

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------
    def _window_counts(self, m: _ArrayMeasurement) -> dict[str, tuple[int, int]]:
        return {
            device: (station.n_windows_valid, station.accumulator.n_windows)
            for device, station in m.stations.items()
        }

    def _set_state(self, m: _ArrayMeasurement, state: HvsrState) -> None:
        m.state = state
        self.arrayStateChanged.emit(m.measurement_id, state.value)

    def _summary(self, m: _ArrayMeasurement) -> ArrayHvsrSummary:
        return ArrayHvsrSummary(
            measurement_id=m.measurement_id,
            devices=tuple(m.stations),
            group_by_device={d: dict(s.group) for d, s in m.stations.items()},
            live=m.live,
            state=m.state,
            window_counts=self._window_counts(m),
            last_compute_ms=m.last_compute_ms,
            same_response_by_device={d: s.same_response for d, s in m.stations.items()},
            last_error=m.last_error,
        )
