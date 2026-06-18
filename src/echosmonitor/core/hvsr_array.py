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
the data path). Stop joins the thread with a bounded wait (rule 7). Archive
runs dispatch ONE slice+compute cycle to the same worker (the N-device
archive read happens off the GUI thread — M6 follow-up of auditor F1); the
no-data outcome is announced asynchronously via ``arrayArchiveNoData``
together with the searched archive roots.

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

import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from echosmonitor.core import hvsr_compute
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
    from echosmonitor.core.hvsr_compute import HvsrComputeClient
    from echosmonitor.core.response import ResponseProvider
    from echosmonitor.core.streaming_engine import StreamingEngine
    from echosmonitor.storage.archive_reader import ArchiveReader

_log = structlog.get_logger(__name__)

# Bounded join wait on stop (rule 7) — generous: after a synchronous stop
# the residual uninterruptible work is ONE device's compute (the stop flag
# is checked between devices), and that one compute may be the first of a
# run (numba JIT, several seconds). The archive slice phase polls the same
# flag between component reads and window steps (M6), so its residual unit
# is one component's day-scan-capped read. Never an unbounded join; a join
# that still times out poisons the thread and the next boot rebuilds.
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
class _ArraySliceRequest:
    """Engine → worker (type-erased): one-shot archive slice + compute (M6).

    ``jobs`` carries the stations' OWN accumulators — the worker owns them
    from dispatch until its result/no-data lands back on the GUI thread
    (the engine never reads a worker-owned accumulator; window counts come
    from engine-side totals).
    """

    measurement_id: str
    # (device, reader, group, accumulator) per device that has a reader.
    jobs: tuple[tuple[str, ArchiveReader, dict[str, str], HvsrAccumulator], ...]
    t_start: UTCDateTime
    t_end: UTCDateTime
    settings: HvsrSettings


@dataclass(slots=True)
class _ArrayComputeResult:
    """Worker → engine (type-erased through the Qt signal)."""

    measurement_id: str
    results: dict[str, HvsrResult]
    errors: dict[str, str]
    elapsed_ms: float
    # Archive cycles only: sliced-window totals per device (None for live —
    # live totals are counted engine-side as _tick adds windows).
    window_totals: dict[str, int] | None = None


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
    # Engine-side window total: incremented by _tick (live) or set from the
    # archive cycle's window_totals. The UI counts read THIS, never the
    # accumulator — during an archive cycle the accumulator is owned by the
    # worker thread (M6).
    n_windows_total: int = 0


