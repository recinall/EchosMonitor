"""StreamingEngine — single source of truth for live SeedLink streams.

Owns one ``(QThread, SeedLinkWorker)`` pair per device and per-stream
``RingBuffer`` + ``_StreamCoalescer`` instances. Widgets subscribe to
engine signals; nothing else opens SeedLink connections.

Devices with a non-empty ``dsp_chain`` configuration get a per-stream
``DspChain`` installed on the dedicated DSP ``QThread``. The DSP work
runs off both the network thread and the GUI thread; per-stream
bounded deques apply drop-oldest backpressure (CLAUDE.md rule 5).

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
import hashlib
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import platformdirs
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
from echosmonitor.core.spectrogram_router import _SpectrogramRouter
from echosmonitor.dsp.factory import build_chain
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.gap_detector import GapDetector
from echosmonitor.storage.mseed_writer import MseedWriter

if TYPE_CHECKING:
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

    The engine's ``_flush_all`` drains each device's bounded inbox and
    emits ``request`` once per dequeued packet. The signal is connected
    to the writer's ``write_trace`` slot via ``QueuedConnection`` so the
    work runs on the storage thread without the engine having to hold
    a reference to that thread's event-loop machinery.
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
    # ``archiveBackpressure(device, dropped_count)`` — fired at most every
    # ``_DROP_LOG_INTERVAL_S`` seconds per device, mirroring
    # :meth:`_note_drop` for ring buffers. ``dropped_count`` is the
    # cumulative count since the last emit.
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
        self._bridges: dict[str, _DeviceBridge] = {}
        # Per-stream state is keyed by ``device_stream_key(device, nslc)``
        # so two devices with overlapping NSLC remain independent. The
        # composite key reads naturally in logs ("iris/IU.ANMO.00.BHZ").
        self._buffers: dict[str, RingBuffer] = {}
        self._coalescers: dict[str, _StreamCoalescer] = {}
        self._status: dict[str, DeviceStatus] = {}
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
        # The storage QThread is lazily started in :meth:`start` (or in
        # :meth:`_setup_archive_writer` for hot-reloaded devices) only
        # if at least one device has ``archive.enabled``.
        self._archive_thread: QThread | None = None
        self._archive_writers: dict[str, MseedWriter] = {}
        self._archive_senders: dict[str, _ArchiveSender] = {}
        # Per-device bounded inbox: drop-oldest backpressure happens
        # naturally via ``deque(maxlen=...)``. Drained on every flush
        # tick into the per-device sender → writer pipeline.
        self._archive_inboxes: dict[str, deque[tuple[str, object]]] = {}
        self._archive_drops_pending: dict[str, int] = {}
        self._archive_drops_last_log: dict[str, float] = {}
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
        """Start workers for all configured devices. Idempotent.

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
        for dev_cfg in self._engine_devices:
            self._start_device(dev_cfg)
        # Detections persist independently of MSEED archiving (rule 8: the
        # detection table is part of the same metadata index, but a device
        # may run STA/LTA with ``archive.enabled=False``). If any device
        # has a detection-producing stage, make sure the DAO + session
        # exist even though no archive writer was created. The DAO row
        # writes funnel through the engine thread, like the M5 metadata
        # writes — no extra QThread is needed for them.
        if self._archive_dao is None and any(
            self._device_has_detection(d) for d in self._engine_devices
        ):
            self._ensure_archive_dao(self._resolve_db_root())
        self._flush_timer.start()
        self._started = True

    def stop(self) -> None:
        """Stop all workers and threads. Idempotent.

        With N devices, the wall time of ``stop()`` must be bounded by
        the slowest single device (~2 s in the pathological case), not
        by N x 2 s. We achieve this by parallelising phase 1: each
        ``worker.stop()`` runs on its own helper thread so the total
        wait collapses to the max, not the sum. Phase 2 is already
        parallelised (all ``QThread.quit()`` calls land before any
        ``wait()``).

        By the time we clear the bridge dict no queued signal is still
        in flight from the worker thread to its bridge — which would
        otherwise dispatch onto a torn-down receiver and segfault.
        Bridge signal connections are dropped *after* the threads have
        joined so a stray cross-thread emission cannot reach a soon-to-
        be-released bridge during garbage collection.
        """
        if not self._started:
            return
        # Detach from the ConfigStore first so a queued ``configChanged``
        # event in flight cannot fire after we've torn down the engine
        # state it would mutate.
        if self._store is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                self._store.configChanged.disconnect(self._on_config_changed)
        # Drain any packets still buffered in the per-device archive
        # inboxes BEFORE stopping the flush timer; without this, packets
        # in flight at ``stop()`` time would be silently dropped — the
        # writer would never see them and the disk file would lack
        # those samples. This final drain is best-effort and only
        # covers the steady state; packets dropped earlier by the
        # bounded deque (rule-5 backpressure) are still lost as
        # documented.
        self._drain_archive()
        self._flush_timer.stop()
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
        from PySide6.QtCore import QCoreApplication

        QCoreApplication.processEvents()
        if self._archive_dao is not None:
            if self._archive_session_id is not None:
                self._archive_dao.end_session(self._archive_session_id)
            self._archive_dao.close()
            self._archive_dao = None
            self._archive_session_id = None
            self._archive_device_ids.clear()
            self._archive_stream_ids.clear()
            self._gap_detectors.clear()
            self._pending_gaps.clear()
            self._open_detection_ids.clear()

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
        self._started = False

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
                archive_drops_total=s.archive_drops_total,
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
        the app-level default. Used by the archive reader for historical
        data -- pure path resolution, no I/O.
        """
        if device_name is not None:
            dev_cfg = next((d for d in self._engine_devices if d.name == device_name), None)
            if dev_cfg is not None:
                return self._resolve_archive_root(dev_cfg)
        return self._resolve_db_root()

    def archive_dao(self) -> ArchiveDao | None:
        """The metadata DAO if one exists this session, else ``None``.

        Read-only use by the archive reader to consult the ``files``
        index. ``None`` when no archiving/detection device created a DAO.
        """
        return self._archive_dao

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _start_device(self, dev_cfg: DeviceConfig) -> None:
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
        if dev_cfg.archive.enabled:
            self._setup_archive_writer(dev_cfg)
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
        # never mutates the trace. Bounded deque applies drop-oldest.
        if device_name in self._archive_inboxes:
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

    @Slot()
    def _flush_all(self) -> None:
        """Tick handler: dispatch DSP work + drain archive, then flush render.

        Runs on the engine/GUI thread. The DSP dispatch step snapshots
        each stream's bounded queue under lock and posts the snapshot to
        the router thread via ``QueuedConnection``. Drops accumulated on
        the bounded queue are reported via ``chainDropped`` at most every
        ``_DROP_LOG_INTERVAL_S`` seconds per stream.

        Ordering matters for CLAUDE.md rule 11: the FULL-RATE science
        drains (DSP-queue snapshot → ``_drainRequested`` toward detection,
        and ``_drain_archive`` toward storage) run FIRST, before the
        coalescer flush that re-emits ``traceReady`` for the best-effort
        render path. The widget render slots are wired ``QueuedConnection``
        in ``main_window._wire_engine`` so render never runs synchronously
        inside this tick — but doing the science drains first makes the
        decoupling robust even if a future caller re-introduces a
        same-thread render slot: the drains have already fired by then.
        """
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

        # Storage drain BEFORE the render flush, so a slow render can never
        # gate the archive inbox toward the writer thread (rule 8 + 11).
        self._drain_archive()

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
        """Lazy-create the per-engine DAO + start a session.

        The DAO is keyed off the FIRST resolved archive root. Devices
        whose ``archive.root_dir`` overrides to a different directory
        still write into the same SQLite index — files have absolute
        paths, so the index can describe a multi-root archive even
        though the DB itself lives at a single location.
        """
        if self._archive_dao is not None:
            return
        db_path = root / "archive.db"
        self._archive_dao = ArchiveDao(db_path)
        self._archive_db_path = db_path
        config_hash = self._compute_config_hash()
        self._archive_session_id = self._archive_dao.start_session(
            host=socket.gethostname(),
            version=self._version_string(),
            config_hash=config_hash,
        )
        _log.info(
            "streaming_engine_archive_session_started",
            db=str(db_path),
            session_id=self._archive_session_id,
        )

    def _resolve_db_root(self) -> Path:
        """Root for the metadata DB when no archive writer resolved one.

        Mirrors the no-per-device tail of :meth:`_resolve_archive_root`:
        the top-level ``app.archive_root`` if set, else the platformdirs
        default. Used only for the detection-only path (a device runs
        STA/LTA but archives nothing); when any device archives,
        :meth:`_setup_archive_writer` resolves the root first and this
        is never reached.
        """
        if self._cfg.app.archive_root is not None:
            return Path(self._cfg.app.archive_root)
        return Path(platformdirs.user_data_dir("echosmonitor", "EchosMonitor")) / "archive"

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
        if self._cfg.app.archive_root is not None:
            return Path(self._cfg.app.archive_root)
        return Path(platformdirs.user_data_dir("echosmonitor", "EchosMonitor")) / "archive"

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
        root = self._resolve_archive_root(dev_cfg)
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
        self._archive_inboxes[name] = deque(maxlen=dev_cfg.archive.queue_max)
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
        hot-reload) and from :meth:`stop` (one writer at a time so a
        slow filesystem on one device does not block the others). Uses
        ``BlockingQueuedConnection`` so the call returns only after the
        storage thread has finished flushing.
        """
        writer = self._archive_writers.pop(name, None)
        sender = self._archive_senders.pop(name, None)
        self._archive_inboxes.pop(name, None)
        self._archive_drops_pending.pop(name, None)
        self._archive_drops_last_log.pop(name, None)
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
        if writer is None:
            return
        if sender is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                sender.request.disconnect()
            sender.deleteLater()
        # Disconnect engine-side listeners BEFORE the close call so a
        # late writeOk landing on a torn-down engine slot is impossible.
        # ``flushedFile`` is reserved for Stage B; no connection exists
        # here so we don't disconnect it (Qt would warn-log).
        with contextlib.suppress(RuntimeError, TypeError):
            writer.writeOk.disconnect()
        with contextlib.suppress(RuntimeError, TypeError):
            writer.writeFailed.disconnect()
        # Block until the storage thread's slot actually finishes its
        # fsync + close pass. The connection type matters: a plain
        # QueuedConnection here would race ``thread.quit()`` exactly the
        # way the M3p2 / M4 closure flake demonstrated for the DSP
        # router (POSTMORTEMS 2026-05-10 entry "Flaky multi-device
        # tests resolved").
        QMetaObject.invokeMethod(
            writer,
            "close_all",
            Qt.ConnectionType.BlockingQueuedConnection,
        )
        writer.deleteLater()
        status = self._status.get(name)
        if status is not None:
            status.archive_enabled = False

    def _enqueue_for_archive(self, device_name: str, nslc: str, trace: object) -> None:
        deque_ = self._archive_inboxes.get(device_name)
        if deque_ is None:
            return
        if len(deque_) == deque_.maxlen:
            self._archive_drops_pending[device_name] = (
                self._archive_drops_pending.get(device_name, 0) + 1
            )
        deque_.append((nslc, trace))

    def _drain_archive(self) -> None:
        """Per-tick: drain each device's bounded inbox to the writer.

        Runs in :meth:`_flush_all` after the DSP drain block. Drops are
        rate-limited per device exactly like ring-buffer drops
        (CLAUDE.md rule 5) — at most once every ``_DROP_LOG_INTERVAL_S``
        seconds the cumulative drop count is logged + signalled, then
        zeroed.
        """
        now = time.monotonic()
        for name, inbox in self._archive_inboxes.items():
            sender = self._archive_senders.get(name)
            if sender is not None:
                while inbox:
                    nslc, trace = inbox.popleft()
                    sender.request.emit(nslc, trace)

            pending = self._archive_drops_pending.get(name, 0)
            if pending > 0:
                last = self._archive_drops_last_log.get(name, 0.0)
                if now - last >= _DROP_LOG_INTERVAL_S:
                    _log.warning(
                        "streaming_engine_archive_backpressure",
                        device=name,
                        dropped=pending,
                    )
                    self.archiveBackpressure.emit(name, pending)
                    status = self._status.get(name)
                    if status is not None:
                        status.archive_drops_total += pending
                    self._archive_drops_pending[name] = 0
                    self._archive_drops_last_log[name] = now

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
        status = self._status.get(device_name)
        if status is not None:
            status.archive_last_error = reason
        self.archiveWriteFailed.emit(device_name, nslc, reason)

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
        """
        detector = self._gap_detectors.get(key)
        if detector is None:
            # The GapDetector's ``stream_id`` field is for log
            # correlation only; the DAO's real ``streams.id`` is
            # looked up at flush time via ``_archive_stream_ids``.
            # Pass 0 here so the field is unambiguously not a DB row id.
            detector = GapDetector(stream_id=0, sample_rate=fs)
            self._gap_detectors[key] = detector
        event = detector.observe(trace)
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
        if self._store is None:
            # Defensive: configChanged was disconnected on stop(), but
            # if a queued emit is still in flight when the slot fires,
            # ignore it rather than crashing on a None store.
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
        for cfg_added in diff.added:
            self._start_device(cfg_added)
        for cfg_restart in diff.restart:
            self._stop_device(cfg_restart.name)
            self._start_device(cfg_restart)
            # ``_stop_device`` deliberately preserves per-stream state
            # so plots survive a transient restart, but that includes
            # the previously-installed DspChain in the router. Without
            # this call the new worker's packets would still be processed
            # by the OLD chain instance until the engine restarts.
            # ``_reinstall_chain`` clears the router state and rebuilds
            # synchronously from the new chain config + cached fs.
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
        self._stop_device(name)
        self._start_device(cfg_match)

    # ------------------------------------------------------------------
    # Test-only accessors (intentionally non-public name)
    # ------------------------------------------------------------------
    def _buffer_for_test(self, device_name: str, nslc: str) -> RingBuffer | None:
        return self._buffers.get(device_stream_key(device_name, nslc))
