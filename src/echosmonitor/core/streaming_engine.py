"""StreamingEngine — single source of truth for live SeedLink streams.

Owns one ``(QThread, SeedLinkWorker)`` pair per device and per-stream
``RingBuffer`` + ``_StreamCoalescer`` instances. Widgets subscribe to
engine signals; nothing else opens SeedLink connections.

Devices with a non-empty ``dsp_chain`` configuration get a per-stream
``DspChain`` installed on the dedicated DSP ``QThread``. The DSP work
runs off both the network thread and the GUI thread; per-stream
bounded deques apply drop-oldest backpressure (CLAUDE.md rule 5).
The ARCHIVE path is the exception by design (M6.5-A): recorded
packets are posted straight to the storage thread and are never
dropped engine-side — backpressure there is an observable in-flight
gauge, not a loss point.

Multi-device isolation (M3 part 1)
----------------------------------
All engine-internal per-stream state is namespaced by device name. The
key is :func:`echosmonitor.core.models.device_stream_key`
(``f"{device}/{nslc}"``). Two devices publishing the same NSLC keep
independent ring buffers, coalescers, drop counters, and DSP chains
— their data never cross-contaminates and a failure on one device
cannot affect the other.

Every engine signal that previously carried a single ``nslc`` argument
now carries ``(device_name, nslc, ...)``. This is a deliberate breaking
change confined to one PR: there is no compatibility shim.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import structlog
import yaml
from obspy import UTCDateTime as _UTCDateTime
from PySide6.QtCore import (
    QMetaObject,
    QObject,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)

from echosmonitor.core.collisions import find_nslc_collisions
from echosmonitor.core.config_diff import diff_devices
from echosmonitor.core.dsp_router import _DspRouter
from echosmonitor.core.models import (
    DEVICE_KEY_SEP,
    AcquisitionState,
    ConnState,
    Detection,
    DeviceStatus,
    StreamID,
    StreamSelector,
    Trigger,
    WorkerDiagnostics,
    device_stream_key,
)
from echosmonitor.core.psd_worker import PsdWorker
from echosmonitor.core.ring_buffer import RingBuffer
from echosmonitor.core.seedlink_worker import SeedLinkWorker
from echosmonitor.core.session import SessionInfo, sanitize_project_name
from echosmonitor.core.spectrogram_router import _SpectrogramRouter
from echosmonitor.dsp.factory import build_chain
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.gap_detector import GapDetector
from echosmonitor.storage.mseed_writer import MseedWriter
from echosmonitor.storage.sessions import ensure_project_root

if TYPE_CHECKING:
    from collections.abc import Sequence

    from obspy.core.trace import Trace
    from obspy.core.utcdatetime import UTCDateTime

    from echosmonitor.config import DeviceConfig, DspStageConfig, RootConfig
    from echosmonitor.core.config_store import ConfigStore
    from echosmonitor.core.exceptions import ConfigError as _ConfigError  # noqa: F401
    from echosmonitor.storage.gap_detector import GapEvent

_log = structlog.get_logger(__name__)

_THREAD_JOIN_MS = 2000
_DROP_LOG_INTERVAL_S = 5.0
# Per-stream bounded queue holds at most this many packets before we
# start dropping oldest. Sized as `refresh_hz * 5` — five engine ticks
# of headroom is enough for a transient hiccup without unbounded growth.
#
# NOTE: this packet-count factor only bounds the queue sanely when the
# packet rate tracks the refresh rate. At high fs with small packets the
# packet rate vastly exceeds refresh_hz, so a flat `refresh_hz * 5` is
# thin headroom (it represents far less than one second of data). The
# fs-aware path below (`_chain_max_q_for_fs`) supersedes it per-stream.
_CHAIN_QUEUE_FACTOR = 5
# Seconds of wall-clock headroom the per-stream DSP queue must hold before
# dropping oldest (CLAUDE.md rule 5: bounded, drop-oldest, logged). At any
# fs the queue is sized so a transient flush stall up to this long never
# loses a sample detection should see; only true SUSTAINED overflow drops.
# Now that render is decoupled from the flush tick (rule 11), the only
# thing that can fall behind is genuine compute saturation, not GUI jank.
_CHAIN_QUEUE_SECONDS = 5.0
# Hard ceiling on the per-stream DSP queue size in SAMPLES, so a
# pathological high-fs stream cannot allocate without bound. At 4 kSPS,
# 5 s of headroom is 20000 samples — this ceiling matches that and stays
# bounded for anything faster (rule 5).
_CHAIN_QUEUE_MAX_SAMPLES = 20000
# float32 = 4 bytes per sample; used for the ring-buffer memory-cost log.
_BYTES_PER_SAMPLE = 4

# Stall watchdog (Bug 2). The expected packet cadence is derived from the
# stream itself — a device emits one ~``npts``-sample packet every ``npts/fs``
# seconds of data — so the stall threshold ADAPTS to the sampling rate instead
# of a fixed timeout. A stream is flagged stalled when no packet arrives for
# ``_STALL_FACTOR`` times that expected interval, clamped to [min, max]. The
# clamp keeps a very fast stream from false-flagging on one late packet and a
# very slow stream from going unmonitored for minutes; the max stays below
# obspy's 120 s ``netto`` so the app notices a stall before the socket does.
_STALL_FACTOR = 12.0
_STALL_MIN_S = 5.0
_STALL_MAX_S = 60.0
# The watchdog runs on the flush tick; throttle the scan to ~1 Hz.
_STALL_CHECK_INTERVAL_S = 1.0


class _DeviceBridge(QObject):
    """Forwards one worker's signals into the engine, attaching device name.

    Lives in the engine's (GUI) thread; receives queued signals from the
    worker thread, then re-emits as DirectConnection in the same thread.
    """

    packetReceivedNamed = Signal(str, object)  # noqa: N815
    stateChangedNamed = Signal(str, int, str)  # noqa: N815
    errorOccurredNamed = Signal(str, str)  # noqa: N815
    statsUpdatedNamed = Signal(str, int, int)  # noqa: N815
    diagnosticsUpdatedNamed = Signal(str, object)  # name, WorkerDiagnostics  # noqa: N815

    def __init__(self, name: str, worker: SeedLinkWorker, parent: QObject) -> None:
        super().__init__(parent)
        self._name = name
        worker.packetReceived.connect(self._fwd_packet, type=Qt.ConnectionType.QueuedConnection)
        worker.stateChanged.connect(self._fwd_state, type=Qt.ConnectionType.QueuedConnection)
        worker.errorOccurred.connect(self._fwd_error, type=Qt.ConnectionType.QueuedConnection)
        worker.statsUpdated.connect(self._fwd_stats, type=Qt.ConnectionType.QueuedConnection)
        worker.diagnosticsUpdated.connect(
            self._fwd_diagnostics, type=Qt.ConnectionType.QueuedConnection
        )

    @Slot(object)
    def _fwd_packet(self, trace: object) -> None:
        self.packetReceivedNamed.emit(self._name, trace)

    @Slot(int, str)
    def _fwd_state(self, state: int, msg: str) -> None:
        self.stateChangedNamed.emit(self._name, state, msg)

    @Slot(str)
    def _fwd_error(self, msg: str) -> None:
        self.errorOccurredNamed.emit(self._name, msg)

    @Slot(int, int)
    def _fwd_stats(self, packets: int, bytes_: int) -> None:
        self.statsUpdatedNamed.emit(self._name, packets, bytes_)

    @Slot(object)
    def _fwd_diagnostics(self, diag: object) -> None:
        self.diagnosticsUpdatedNamed.emit(self._name, diag)


class _StreamCoalescer(QObject):
    """Per-stream concatenator. Flushed on demand by the engine's shared timer.

    Both ``push()`` and ``flush()`` run on the engine/GUI thread today, so
    the underlying list mutation would be safe even without a lock — but a
    cheap ``threading.Lock`` makes the invariant explicit so a future
    change to the bridge's connection type doesn't quietly turn this into
    a race.

    Carries ``(device_name, nslc)`` in its emitted signal so consumers can
    route to the correct plot when two devices share the same NSLC.
    """

    flushed = Signal(str, str, object)  # device_name, nslc, ndarray

    def __init__(self, device_name: str, nslc: str, parent: QObject) -> None:
        super().__init__(parent)
        self._device_name = device_name
        self._nslc = nslc
        self._buf: list[np.ndarray] = []
        self._lock = threading.Lock()

    def push(self, samples: np.ndarray) -> None:
        with self._lock:
            self._buf.append(samples)

    def flush(self) -> None:
        with self._lock:
            if not self._buf:
                return
            chunks = self._buf
            self._buf = []
        out = np.concatenate(chunks)
        self.flushed.emit(self._device_name, self._nslc, out)


class _ArchiveSender(QObject):
    """Per-device QObject whose ``request`` signal fires the writer slot.

    ``_enqueue_for_archive`` emits ``request`` once per recorded packet,
    directly from ``_on_packet`` on the engine thread (M6.5-A — there is
    no engine-side inbox and no drop point on this seam). The signal is
    connected to the writer's ``write_trace`` slot via
    ``QueuedConnection`` so the work runs on the storage thread without
    the engine having to hold a reference to that thread's event-loop
    machinery.
    """

    request = Signal(str, object)  # nslc, trace


class StreamingEngine(QObject):
    """Owns all live SeedLink connections; one source of truth for streams.

    All per-stream state is keyed by ``device_stream_key(device, nslc)``
    so two devices publishing the same NSLC stay independent. Public
    signals carry ``(device_name, nslc, ...)`` — a stream is identified
    by the pair, never by the NSLC alone.
    """

    # Public signals — all per-stream signals carry (device_name, nslc, ...).
    traceReady = Signal(str, str, object)  # device, nslc, ndarray (coalesced)  # noqa: N815
    processedTraceReady = Signal(str, str, object)  # device, nslc, ndarray (post-DSP)  # noqa: N815
    processedStreamMeta = Signal(str, str, float)  # device, nslc, fs_out  # noqa: N815
    triggerFired = Signal(object, str, str)  # Trigger, device, nslc  # noqa: N815
    # ----- M8 detection signals ---------------------------------------
    # ``detectionRecorded(detection)`` fires AFTER the DAO row is durably
    # committed (CLAUDE.md rule 8 — persisted before announced) for every
    # NEW ``detections`` row: an open trigger's onset (``t_off=None``) or a
    # trigger that opened and closed within one packet (``t_off`` set).
    # ``detectionUpdated(detection)`` fires when a previously-open row is
    # closed in place (its ``t_off`` + final score updated); the table
    # uses it to freeze the ticking "open" duration. Both carry a
    # :class:`core.models.Detection` with ``id`` populated.
    detectionRecorded = Signal(object)  # Detection  # noqa: N815
    detectionUpdated = Signal(object)  # Detection  # noqa: N815
    chainDropped = Signal(str, str, int)  # device, nslc, dropped packet count  # noqa: N815
    newStreamSeen = Signal(str, str)  # device, nslc  # noqa: N815
    streamMeta = Signal(str, str, float, str)  # device, nslc, fs, starttime ISO  # noqa: N815
    deviceStateChanged = Signal(str, int)  # device_name, ConnState int  # noqa: N815
    # Stall watchdog (Bug 2): a CONNECTED device whose stream went silent for
    # longer than its sampling-rate-derived expected cadence (True), or that
    # has since resumed (False). The GUI uses this to resume full-cadence REST
    # polling while a stream is stalled (the slow heartbeat assumes "data
    # proves liveness", which is false on a silent-but-open socket).
    streamStalled = Signal(str, bool)  # device_name, is_stalled  # noqa: N815
    # ----- M2 acquisition lifecycle (rule 13) ---------------------------
    # ``acquisitionStateChanged(device, AcquisitionState int)`` — fired on
    # every user-driven state transition (Idle/Monitoring/Recording) and
    # on the implicit transitions a hot-reload forces (a removed device
    # goes Idle). Mirrors the ``deviceStateChanged`` int-payload pattern;
    # receivers reconstruct via ``AcquisitionState(value)``.
    acquisitionStateChanged = Signal(str, int)  # noqa: N815
    # ``sessionChanged(SessionInfo | None)`` — fired when a recording
    # session starts (payload: frozen SessionInfo), when its membership
    # grows, and when it ends (payload: None). Type-erased object
    # payload; receivers ``isinstance``-guard per rule 4. M2-C's
    # Session toolbar is the intended consumer.
    sessionChanged = Signal(object)  # noqa: N815
    errorOccurred = Signal(str, str)  # device_name, message  # noqa: N815
    # Zero-payload notification: the configured device list mutated; readers
    # re-query ``devices()``. Introduced in M4 stage A so the StationBrowser
    # can wire at construction time; Stage B is the one that emits it (when
    # ConfigStore writes land), so in Stage A this signal is defined but
    # never fires. Adding it now avoids a Stage-B refactor of the browser's
    # signal wiring.
    devicesChanged = Signal()  # noqa: N815

    # ----- M5 archive signals -----------------------------------------
    # ``archiveWriteOk(device, nslc, bytes_written, path, split, encoding)``
    # mirrors :class:`MseedWriter.writeOk` after the engine has folded
    # the bytes/path into ``DeviceStatus``. Subscribers (DevicePanel) use
    # this to refresh stats; widgets that just want counters can read
    # ``device_status()`` once per UI tick instead.
    archiveWriteOk = Signal(str, str, int, object, bool, str)  # noqa: N815
    archiveWriteFailed = Signal(str, str, str)  # noqa: N815
    # ``archiveBackpressure(device, inflight_count)`` — advisory (M6.5-A):
    # NO samples were dropped. Fired at most every
    # ``_DROP_LOG_INTERVAL_S`` seconds per device when the in-flight
    # trace count toward the storage thread exceeds
    # ``archive.queue_max``, i.e. the storage thread is draining slower
    # than packets arrive (replay catch-up or a slow filesystem).
    archiveBackpressure = Signal(str, int)  # noqa: N815

    # ----- M6 PSD signals ---------------------------------------------
    # GUI consumers emit ``psdRequested(device, nslc, seconds)`` to ask
    # for an on-demand Welch PSD; the engine routes that to
    # :class:`PsdWorker` (lives on ``_dsp_thread``), which emits the
    # result via ``psdReady(device, nslc, seconds, freqs, db)``. The
    # ``seconds`` field round-trips so widgets with overlapping
    # requests can drop stale results (latest-result-wins).
    psdRequested = Signal(str, str, float)  # noqa: N815
    psdReady = Signal(str, str, float, object, object)  # noqa: N815

    # ----- M6 spectrogram signals -------------------------------------
    # ``spectrogramColumnReady(device, nslc, column, freqs, t_end)`` — one
    # PSD column per ``RollingSpectrogram`` step. Fired on the engine
    # thread (re-emit of :class:`_SpectrogramRouter.columnReady`).
    # ``column`` is float32 of shape ``(n_freq_bins,)``; ``freqs`` is the
    # bin-centre array (in Hz), of the same length. ``t_end`` is reserved
    # for a future per-column wall-clock end timestamp; in stage 1 it is
    # always ``None`` and consumers MUST fall back to arrival wall time.
    spectrogramColumnReady = Signal(str, str, object, object, object)  # noqa: N815
    # Backpressure signal for the spectrogram router, mirroring
    # ``chainDropped`` in cadence (≤1 emit / 5 s / stream).
    spectrogramDropped = Signal(str, str, int)  # noqa: N815

    # Internal — used to dispatch to the DSP router via QueuedConnection.
    # Q_ARG(object, ...) does not work cleanly across PySide6 versions, so
    # we go through Signal-based wiring which serialises Python objects via
    # the metatype machinery automatically.
    _installChainRequested = Signal(str, str, object)  # device, nslc, chain  # noqa: N815
    _drainRequested = Signal(str, str, object)  # device, nslc, items  # noqa: N815
    _clearChainsRequested = Signal()  # noqa: N815
    # Per-stream chain clear (M4 stage B chain-only hot-reload). The engine
    # emits this for every stream whose device's chain just changed; the
    # router drops the entry from its dict and the next packet re-triggers
    # ``_maybe_install_chain`` with the updated chain config.
    _removeChainRequested = Signal(str, str)  # device, nslc  # noqa: N815
    # Spectrogram-router lifecycle signals. Wired to slots on the
    # _SpectrogramRouter living on _dsp_thread. ``_clearSpectrogramsRequested``
    # uses BlockingQueuedConnection so engine.stop() is sequenced cleanly
    # before _dsp_thread.quit() (POSTMORTEMS 2026-05-10 lesson, mirrored
    # from the M3p1 chains fix).
    _installSpectrogramRequested = Signal(str, str, float)  # device, nslc, fs  # noqa: N815
    _reinstallSpectrogramRequested = Signal(str, str, float)  # noqa: N815
    _removeSpectrogramRequested = Signal(str, str)  # noqa: N815
    _clearSpectrogramsForDeviceRequested = Signal(str)  # noqa: N815
    _clearSpectrogramsRequested = Signal()  # noqa: N815
    _spectrogramFeedRequested = Signal(str, str, object, object)  # noqa: N815

    def __init__(
        self,
        cfg: RootConfig,
        parent: QObject | None = None,
        *,
        store: ConfigStore | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        # Optional ConfigStore (M4 stage B). When provided, the engine
        # subscribes to ``store.configChanged`` and applies a minimal
        # diff on each mutation (added / removed / restart / chain-only).
        # ``None`` keeps the engine purely YAML-driven for tests and the
        # pre-Stage-B path.
        self._store: ConfigStore | None = store
        # The engine's authoritative view of "what devices am I running".
        # Populated by ``start()`` and mutated by ``_on_config_changed``.
        # Distinct from ``self._cfg.devices`` because Stage B mutations
        # advance this list while ``_cfg`` is the original YAML snapshot.
        self._engine_devices: tuple[DeviceConfig, ...] = ()
        self._refresh_hz = int(cfg.ui.refresh_hz)
        self._window_seconds = float(cfg.ui.default_window_seconds)
        # Fallback packet-count bound for streams whose fs we don't yet
        # know (none in practice — every chain installs with a known fs —
        # but keeps ``_enqueue_for_dsp`` total). The authoritative per-
        # stream bound lives in ``_chain_max_q_by_key`` (fs-aware).
        self._chain_max_q = max(1, self._refresh_hz * _CHAIN_QUEUE_FACTOR)
        self._threads: dict[str, QThread] = {}
        self._workers: dict[str, SeedLinkWorker] = {}
        # (QThread, SeedLinkWorker) pairs whose bounded join timed out during
        # teardown — held for the engine's lifetime so the still-running
        # QThread is never garbage-collected (a hard "QThread: Destroyed while
        # thread is still running" abort). This mirrors the HVSR engines'
        # abandoned-thread retention (M6-0 decision log). Seen on macOS, where
        # obspy's blocking recv can outlive the stop() deadline; the thread
        # finishes on its own once the socket finally unwinds.
        self._abandoned_threads: list[tuple[QThread, SeedLinkWorker]] = []
        self._bridges: dict[str, _DeviceBridge] = {}
        # Per-stream state is keyed by ``device_stream_key(device, nslc)``
        # so two devices with overlapping NSLC remain independent. The
        # composite key reads naturally in logs ("iris/IU.ANMO.00.BHZ").
        self._buffers: dict[str, RingBuffer] = {}
        self._coalescers: dict[str, _StreamCoalescer] = {}
        self._status: dict[str, DeviceStatus] = {}
        # Stall watchdog (Bug 2) — engine-thread only (updated in _on_packet /
        # _on_state, scanned in _flush_all), so no lock. Per device: the
        # monotonic time of the last packet, the sampling-rate-derived expected
        # inter-packet interval, and whether it is currently flagged stalled.
        self._last_packet_monotonic: dict[str, float] = {}
        self._expected_packet_interval_s: dict[str, float] = {}
        self._stalled: set[str] = set()
        self._last_stall_scan_s: float = 0.0
        # Reverse lookup: composite key → (device_name, nslc). Used by the
        # bounded-queue snapshot pass to dispatch to the router on the
        # right (device, nslc) pair without parsing the key string.
        self._key_to_pair: dict[str, tuple[str, str]] = {}
        self._stream_fs: dict[str, float] = {}
        # Latest RAW packet end time per stream (UTCDateTime). The DSP
        # path does not carry a chunk end time through its queue, so the
        # processed-spectrogram feed (``_on_processed_for_spec``) anchors
        # its columns to this — the same wall-clock-from-raw mechanism the
        # processed trace uses (``TracePlot._latest_processed_t =
        # _latest_raw_t``). Without it the spectrogram falls back to
        # ``time.time()`` per column and the DateAxisItem collapses to a
        # meaningless sub-second slice (the "20.000…21.799" symptom).
        self._latest_raw_endtime: dict[str, UTCDateTime] = {}
        # Streams (composite key) already warned this session that their
        # LIVE chain contains a linear detrend. The warning is discouraged-
        # but-allowed config advice, so it fires once per (device, nslc)
        # per session — NOT once per stage instance, which re-warned on
        # every chain reinstall (config tweak) and every reconnect. Held on
        # the engine so it survives chain hot-reloads; a fresh engine (app
        # restart) starts empty and reminds once again.
        self._detrend_linear_warned: set[str] = set()
        self._stream_drops_pending: dict[str, int] = {}
        self._stream_drops_last_log: dict[str, float] = {}
        # Streams whose display-history ring has reached capacity at least
        # once. The first saturation is logged at INFO (expected, not loss);
        # subsequent rolling is DEBUG. See :meth:`_note_drop`.
        self._ring_saturated: set[str] = set()
        self._started = False
        # ----- M2 acquisition lifecycle (rule 13) -----------------------
        # Per-device user state. Absent key == IDLE (the launch state).
        # Owned exclusively by the public lifecycle API + the hot-reload
        # diff path; the internal _start_device/_stop_device helpers never
        # touch it (they implement mechanics, not user intent).
        self._acq_state: dict[str, AcquisitionState] = {}
        # ----- M2-B recording session (rule 14) -------------------------
        # The active session, or None. Frozen snapshot — replaced (never
        # mutated) when membership grows; published via active_session()
        # and the sessionChanged signal.
        self._session: SessionInfo | None = None
        # True while start_session/end_session runs its DAO swap. The
        # swap absorbs queued events via processEvents(), which can
        # dispatch reentrant lifecycle calls on this same thread; the
        # flag turns those into a loud SessionError (or a deferred
        # config diff) instead of letting them corrupt the swap
        # (qt-concurrency-auditor F1 on the M2-B diff).
        self._session_transition = False
        # ----- DSP plumbing ----------------------------------------------
        # Set of streams that have a chain installed (built on first packet
        # if the device has a non-empty dsp_chain). Composite keys.
        self._chain_installed: set[str] = set()
        # Per-stream bounded queue of (samples, t_start). Accessed from the
        # network bridge thread (engine thread, via DirectConnection) and
        # the engine thread (timer flush) — guarded by a lock.
        self._chain_queues: dict[str, deque[tuple[np.ndarray, UTCDateTime]]] = {}
        # Per-stream fs-aware bound on the DSP queue length (packets). Set
        # when the chain installs (fs known there) via
        # :meth:`_chain_max_q_for_fs`. Bounded drop-oldest (rule 5).
        self._chain_max_q_by_key: dict[str, int] = {}
        self._chain_lock = threading.Lock()
        self._chain_drops_pending: dict[str, int] = {}
        self._chain_drops_last_log: dict[str, float] = {}
        self._dsp_router = _DspRouter()
        self._dsp_thread = QThread(self)
        self._dsp_thread.setObjectName("dsp-router")
        self._dsp_router.moveToThread(self._dsp_thread)
        # Router → engine: signals carry (device_name, nslc, payload) and
        # connect 1:1 to the engine's public signals.
        self._dsp_router.processedTraceReady.connect(
            self.processedTraceReady,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # Same processed samples are ALSO routed into the spectrogram
        # router, so spectrograms reflect the filtered signal users see.
        # Two separate slots are simpler than fan-out via a single
        # signal: the spectrogram path is a hot loop and dropping it on
        # an unrelated slot's exception would be a cross-cutting bug.
        self._dsp_router.processedTraceReady.connect(
            self._on_processed_for_spec,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # Route through the engine's own slot (not straight to the public
        # signal) so the engine can persist the detection BEFORE re-emitting
        # ``triggerFired`` and the M8 ``detectionRecorded`` / ``detectionUpdated``
        # signals. The slot re-emits ``triggerFired`` first, preserving the
        # M3 contract for existing subscribers (main-window logging).
        self._dsp_router.triggerFired.connect(
            self._on_trigger_fired,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # Engine → router: queued so the slots run on the router's thread.
        self._installChainRequested.connect(
            self._dsp_router.install_chain,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._drainRequested.connect(
            self._dsp_router.drain,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # BlockingQueuedConnection (not QueuedConnection): ``stop()`` emits
        # this signal RIGHT before ``_dsp_thread.quit()``. ``QThread.quit()``
        # interrupts the dispatcher synchronously, so a queued
        # ``clear_chains`` slot can be preempted before it runs - leaving
        # stale chains across a stop()/start() cycle (~5 % flake on a
        # 50-iter loop). Blocking the emit until the slot returns
        # eliminates the race. Safe because the engine thread (signal
        # source) is never the dsp-router thread (signal receiver).
        # See POSTMORTEMS 2026-05-10 entry "Flaky multi-device tests
        # resolved".
        self._clearChainsRequested.connect(
            self._dsp_router.clear_chains,
            type=Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._removeChainRequested.connect(
            self._dsp_router.remove_chain,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # Spectrogram router lives on the same DSP thread (sibling of
        # _DspRouter). STFT-per-stream is cheap; spawning a third QThread
        # for it would only add lifecycle complexity. See plan H1 /
        # decision 10.
        self._spec_router = _SpectrogramRouter()
        self._spec_router.moveToThread(self._dsp_thread)
        # Router → engine: re-emit columns on the engine's public signal.
        self._spec_router.columnReady.connect(
            self.spectrogramColumnReady,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._spec_router.spectrogramDropped.connect(
            self.spectrogramDropped,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # Engine → router: queued so slots run on the router's thread.
        self._installSpectrogramRequested.connect(
            self._spec_router.install_for,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._reinstallSpectrogramRequested.connect(
            self._spec_router.reinstall_for,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._removeSpectrogramRequested.connect(
            self._spec_router.remove_for,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._clearSpectrogramsForDeviceRequested.connect(
            self._spec_router.clear_for_device,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # BlockingQueuedConnection mirrors the chains-clear barrier: when
        # ``stop()`` emits this signal, the router-thread slot must run
        # to completion before ``_dsp_thread.quit()`` interrupts the
        # dispatcher. See POSTMORTEMS 2026-05-10 entry "Flaky multi-device
        # tests resolved" for why a queued emit + quit() is racy by design.
        self._clearSpectrogramsRequested.connect(
            self._spec_router.clear_all,
            type=Qt.ConnectionType.BlockingQueuedConnection,
        )
        self._spectrogramFeedRequested.connect(
            self._spec_router.feed,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # PSD worker also lives on _dsp_thread. Welch is a short
        # one-shot compute; we expose the request as a public engine
        # signal so widgets do not need a reference to the worker
        # object, and re-publish the result on the engine's own thread
        # via a queued connection.
        self._psd_worker = PsdWorker(self)
        self._psd_worker.moveToThread(self._dsp_thread)
        self.psdRequested.connect(
            self._psd_worker.compute,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._psd_worker.psdReady.connect(
            self.psdReady,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # One QTimer fans flush ticks out to every coalescer at refresh_hz.
        # Per-coalescer timers used to be the model; collapsing to a single
        # timer keeps GUI-thread wakeups bounded as the stream count grows.
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(max(1, 1000 // max(1, self._refresh_hz)))
        self._flush_timer.timeout.connect(self._flush_all)
        # Index of dsp_chain configs by device for chain construction.
        self._device_dsp_cfg: dict[str, list[DspStageConfig]] = {
            dev.name: list(dev.dsp_chain) for dev in cfg.devices
        }

        # ----- M5 archive plumbing --------------------------------------
        # The storage QThread is lazily started by
        # :meth:`_setup_archive_writer` when the first device enters the
        # RECORDING state (rule 13 — never from ``archive.enabled``).
        self._archive_thread: QThread | None = None
        self._archive_writers: dict[str, MseedWriter] = {}
        self._archive_senders: dict[str, _ArchiveSender] = {}
        # M6.5-A: packets are posted to the storage thread DIRECTLY from
        # ``_on_packet`` (per-packet QueuedConnection emit) — there is no
        # engine-side inbox and no drop point on this seam. The archive
        # is the SCIENCE sink (the field run lost 33 s of recorded data
        # to the old bounded deque when a replay burst starved the flush
        # tick); recording correctness beats liveness here, and rule 11
        # protects the DISPLAY consumers, not this path. Rule 5's
        # observability is kept via an in-flight gauge: ``sent`` counts
        # emits, ``acked`` counts the writer's terminal signals (exactly
        # one ``writeOk`` XOR ``writeFailed`` per trace — invariant
        # pinned in ``MseedWriter.write_trace``); the difference is the
        # depth of the storage thread's event queue, warn-logged +
        # signalled (throttled) above ``archive.queue_max``. The real
        # bound is physical: a replay burst can never exceed the device
        # ring, and sustained writer slowness trips the writer's own
        # slow-IO pause valve.
        self._archive_sent: dict[str, int] = {}
        self._archive_acked: dict[str, int] = {}
        self._archive_inflight_warn: dict[str, int] = {}
        self._archive_inflight_last_log: dict[str, float] = {}
        # M6.5-B: per-device gap-detector jitter tolerance (seconds),
        # snapshotted from ``archive.jitter_tolerance_ms`` at writer
        # setup; per-stream rectification accounting, throttled like
        # every other per-stream log (rule 5). ``_rect_pending`` maps
        # composite stream key → (count, max_abs_snap_s) since the
        # last log line.
        self._archive_jitter_tol_s: dict[str, float] = {}
        self._rect_pending: dict[str, tuple[int, float]] = {}
        self._rect_last_log: dict[str, float] = {}
        # Distinct SDS paths the writer has touched this session per
        # device. ``len(set)`` populates ``DeviceStatus.archive_files_open``.
        self._archive_paths_seen: dict[str, set[Path]] = {}

        # ----- M5 stage B — metadata index --------------------------------
        # One ArchiveDao per engine, keyed off the resolved archive root
        # of the first archive-enabled device. Lazy-created in
        # :meth:`_ensure_archive_dao` (called from
        # :meth:`_setup_archive_writer`). ``None`` until then.
        self._archive_dao: ArchiveDao | None = None
        self._archive_db_path: Path | None = None
        self._archive_session_id: int | None = None
        # Cache DAO row IDs to avoid an UPSERT per packet. The dicts
        # are populated on first flushedFile from a stream.
        self._archive_device_ids: dict[str, int] = {}
        self._archive_stream_ids: dict[str, int] = {}
        # Runtime observed-collision net (rule 8 catch-all): the first
        # device to register each concrete NSLC owns it; a different
        # device registering the same NSLC is logged once per
        # (nslc, second-device) pair. Log-only — never alters caching.
        self._nslc_first_owner: dict[str, str] = {}
        self._nslc_collision_logged: set[tuple[str, str]] = set()
        # Per-stream GapDetector (composite key). Created lazily on
        # first packet seen.
        self._gap_detectors: dict[str, GapDetector] = {}
        # Buffered gap events keyed by composite stream key. Drained
        # on flushedFile so DAO writes happen AFTER fsync (the strict
        # DB-after-fsync ordering).
        self._pending_gaps: dict[str, list[GapEvent]] = {}

        # ----- M8 detections ---------------------------------------------
        # Currently-open detection per stream (composite key) →
        # ``(detection_id, t_on)``. StaLta keeps at most one trigger open
        # per stream at a time, so a single id per key is sufficient. The
        # entry is created when an ON marker (``t_off=None``) is recorded
        # and popped when the finalising trigger closes it.
        self._open_detection_ids: dict[str, tuple[int, UTCDateTime]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start MONITORING every configured device. Idempotent.

        Convenience wrapper over :meth:`start_monitoring` for tests and
        headless drivers; the GUI never calls it (rule 13 — acquisition
        is per-device and user-initiated). Archive writers are NOT
        created here regardless of any ``archive.*`` config: recording
        is an explicit per-device action (:meth:`start_recording`).

        Idempotence is per-device: devices already Monitoring/Recording
        are untouched (``start_monitoring`` no-ops on them; a Recording
        device would be DOWNGRADED by ``start_monitoring``, so those are
        skipped here), idle ones start.
        """
        self._ensure_started()
        for dev_cfg in self._engine_devices:
            if self.acquisition_state(dev_cfg.name) is AcquisitionState.IDLE:
                self.start_monitoring(dev_cfg.name)

    def _ensure_started(self) -> None:
        """Bring up the device-independent engine infrastructure once.

        DSP thread, flush timer, ConfigStore subscription and the
        device-list snapshot — everything :meth:`start` used to do
        besides spawning workers. Called lazily by the per-device
        lifecycle methods so the first user action on any device boots
        the shared machinery; until then nothing runs (rule 13).

        When a :class:`ConfigStore` was passed at construction, the
        engine snapshots its current device list and subscribes to
        ``store.configChanged`` so subsequent mutations hot-reload via
        :meth:`_on_config_changed` rather than requiring a full
        engine restart.
        """
        if self._started:
            return
        self._dsp_thread.start()
        # Snapshot the device list the engine will run with. When a
        # ConfigStore is wired, future mutations advance this snapshot
        # via ``_on_config_changed``; when not, it stays equal to the
        # frozen YAML for the lifetime of the engine.
        if self._store is not None:
            self._engine_devices = tuple(self._store.root.devices)
            self._store.configChanged.connect(
                self._on_config_changed, type=Qt.ConnectionType.QueuedConnection
            )
        else:
            self._engine_devices = tuple(self._cfg.devices)
        # Config-time NSLC-collision check (informational, non-blocking):
        # two devices whose selectors overlap on a concrete station would
        # emit the same SEED NSLC. The per-device SDS namespacing keeps
        # their files/index rows separate, but the operator probably did
        # not intend the duplication — warn once per shared NSLC.
        for collision in find_nslc_collisions(self._engine_devices):
            _log.warning(
                "streaming_engine_nslc_collision",
                nslc=collision.nslc,
                devices=list(collision.devices),
            )
        self._flush_timer.start()
        self._started = True

    def start_monitoring(self, name: str) -> None:
        """Idle → Monitoring: live streaming, zero disk writes (rule 13).

        From RECORDING this is a downgrade: the archive writer is torn
        down (flush + close, bounded) while the live socket stays
        untouched. Already-MONITORING is a no-op.

        Raises:
            KeyError: ``name`` does not match any configured device.
        """
        # Validate BEFORE booting shared infrastructure: a typo must not
        # leave the DSP thread + flush timer running with zero devices.
        dev_cfg = self._device_cfg_or_raise(name)
        self._ensure_started()
        state = self.acquisition_state(name)
        if state is AcquisitionState.MONITORING:
            return
        if state is AcquisitionState.RECORDING:
            self._teardown_archive_writer(name)
            self._set_acq_state(name, AcquisitionState.MONITORING)
            _log.info("device_monitoring_started", device=name, downgraded_from="recording")
            return
        self._start_device(dev_cfg, with_archive=False)
        self._set_acq_state(name, AcquisitionState.MONITORING)
        _log.info("device_monitoring_started", device=name)

    def start_session(
        self,
        project_name: str,
        device_names: Sequence[str] = (),
    ) -> SessionInfo:
        """Open a recording session for ``project_name`` (rule 14).

        Sessions are THE archive unit: every archive write lands under
        ``<archive_root>/<sanitized_project>/<device>/<SDS…>`` with one
        ``archive.db`` at the session root. This method validates the
        project name against existing project dirs (injectivity guard —
        a name that sanitises onto a DIFFERENT existing project is
        rejected loudly), swaps the engine's metadata DAO to the session
        DB (closing any sessionless monitoring index first; crash-dirty
        rows in the session DB are closed-as-dirty on open), starts the
        session row, then starts recording on every listed device.

        One session at a time. Devices not listed can join later via
        :meth:`start_recording`.

        Raises:
            SessionError: a session is already active, or the project
                name is blank.
            ProjectNameCollisionError: the sanitized name belongs to a
                different existing project.
            KeyError: a listed device name is unknown.
        """
        from PySide6.QtCore import QCoreApplication, QEventLoop

        from echosmonitor.core.exceptions import SessionError

        if self._session_transition:
            raise SessionError("a session transition is already in progress")
        if self._session is not None:
            raise SessionError(
                f"a session is already active: {self._session.project_name!r}; "
                f"end it before starting another"
            )
        if not project_name.strip():
            raise SessionError("project name must not be empty")
        # Validate everything BEFORE mutating engine state: unknown
        # device → KeyError here, with no half-started session behind it.
        cfgs = [self._device_cfg_or_raise(n) for n in device_names]
        self._ensure_started()
        base_root = self._resolve_db_root()
        session_root = ensure_project_root(base_root, project_name)
        self._session_transition = True
        try:
            # Absorb queued cross-thread events that target the OLD DAO
            # before swapping it out. Detections are NOT engine-thread-
            # synchronous: ``triggerFired`` arrives queued from the DSP
            # thread, so a trigger posted under the sessionless
            # monitoring index must be recorded THERE, not in the new
            # session DB. User-input events are excluded so a queued
            # click cannot reenter the lifecycle inside this barrier.
            QCoreApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
            )
            # Swap the metadata DAO: the sessionless monitoring index
            # (if one is open for detections) closes cleanly; rows
            # produced during the session — including detections —
            # belong to the session DB at the session root (one
            # archive.db per session root, skill miniseed-sds).
            self._close_archive_dao()
            try:
                # The first DAO call below is what actually opens the
                # connection (mkdir + DDL) — disk-full/permissions
                # surface HERE, after the monitoring index was closed.
                dao = ArchiveDao(session_root / "archive.db")
                recovered = dao.close_dirty_sessions()
                session_id = dao.start_session(
                    host=socket.gethostname(),
                    version=self._version_string(),
                    config_hash=self._compute_config_hash(),
                    project_name=project_name,
                )
            except Exception as exc:
                _log.error(
                    "session_start_failed",
                    project=project_name,
                    root=str(session_root),
                    error=str(exc),
                )
                # Detections must not silently stop persisting (rule 8):
                # restore the sessionless monitoring index before
                # surfacing the failure.
                for dev_cfg in self._engine_devices:
                    if dev_cfg.name in self._workers and self._device_has_detection(dev_cfg):
                        self._ensure_archive_dao(self._resolve_db_root())
                        break
                raise
            self._archive_dao = dao
            self._archive_db_path = session_root / "archive.db"
            self._archive_session_id = session_id
            # started_at comes from the row the DAO just wrote, fetched
            # by its id (rule 9 — no parallel in-memory clock, and no
            # ordering heuristic a crash-dirty future-dated row could
            # fool).
            started_at = dao.session_started_at(session_id)
            self._session = SessionInfo(
                session_id=session_id,
                project_name=project_name,
                sanitized_name=sanitize_project_name(project_name),
                started_at=started_at,
                devices=(),
                db_root=session_root,
            )
        finally:
            self._session_transition = False
        _log.info(
            "session_started",
            project=project_name,
            root=str(session_root),
            session_id=session_id,
            devices=list(device_names),
            recovered_dirty=recovered,
        )
        self.sessionChanged.emit(self._session)
        for dev_cfg in cfgs:
            self.start_recording(dev_cfg.name)
        return self._session

    def end_session(self) -> None:
        """Close the active session; no-op when none is active.

        Every still-recording member downgrades to MONITORING (writer
        teardown drains + flushes, rule-7 bounded; live sockets stay
        up), the queued durability claims are absorbed, the session row
        is closed clean, and the DAO reverts to the sessionless
        monitoring index if a running device still produces detections.
        """
        from echosmonitor.core.exceptions import SessionError

        if self._session_transition:
            raise SessionError("a session transition is already in progress")
        session = self._session
        if session is None:
            return
        t0 = time.monotonic()
        self._session_transition = True
        try:
            for dev_name, state in list(self._acq_state.items()):
                if state is AcquisitionState.RECORDING:
                    self.start_monitoring(dev_name)
            self._finalize_session(reensure_detection_dao=True)
        finally:
            self._session_transition = False
        _log.info(
            "session_ended",
            project=session.project_name,
            session_id=session.session_id,
            elapsed_s=round(time.monotonic() - t0, 3),
        )

    def active_session(self) -> SessionInfo | None:
        """The active session's frozen snapshot, or ``None``."""
        return self._session

    def persist_session_stationxml(self, device_name: str, xml_blob: str) -> bool:
        """Persist a fetched StationXML blob for the active session (M6.6-B).

        Scoped to the session that recorded with it (rule 14): a no-op
        unless a recording session is open AND ``device_name`` is one of its
        members. Writes via the session DAO and commits immediately, exactly
        like the session row itself (the DAO funnels metadata writes; rule 8
        DB-after-fsync is satisfied by the immediate commit). Returns whether
        the blob was persisted. Called on the GUI thread.
        """
        if self._archive_dao is None or self._archive_session_id is None:
            return False
        if self._session is None or device_name not in self._session.devices:
            return False
        self._archive_dao.upsert_session_stationxml(
            self._archive_session_id, device_name, xml_blob
        )
        _log.info(
            "session_stationxml_persisted",
            device=device_name,
            session_id=self._archive_session_id,
            blob_bytes=len(xml_blob),
        )
        return True

    def _finalize_session(self, *, reensure_detection_dao: bool) -> None:
        """Close the session row + DAO and announce the session's end.

        ``reensure_detection_dao`` is False on the engine-stop path: the
        engine is going down, so re-opening the sessionless detection DB
        would only create a row that immediately closes again.
        """
        # Absorb queued ``flushedFile`` events so the final fsync's
        # metadata lands in the session DB before its row closes
        # (mirrors the engine-stop sequence; rule 8). User-input events
        # are excluded: a queued click dispatched here could reenter the
        # session lifecycle mid-swap (the ``_session_transition`` guard
        # backstops anything else that slips through).
        from PySide6.QtCore import QCoreApplication, QEventLoop

        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        self._close_archive_dao()
        self._session = None
        self.sessionChanged.emit(None)
        if reensure_detection_dao:
            # Open question 3 interim: detections keep persisting while
            # monitoring. Any still-running detection-capable device
            # gets the sessionless base-root index back.
            for dev_cfg in self._engine_devices:
                if dev_cfg.name in self._workers and self._device_has_detection(dev_cfg):
                    self._ensure_archive_dao(self._resolve_db_root())
                    break

    def _close_archive_dao(self) -> None:
        """End the current DAO's session row, close it, drop the caches.

        Shared by the session swap (:meth:`start_session`), the session
        close (:meth:`_finalize_session`) and engine :meth:`stop`.
        """
        if self._archive_dao is None:
            return
        if self._archive_session_id is not None:
            self._archive_dao.end_session(self._archive_session_id)
        self._archive_dao.close()
        self._archive_dao = None
        self._archive_db_path = None
        self._archive_session_id = None
        self._archive_device_ids.clear()
        self._archive_stream_ids.clear()
        self._gap_detectors.clear()
        self._pending_gaps.clear()
        self._rect_pending.clear()
        self._rect_last_log.clear()
        # A detection open across a DAO swap stays an open row in the
        # closing DB (crash-equivalent semantics) and its later close
        # lands as a separate closed row in the next DB. Rare (STA/LTA
        # triggers are short relative to session transitions) but worth
        # an audit trail when it happens.
        if self._open_detection_ids:
            _log.info(
                "archive_dao_closed_with_open_detections",
                count=len(self._open_detection_ids),
                streams=sorted(self._open_detection_ids),
            )
        self._open_detection_ids.clear()

    def start_recording(self, name: str) -> None:
        """Monitoring (or Idle) → Recording into the ACTIVE session.

        The archive writer is created HERE and only here — recording is
        an explicit user action, never a config side effect (rule 13) —
        and it records into the active session's root (rule 14: no
        session, no archive writes). From MONITORING the live socket is
        untouched (the writer simply attaches); from IDLE the device
        starts streaming and archiving in one step. Already-RECORDING
        is a no-op. The device joins the session's membership row
        before the state is announced (persisted-before-announced,
        rule 8).

        Raises:
            SessionError: no active session — call :meth:`start_session`
                first.
            KeyError: ``name`` does not match any configured device.
        """
        from echosmonitor.core.exceptions import SessionError

        # Validate first — see start_monitoring for why.
        dev_cfg = self._device_cfg_or_raise(name)
        session = self._session
        if session is None:
            raise SessionError(
                f"cannot record {name!r}: no active session — "
                f"start_session(project, devices) first (rule 14)"
            )
        state = self.acquisition_state(name)
        if state is AcquisitionState.RECORDING:
            return
        if state is AcquisitionState.MONITORING:
            self._setup_archive_writer(dev_cfg)
        else:
            self._start_device(dev_cfg, with_archive=True)
        membership_grew = False
        if name not in session.devices and self._archive_dao is not None:
            # Persisted before announced (rule 8): the membership row
            # commits here; the emit waits until the acquisition state
            # is settled so a direct receiver never sees a member whose
            # state still reads MONITORING/IDLE.
            self._archive_dao.add_session_device(session.session_id, name)
            self._session = dataclasses.replace(
                session, devices=(*session.devices, name)
            )
            membership_grew = True
        self._set_acq_state(name, AcquisitionState.RECORDING)
        if membership_grew:
            self.sessionChanged.emit(self._session)
        _log.info(
            "device_recording_started",
            device=name,
            project=session.project_name,
        )

    def acquisition_state(self, name: str) -> AcquisitionState:
        """Current user acquisition state for ``name``; IDLE if unknown."""
        return self._acq_state.get(name, AcquisitionState.IDLE)

    def _set_acq_state(self, name: str, state: AcquisitionState) -> None:
        prev = self._acq_state.get(name, AcquisitionState.IDLE)
        self._acq_state[name] = state
        if prev is not state:
            self.acquisitionStateChanged.emit(name, int(state))

    def _device_cfg_or_raise(self, name: str) -> DeviceConfig:
        """Look up a device config in the engine's current view.

        Prefers ``_engine_devices`` (advanced by hot-reload); before the
        infrastructure has booted, falls back to the live store / frozen
        YAML exactly like :meth:`devices`.
        """
        if self._engine_devices:
            source: tuple[DeviceConfig, ...] = self._engine_devices
        elif self._store is not None:
            source = tuple(self._store.root.devices)
        else:
            source = tuple(self._cfg.devices)
        dev_cfg = next((d for d in source if d.name == name), None)
        if dev_cfg is None:
            raise KeyError(f"unknown device: {name!r}")
        return dev_cfg

    def stop(self, name: str | None = None) -> None:
        """Stop one device (``name`` given) or the whole engine. Idempotent.

        Per-device: tears down that device's worker, thread and archive
        writer (if recording) with the rule-7 bounded joins of
        :meth:`_stop_device`, sets it IDLE, and leaves every other
        device — and the shared DSP/flush infrastructure — running.
        Stopping an already-idle device is a no-op.

        Globally (no ``name``), with N devices the wall time of
        ``stop()`` must be bounded by the slowest single device (~2 s in
        the pathological case), not by N x 2 s. We achieve this by
        parallelising phase 1: each ``worker.stop()`` runs on its own
        helper thread so the total wait collapses to the max, not the
        sum. Phase 2 is already parallelised (all ``QThread.quit()``
        calls land before any ``wait()``).

        By the time we clear the bridge dict no queued signal is still
        in flight from the worker thread to its bridge — which would
        otherwise dispatch onto a torn-down receiver and segfault.
        Bridge signal connections are dropped *after* the threads have
        joined so a stray cross-thread emission cannot reach a soon-to-
        be-released bridge during garbage collection.
        """
        if name is not None:
            if self.acquisition_state(name) is AcquisitionState.IDLE:
                return
            t0 = time.monotonic()
            self._stop_device(name)
            self._set_acq_state(name, AcquisitionState.IDLE)
            _log.info(
                "device_acquisition_stopped",
                device=name,
                elapsed_s=round(time.monotonic() - t0, 3),
            )
            return
        if self._session_transition:
            # Reentrant global stop from inside a session transition's
            # processEvents barrier: skipping is correct — the outer
            # transition (and whatever invoked it) resumes when the
            # barrier returns; tearing the engine down underneath it
            # would corrupt the swap.
            _log.warning("streaming_engine_stop_skipped_during_session_transition")
            return
        if not self._started:
            return
        # Flip _started FIRST, before any barrier in this teardown: a
        # queued configChanged meta-call posted before the disconnect
        # below survives it (POSTMORTEMS 2026-06-01) and would otherwise
        # dispatch inside this method's processEvents with the guard
        # still open — _on_config_changed could then restart a non-idle
        # device, registering a fresh QThread that the remaining
        # teardown clears from ``_threads`` without ever quitting it
        # (the M3-prep segfault class). ``_on_config_changed`` guards on
        # ``_started``; the early flip closes that window. Reentrant
        # direct-connected receivers of the late IDLE/session emits get
        # the same protection for free.
        self._started = False
        # Detach from the ConfigStore first so a queued ``configChanged``
        # event in flight cannot fire after we've torn down the engine
        # state it would mutate.
        if self._store is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                self._store.configChanged.disconnect(self._on_config_changed)
        # Archive packets need no drain here (M6.5-A): every packet was
        # already posted to the storage thread's event queue at receive
        # time, and the per-device ``close_all`` barrier below
        # (BlockingQueuedConnection in ``_teardown_archive_writer``)
        # dispatches strictly after those queued ``write_trace`` events,
        # so everything in flight reaches disk before the writer closes.
        self._flush_timer.stop()
        self._drain_abandoned_threads()
        # Phase 1: stop every worker. ``worker.stop()`` blocks until
        # ``run()`` has fully unwound (capped internally at 2 s); we
        # run all of them concurrently on helper threads so the engine
        # stop is bounded by the slowest single device rather than the
        # sum across devices.
        #
        # The phase-1 budget is *shared* across helpers, not per-helper:
        # we anchor a single deadline at start, then each ``join`` waits
        # only for the remaining time. Without this, two workers both
        # exceeding their internal 2 s cap could leave both helpers
        # alive past phase 1 — they'd then run concurrently with
        # ``thread.quit()``/``thread.wait()`` in phase 2 and re-open the
        # M3-prep race A (helper still calling ``_close_client_socket``
        # on a client whose QThread is being torn down). With a shared
        # deadline, any straggler is detected and warn-logged before we
        # quit the QThreads.
        stop_threads: list[tuple[str, threading.Thread]] = []
        for name, worker in self._workers.items():
            t = threading.Thread(
                target=worker.stop,
                name=f"sl-stop-{name}",
                daemon=True,
            )
            t.start()
            stop_threads.append((name, t))
        deadline = time.monotonic() + (_THREAD_JOIN_MS / 1000.0)
        for name, t in stop_threads:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)
            if t.is_alive():
                _log.warning(
                    "streaming_engine_worker_stop_helper_alive",
                    device=name,
                    note=(
                        "worker.stop() helper outlived the phase-1 deadline; "
                        "phase 2 may race the helper's socket teardown"
                    ),
                )
        # Phase 2: ask all QThreads to quit, then join. quit() is
        # cheap and non-blocking; doing all quits before any wait()
        # lets the underlying threads tear down concurrently — N
        # devices stop in roughly the time of one, not N times the
        # time of one.
        for thread in self._threads.values():
            thread.quit()
        for name, thread in self._threads.items():
            if not thread.wait(_THREAD_JOIN_MS):
                _log.warning("streaming_engine_thread_join_timeout", device=name)
                # Retain the still-running pair so the `_threads.clear()` below
                # cannot drop its last reference and trigger a Qt abort.
                stuck_worker = self._workers.get(name)
                if stuck_worker is not None:
                    self._abandon_worker_thread(name, stuck_worker, thread)

        # Stop the DSP thread. Both ``_clearChainsRequested`` and
        # ``_clearSpectrogramsRequested`` are wired with
        # ``BlockingQueuedConnection`` (see __init__) so the emits below
        # block until their slots have run on the router thread. Without
        # this barrier, ``QThread.quit()`` could interrupt the dispatcher
        # before the queued slots ran and leave stale chain / spectrogram
        # state across the stop()/start() cycle. See POSTMORTEMS
        # 2026-05-10 entry "Flaky multi-device tests resolved". The two
        # barriers are independent; the chain-first ordering is
        # convention, not a safety requirement (the spectrogram router
        # receives samples via its own signal — not from the chain).
        self._clearChainsRequested.emit()
        self._clearSpectrogramsRequested.emit()
        self._dsp_thread.quit()
        if not self._dsp_thread.wait(_THREAD_JOIN_MS):
            _log.warning("streaming_engine_dsp_thread_join_timeout")

        # Flush + close every writer, then quit the storage thread. The
        # per-device close runs via BlockingQueuedConnection so the
        # storage thread has actually finished its fsync pass before
        # ``thread.quit()`` interrupts the dispatcher. Total budget
        # contributes to the engine's overall ~5 s stop budget alongside
        # the DSP teardown above.
        for name in list(self._archive_writers.keys()):
            self._teardown_archive_writer(name)
        if self._archive_thread is not None:
            self._archive_thread.quit()
            if not self._archive_thread.wait(_THREAD_JOIN_MS):
                _log.warning("streaming_engine_archive_thread_join_timeout")
            self._archive_thread = None

        # Close the metadata DAO last. The storage thread has exited
        # by now, but a small number of ``flushedFile`` events may
        # still be in the engine's event queue waiting to be
        # dispatched to ``_on_archive_flushed_file``; processing one
        # round of pending events absorbs them so the corresponding
        # ``record_file``/``record_packet``/``record_gap`` rows make
        # it into the DB before ``end_session`` finalises.
        from PySide6.QtCore import QCoreApplication, QEventLoop

        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        self._close_archive_dao()
        # An active session ends cleanly with the engine: the row above
        # was closed by _close_archive_dao. State clears NOW; the
        # sessionChanged emit waits until after ``_started`` flips (same
        # reentrancy rule as the IDLE announcements below).
        session_was_active = self._session is not None
        if self._session is not None:
            _log.info(
                "session_ended",
                project=self._session.project_name,
                session_id=self._session.session_id,
                reason="engine_stop",
            )
            self._session = None

        # Drop all signal connections from the bridges before the bridges
        # themselves are released. Without this, a queued cross-thread
        # emission that arrived just before quit() could fire its slot on
        # an object that Python is about to garbage-collect. PySide6 no
        # longer accepts the no-arg form of `QObject.disconnect()`, so
        # disconnect each named signal individually.
        for bridge in self._bridges.values():
            for signal in (
                bridge.packetReceivedNamed,
                bridge.stateChangedNamed,
                bridge.errorOccurredNamed,
                bridge.statsUpdatedNamed,
                bridge.diagnosticsUpdatedNamed,
            ):
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect()
        self._threads.clear()
        self._workers.clear()
        self._bridges.clear()
        self._coalescers.clear()
        self._buffers.clear()
        self._key_to_pair.clear()
        self._stream_fs.clear()
        self._latest_raw_endtime.clear()
        self._last_packet_monotonic.clear()
        self._expected_packet_interval_s.clear()
        self._stalled.clear()
        self._detrend_linear_warned.clear()
        self._stream_drops_pending.clear()
        self._stream_drops_last_log.clear()
        self._ring_saturated.clear()
        self._chain_installed.clear()
        with self._chain_lock:
            self._chain_queues.clear()
            self._chain_max_q_by_key.clear()
            self._chain_drops_pending.clear()
            self._chain_drops_last_log.clear()
        # ``_started`` flipped at the top of this method — by the time
        # the IDLE / session-end announcements below reach a reentrant
        # direct-connected receiver, the engine is fully torn down and
        # ``_ensure_started`` would reboot it cleanly.
        for dev_name in list(self._acq_state):
            self._set_acq_state(dev_name, AcquisitionState.IDLE)
        if session_was_active:
            self.sessionChanged.emit(None)

    def devices(self) -> tuple[DeviceConfig, ...]:
        """Snapshot of the engine's currently-running device list.

        Returned as a tuple so the caller cannot mutate the engine's
        view by appending. When a :class:`ConfigStore` is wired, this
        reflects the engine's view *after* the most recent
        ``_on_config_changed`` — which can lag the store by a queued-
        connection hop. Pre-:meth:`start` callers see the snapshot
        the engine will use when it starts; post-:meth:`stop` callers
        see the snapshot in effect at stop time.
        """
        if self._engine_devices:
            return self._engine_devices
        # Fallback path: ``start()`` hasn't run yet. Surface the
        # construction-time view so the GUI's StationBrowser combo
        # populates correctly before the engine starts.
        if self._store is not None:
            return tuple(self._store.root.devices)
        return tuple(self._cfg.devices)

    def device_status(self) -> dict[str, DeviceStatus]:
        """Snapshot of every known device's status. Mutates won't be visible."""
        return {
            name: DeviceStatus(
                name=s.name,
                state=s.state,
                last_event_at=s.last_event_at,
                last_error=s.last_error,
                packets_received=s.packets_received,
                bytes_received=s.bytes_received,
                attempt_count=s.attempt_count,
                last_failure_kind=s.last_failure_kind,
                next_attempt_at=s.next_attempt_at,
                since_first_attempt_at=s.since_first_attempt_at,
                # Shallow-copy the dict so a panel mutation can't bleed
                # back into engine state. The schema is JSON-friendly
                # (ints, strs, list[str]) so a shallow copy is enough.
                last_failure_detail=(
                    dict(s.last_failure_detail) if s.last_failure_detail is not None else None
                ),
                archive_enabled=s.archive_enabled,
                archive_bytes_written=s.archive_bytes_written,
                archive_files_open=s.archive_files_open,
                archive_last_write_at=s.archive_last_write_at,
                archive_last_error=s.archive_last_error,
                archive_gaps_total=s.archive_gaps_total,
                archive_overlaps_total=s.archive_overlaps_total,
                archive_last_gap_at=s.archive_last_gap_at,
                detections_total=s.detections_total,
                last_detection_at=s.last_detection_at,
            )
            for name, s in self._status.items()
        }

    def recent_detections(
        self,
        limit: int,
        since: UTCDateTime | None = None,
    ) -> list[Detection]:
        """Read recent persisted detections from the metadata index.

        Used by the GUI on startup for the "recent detections" historical
        taste. A bounded, index-backed DB read — it does NOT load or
        replay any waveforms (rule 8 / honest forward-reference to a later
        archive-replay milestone). Returns ``[]`` when no DAO exists
        (no detection-capable or archive-enabled device this session).
        Safe to call on the engine thread (same thread the DAO writes on).
        """
        if self._archive_dao is None or limit <= 0:
            return []
        return self._archive_dao.recent_detections(limit, since)

    def count_detections(self, since: UTCDateTime | None = None) -> int:
        """``COUNT(*)`` of persisted detections (rule 9). 0 if no DAO."""
        if self._archive_dao is None:
            return 0
        return self._archive_dao.count_detections(since)

    def read_recent(
        self,
        device_name: str,
        nslc: str,
        seconds: float,
    ) -> tuple[np.ndarray, float, UTCDateTime | None]:
        """Snapshot the last ``seconds`` of samples for one stream.

        Args:
            device_name: Device the stream belongs to.
            nslc: Stream NSLC string.
            seconds: Window length in seconds. The result is clipped to
                what the ring buffer actually holds — fewer samples
                returned if the buffer hasn't filled yet.

        Returns:
            Tuple ``(samples_float32, fs, latest_t)``. For an unknown
            stream or a non-positive ``seconds``, returns
            ``(empty_array, 0.0, None)``.

        Pure read; lock-protected via the underlying ``RingBuffer``.
        Used by the M6 PSD widget and the chain editor's live preview
        (Stages 2 and 3). Safe to call from any thread.
        """
        if seconds <= 0:
            return np.empty(0, dtype=np.float32), 0.0, None
        key = device_stream_key(device_name, nslc)
        buf = self._buffers.get(key)
        if buf is None:
            return np.empty(0, dtype=np.float32), 0.0, None
        fs = float(buf.sampling_rate)
        n = max(1, round(seconds * fs))
        samples = buf.read_last(n)
        return samples, fs, buf.latest_t

    def archive_root(self, device_name: str | None = None) -> Path:
        """Resolved SDS archive root for READING.

        For a known device, returns its effective root (per-device
        ``archive.root_dir`` override, else the app default); otherwise
        the app-level default — in both cases session-rooted while a
        session is active (rule 14; same :meth:`_session_rooted` funnel
        the writers use, so readers and writers can never disagree on
        the layout). Used by the archive reader for historical data --
        pure path resolution, no I/O.
        """
        if device_name is not None:
            dev_cfg = next((d for d in self._engine_devices if d.name == device_name), None)
            if dev_cfg is not None:
                return self._session_rooted(self._resolve_archive_root(dev_cfg))
        return self._session_rooted(self._resolve_db_root())

    def _session_rooted(self, base: Path) -> Path:
        """THE session-aware root resolver (rule 14).

        Every archive-path site — writer construction, the public
        reading accessor, the DB location — funnels through here:
        ``<base>/<sanitized_project>`` while a session is active, the
        bare base otherwise (the sessionless monitoring index). NOTE:
        between sessions the bare base does NOT reach data recorded in
        closed sessions (those live under their project dirs) —
        session-aware historical reading is M3-A's session browser; the
        live readers only need the ACTIVE context to agree with the
        writers, which this funnel guarantees.
        """
        if self._session is not None:
            return base / self._session.sanitized_name
        return base

    def archive_dao(self) -> ArchiveDao | None:
        """The CURRENT metadata DAO, else ``None``.

        Read-only use by the archive reader to consult the ``files``
        index. ``None`` when no recording session is active and no
        detection-capable device is running.

        LIFETIME (M2-B): the DAO is per-context, not per-engine — it is
        REPLACED on every ``start_session``/``end_session`` swap.
        Consumers must re-resolve per request (never cache the return
        across requests) or subscribe to ``sessionChanged`` (queued) to
        refresh; a cached reference goes stale on the first swap and a
        worker-thread connection opened on it leaks (``ArchiveDao.close``
        only closes the calling thread's connection). Every current
        call site re-resolves.
        """
        return self._archive_dao

    def archive_db_path(self) -> Path | None:
        """The CURRENT context's ``archive.db`` path, else ``None``.

        Same per-context lifetime as :meth:`archive_dao` — re-resolve per
        request, never cache. Read by the GUI to snapshot a metadata-index
        path into loader requests (the loaders open it READ-ONLY on their
        own threads and close it per request — the M2-B leak note); the
        engine's own DAO is never handed across threads.
        """
        return self._archive_db_path

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _start_device(self, dev_cfg: DeviceConfig, *, with_archive: bool = False) -> None:
        name = dev_cfg.name
        # Refresh the device's chain cache to match this start. M3-era
        # code populated ``_device_dsp_cfg`` only at engine construction
        # from the frozen YAML; Stage B's hot-reload path can spawn or
        # restart a device with a chain that differs from the original
        # config, so the chain installed on first packet must come from
        # ``dev_cfg`` (the live snapshot), not ``self._cfg``.
        self._device_dsp_cfg[name] = list(dev_cfg.dsp_chain)
        # Surface a one-line INFO when a device starts with no DSP chain.
        # An explicitly empty `dsp_chain: []` and a missing `dsp_chain` are
        # indistinguishable downstream (both produce a single raw plot per
        # stream); the common confusion is that users expect filtering
        # without realising they need to add stages. INFO, not WARNING —
        # this is a valid configuration, just often surprising.
        if not dev_cfg.dsp_chain:
            _log.info(
                "device_no_dsp_chain",
                device=name,
                nslc_count=len(dev_cfg.selectors),
                hint=(
                    "device has empty dsp_chain — only raw plots will be shown; "
                    "add stages to enable filtering"
                ),
            )
        selectors = [
            StreamSelector(
                network=s.network,
                station=s.station,
                location=s.location,
                channel=s.channel,
            )
            for s in dev_cfg.selectors
        ]
        worker = SeedLinkWorker(
            name=name,
            host=dev_cfg.host,
            port=dev_cfg.port,
            selectors=selectors,
            reconnect=dev_cfg.reconnect,
        )
        thread = QThread(self)
        thread.setObjectName(f"sl-{name}")
        worker.moveToThread(thread)
        thread.started.connect(worker.run, type=Qt.ConnectionType.QueuedConnection)

        bridge = _DeviceBridge(name, worker, parent=self)
        bridge.packetReceivedNamed.connect(self._on_packet, type=Qt.ConnectionType.DirectConnection)
        bridge.stateChangedNamed.connect(self._on_state, type=Qt.ConnectionType.DirectConnection)
        bridge.errorOccurredNamed.connect(self._on_error, type=Qt.ConnectionType.DirectConnection)
        bridge.statsUpdatedNamed.connect(self._on_stats, type=Qt.ConnectionType.DirectConnection)
        bridge.diagnosticsUpdatedNamed.connect(
            self._on_diagnostics, type=Qt.ConnectionType.DirectConnection
        )

        self._threads[name] = thread
        self._workers[name] = worker
        self._bridges[name] = bridge
        # Preserve any prior DeviceStatus entry across a stop/restart of
        # the same device — the packets/bytes counters and last-error
        # string carry forward, matching the contract documented on
        # ``_stop_device``. ``setdefault`` is the right primitive: a
        # first-time start populates the dict, a restart leaves the
        # accumulated counters intact.
        self._status.setdefault(name, DeviceStatus(name=name))
        # Archive writers are created only when the caller is in the
        # RECORDING path (rule 13) — never from ``archive.enabled``
        # config. The ``archive.*`` block still parameterises the writer
        # (root, encoding, fsync cadence, queue bound) when it IS created.
        if with_archive:
            self._setup_archive_writer(dev_cfg)
        # Detections persist independently of MSEED archiving (rule 8: the
        # detection table is part of the same metadata index, but a device
        # may run STA/LTA while only monitoring). If this device has a
        # detection-producing stage, make sure the DAO + session exist
        # even though no archive writer was created. The DAO row writes
        # funnel through the engine thread, like the M5 metadata writes —
        # no extra QThread is needed for them. (ROADMAP open question 3:
        # Monitoring deliberately keeps writing the metadata DB.)
        if self._archive_dao is None and self._device_has_detection(dev_cfg):
            self._ensure_archive_dao(self._resolve_db_root())
        thread.start()

    @Slot(str, object)
    def _on_packet(self, device_name: str, trace: Trace) -> None:
        try:
            sid = StreamID.from_trace_id(trace.id)
        except ValueError:
            _log.warning(
                "streaming_engine_bad_trace_id",
                device=device_name,
                trace_id=trace.id,
            )
            return
        nslc = sid.nslc
        key = device_stream_key(device_name, nslc)
        fs = float(trace.stats.sampling_rate)
        endtime = trace.stats.endtime
        samples = np.ascontiguousarray(trace.data, dtype=np.float32)

        # Stall watchdog (Bug 2): record arrival + the sampling-rate-derived
        # expected cadence (one ~npts-sample packet every npts/fs s of data).
        # A device flagged stalled that produces a packet has recovered.
        self._last_packet_monotonic[device_name] = time.monotonic()
        if fs > 0.0:
            self._expected_packet_interval_s[device_name] = float(trace.stats.npts) / fs
        if device_name in self._stalled:
            self._stalled.discard(device_name)
            _log.info("seedlink_stream_resumed", device=device_name)
            self.streamStalled.emit(device_name, False)

        if key not in self._buffers:
            capacity = max(1, int(self._window_seconds * fs * 2))
            # Surface the per-stream ring-buffer allocation cost so a
            # high-fs stream's memory footprint is visible in the logs
            # (float32 → 4 bytes/sample). At 4 kSPS over a 30 s window the
            # buffer is ~960 kB; with many streams this adds up and a silent
            # allocation would be hard to attribute after the fact.
            _log.info(
                "ring_buffer_allocated",
                device=device_name,
                nslc=nslc,
                fs=fs,
                window_seconds=self._window_seconds,
                capacity=capacity,
                approx_bytes=capacity * _BYTES_PER_SAMPLE,
            )
            self._buffers[key] = RingBuffer(capacity, fs)
            self._stream_fs[key] = fs
            # Announce the stream first so subscribers (LiveStack) can build
            # their plot widgets, *then* install the chain — that way the
            # processedStreamMeta signal lands on a plot that already exists.
            self.newStreamSeen.emit(device_name, nslc)
            self.streamMeta.emit(device_name, nslc, fs, str(trace.stats.starttime))
            # Install a raw-fs spectrogram by default. ``_maybe_install_chain``
            # below replaces it with an fs_out spectrogram if a chain is
            # configured for this device.
            self._installSpectrogramRequested.emit(device_name, nslc, fs)
            self._maybe_install_chain(device_name, nslc, sid, fs)
        else:
            old_fs = self._stream_fs[key]
            denom = max(old_fs, fs)
            if denom > 0 and abs(old_fs - fs) / denom > 0.01:
                self._stream_fs[key] = fs
                self.streamMeta.emit(device_name, nslc, fs, str(trace.stats.starttime))

        # Coalescer creation is decoupled from buffer creation: a
        # ``_stop_device`` drops the per-device coalescer (so a flush tick
        # arriving after stop cannot replay buffered packets — see
        # POSTMORTEMS 2026-05-10) but preserves the ring buffer so that
        # ``_start_device_by_name`` resumes plotting into the same widget.
        # The next packet after restart therefore needs to recreate the
        # coalescer here even though the buffer already exists.
        if key not in self._coalescers:
            coalescer = _StreamCoalescer(device_name, nslc, parent=self)
            # Re-emit the coalesced trace on the engine's public ``traceReady``
            # via QueuedConnection — NOT Direct. ``coalescer.flush()`` runs
            # inside ``_flush_all`` on the engine/GUI thread, so a Direct
            # re-emit would invoke every ``traceReady`` slot synchronously
            # inside the flush tick. The render path is best-effort
            # (CLAUDE.md rule 11): deferring the re-emit to a later
            # event-loop turn keeps the flush/drain tick — which feeds DSP,
            # detection and storage — from ever being gated by render
            # latency. Same-thread QueuedConnection still runs render on the
            # GUI thread eventually; it just no longer runs inside this tick.
            coalescer.flushed.connect(self.traceReady, type=Qt.ConnectionType.QueuedConnection)
            self._coalescers[key] = coalescer
            self._key_to_pair[key] = (device_name, nslc)

        # Anchor for the processed-spectrogram feed (see
        # ``_latest_raw_endtime`` init). Updated on every raw packet so a
        # later processed chunk borrows the freshest wall-clock end time.
        self._latest_raw_endtime[key] = endtime

        dropped = self._buffers[key].push(samples, end_time=endtime)
        if dropped:
            self._note_drop(device_name, nslc, key, dropped)
        self._coalescers[key].push(samples)

        if key in self._chain_installed:
            self._enqueue_for_dsp(key, samples, trace.stats.starttime)
        else:
            # Raw-source spectrogram path: only fire when no DSP chain is
            # installed — otherwise the chain's processed-output emission
            # would double-feed the spectrogram. The single-emission
            # invariant matters during chain install/uninstall windows
            # (see plan decision 1).
            self._spectrogramFeedRequested.emit(device_name, nslc, samples, endtime)

        # Archive consumes RAW packets in parallel with the DSP path —
        # rule 8 (persistence boundary). Same packet object; the writer
        # never mutates the trace. Posted straight to the storage
        # thread — never dropped on this seam (M6.5-A).
        if device_name in self._archive_senders:
            self._observe_gap(device_name, nslc, key, fs, trace)
            self._enqueue_for_archive(device_name, nslc, trace)

        status = self._status.get(device_name)
        if status is not None:
            status.last_event_at = endtime

    @Slot(str, str, object)
    def _on_processed_for_spec(self, device_name: str, nslc: str, samples: object) -> None:
        """Forward post-DSP samples into the spectrogram router.

        Connected to ``_DspRouter.processedTraceReady``; we re-emit
        through the engine's own ``_spectrogramFeedRequested`` signal so
        the dispatch happens on the DSP thread (where the router lives)
        rather than on the engine thread that received this slot call.
        The DSP path doesn't carry the absolute end time of the chunk
        through its queue, so — like the processed trace, which borrows
        the raw stream's latest end time (``TracePlot._latest_processed_t
        = _latest_raw_t``) — we anchor the spectrogram columns to the
        latest RAW packet end time for this stream. Passing ``None`` here
        instead made the view fall back to ``time.time()`` per column,
        collapsing the wall-clock axis to a sub-second slice.
        """
        if not isinstance(samples, np.ndarray):
            return
        key = device_stream_key(device_name, nslc)
        t_end = self._latest_raw_endtime.get(key)
        self._spectrogramFeedRequested.emit(device_name, nslc, samples, t_end)

    def _maybe_install_chain(
        self,
        device_name: str,
        nslc: str,
        sid: StreamID,
        fs: float,
    ) -> None:
        """Build a `DspChain` for this stream if the device requests one.

        Failures in chain construction (e.g. a freqmax >= fs/2) are logged
        and the stream falls back to the raw-only pipeline. The user gets
        a warning rather than a crash.
        """
        stage_cfgs = self._device_dsp_cfg.get(device_name, [])
        if not stage_cfgs:
            return
        try:
            chain = build_chain(
                stages=stage_cfgs,
                fs_in=fs,
                stream_id=sid,
                live=True,
            )
        except Exception as exc:
            # Bad config shouldn't crash the engine — fall back to raw-only.
            _log.warning(
                "dsp_chain_build_failed",
                device=device_name,
                nslc=nslc,
                error=str(exc),
            )
            return
        key = device_stream_key(device_name, nslc)
        # Warn once per (device, nslc) per session that this live chain uses
        # a linear detrend (per-packet least-squares → cross-packet
        # discontinuities; "constant" is the live-safe kind). The flag used
        # to live on the Detrend instance and reset on every chain rebuild,
        # so a config tweak or reconnect re-spammed the log. ``demean`` maps
        # to constant, so only an explicit ``kind="linear"`` warns.
        if key not in self._detrend_linear_warned and any(
            getattr(c, "type", None) == "detrend" and getattr(c, "kind", None) == "linear"
            for c in stage_cfgs
        ):
            self._detrend_linear_warned.add(key)
            _log.warning(
                "dsp_detrend_linear_in_live_chain",
                device=device_name,
                nslc=nslc,
            )
        self._installChainRequested.emit(device_name, nslc, chain)
        self._chain_installed.add(key)
        max_samples = self._chain_max_samples_for_fs(fs)
        with self._chain_lock:
            self._chain_queues[key] = deque()
            self._chain_max_q_by_key[key] = max_samples
        # Re-bind the spectrogram to the chain's output fs. The chain
        # may decimate (fs_out != fs_in); a RollingSpectrogram sized
        # for the input fs would have wrong frequency bins for the
        # processed signal it's now being fed.
        self._reinstallSpectrogramRequested.emit(device_name, nslc, float(chain.fs_out))
        # Emit fs_out so TracePlot can size its filtered buffer correctly
        # — important when the chain decimates (fs_out != fs_in).
        self.processedStreamMeta.emit(device_name, nslc, float(chain.fs_out))

    @staticmethod
    def _chain_max_samples_for_fs(fs: float) -> int:
        """Per-stream DSP-queue bound in SAMPLES, derived from fs.

        The queue is sized to hold ``_CHAIN_QUEUE_SECONDS`` of wall-clock
        data at this stream's sampling rate, so a transient flush stall up
        to that long never loses a sample detection should see. Bounding by
        SAMPLES (not packet count) makes the headroom independent of packet
        size: a 4 kSPS stream sending 10-sample packets and one sending
        4000-sample packets both get five seconds of slack. Clamped to a
        hard sample ceiling (``_CHAIN_QUEUE_MAX_SAMPLES``) so a pathological
        high-fs stream stays bounded (CLAUDE.md rule 5). With render now
        decoupled from the flush tick (rule 11), only genuine compute
        saturation can fill this.

        This is the SAMPLE half of a dual bound: :meth:`_enqueue_for_dsp`
        also keeps a packet-count floor (``self._chain_max_q``) so the
        fs-aware budget never shrinks the pre-fix headroom for low-fs /
        large-packet streams.

        Args:
            fs: Stream input sampling rate in Hz.

        Returns:
            Maximum number of buffered SAMPLES before drop-oldest kicks in.
        """
        by_seconds = int(max(1.0, fs) * _CHAIN_QUEUE_SECONDS)
        return max(1, min(by_seconds, _CHAIN_QUEUE_MAX_SAMPLES))

    def _enqueue_for_dsp(
        self,
        key: str,
        samples: np.ndarray,
        t_start: UTCDateTime,
    ) -> None:
        """Append a packet to the per-stream bounded queue. Drops oldest
        on overflow and accumulates a per-stream drop count for periodic
        logging (mirrors `_note_drop` cadence).

        The bound is fs-aware (``_chain_max_q_by_key``, sized in SAMPLES via
        :meth:`_chain_max_samples_for_fs`) so high-fs / small-packet streams
        keep seconds of headroom rather than a thin packet count. Falls back
        to the flat packet-count bound for any stream without a recorded fs
        (none in practice — every chain installs with a known fs)."""
        n_new = int(samples.shape[0]) if samples.ndim else 0
        with self._chain_lock:
            queue = self._chain_queues.get(key)
            if queue is None:
                return
            max_samples = self._chain_max_q_by_key.get(key)
            if max_samples is None:
                # No fs recorded — fall back to the flat packet-count bound.
                while len(queue) >= self._chain_max_q:
                    queue.popleft()
                    self._chain_drops_pending[key] = self._chain_drops_pending.get(key, 0) + 1
                queue.append((samples, t_start))
                return
            buffered = sum(int(s.shape[0]) for s, _ in queue)
            # Drop oldest only when the queue exceeds BOTH bounds:
            #   * the fs-aware SAMPLE budget (seconds of wall-clock data), and
            #   * a PACKET-count floor (``self._chain_max_q`` =
            #     ``refresh_hz * _CHAIN_QUEUE_FACTOR``).
            # The packet floor preserves the pre-fix headroom for low-fs /
            # large-packet streams (which buffer few packets but many
            # seconds), while the sample budget adds real wall-clock headroom
            # for high-fs / small-packet streams (many packets, few samples
            # each) where the flat packet count was thin. A stream only drops
            # when it is over both — i.e. genuinely sustained overflow
            # (CLAUDE.md rule 5). Always keep the incoming packet so a single
            # oversized packet is never silently swallowed.
            while queue and buffered + n_new > max_samples and len(queue) >= self._chain_max_q:
                old, _ = queue.popleft()
                buffered -= int(old.shape[0])
                self._chain_drops_pending[key] = self._chain_drops_pending.get(key, 0) + 1
            queue.append((samples, t_start))

    def _scan_stalls(self) -> None:
        """Flag CONNECTED streams gone silent past their expected cadence (Bug 2).

        Engine-thread only (called from the flush tick). The threshold adapts
        to the sampling rate: a device emitting one ``npts``-sample packet every
        ``npts/fs`` seconds is stalled when nothing arrives for ``_STALL_FACTOR``
        times that, clamped to ``[_STALL_MIN_S, _STALL_MAX_S]``. The flag does
        NOT force a reconnect — a transient gap shorter than obspy's ``netto``
        recovers on the SAME socket with no loss; the flag only resumes
        full-cadence REST polling and surfaces the stall (rule 7 observability).
        Recovery is detected in :meth:`_on_packet`.
        """
        now = time.monotonic()
        if now - self._last_stall_scan_s < _STALL_CHECK_INTERVAL_S:
            return
        self._last_stall_scan_s = now
        for name, status in self._status.items():
            if status.state is not ConnState.CONNECTED or name in self._stalled:
                continue
            last = self._last_packet_monotonic.get(name)
            if last is None:
                continue
            expected = self._expected_packet_interval_s.get(name, 0.0)
            threshold = min(max(_STALL_FACTOR * expected, _STALL_MIN_S), _STALL_MAX_S)
            silent = now - last
            if silent > threshold:
                self._stalled.add(name)
                _log.warning(
                    "seedlink_stream_stalled",
                    device=name,
                    silent_s=round(silent, 1),
                    expected_interval_s=round(expected, 3),
                    threshold_s=round(threshold, 1),
                )
                self.streamStalled.emit(name, True)

    def _flush_all(self) -> None:
        """Tick handler: dispatch DSP work, then flush render.

        Runs on the engine/GUI thread. The DSP dispatch step snapshots
        each stream's bounded queue under lock and posts the snapshot to
        the router thread via ``QueuedConnection``. Drops accumulated on
        the bounded queue are reported via ``chainDropped`` at most every
        ``_DROP_LOG_INTERVAL_S`` seconds per stream.

        Archive packets do NOT pass through this tick (M6.5-A — they
        are posted to the storage thread per-packet in ``_on_packet``,
        so a starved tick cannot touch the science sink).

        Ordering matters for CLAUDE.md rule 11: the FULL-RATE science
        drain (DSP-queue snapshot → ``_drainRequested`` toward detection)
        runs FIRST, before the
        coalescer flush that re-emits ``traceReady`` for the best-effort
        render path. The widget render slots are wired ``QueuedConnection``
        in ``main_window._wire_engine`` so render never runs synchronously
        inside this tick — but doing the science drains first makes the
        decoupling robust even if a future caller re-introduces a
        same-thread render slot: the drains have already fired by then.
        """
        self._scan_stalls()
        snapshots: dict[str, list[tuple[np.ndarray, UTCDateTime]]] = {}
        drops: list[tuple[str, int]] = []
        # Snapshot wall time once outside the lock so every per-stream
        # last-log stamp set in this tick shares the same reference;
        # also keeps the lock's critical section minimal.
        now = time.monotonic()
        with self._chain_lock:
            for key, queue in self._chain_queues.items():
                if queue:
                    snapshots[key] = list(queue)
                    queue.clear()
            for key, count in list(self._chain_drops_pending.items()):
                if count > 0:
                    last = self._chain_drops_last_log.get(key, 0.0)
                    if now - last >= _DROP_LOG_INTERVAL_S:
                        drops.append((key, count))
                        self._chain_drops_pending[key] = 0
                        self._chain_drops_last_log[key] = now

        for key, items in snapshots.items():
            pair = self._key_to_pair.get(key)
            if pair is None:
                continue
            device_name, nslc = pair
            self._drainRequested.emit(device_name, nslc, items)
        for key, count in drops:
            pair = self._key_to_pair.get(key)
            if pair is None:
                continue
            device_name, nslc = pair
            _log.warning(
                "dsp_chain_drop",
                device=device_name,
                nslc=nslc,
                dropped=count,
            )
            self.chainDropped.emit(device_name, nslc, count)

        # Archive packets do not pass through this tick (M6.5-A): they
        # are posted to the storage thread directly from ``_on_packet``,
        # so a starved tick can no longer delay — let alone drop — the
        # science sink (rule 8).

        # Best-effort render LAST. ``coalescer.flushed`` re-emits
        # ``traceReady`` on this thread (cheap concatenate + emit); the
        # actual widget ``setData`` is deferred via the QueuedConnection on
        # the GUI side, so it does not run inside this tick.
        for coalescer in self._coalescers.values():
            coalescer.flush()

    def _note_drop(self, device_name: str, nslc: str, key: str, dropped: int) -> None:
        """Account the display-history ring rolling once it is full.

        The per-stream :class:`RingBuffer` is a fixed
        ``window_seconds * fs * 2`` circular *history* read only by
        on-demand snapshots (PSD, chain-editor preview) via
        :meth:`read_recent`; the live trace plot is fed by the coalescer,
        NOT this buffer. Once the buffer fills, every push evicts the
        oldest sample *by design* — that is the window rolling, NOT
        science-data loss: DSP, detection and storage each consume the
        packet on their own independent queues earlier in
        :meth:`_on_packet`, before this push.

        So we surface the first saturation per stream once at INFO and
        keep the steady-state rolling at DEBUG, rather than emitting a
        WARNING storm that reads as data loss to an operator. The genuine
        rule-5 backpressure signals — ``dsp_chain_drop`` and
        ``streaming_engine_archive_backpressure`` — stay at WARNING,
        because those mean a full-rate consumer actually lost samples.
        (A single packet longer than the entire window would be true loss,
        but that is a misconfiguration — capacity is ``2 * window`` of
        data — not the steady-state path exercised here.)
        """
        if key not in self._ring_saturated:
            self._ring_saturated.add(key)
            _log.info(
                "ring_buffer_saturated",
                device=device_name,
                nslc=nslc,
                note=(
                    "display-history window full; now rolling oldest samples — "
                    "expected, not science-data loss (DSP, detection and storage "
                    "are fed on independent queues)"
                ),
            )
        self._stream_drops_pending[key] = self._stream_drops_pending.get(key, 0) + dropped
        now = time.monotonic()
        last = self._stream_drops_last_log.get(key, 0.0)
        if now - last >= _DROP_LOG_INTERVAL_S:
            total = self._stream_drops_pending.pop(key)
            self._stream_drops_last_log[key] = now
            _log.debug(
                "ring_buffer_overwrite",
                device=device_name,
                nslc=nslc,
                overwritten=total,
            )

    @Slot(str, int, str)
    def _on_state(self, device_name: str, state: int, _msg: str) -> None:
        status = self._status.get(device_name)
        if status is not None:
            status.state = ConnState(state)
        # Stall watchdog (Bug 2): seed the baseline on CONNECT so the first
        # packet has a grace window; on any non-CONNECTED transition clear the
        # baseline + flag so a reconnect starts clean (and a stalled device
        # that drops is un-flagged rather than left stuck stalled).
        if ConnState(state) is ConnState.CONNECTED:
            self._last_packet_monotonic[device_name] = time.monotonic()
        else:
            # On any drop, clear the baseline + flag silently: the GUI's own
            # state handler already removes a non-CONNECTED device from the
            # streaming set, so no streamStalled(False) is needed here (and it
            # would race the state change). Only a genuine packet-resume while
            # still CONNECTED emits the recovery (see _on_packet).
            self._last_packet_monotonic.pop(device_name, None)
            self._stalled.discard(device_name)
        self.deviceStateChanged.emit(device_name, state)

    @Slot(str, str)
    def _on_error(self, device_name: str, msg: str) -> None:
        status = self._status.get(device_name)
        if status is not None:
            status.last_error = msg
        self.errorOccurred.emit(device_name, msg)

    @Slot(str, int, int)
    def _on_stats(self, device_name: str, packets: int, bytes_: int) -> None:
        status = self._status.get(device_name)
        if status is not None:
            status.packets_received = packets
            status.bytes_received = bytes_

    @Slot(str, object)
    def _on_diagnostics(self, device_name: str, diag: object) -> None:
        """Sink for ``WorkerDiagnostics`` payloads from the worker bridge.

        The worker emits a fresh snapshot whenever any diagnostic field
        changes; the engine merges it into the corresponding
        ``DeviceStatus`` so the DevicePanel's 1 Hz timer renders an
        up-to-date view (attempt counter, last failure kind, next retry
        ETA) without needing yet another timer.
        """
        status = self._status.get(device_name)
        if status is None or not isinstance(diag, WorkerDiagnostics):
            return
        status.attempt_count = diag.attempt_count
        status.last_failure_kind = diag.last_failure_kind
        status.next_attempt_at = diag.next_attempt_at
        status.since_first_attempt_at = diag.since_first_attempt_at
        status.last_failure_detail = diag.last_failure_detail

    def _abandon_worker_thread(
        self, name: str, worker: SeedLinkWorker, thread: QThread
    ) -> None:
        """Sever then retain a worker/thread pair whose bounded join timed out.

        Mirrors ``HvsrEngine._disconnect_worker`` + the abandoned-thread
        retention (M6-0 decision log). The worker may still be parked in
        obspy's blocking ``recv`` (seen on macOS); severing its outbound
        signals from the bridge guarantees a late in-flight emit can never
        reach a soon-to-be-torn-down slot, and keeping the Python reference
        stops a later GC from destroying a still-running ``QThread`` (a hard
        "QThread: Destroyed while thread is still running" abort). The pair is
        re-joined and reclaimed by ``_drain_abandoned_threads`` on a later
        ``stop()`` once the socket finally unwinds.
        """
        worker._stop = True
        bridge = self._bridges.get(name)
        if bridge is not None:
            for signal, slot in (
                (worker.packetReceived, bridge._fwd_packet),
                (worker.stateChanged, bridge._fwd_state),
                (worker.errorOccurred, bridge._fwd_error),
                (worker.statsUpdated, bridge._fwd_stats),
                (worker.diagnosticsUpdated, bridge._fwd_diagnostics),
            ):
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect(slot)
        self._abandoned_threads.append((thread, worker))

    def _drain_abandoned_threads(self) -> None:
        """Re-join previously abandoned threads; reclaim the finished ones.

        A pair lands in ``_abandoned_threads`` when its join timed out (obspy
        recv slow to unwind on macOS); by a later ``stop()`` the socket has
        usually closed and the thread finished, so a bounded re-join reclaims
        it. Still-running pairs stay REFERENCED — dropping the last reference
        to a running ``QThread`` aborts. Mirrors ``HvsrEngine.shutdown``'s
        drain; the retained count is logged per rule 5.
        """
        if not self._abandoned_threads:
            return
        still_running: list[tuple[QThread, SeedLinkWorker]] = []
        for thread, worker in self._abandoned_threads:
            if thread.isRunning():
                thread.quit()
                if not thread.wait(_THREAD_JOIN_MS):
                    still_running.append((thread, worker))
        self._abandoned_threads = still_running
        if self._abandoned_threads:
            _log.warning(
                "streaming_engine_abandoned_threads_retained",
                abandoned=len(self._abandoned_threads),
            )

    # ------------------------------------------------------------------
    # Internal device lifecycle (selective stop/start) — wired from
    # tests today; M3 part 2 exposes a public surface on top of this.
    # Public API for dynamic add/remove is intentionally deferred so
    # the contract can be designed against the multi-device-isolation
    # tests rather than reverse-engineered from a UI dialog later.
    # ------------------------------------------------------------------
    def _stop_device(self, name: str) -> None:
        """Stop one device without disturbing the others.

        Joins that device's worker and thread, drops its bridge signal
        connections, and removes its entries from the lifecycle dicts.
        Per-stream state (ring buffers, coalescers, DSP chains) is left
        intact so a subsequent ``_start_device_by_name`` of the same
        device picks up where it left off — useful for the
        stop-one-keep-the-other-running scenario in multi-device tests.
        """
        worker = self._workers.get(name)
        thread = self._threads.get(name)
        bridge = self._bridges.get(name)
        if worker is not None:
            worker.stop()
        if thread is not None:
            thread.quit()
            if not thread.wait(_THREAD_JOIN_MS):
                _log.warning("streaming_engine_thread_join_timeout", device=name)
                # Retain the still-running pair so the pops below cannot drop
                # its last reference and trigger a Qt abort (see stop()).
                if worker is not None:
                    self._abandon_worker_thread(name, worker, thread)
        if bridge is not None:
            for signal in (
                bridge.packetReceivedNamed,
                bridge.stateChangedNamed,
                bridge.errorOccurredNamed,
                bridge.statsUpdatedNamed,
                bridge.diagnosticsUpdatedNamed,
            ):
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect()
        # Drop per-stream coalescers for this device: the engine's
        # ``_flush_all`` timer keeps ticking even with the worker gone,
        # and any data the coalescer had buffered before stop would
        # otherwise replay on the next tick — violating the
        # "stopped device emits no further packets" invariant exercised
        # by ``test_stop_one_device_keeps_other_streaming_then_restart``.
        # Since the high-fs fix made ``coalescer.flushed -> traceReady`` a
        # QueuedConnection (rule 11), this no-replay guarantee covers the
        # SCIENCE path (DSP queue, archive inbox, ring buffer) firmly; a
        # single out-of-band render frame queued in the same tick just
        # before this teardown may still reach the widgets, which is
        # harmless (best-effort display) and below the post-stop settle
        # window the test waits out.
        # Ring buffers, ``_chain_installed`` and DSP chains are
        # preserved so ``_start_device_by_name`` resumes plotting into
        # the same widget without re-emitting ``newStreamSeen``.
        prefix = f"{name}{DEVICE_KEY_SEP}"
        stale_keys = [k for k in self._coalescers if k.startswith(prefix)]
        for k in stale_keys:
            coalescer = self._coalescers.pop(k)
            with contextlib.suppress(RuntimeError, TypeError):
                coalescer.flushed.disconnect()
            coalescer.deleteLater()
            self._key_to_pair.pop(k, None)
        # Spectrogram-router state is INTENTIONALLY preserved across
        # ``_stop_device`` for the same reason the ring buffers and
        # ``_chain_installed`` are: ``_start_device_by_name`` resumes
        # plotting into the existing widgets without re-emitting
        # ``newStreamSeen``, and the next packet for an existing
        # ``key in self._buffers`` does NOT re-fire
        # ``_installSpectrogramRequested``. Clearing the router here
        # would silently kill the post-restart spectrogram. The router's
        # state IS cleared via ``_clearSpectrogramsForDeviceRequested``
        # on the truly-removed branch of ``_on_config_changed`` instead.
        self._workers.pop(name, None)
        self._threads.pop(name, None)
        self._bridges.pop(name, None)
        # Stall-watchdog state (Bug 2): clear it here so EVERY path that stops a
        # device (user stop, config-diff remove, restart) starts clean. Without
        # this, ``_status[name]`` is preserved as a stale CONNECTED and the
        # frozen last-packet time ages past the threshold, so the flush-tick
        # watchdog would flag a stopped device "stalled" forever and resume
        # full-cadence REST polling against a device the user told it to stop.
        self._last_packet_monotonic.pop(name, None)
        self._expected_packet_interval_s.pop(name, None)
        self._stalled.discard(name)
        # Tear down the archive writer for this device. Per-device
        # writers are independent — stopping one's writer does not
        # affect the others. The writer's flush/close happens before
        # the engine's accumulators are dropped so a final ``writeOk``
        # can still update ``archive_bytes_written`` for the row.
        if name in self._archive_writers:
            self._teardown_archive_writer(name)

    def _start_device_by_name(self, name: str) -> None:
        """Re-attach a previously-stopped device by name.

        Looks up the device's config from the engine's current view
        (``_engine_devices`` when populated, ``self._cfg.devices``
        otherwise) and spins up a fresh worker + thread + bridge for
        it. Per-stream state (buffers / coalescers / chains) is
        preserved across the cycle, so the device resumes streaming
        into the same plots.

        CAVEAT (test-only seam): like ``_stop_device``, this does NOT
        touch ``_acq_state`` — it implements mechanics, not user
        intent. A device resumed here after a raw ``_stop_device`` is
        consistent only because neither call moved the state; anything
        user-facing must go through ``start_monitoring`` /
        ``start_recording`` / ``stop(name)`` or ``stop(name)``'s IDLE
        guard will not see the worker.
        """
        # Prefer the engine's current view: Stage B's hot-reload may
        # have advanced ``_engine_devices`` past the original
        # ``_cfg.devices`` list, and looking up there would miss a
        # newly-added device or hit a stale config for a
        # restart-then-rename case.
        source = self._engine_devices or tuple(self._cfg.devices)
        for dev_cfg in source:
            if dev_cfg.name == name:
                self._start_device(dev_cfg)
                return
        raise KeyError(f"unknown device: {name!r}")

    # ------------------------------------------------------------------
    # M5 — archive plumbing
    # ------------------------------------------------------------------
    def _ensure_archive_dao(self, root: Path) -> None:
        """Lazy-open the SESSIONLESS monitoring index (detections only).

        Since M2-B this serves exactly one purpose: persisting STA/LTA
        detections while NO recording session is active (open question 3
        interim). Its row carries ``project_name=NULL``. During a
        session it is a no-op (the session DAO is installed by
        ``start_session``); recording-session DBs are opened there, one
        per session root (rule 14), never here. Like the session path,
        opening sweeps crash-dirty rows first.
        """
        if self._archive_dao is not None:
            return
        db_path = root / "archive.db"
        # Install only after the open + sweep + row insert all succeed,
        # so a failure can never leave a half-initialised DAO (open
        # connection, no session row) behind on the engine.
        dao = ArchiveDao(db_path)
        recovered = dao.close_dirty_sessions()
        session_id = dao.start_session(
            host=socket.gethostname(),
            version=self._version_string(),
            config_hash=self._compute_config_hash(),
        )
        self._archive_dao = dao
        self._archive_db_path = db_path
        self._archive_session_id = session_id
        _log.info(
            "streaming_engine_archive_session_started",
            db=str(db_path),
            session_id=session_id,
            recovered_dirty=recovered,
        )

    def _resolve_db_root(self) -> Path:
        """The app-level BASE archive root (no device, no session).

        Mirrors the no-per-device tail of :meth:`_resolve_archive_root`:
        the top-level ``app.archive_root`` if set, else the platformdirs
        default. Three callers since M2-B: the sessionless monitoring
        index (:meth:`_ensure_archive_dao`), the base ``start_session``
        roots projects under (``ensure_project_root``), and the
        device-less branch of the public :meth:`archive_root` accessor.
        Delegates to :func:`core.session.resolve_base_archive_root` so
        the launch-time crash-recovery sweep resolves identically.
        """
        from echosmonitor.core.session import resolve_base_archive_root

        return resolve_base_archive_root(self._cfg)

    @staticmethod
    def _device_has_detection(dev_cfg: DeviceConfig) -> bool:
        """True if the device's DSP chain contains a detection-producing
        stage (STA/LTA)."""
        return any(getattr(stage, "type", None) == "sta_lta" for stage in dev_cfg.dsp_chain)

    def _ensure_stream_row(self, device_name: str, nslc: str) -> int | None:
        """Resolve (and cache) the ``streams.id`` for one stream.

        Upserts the device + stream rows on first call, then serves from
        the ``_archive_device_ids`` / ``_archive_stream_ids`` caches.
        Returns ``None`` if the DAO is absent, the device config can't be
        found, or the NSLC won't parse — callers skip the write in that
        case. Shared by the archive metadata path and the detection path
        so both reference identical device/stream rows.
        """
        if self._archive_dao is None:
            return None
        if device_name not in self._archive_device_ids:
            dev_cfg = next((d for d in self._engine_devices if d.name == device_name), None)
            if dev_cfg is None:
                return None
            self._archive_device_ids[device_name] = self._archive_dao.upsert_device(
                name=device_name,
                host=dev_cfg.host,
                port=dev_cfg.port,
                config_dict=dev_cfg.model_dump(mode="json"),
            )
        device_id = self._archive_device_ids[device_name]
        key = device_stream_key(device_name, nslc)
        if key not in self._archive_stream_ids:
            # Observed-collision net: first device to register this
            # concrete NSLC owns it; a different device claiming the same
            # NSLC is logged once per (nslc, second-device). Strictly
            # log-only — the stream-row caching below is unchanged.
            owner = self._nslc_first_owner.setdefault(nslc, device_name)
            if owner != device_name:
                gate = (nslc, device_name)
                if gate not in self._nslc_collision_logged:
                    self._nslc_collision_logged.add(gate)
                    _log.warning(
                        "streaming_engine_nslc_collision_observed",
                        nslc=nslc,
                        devices=sorted({owner, device_name}),
                    )
            fs = self._stream_fs.get(key, 0.0)
            try:
                sid = StreamID.from_trace_id(nslc)
            except ValueError:
                return None
            self._archive_stream_ids[key] = self._archive_dao.upsert_stream(
                device_id,
                (sid.network, sid.station, sid.location, sid.channel),
                fs,
            )
        return self._archive_stream_ids[key]

    def _detection_meta(self, device_name: str) -> dict[str, object]:
        """JSON-friendly STA/LTA parameters for the 'why did this fire?'
        detail pane — read from the device's configured ``sta_lta`` stage.
        Empty dict if the device has no such stage (defensive)."""
        for stage in self._device_dsp_cfg.get(device_name, []):
            if getattr(stage, "type", None) == "sta_lta":
                return {
                    "sta_s": float(stage.sta),  # type: ignore[union-attr]
                    "lta_s": float(stage.lta),  # type: ignore[union-attr]
                    "on_thr": float(stage.on_threshold),  # type: ignore[union-attr]
                    "off_thr": float(stage.off_threshold),  # type: ignore[union-attr]
                }
        return {}

    def _bump_detection_status(self, device_name: str, t_on: UTCDateTime) -> None:
        status = self._status.get(device_name)
        if status is not None:
            status.detections_total += 1
            status.last_detection_at = t_on

    @Slot(object, str, str)
    def _on_trigger_fired(self, trigger: object, device_name: str, nslc: str) -> None:
        """Persist a STA/LTA trigger, then announce it.

        Re-emits the public ``triggerFired`` FIRST so M3-era subscribers
        keep their contract, then records the detection and emits the M8
        signals. The DAO write commits before ``detectionRecorded`` /
        ``detectionUpdated`` fire (CLAUDE.md rule 8 — persisted before
        announced).

        Maps the STA/LTA open-trigger contract onto exactly one row:

        * ``t_off is None`` (onset at a packet boundary) → INSERT an open
          row, cache its id by ``(device, nslc)``.
        * ``t_off`` set with a matching cached open row → UPDATE that row
          in place (final ``t_off`` + peak score), then emit
          ``detectionUpdated``.
        * ``t_off`` set with no matching open row (opened+closed inside one
          packet) → INSERT a single closed row.
        """
        # Public contract first — unconditional, even if persistence is off.
        self.triggerFired.emit(trigger, device_name, nslc)
        if not isinstance(trigger, Trigger) or self._archive_dao is None:
            return

        cache_key = device_stream_key(device_name, nslc)
        now = _UTCDateTime()

        if trigger.t_off is None:
            stream_id = self._ensure_stream_row(device_name, nslc)
            if stream_id is None:
                return
            detection = Detection(
                device=device_name,
                nslc=nslc,
                kind="sta_lta",
                t_on=trigger.t_on,
                t_off=None,
                score=float(trigger.peak_ratio),
                detected_at=now,
                meta=self._detection_meta(device_name),
            )
            det_id = self._archive_dao.record_detection(stream_id, detection)
            detection.id = det_id
            self._open_detection_ids[cache_key] = (det_id, trigger.t_on)
            self._bump_detection_status(device_name, trigger.t_on)
            self.detectionRecorded.emit(detection)
            return

        # Finalising event (t_off set).
        open_entry = self._open_detection_ids.pop(cache_key, None)
        if open_entry is not None and open_entry[1] == trigger.t_on:
            det_id = open_entry[0]
            self._archive_dao.update_detection_offtime(
                det_id, trigger.t_off, score=float(trigger.peak_ratio)
            )
            detection = Detection(
                device=device_name,
                nslc=nslc,
                kind="sta_lta",
                t_on=trigger.t_on,
                t_off=trigger.t_off,
                score=float(trigger.peak_ratio),
                detected_at=now,
                meta=self._detection_meta(device_name),
                id=det_id,
            )
            self.detectionUpdated.emit(detection)
            return

        if open_entry is not None:
            # An open row existed but its onset doesn't match this close —
            # leave it open (orphaned) and record the close as its own row.
            # This should not happen given StaLta's single-open-trigger
            # invariant; log it so a regression surfaces.
            _log.warning(
                "streaming_engine_detection_onset_mismatch",
                device=device_name,
                nslc=nslc,
                open_t_on=str(open_entry[1]),
                close_t_on=str(trigger.t_on),
            )
        stream_id = self._ensure_stream_row(device_name, nslc)
        if stream_id is None:
            return
        detection = Detection(
            device=device_name,
            nslc=nslc,
            kind="sta_lta",
            t_on=trigger.t_on,
            t_off=trigger.t_off,
            score=float(trigger.peak_ratio),
            detected_at=now,
            meta=self._detection_meta(device_name),
        )
        detection.id = self._archive_dao.record_detection(stream_id, detection)
        self._bump_detection_status(device_name, trigger.t_on)
        self.detectionRecorded.emit(detection)

    @staticmethod
    def _version_string() -> str:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return str(version("echosmonitor"))
        except PackageNotFoundError:  # pragma: no cover - editable install
            return "0.0.0"

    def _compute_config_hash(self) -> str:
        """SHA256 of the canonical YAML dump of the active RootConfig.

        Uses ``model_dump(mode="json")`` so Pydantic types serialise
        deterministically (e.g. Paths → strings) and ``yaml.safe_dump``
        with ``sort_keys=True`` ensures the same config always produces
        the same hash.
        """
        snapshot = self._cfg.model_dump(mode="json")
        canonical = yaml.safe_dump(snapshot, sort_keys=True).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _resolve_archive_root(self, dev_cfg: DeviceConfig) -> Path:
        """Per-device override → top-level ``app.archive_root`` → platformdirs.

        Resolved lazily at writer construction (engine.start time, or
        on hot-reload toggle) rather than at config-load so that a
        config file moved between machines still works without
        rewriting the absolute path.
        """
        if dev_cfg.archive.root_dir is not None:
            return Path(dev_cfg.archive.root_dir)
        return self._resolve_db_root()

    def _setup_archive_writer(self, dev_cfg: DeviceConfig) -> None:
        name = dev_cfg.name
        if self._archive_thread is None:
            self._archive_thread = QThread(self)
            self._archive_thread.setObjectName("storage")
            self._archive_thread.start()
        if name in self._archive_writers:
            # Idempotent: a config-changed cycle may re-enable an already-
            # configured archive; nothing to do.
            return
        # Session-rooted (rule 14): the writer's tree lands under
        # ``<base>/<project>/<device>/<SDS…>``. Writers only exist
        # while a session is active, so the funnel always adds the
        # project segment here; the bare-base branch serves readers.
        root = self._session_rooted(self._resolve_archive_root(dev_cfg))
        self._ensure_archive_dao(root)
        writer = MseedWriter(name, root, dev_cfg.archive)
        writer.moveToThread(self._archive_thread)
        # Auto-connection (queued on cross-thread) keeps the engine slots
        # on the engine thread, where ``_status`` lives.
        writer.writeOk.connect(self._on_archive_write_ok)
        writer.writeFailed.connect(self._on_archive_write_failed)
        # Stage B: ``flushedFile`` carries the durability claim. The
        # DAO writes happen on the engine thread (queued from the
        # writer's storage thread) — the DAO is thread-safe via
        # threading.local, but funnelling all writes through one
        # thread keeps the row-id caches simple.
        writer.flushedFile.connect(self._on_archive_flushed_file)

        sender = _ArchiveSender(self)
        sender.request.connect(writer.write_trace, type=Qt.ConnectionType.QueuedConnection)

        self._archive_writers[name] = writer
        self._archive_senders[name] = sender
        self._archive_sent[name] = 0
        self._archive_acked[name] = 0
        # ``queue_max`` is the in-flight WARN threshold (M6.5-A): above
        # this many unacknowledged traces toward the storage thread the
        # engine logs + signals backpressure — without dropping.
        self._archive_inflight_warn[name] = dev_cfg.archive.queue_max
        self._archive_jitter_tol_s[name] = dev_cfg.archive.jitter_tolerance_ms / 1000.0
        self._archive_paths_seen[name] = set()
        status = self._status.get(name)
        if status is not None:
            status.archive_enabled = True
            status.archive_last_error = None

        # Kick the writer's fsync timer on its own thread.
        QMetaObject.invokeMethod(
            writer,
            "start",
            Qt.ConnectionType.QueuedConnection,
        )
        _log.info(
            "streaming_engine_archive_writer_started",
            device=name,
            archive_root=str(root),
            queue_max=dev_cfg.archive.queue_max,
        )

    def _teardown_archive_writer(self, name: str) -> None:
        """Synchronously close + drop the writer for one device.

        Called from :meth:`_stop_device` (when a device is removed via
        hot-reload or stopped per-device), from :meth:`start_monitoring`
        (Recording → Monitoring downgrade) and from :meth:`stop` (one
        writer at a time so a slow filesystem on one device does not
        block the others). Uses ``BlockingQueuedConnection`` so the call
        returns only after the storage thread has finished flushing.
        """
        writer = self._archive_writers.pop(name, None)
        sender = self._archive_senders.pop(name, None)
        # No inbox to drain (M6.5-A): every packet was posted to the
        # storage thread at receive time, and those queued write_trace
        # events dispatch FIFO on the storage thread BEFORE the
        # BlockingQueuedConnection close_all below — so everything in
        # flight is fsynced by the close pass. The in-flight gauge for
        # this device is discarded, but its final reading is logged
        # around the close barrier below (rule 7): the barrier's wait is
        # proportional to this backlog.
        inflight_at_close = self._archive_sent.pop(name, 0) - self._archive_acked.pop(name, 0)
        self._archive_jitter_tol_s.pop(name, None)
        self._archive_inflight_warn.pop(name, None)
        self._archive_inflight_last_log.pop(name, None)
        self._archive_paths_seen.pop(name, None)
        # Clear DAO row-id caches and stage-B gap state for every
        # stream that belonged to this device. Without this, a
        # remove-then-readd hot-reload cycle would carry the prior
        # session's GapDetector last_end forward and emit a spurious
        # multi-second "gap" against the freshly-arrived first packet.
        self._archive_device_ids.pop(name, None)
        prefix = f"{name}{DEVICE_KEY_SEP}"
        for stale_key in [k for k in self._archive_stream_ids if k.startswith(prefix)]:
            self._archive_stream_ids.pop(stale_key, None)
        for stale_key in [k for k in self._gap_detectors if k.startswith(prefix)]:
            self._gap_detectors.pop(stale_key, None)
        for stale_key in [k for k in self._pending_gaps if k.startswith(prefix)]:
            self._pending_gaps.pop(stale_key, None)
        # Flush the residual rectification tally (≤ one throttle window)
        # so session totals in the logs are exact, then drop the state.
        for stale_key in [k for k in self._rect_pending if k.startswith(prefix)]:
            residual_count, residual_max = self._rect_pending.pop(stale_key)
            _log.debug(
                "streaming_engine_archive_stamp_rectified",
                device=name,
                stream_key=stale_key,
                n_packets=residual_count,
                max_abs_ms=round(residual_max * 1000.0, 3),
                at="writer_teardown",
            )
        for stale_key in [k for k in self._rect_last_log if k.startswith(prefix)]:
            self._rect_last_log.pop(stale_key, None)
        if writer is None:
            return
        if sender is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                sender.request.disconnect()
            sender.deleteLater()
        # Disconnect engine-side listeners BEFORE the close call so the
        # close pass posts no NEW writeOk/writeFailed events. Terminal
        # events already queued on the engine thread still deliver after
        # this disconnect (Qt does not cancel posted QMetaCallEvents) —
        # that is harmless: the receiving slots guard on dicts this
        # teardown has already cleared (``_ack_archive_trace`` membership,
        # ``_archive_paths_seen.get``, ``_status.get``).
        # ``flushedFile`` is DELIBERATELY left connected: ``close_all``'s
        # final fsync pass emits the last durability claims through it,
        # and ``_on_archive_flushed_file`` (the receiver: the engine,
        # which outlives this teardown) records those final DAO rows.
        # Disconnecting it here would lose the closing fsync's metadata.
        with contextlib.suppress(RuntimeError, TypeError):
            writer.writeOk.disconnect()
        with contextlib.suppress(RuntimeError, TypeError):
            writer.writeFailed.disconnect()
        # Block until the storage thread's slot actually finishes its
        # fsync + close pass. The connection type matters: a plain
        # QueuedConnection here would race ``thread.quit()`` exactly the
        # way the M3p2 / M4 closure flake demonstrated for the DSP
        # router (POSTMORTEMS 2026-05-10 entry "Flaky multi-device
        # tests resolved"). The barrier dispatches after every queued
        # write_trace already in flight (FIFO), so its wait scales with
        # the backlog — start/elapsed logged per rule 7; the backlog is
        # physically bounded by the device ring + live rate.
        close_t0 = time.monotonic()
        if inflight_at_close > 0:
            _log.info(
                "streaming_engine_archive_close_start",
                device=name,
                inflight=inflight_at_close,
            )
        QMetaObject.invokeMethod(
            writer,
            "close_all",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        close_elapsed_ms = (time.monotonic() - close_t0) * 1000.0
        if close_elapsed_ms > 1000.0:
            _log.warning(
                "streaming_engine_archive_close_slow",
                device=name,
                inflight=inflight_at_close,
                elapsed_ms=round(close_elapsed_ms, 1),
            )
        writer.deleteLater()
        status = self._status.get(name)
        if status is not None:
            status.archive_enabled = False

    def _enqueue_for_archive(self, device_name: str, nslc: str, trace: object) -> None:
        """Post one recorded packet straight to the storage thread.

        Never drops (M6.5-A — the archive is the science sink; the
        field run lost 33 s of recorded data to the old bounded-deque
        drop-oldest when a replay burst starved the flush tick). The
        rule-5 seam observability lives in the in-flight gauge: the
        difference between packets sent and the writer's terminal
        acks is the storage event-queue depth; above
        ``archive.queue_max`` it is warn-logged + signalled, throttled
        to one line per ``_DROP_LOG_INTERVAL_S`` per device.
        """
        sender = self._archive_senders.get(device_name)
        if sender is None:
            return
        sender.request.emit(nslc, trace)
        sent = self._archive_sent.get(device_name, 0) + 1
        self._archive_sent[device_name] = sent
        inflight = sent - self._archive_acked.get(device_name, 0)
        if inflight > self._archive_inflight_warn.get(device_name, 1024):
            now = time.monotonic()
            last = self._archive_inflight_last_log.get(device_name, 0.0)
            if now - last >= _DROP_LOG_INTERVAL_S:
                self._archive_inflight_last_log[device_name] = now
                _log.warning(
                    "streaming_engine_archive_backpressure",
                    device=device_name,
                    inflight=inflight,
                    warn_threshold=self._archive_inflight_warn.get(device_name),
                    note="no samples dropped; storage thread is lagging",
                )
                self.archiveBackpressure.emit(device_name, inflight)

    @Slot(str, str, int, object, bool, str)
    def _on_archive_write_ok(
        self,
        device_name: str,
        nslc: str,
        bytes_written: int,
        path: object,
        split: bool,
        encoding_chosen: str,
    ) -> None:
        self._ack_archive_trace(device_name)
        status = self._status.get(device_name)
        if status is not None:
            status.archive_bytes_written += int(bytes_written)
            status.archive_last_write_at = _UTCDateTime()
            status.archive_last_error = None
            paths = self._archive_paths_seen.get(device_name)
            if paths is not None and isinstance(path, Path):
                paths.add(path)
                status.archive_files_open = len(paths)
        self.archiveWriteOk.emit(device_name, nslc, bytes_written, path, split, encoding_chosen)

    @Slot(str, str, str)
    def _on_archive_write_failed(self, device_name: str, nslc: str, reason: str) -> None:
        self._ack_archive_trace(device_name)
        status = self._status.get(device_name)
        if status is not None:
            status.archive_last_error = reason
        self.archiveWriteFailed.emit(device_name, nslc, reason)

    def _ack_archive_trace(self, device_name: str) -> None:
        """Count one writer terminal signal toward the in-flight gauge.

        Accuracy relies on the writer's terminal-signal invariant
        (exactly one ``writeOk`` XOR ``writeFailed`` per ``write_trace``
        — pinned in the MseedWriter module docstring). The clamp to
        ``sent`` prevents underflow; a stale ack from a previous writer
        incarnation dispatching after a teardown→re-setup can still
        transiently make the gauge read LOWER (by at most the backlog
        at teardown) until it self-corrects — an accepted imprecision
        on an advisory signal.
        """
        if device_name not in self._archive_sent:
            return  # writer already torn down; gauge discarded
        acked = self._archive_acked.get(device_name, 0) + 1
        self._archive_acked[device_name] = min(acked, self._archive_sent[device_name])

    @Slot(str, str, object, object, object, int, int)
    def _on_archive_flushed_file(
        self,
        device_name: str,
        nslc: str,
        path: object,
        t_start: object,
        t_end: object,
        bytes_added: int,
        file_size: int,
    ) -> None:
        """DAO bridge slot — runs DAO writes AFTER the writer's fsync.

        This is the strict DB-after-fsync ordering: every row inserted
        here describes data already on disk. ``upsert_device`` /
        ``upsert_stream`` are idempotent; their row IDs are cached so
        the steady state is just ``record_packet`` + ``record_file``
        + zero or more ``record_gap``.

        ``bytes_added`` is the per-fsync delta (additive into
        ``streams.total_bytes``). ``file_size`` is the post-fsync
        durable size from ``os.fstat`` (replaces ``files.bytes`` —
        UPSERT semantics on path require the cumulative size, not a
        delta, otherwise the cross-session restart loses session 1's
        contribution; POSTMORTEMS 2026-05-10).
        """
        if self._archive_dao is None:
            return
        if not isinstance(path, Path):
            return  # defensive — Qt object payload guard
        # Upsert device + stream (cached after first call). Shared with
        # the detection path so both produce identical device/stream rows.
        stream_id = self._ensure_stream_row(device_name, nslc)
        if stream_id is None:
            return

        # Record the file + packet metadata. The cast avoids carrying
        # the Qt-typed ``object`` parameter through to the DAO.
        t_start_v = t_start if isinstance(t_start, _UTCDateTime) else _UTCDateTime(str(t_start))
        t_end_v = t_end if isinstance(t_end, _UTCDateTime) else _UTCDateTime(str(t_end))
        self._archive_dao.record_packet(stream_id, t_end_v, int(bytes_added))
        self._archive_dao.record_file(stream_id, path, t_start_v, t_end_v, int(file_size))

        # Drain any gap events buffered for this stream. Gaps belong
        # to the stream (not the file), so we drain ALL of them on
        # every flushedFile — they reflect discontinuities the
        # detector observed since the last flush.
        key = device_stream_key(device_name, nslc)
        pending = self._pending_gaps.pop(key, [])
        for event in pending:
            self._archive_dao.record_gap(
                stream_id,
                event.t_start,
                event.t_end,
                event.samples_missing,
                event.kind,
            )

    def _observe_gap(self, device_name: str, nslc: str, key: str, fs: float, trace: Trace) -> None:
        """Run the gap detector on ``trace`` and buffer any event.

        Called from :meth:`_on_packet` BEFORE :meth:`_enqueue_for_archive`
        so the detector sees every raw packet exactly once. Buffered
        events are drained in :meth:`_on_archive_flushed_file` after
        the next fsync confirms the corresponding data is durable —
        gaps are tied to the stream, not to any one file, so all
        pending events for a stream flush together regardless of which
        file the writer was filling.

        Stamp rectification (M6.5-B): when the detector judges the
        packet contiguous-within-jitter-tolerance it reports a grid
        correction, applied here to ``trace.stats.starttime`` so the
        WRITER sees exactly-contiguous stamps (no record splits, no
        fragmented reads). Mutating the shared trace is safe at this
        point: every display/DSP consumer in ``_on_packet`` ran before
        the archive branch and captured its own times; the writer is
        the only downstream reader. The correction is ≤ the tolerance
        (10 ms default) — inside the device's own stamping noise.
        """
        detector = self._gap_detectors.get(key)
        if detector is None:
            # The GapDetector's ``stream_id`` field is for log
            # correlation only; the DAO's real ``streams.id`` is
            # looked up at flush time via ``_archive_stream_ids``.
            # Pass 0 here so the field is unambiguously not a DB row id.
            detector = GapDetector(
                stream_id=0,
                sample_rate=fs,
                jitter_tolerance_s=self._archive_jitter_tol_s.get(device_name, 0.0),
            )
            self._gap_detectors[key] = detector
        event = detector.observe(trace)
        snap_s = detector.last_snap_s
        if snap_s != 0.0:
            trace.stats.starttime = trace.stats.starttime + snap_s
            count, max_abs = self._rect_pending.get(key, (0, 0.0))
            self._rect_pending[key] = (count + 1, max(max_abs, abs(snap_s)))
            now = time.monotonic()
            if now - self._rect_last_log.get(key, 0.0) >= _DROP_LOG_INTERVAL_S:
                logged_count, logged_max = self._rect_pending.pop(key)
                self._rect_last_log[key] = now
                _log.debug(
                    "streaming_engine_archive_stamp_rectified",
                    device=device_name,
                    nslc=nslc,
                    n_packets=logged_count,
                    max_abs_ms=round(logged_max * 1000.0, 3),
                )
        if event is None:
            return
        self._pending_gaps.setdefault(key, []).append(event)
        status = self._status.get(device_name)
        if status is not None:
            if event.kind == "gap":
                status.archive_gaps_total += 1
            elif event.kind == "overlap":
                status.archive_overlaps_total += 1
            status.archive_last_gap_at = event.t_start
        _log.info(
            "streaming_engine_archive_gap_detected",
            device=device_name,
            nslc=nslc,
            kind=event.kind,
            samples_missing=event.samples_missing,
        )

    # ------------------------------------------------------------------
    # M4 stage B — hot-reload from ConfigStore
    # ------------------------------------------------------------------
    @Slot()
    def _on_config_changed(self) -> None:
        """Apply the minimum-work diff between old and new device lists.

        Connected to ``ConfigStore.configChanged`` via ``QueuedConnection``
        so the slot runs on the engine thread. Each diff bucket maps
        cleanly to an existing private helper:

        * ``removed`` → :meth:`_stop_device`
        * ``added`` → :meth:`_start_device`
        * ``restart`` → ``_stop_device`` + ``_start_device`` (fresh
          worker; per-stream buffers preserved so plots survive)
        * ``chain_only`` → :meth:`_reinstall_chain` (no socket reset)

        After the diff is applied, the engine's
        :attr:`devicesChanged` signal fires so the
        :class:`StationBrowser` (and any future device-aware widget)
        can refresh its combo / panel.
        """
        if self._store is None or not self._started:
            # Defensive: configChanged is disconnected on stop(), but a
            # queued emit already posted before the disconnect still
            # dispatches after stop() returns (POSTMORTEMS 2026-06-01
            # lesson). ``_store`` is never nulled, so the live guard is
            # ``_started`` — applying a diff to a torn-down engine would
            # queue router work onto the dead DSP thread, to replay
            # stale on the next lazy boot.
            return
        if self._session_transition:
            # We are inside a session transition's processEvents
            # barrier on this same thread. Re-queue the diff for after
            # the transition — the slot re-reads the store, so nothing
            # is lost; applying it mid-swap could restart a recording
            # device against a half-swapped DAO.
            QTimer.singleShot(0, self._on_config_changed)
            return
        new_devices = tuple(self._store.root.devices)
        diff = diff_devices(self._engine_devices, new_devices)
        if diff.is_empty:
            return
        _log.info(
            "streaming_engine_config_diff",
            added=[d.name for d in diff.added],
            removed=list(diff.removed),
            restart=[d.name for d in diff.restart],
            chain_only=[d.name for d in diff.chain_only],
        )
        # Order: removed first (frees thread budget), then added,
        # then restart (which is stop + start). chain_only is fully
        # in-memory and order-independent.
        for name in diff.removed:
            self._stop_device(name)
            # ``_stop_device`` preserves per-stream state so a transient
            # restart can resume into the same plots; here the device
            # is *gone*, so we walk every per-stream dict and drop the
            # entries whose composite key starts with ``f"{name}/"``.
            # Without this cleanup, an add-then-remove cycle leaks a
            # full set of buffers + coalescers + chain queues per
            # cycle for the engine's lifetime.
            prefix = f"{name}/"
            doomed_keys = [k for k in self._key_to_pair if k.startswith(prefix)]
            # Drop the device's spectrograms in one shot — the router's
            # ``clear_for_device`` slot iterates by composite-key prefix
            # exactly as the chain teardown loop does below.
            self._clearSpectrogramsForDeviceRequested.emit(name)
            for key in doomed_keys:
                # Router state — emit BEFORE we drop the local mapping
                # so the queued slot still has a sender on receipt.
                pair = self._key_to_pair.get(key)
                if pair is not None:
                    self._removeChainRequested.emit(pair[0], pair[1])
                self._chain_installed.discard(key)
                self._buffers.pop(key, None)
                # Coalescers are QObjects parented to the engine; popping
                # the dict drops the only ref held by the engine besides
                # the parent. Qt collects them when the parent goes — we
                # don't ``deleteLater`` here because the engine outlives
                # the entry and a delete during emit could race a stale
                # signal in the router thread's queue.
                self._coalescers.pop(key, None)
                self._key_to_pair.pop(key, None)
                self._stream_fs.pop(key, None)
                self._latest_raw_endtime.pop(key, None)
                self._stream_drops_pending.pop(key, None)
                self._stream_drops_last_log.pop(key, None)
                self._ring_saturated.discard(key)
            with self._chain_lock:
                for key in doomed_keys:
                    self._chain_queues.pop(key, None)
                    self._chain_max_q_by_key.pop(key, None)
                    self._chain_drops_pending.pop(key, None)
                    self._chain_drops_last_log.pop(key, None)
            self._device_dsp_cfg.pop(name, None)
            self._status.pop(name, None)
            # (watchdog state already cleared by _stop_device above)
            # A removed device is implicitly idle; announce it so any
            # state badge tracking the device clears before the
            # ``devicesChanged`` refresh below drops the row entirely.
            if self._acq_state.pop(name, AcquisitionState.IDLE) is not AcquisitionState.IDLE:
                self.acquisitionStateChanged.emit(name, int(AcquisitionState.IDLE))
        for cfg_added in diff.added:
            # Rule 13: a device added at runtime registers IDLE and does
            # NOT start — acquisition begins only when the user invokes
            # ``start_monitoring``/``start_recording`` on it.
            self._acq_state.setdefault(cfg_added.name, AcquisitionState.IDLE)
        for cfg_restart in diff.restart:
            # Restart only what the user actually has running; an idle
            # device just absorbs the new config (its next start reads
            # from the advanced ``_engine_devices`` snapshot). A
            # recording device keeps recording across the restart —
            # the writer is torn down with the worker and re-created.
            state = self.acquisition_state(cfg_restart.name)
            if state is not AcquisitionState.IDLE:
                self._stop_device(cfg_restart.name)
                self._start_device(
                    cfg_restart,
                    with_archive=state is AcquisitionState.RECORDING,
                )
            # ``_stop_device`` deliberately preserves per-stream state
            # so plots survive a transient restart, but that includes
            # the previously-installed DspChain in the router. Without
            # this call the new worker's packets would still be processed
            # by the OLD chain instance until the engine restarts.
            # ``_reinstall_chain`` clears the router state and rebuilds
            # synchronously from the new chain config + cached fs. It
            # runs for idle devices too: their preserved router chains
            # (from an earlier session) must track the new config or a
            # later start would resume with a stale chain.
            self._reinstall_chain(cfg_restart)
        for cfg_chain in diff.chain_only:
            self._reinstall_chain(cfg_chain)

        self._engine_devices = new_devices
        self.devicesChanged.emit()

    def _reinstall_chain(self, dev_cfg: DeviceConfig) -> None:
        """Hot-swap the DSP chain for a device without dropping the socket.

        Updates the per-device chain config cache, drops every
        installed chain belonging to this device from the router, and
        then **synchronously rebuilds** the new chain for each stream
        from the updated config + the stream's cached sample rate.

        The synchronous rebuild matters: ``_maybe_install_chain`` is
        only invoked from the "first packet for this stream" branch in
        :meth:`_on_packet`, which has already fired for every existing
        stream. A chain change that simply waited for the next packet
        would silently leave DSP off forever — buffer exists →
        first-packet branch is skipped → ``_chain_installed`` empty →
        ``_enqueue_for_dsp`` is skipped too. Code-reviewer caught this
        in the M4 stage B pass.

        An empty chain config (the user removed all stages) drops the
        chain entirely; the stream falls back to raw-only.
        """
        name = dev_cfg.name
        self._device_dsp_cfg[name] = list(dev_cfg.dsp_chain)
        # Find every installed chain belonging to this device. Composite
        # keys are ``f"{device}/{nslc}"`` so a startswith on the device
        # prefix is the right match. We additionally consider streams
        # the engine knows about (via ``_key_to_pair``) but where the
        # chain wasn't installed — happens during a restart where
        # ``_stop_device`` preserved the buffer but ``_chain_installed``
        # was cleaned by an earlier path. Walking ``_key_to_pair`` is
        # the canonical "every stream this device has buffers for".
        prefix = f"{name}/"
        affected_keys: list[str] = []
        for key in self._key_to_pair:
            if key.startswith(prefix):
                affected_keys.append(key)
        for key in affected_keys:
            self._chain_installed.discard(key)
            pair = self._key_to_pair.get(key)
            if pair is not None:
                self._removeChainRequested.emit(pair[0], pair[1])
        # Also clear the per-stream bounded queue so packets buffered
        # under the OLD chain don't drain into the NEW chain mid-flush.
        with self._chain_lock:
            for key in affected_keys:
                self._chain_queues.pop(key, None)
                self._chain_max_q_by_key.pop(key, None)
                self._chain_drops_pending.pop(key, None)
                self._chain_drops_last_log.pop(key, None)
        # Reset every affected stream's spectrogram back to its raw fs.
        # If ``_maybe_install_chain`` below succeeds in building a new
        # chain, it will queue a second ``_reinstallSpectrogramRequested``
        # at the chain's fs_out; queued signals preserve order, so the
        # final state on the router is the chain's fs_out. If the chain
        # build is skipped (empty new chain), the raw-fs reset is the
        # final state — which matches the post-reinstall packet flow:
        # ``_on_packet`` will hit the "no chain installed" raw branch.
        for key in affected_keys:
            pair = self._key_to_pair.get(key)
            fs = self._stream_fs.get(key)
            if pair is None or fs is None:
                continue
            self._reinstallSpectrogramRequested.emit(pair[0], pair[1], fs)

        # Synchronously rebuild and install for each affected stream
        # using the cached sample rate. ``_maybe_install_chain`` is a
        # no-op when ``dev_cfg.dsp_chain`` is empty, so removing all
        # stages cleanly drops DSP for the device's streams without a
        # special-case branch here.
        for key in affected_keys:
            pair = self._key_to_pair.get(key)
            fs = self._stream_fs.get(key)
            if pair is None or fs is None:
                continue
            device_name, nslc = pair
            try:
                sid = StreamID.from_trace_id(nslc)
            except ValueError:
                _log.warning(
                    "streaming_engine_reinstall_chain_bad_nslc",
                    device=device_name,
                    nslc=nslc,
                )
                continue
            self._maybe_install_chain(device_name, nslc, sid, fs)

    @Slot(str)
    def reconnect_device(self, name: str) -> None:
        """Force an immediate reconnect attempt on a device.

        Tears the worker down and brings it back up with the same
        config. The DevicePanel's "Reconnect now" button is the
        primary caller; tests use it directly to drive a clean
        cycle without waiting for the backoff to elapse.

        Raises:
            KeyError: ``name`` does not match any currently-known
                device.
        """
        # Resolve the config FIRST so a typo surfaces before we tear
        # down anything.
        source = self._engine_devices or tuple(self._cfg.devices)
        cfg_match = next((d for d in source if d.name == name), None)
        if cfg_match is None:
            raise KeyError(f"unknown device: {name!r}")
        # Reconnect restarts what's running; it never starts an idle
        # device (rule 13 — that would be acquisition by side effect).
        state = self.acquisition_state(name)
        if state is AcquisitionState.IDLE:
            _log.info("streaming_engine_reconnect_ignored_idle", device=name)
            return
        self._stop_device(name)
        self._start_device(cfg_match, with_archive=state is AcquisitionState.RECORDING)

    # ------------------------------------------------------------------
    # Test-only accessors (intentionally non-public name)
    # ------------------------------------------------------------------
    def _buffer_for_test(self, device_name: str, nslc: str) -> RingBuffer | None:
        return self._buffers.get(device_stream_key(device_name, nslc))