@dataclass(slots=True)
class _ArrayMeasurement:
    """Engine-side bookkeeping for the single active array measurement."""

    measurement_id: str
    stations: dict[str, _Station]  # insertion order = start order
    settings: HvsrSettings
    geometry: StationGeometry
    live: bool
    # Archive runs: unique reader roots, for the no-data message (M6).
    searched_roots: tuple[str, ...] = ()
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
    # An archive cycle found NO gap-free 3C window on any device (M6).
    sliceEmpty = Signal(str)  # measurement_id  # noqa: N815

    def __init__(self, client: HvsrComputeClient) -> None:
        super().__init__()
        # The per-device hvsrpy computes run through this client — in
        # production a subprocess (GIL-free), so a slow N-device cycle never
        # freezes the GUI/engine or the SeedLink worker (rule 1 / rule 10).
        self._client = client
        self._stop = False
        # Latest-wins token (skill §2): the engine writes the live
        # measurement id GIL-atomically; a stale queued compute (its posted
        # event can survive quit() and dispatch on the next thread start)
        # dies at the first check instead of burning a full N-device cycle
        # ahead of the new run's first honest result.
        self._active_id = ""

    def _superseded(self, measurement_id: str) -> bool:
        return self._stop or measurement_id != self._active_id

    def _run_cycle(
        self, measurement_id: str, accumulators: tuple[tuple[str, HvsrAccumulator], ...]
    ) -> tuple[dict[str, HvsrResult], dict[str, str]] | None:
        """Serial per-device computes; ``None`` if stopped/superseded mid-cycle."""
        results: dict[str, HvsrResult] = {}
        errors: dict[str, str] = {}
        for device, accumulator in accumulators:
            if self._superseded(measurement_id):
                return None  # do not announce
            try:
                result = self._client.compute(
                    accumulator,
                    should_stop=lambda: self._superseded(measurement_id),
                )
            except Exception as exc:  # never crash the worker thread
                _log.error(
                    "hvsr_array_compute_failed",
                    measurement=measurement_id,
                    device=device,
                    error=str(exc),
                )
                errors[device] = str(exc)
                continue
            if result is None:
                return None  # cancelled mid-device (subprocess killed) — do not announce
            results[device] = result
        if self._superseded(measurement_id):
            return None  # stopped/superseded after the last device — do not announce
        return results, errors

    @Slot(object)
    def compute(self, request: object) -> None:
        if not isinstance(request, _ArrayComputeRequest):  # defensive (type-erased)
            return
        t0 = time.monotonic()
        cycle = self._run_cycle(request.measurement_id, request.accumulators)
        if cycle is None:
            return
        results, errors = cycle
        elapsed = (time.monotonic() - t0) * 1000.0
        self.computed.emit(_ArrayComputeResult(request.measurement_id, results, errors, elapsed))

    @Slot(object)
    def slice_and_compute(self, request: object) -> None:
        """One-shot archive cycle: slice each device's windows from its reader,
        fill the stations' accumulators (worker-owned for the duration), then
        run the serial compute cycle. The N-devices-by-range read budget lives
        HERE, off the GUI thread (M6 follow-up of auditor F1). The stop flag
        is observed between devices, between component reads and at every
        window step (rule 7 — via ``should_stop``); the residual
        uninterruptible unit is one component's day-scan-capped read. A
        device whose slice raises is reported in the result's ``errors`` —
        the others proceed (per-device independence) — and when EVERY device
        is empty because of slice errors the cycle still announces the
        errors, never a misleading "no data".
        """
        if not isinstance(request, _ArraySliceRequest):  # defensive (type-erased)
            return
        try:
            self._slice_and_compute(request)
        except Exception as exc:  # never strand the measurement at pending=1
            _log.error(
                "hvsr_array_archive_cycle_failed",
                measurement=request.measurement_id,
                error=str(exc),
            )
            if not self._superseded(request.measurement_id):
                self.computed.emit(
                    _ArrayComputeResult(
                        request.measurement_id, {}, {"cycle": str(exc)}, 0.0, {}
                    )
                )

    def _slice_and_compute(self, request: _ArraySliceRequest) -> None:
        # Deferred import: tests monkeypatch the function on core.hvsr, and
        # only a call-time lookup sees the patched attribute.
        from echosmonitor.core.hvsr import slice_archive_windows

        t0 = time.monotonic()
        slice_errors: dict[str, str] = {}
        for device, reader, group, accumulator in request.jobs:
            if self._superseded(request.measurement_id):
                return  # stopped/superseded mid-slice — do not announce
            try:
                windows = slice_archive_windows(
                    reader,
                    device,
                    group,
                    request.t_start,
                    request.t_end,
                    request.settings,
                    should_stop=lambda: self._superseded(request.measurement_id),
                )
            except Exception as exc:  # never crash the worker thread
                _log.error(
                    "hvsr_array_archive_slice_failed",
                    measurement=request.measurement_id,
                    device=device,
                    error=str(exc),
                )
                slice_errors[device] = str(exc)
                continue
            if not windows:
                if self._superseded(request.measurement_id):
                    return  # aborted mid-slice — not a real "no windows"
                _log.info(
                    "hvsr_array_archive_no_windows",
                    measurement=request.measurement_id,
                    device=device,
                )
                continue
            for window in windows:
                try:
                    accumulator.add_window(*window)
                except Exception as exc:
                    _log.warning(
                        "hvsr_array_archive_add_window_failed",
                        measurement=request.measurement_id,
                        device=device,
                        error=str(exc),
                    )
        totals = {device: acc.n_windows for device, _r, _g, acc in request.jobs}
        _log.info(
            "hvsr_array_archive_sliced",
            measurement=request.measurement_id,
            elapsed_ms=round((time.monotonic() - t0) * 1000.0, 1),
            windows={device: n for device, n in totals.items() if n},
            devices=len(request.jobs),
        )
        if self._superseded(request.measurement_id):
            return
        if not any(totals.values()):
            if slice_errors:
                # Nothing sliced because the READS failed: announce the
                # per-device errors (empty-results cycle), not "no data" —
                # the user must see the real cause, not the log.
                self.computed.emit(
                    _ArrayComputeResult(
                        request.measurement_id,
                        {},
                        slice_errors,
                        (time.monotonic() - t0) * 1000.0,
                        totals,
                    )
                )
                return
            # Genuinely empty: no device has a single gap-free 3C window —
            # the engine turns this into the public arrayArchiveNoData with
            # the searched roots.
            self.sliceEmpty.emit(request.measurement_id)
            return
        eligible = tuple(
            (device, acc) for device, _r, _g, acc in request.jobs if acc.n_windows >= 1
        )
        cycle = self._run_cycle(request.measurement_id, eligible)
        if cycle is None:
            return
        results, errors = cycle
        errors.update(slice_errors)
        elapsed = (time.monotonic() - t0) * 1000.0
        self.computed.emit(
            _ArrayComputeResult(request.measurement_id, results, errors, elapsed, totals)
        )

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
    # Live accumulation progress: id, {device: (n_valid, n_total)}.
    arrayWindowCounts = Signal(str, object)  # noqa: N815
    arrayStateChanged = Signal(str, str)  # id, state.value  # noqa: N815
    arrayBackpressure = Signal(str, int)  # id, skipped  # noqa: N815
    # An archive run found NO gap-free 3C window on any device: id +
    # tuple[str, ...] of the searched archive roots (M6 — the message must
    # say WHERE it looked). Terminal for that run: the measurement is
    # discarded without an arrayMeasurementStopped (it never produced
    # anything to stop).
    arrayArchiveNoData = Signal(str, object)  # noqa: N815

    # Engine → worker (QueuedConnection → runs on the array thread).
    _computeRequested = Signal(object)  # _ArrayComputeRequest  # noqa: N815
    _sliceRequested = Signal(object)  # _ArraySliceRequest  # noqa: N815
    _stopRequested = Signal()  # noqa: N815
    _clearStopRequested = Signal()  # noqa: N815

    def __init__(
        self,
        engine: StreamingEngine,
        provider: ResponseProvider | None,
        parent: QObject | None = None,
        *,
        compute_client_factory: Callable[[], HvsrComputeClient] | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._provider = provider
        # One fresh compute client per worker build (the subprocess is owned
        # by that worker thread). Default = the production subprocess client;
        # the test suite points the factory at the in-process client.
        self._client_factory = compute_client_factory or hvsr_compute.make_default_compute_client
        self._measurement: _ArrayMeasurement | None = None
        self._seq = 0
        # Set when a stop's bounded join timed out: the thread is still
        # finishing an uninterruptible compute with a quit() pending, and
        # once that slot returns exec() exits DISCARDING queued events (the
        # recorded postmortem race) — a new measurement dispatched into it
        # would hang forever. _boot_worker rebuilds in that case.
        self._join_timed_out = False
        # Abandoned (worker, thread) pairs kept alive until shutdown so a
        # still-running QThread object is never garbage-collected.
        self._abandoned: list[tuple[_ArrayWorker, QThread]] = []

        self._worker, self._array_thread = self._make_worker()

        # GUI-thread pull timer (live mode). Interval set per measurement.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _make_worker(self) -> tuple[_ArrayWorker, QThread]:
        worker = _ArrayWorker(self._client_factory())
        thread = QThread()
        thread.setObjectName("hvsr-array-worker")
        worker.moveToThread(thread)
        # Worker → engine: QueuedConnection so the slot runs on the GUI thread.
        worker.computed.connect(self._on_computed, Qt.ConnectionType.QueuedConnection)
        worker.sliceEmpty.connect(self._on_slice_empty, Qt.ConnectionType.QueuedConnection)
        # Engine → worker: QueuedConnection so the slot body runs on the worker.
        self._computeRequested.connect(worker.compute, Qt.ConnectionType.QueuedConnection)
        self._sliceRequested.connect(worker.slice_and_compute, Qt.ConnectionType.QueuedConnection)
        self._stopRequested.connect(worker.request_stop, Qt.ConnectionType.QueuedConnection)
        self._clearStopRequested.connect(worker.clear_stop, Qt.ConnectionType.QueuedConnection)
        return worker, thread

    def _disconnect_worker(self, worker: _ArrayWorker) -> None:
        """Sever an abandoned worker so it can never announce or receive."""
        worker._stop = True
        worker._active_id = ""
        for signal, slot in (
            (worker.computed, self._on_computed),
            (worker.sliceEmpty, self._on_slice_empty),
            (self._computeRequested, worker.compute),
            (self._sliceRequested, worker.slice_and_compute),
            (self._stopRequested, worker.request_stop),
            (self._clearStopRequested, worker.clear_stop),
        ):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)

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
        self._validate_selection(devices)
        self.stop_measurement()
        self._seq += 1
        measurement_id = f"hvsr-array-{self._seq}"
        stations = self._build_stations(devices, settings, UTCDateTime(), provenance="live")
        m = _ArrayMeasurement(
            measurement_id=measurement_id,
            stations=stations,
            settings=settings,
            geometry=geometry,
            live=True,
        )
        self._measurement = m
        self._boot_worker(measurement_id)
        self._set_state(m, HvsrState.ACCUMULATING)
        self.arrayMeasurementStarted.emit(measurement_id, self._summary(m))
        # Tick fast enough to notice a full fresh window promptly; the capture
        # cadence itself is gated on data availability, not the timer rate.
        interval_ms = max(100, int(settings.window_length_s / 4.0 * 1000.0))
        self._timer.setInterval(interval_ms)
        self._timer.start()
        return measurement_id

    def start_archive_measurement(
        self,
        devices: Mapping[str, Mapping[str, str]],
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        settings: HvsrSettings,
        geometry: StationGeometry,
        readers: Mapping[str, ArchiveReader],
    ) -> str:
        """Run the array analysis over an archived ``[t_start, t_end]`` (M5-D).

        The N-device slicing read runs ON THE WORKER thread (M6 follow-up
        of auditor F1): this returns the measurement id immediately and
        the outcome arrives asynchronously — ONE ``arrayUpdated`` cycle
        when any device holds a gap-free 3C window, or
        ``arrayArchiveNoData(id, searched_roots)`` when none does (the
        measurement is then discarded). Windows stay per-device independent
        (a device with gaps just contributes fewer windows; a device whose
        slice fails is reported in the result's ``errors``). ``readers``
        maps each device to its (possibly shared, session-rooted — rule 14)
        :class:`ArchiveReader`; a device without a reader stays selected
        (it appears on the comparison page as "no result") but never enters
        the cycle. Returns ``""`` only when NO device has a reader. The
        stations' accumulators are owned by the worker until the cycle
        lands — the engine reads window counts from engine-side totals,
        never from a worker-owned accumulator.
        """
        self._validate_selection(devices)
        self.stop_measurement()
        sliceable = [device for device in devices if readers.get(device) is not None]
        for device in devices:
            if readers.get(device) is None:
                _log.warning("hvsr_array_archive_no_reader", device=device)
        if not sliceable:
            return ""
        self._seq += 1
        measurement_id = f"hvsr-array-{self._seq}"
        stations = self._build_stations(devices, settings, t_start, provenance="archive")
        # Unique reader roots, order-preserving — carried for the no-data
        # message (M6: say WHERE we looked). Duck-typed: a reader without a
        # ``root`` simply contributes nothing to the message.
        searched: list[str] = []
        for device in sliceable:
            root = getattr(readers[device], "root", None)
            if root is not None and str(root) not in searched:
                searched.append(str(root))
        m = _ArrayMeasurement(
            measurement_id=measurement_id,
            stations=stations,
            settings=settings,
            geometry=geometry,
            live=False,
            searched_roots=tuple(searched),
        )
        # The slice+compute cycle is in flight from the start: pending=1
        # keeps every accumulator read (eligibility, snapshot) out of
        # _request_recompute until the worker hands ownership back.
        m.pending = 1
        self._measurement = m
        self._boot_worker(measurement_id)
        self._set_state(m, HvsrState.ACCUMULATING)
        self.arrayMeasurementStarted.emit(measurement_id, self._summary(m))
        self._sliceRequested.emit(
            _ArraySliceRequest(
                measurement_id=measurement_id,
                jobs=tuple(
                    (device, readers[device], dict(devices[device]), stations[device].accumulator)
                    for device in sliceable
                ),
                t_start=t_start,
                t_end=t_end,
                settings=settings,
            )
        )
        return measurement_id

    def _validate_selection(self, devices: Mapping[str, Mapping[str, str]]) -> None:
        if not devices:
            raise ValueError("array HVSR needs at least one device")
        for device, group in devices.items():
            # Exactly Z/N/E: an extra component would make the shared capture
            # primitive (len==3 gate) silently never ready — reject loudly.
            if set(group) != {"Z", "N", "E"}:
                raise ValueError(
                    f"device {device!r} group must be exactly Z/N/E, got {sorted(group)}"
                )

    def _build_stations(
        self,
        devices: Mapping[str, Mapping[str, str]],
        settings: HvsrSettings,
        t: UTCDateTime,
        *,
        provenance: Provenance,
    ) -> dict[str, _Station]:
        stations: dict[str, _Station] = {}
        for device, group in devices.items():
            # The counts-honesty layer runs PER DEVICE (skill hvsr-array);
            # each verdict rides that device's HvsrResult verbatim.
            same_response, detail = responses_identical(self._provider, device, dict(group), t)
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
                    provenance=provenance,
                ),
                same_response=same_response,
            )
        return stations

    def _boot_worker(self, measurement_id: str) -> None:
        if self._join_timed_out:
            # The prior stop's join timed out: the thread is (probably)
            # still inside an uninterruptible compute with a quit() pending.
            # Dispatching into it would be silently discarded when exec()
            # exits (auditor F2). One brief second chance, else abandon the
            # poisoned pair and rebuild fresh.
            if self._array_thread.isRunning() and not self._array_thread.wait(100):
                self._disconnect_worker(self._worker)
                self._abandoned.append((self._worker, self._array_thread))
                _log.warning(
                    "hvsr_array_worker_rebuilt_after_join_timeout",
                    abandoned=len(self._abandoned),
                )
                self._worker, self._array_thread = self._make_worker()
            self._join_timed_out = False
        if not self._array_thread.isRunning():
            self._array_thread.start()
        self._clearStopRequested.emit()  # reset a stop flag from a prior run
        self._worker._stop = False
        self._worker._active_id = measurement_id  # latest-wins token (skill §2)

    def set_window_override(
        self, measurement_id: str, device: str, window_id: int, accepted: bool
    ) -> None:
        """Manually include/exclude one device's window; recomputes immediately."""
        m = self._measurement
        if m is None or m.measurement_id != measurement_id:
            return
        if not m.live and m.pending >= 1:
            # The archive slice+compute cycle is in flight: the accumulators
            # are WORKER-owned (M6) and a GUI-thread override write would
            # race add_window/compute. Unreachable from the UI today (window
            # ids only exist after the first arrayUpdated) — guard the
            # public API anyway.
            _log.warning(
                "hvsr_array_override_ignored_cycle_inflight",
                measurement=measurement_id,
                device=device,
            )
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
        self._worker._active_id = ""  # supersede any queued/in-flight cycle
        self._stopRequested.emit()
        if self._array_thread.isRunning():
            self._array_thread.quit()
            if not self._array_thread.wait(_THREAD_JOIN_MS):
                _log.warning("hvsr_array_thread_join_timeout", measurement=m.measurement_id)
                self._join_timed_out = True  # next boot rebuilds if still stuck
        self._set_state(m, HvsrState.IDLE)
        stopped_id = m.measurement_id
        self._measurement = None
        self.arrayMeasurementStopped.emit(stopped_id)

    def active_measurement(self) -> ArrayHvsrSummary | None:
        """Snapshot of the current measurement for the UI, or ``None``."""
        m = self._measurement
        return None if m is None else self._summary(m)

    def shutdown(self) -> None:
        """Tear down for app exit — stop the measurement and every thread."""
        self.stop_measurement()
        joined = True
        if self._array_thread.isRunning():
            self._array_thread.quit()
            joined = self._array_thread.wait(_THREAD_JOIN_MS)
        # Close the compute client (kill the warm subprocess, bounded) ONLY
        # once the worker thread has joined — close() takes the client lock a
        # wedged in-flight compute still holds, so closing a non-joined worker
        # would block. A still-stuck thread's child is daemonic (OS-reaped at
        # exit); skipping its close trades a rare orphan for a non-blocking
        # teardown.
        if joined:
            with contextlib.suppress(Exception):
                self._worker._client.close()
        # Drain any threads abandoned by a poisoned-thread rebuild: their
        # severed workers can no longer announce, but a still-running
        # QThread must be joined (bounded) before the process exits. A pair
        # whose join times out stays REFERENCED — dropping the last Python
        # reference to a running QThread aborts (destroyed-while-running).
        still_running: list[tuple[_ArrayWorker, QThread]] = []
        for worker, thread in self._abandoned:
            if thread.isRunning():
                thread.quit()
                if not thread.wait(_THREAD_JOIN_MS):
                    _log.warning("hvsr_array_abandoned_thread_join_timeout")
                    still_running.append((worker, thread))
                    continue
            # Thread confirmed stopped — its subprocess can be closed safely.
            with contextlib.suppress(Exception):
                worker._client.close()
        self._abandoned = still_running

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
            station.n_windows_total = station.accumulator.n_windows
            any_added = True
        if not any_added:
            return
        self.arrayWindowCounts.emit(m.measurement_id, self._window_counts(m))
        self._request_recompute(m, force=False)

    def _request_recompute(self, m: _ArrayMeasurement, *, force: bool) -> None:
        """Dispatch ONE cycle over the eligible devices, or skip (rule 11).

        The pending gate runs FIRST: while a cycle is in flight the
        accumulators may be worker-owned (the archive slice), so they must
        not be read — let alone snapshotted — until it lands.
        """
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
        threshold = 1 if force else _MIN_WINDOWS
        eligible = [s for s in m.stations.values() if s.accumulator.n_windows >= threshold]
        if not eligible:
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
        if payload.window_totals is not None:
            # Archive cycle: the worker owned the accumulators while it
            # sliced; its totals are the engine-side truth from here on.
            for device, total in payload.window_totals.items():
                station = m.stations.get(device)
                if station is not None:
                    station.n_windows_total = total
        for device, result in payload.results.items():
            station = m.stations.get(device)
            if station is not None:
                station.n_windows_valid = result.n_windows_valid
        self.arrayWindowCounts.emit(m.measurement_id, self._window_counts(m))
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
            # Archive runs are one-shot (M5-D): the single cycle is done;
            # stay selectable for manual override (which re-dispatches),
            # but the measurement is otherwise idle.
            self._set_state(m, HvsrState.IDLE)
        elif m.state is HvsrState.COMPUTING:
            self._set_state(m, HvsrState.ACCUMULATING)

    @Slot(str)
    def _on_slice_empty(self, measurement_id: str) -> None:
        """The archive cycle found NO gap-free 3C window on any device (M6).

        Terminal for that run: the measurement is discarded and
        ``arrayArchiveNoData`` (with the searched roots) is the one
        announcement — no ``arrayMeasurementStopped``, there is nothing
        running to stop.
        """
        m = self._measurement
        if m is None or m.measurement_id != measurement_id:
            return  # stale: stopped/superseded while the slice ran
        self._set_state(m, HvsrState.IDLE)
        self._measurement = None
        self.arrayArchiveNoData.emit(measurement_id, m.searched_roots)

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------
    def _window_counts(self, m: _ArrayMeasurement) -> dict[str, tuple[int, int]]:
        # Engine-side totals, never the accumulator: during an archive
        # cycle the accumulators are owned by the worker thread (M6).
        return {
            device: (station.n_windows_valid, station.n_windows_total)
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
