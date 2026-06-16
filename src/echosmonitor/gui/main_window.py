"""Main application window — dockable workspace around the central tabs.

A `StreamingEngine` is constructed at startup; signals flow to the
`DevicePanel` (state badges + stream rows) and the `LiveTabs` facade
(an "All" overview plus one per-device tab, each rendering its streams).
The window owns a central `QTabWidget` (Detections | Live | PSD | HVSR |
Archive) set via :meth:`setCentralWidget` plus four docks in a stable
workflow order (see :class:`MainWindow`) with focus mode (F11) and dock
detach (Ctrl+Shift+N) layered on by M7.
"""

from __future__ import annotations

import contextlib
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import (
    QByteArray,
    QLockFile,
    QMetaObject,
    QRect,
    QSettings,
    Qt,
    QThread,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QKeySequence,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QWidget,
)

from echosmonitor import __version__
from echosmonitor.config import RootConfig
from echosmonitor.core.archive_browser_loader import ArchiveBrowserLoader
from echosmonitor.core.archive_detail_loader import (
    ArchiveDetailLoader,
    ArchiveDetailResult,
    ComponentTrace,
)
from echosmonitor.core.archive_export_worker import ArchiveExportLoader, ArchiveExportResult
from echosmonitor.core.archive_reindex_worker import (
    ArchiveReindexLoader,
    ArchiveReindexProgressEvent,
    ArchiveReindexResult,
)
from echosmonitor.core.archive_window_loader import (
    ArchiveWindowLoader,
    ArchiveWindowResult,
)
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.core.deconvolution_worker import DeconvolutionWorker
from echosmonitor.core.echos_status import EchosStatusWorker
from echosmonitor.core.hvsr_array import ArrayHvsrResult, HvsrArrayEngine
from echosmonitor.core.hvsr_engine import HvsrEngine
from echosmonitor.core.info_worker import InfoWorker
from echosmonitor.core.models import AcquisitionState, ConnState, EchosPollTarget
from echosmonitor.core.positions import PositionQuery, PositionResolver
from echosmonitor.core.response import ResponseProvider
from echosmonitor.core.session import resolve_base_archive_root
from echosmonitor.core.streaming_engine import StreamingEngine
from echosmonitor.gui.dialogs.first_run_wizard import FirstRunWizard
from echosmonitor.gui.dialogs.shortcuts_dialog import ShortcutsDialog
from echosmonitor.gui.widgets.archive_tab import ArchiveTab
from echosmonitor.gui.widgets.detection_detail import (
    NO_RESPONSE_TOOLTIP,
    DetectionDetailPane,
)
from echosmonitor.gui.widgets.detection_table import DetectionTable
from echosmonitor.gui.widgets.device_panel import DevicePanel
from echosmonitor.gui.widgets.dock_title_bar import DockTitleBar
from echosmonitor.gui.widgets.hvsr_array_widget import HvsrArrayWidget
from echosmonitor.gui.widgets.hvsr_widget import HvsrWidget
from echosmonitor.gui.widgets.live_stack import LiveStack
from echosmonitor.gui.widgets.live_tabs import LiveTabs
from echosmonitor.gui.widgets.log_widget import LogWidget
from echosmonitor.gui.widgets.map_widget import MapWidget
from echosmonitor.gui.widgets.psd_widget import PsdWidget
from echosmonitor.gui.widgets.session_toolbar import SessionToolbar
from echosmonitor.gui.widgets.spectrogram_dock import SpectrogramDock
from echosmonitor.gui.widgets.station_browser import StationBrowser
from echosmonitor.storage.sessions import sweep_dirty_sessions
from echosmonitor.utils.docs import find_manual_tests
from echosmonitor.utils.logging import QtLogSink

if TYPE_CHECKING:
    import numpy as np
    from obspy import UTCDateTime

    from echosmonitor.core.models import Detection, SessionEntry

_log = structlog.get_logger(__name__)

_ORG_NAME = "EchosMonitor"
_APP_NAME = "EchosMonitor"
# Pre-rename QSettings org/app (M0-A). Old window state is reset, not
# migrated (decision log) — :meth:`MainWindow._restore_state` logs once
# when the legacy store exists but the new one is still empty.
_LEGACY_ORG_NAME = "SeedLinkDashboard"
_LEGACY_APP_NAME = "SeedLinkDashboard"

# From-scratch default window size (M7 Stage C1). Bumped from 1280x800 so
# the de-crammed default layout breathes while still fitting a 1080p
# screen. ``_restore_state`` still honours saved geometry, so existing
# users keep their window size — only a cleared-QSettings launch uses
# these.
_DEFAULT_WIDTH = 1600
_DEFAULT_HEIGHT = 1000

# Dock contents minimum sizes (M7 Stage C1), in device-independent px.
# Set on the dock *contents* widgets (not the QDockWidget) so they apply
# whether docked or floating. Kept modest enough that the full set still
# fits a 1080p screen and never blocks the all-docks-hidden fallback,
# focus mode, or dock detach.
_LIVE_MIN_HEIGHT_PX = 400
_SPECTROGRAM_MIN_HEIGHT_PX = 250
_PSD_MIN_WIDTH_PX = 300
_SIDE_DOCK_MIN_WIDTH_PX = 220  # Devices + Stations

# Default dock proportions (M7 Stage C1). The central QTabWidget is the
# primary view now, so the side docks stay secondary and the bottom
# Spectrogram row stays modest — the central area absorbs the rest. Fed
# to ``QMainWindow.resizeDocks`` as relative weights — the absolute pixel
# values matter only by ratio.
_SIDE_DOCK_WIDTH_WEIGHT = 320
# Bottom row: the Spectrogram height. Kept modest so the central tabs
# above it keep the bulk of the vertical space.
_SPECTROGRAM_DOCK_HEIGHT_WEIGHT = 260

# How long ``closeEvent`` blocks waiting for the InfoWorker's QThread
# event loop to exit. Should comfortably exceed the worker's internal
# 2 s ``stop()`` cap so a clean shutdown never logs a join warning.
_INFO_THREAD_JOIN_MS = 2000

# closeEvent join budget for the Echos status poller thread. Its stop()
# cancels the in-flight asyncio poll, so the slot unwinds in
# milliseconds; 2 s matches the other worker-thread budgets.
_ECHOS_THREAD_JOIN_MS = 2000

# How long ``closeEvent`` blocks waiting for the deconvolution QThread's
# event loop to exit. A one-shot deconvolution is sub-second, so a 2 s cap
# is generous.
_DECON_THREAD_JOIN_MS = 2000

# Archive-fallback window padding for physical-unit inspection of a
# scrolled-out detection (B2): read [t_on - PRE, (t_off or t_on) + POST].
_ARCHIVE_INSPECT_PRE_S = 10.0
_ARCHIVE_INSPECT_POST_S = 30.0
# A recomputed recursive STA/LTA needs the long-term average to converge
# *before* the event, or the ratio is flat through the onset and peaks
# spuriously near the right edge (the time-axis bug class — POSTMORTEMS).
# So for an STA/LTA detection the archive read pulls this many LTA windows
# of EXTRA pre-roll ahead of the inspect window; that pre-roll is computed
# but rendered off-screen. The simulation in the H3 diagnosis shows the
# peak already lands on the onset at 1x lta_s; 2x is a safe margin.
_ARCHIVE_RATIO_WARMUP_LTA_MULT = 2.0
# Detection.kind for the STA/LTA detector, whose detail-pane ratio is
# RECOMPUTED from the waveform.
_STA_LTA_DETECTION_KIND = "sta_lta"

# Provisional plot sample-rate used when a stream is first announced before
# the engine has dispatched its `streamMeta` update. The actual value is
# corrected on first arrival.
_PROVISIONAL_FS_HZ = 100.0

# QSettings group under which each dock's last floating geometry is
# persisted, keyed by the dock's objectName. See ``_float_geometry_key``.
_FLOAT_GEOMETRY_GROUP = "floatGeometry"

# Height of the thin focus-mode banner, in device-independent pixels.
_FOCUS_BANNER_HEIGHT_PX = 22

# Dock identifiers must be stable for QSettings to round-trip window state.
_DOCK_DEVICES = "Devices"
_DOCK_STATIONS = "Stations"
_DOCK_SPECTROGRAM = "Spectrogram"
_DOCK_LOG = "Log"


class MainWindow(QMainWindow):
    """Top-level window.

    The primary view is a central :class:`QTabWidget`
    (``self._central_tabs``) set via :meth:`setCentralWidget`, holding
    **Detections | Live | PSD | HVSR | Archive** (Detections is a
    master-detail :class:`QSplitter` — table left, detail pane right).
    The central tabs are not detachable; a solid central widget is the
    robust anchor :class:`QMainWindow` expects.

    Around it the window owns four dock widgets that follow a stable
    workflow order. The order is the contract for the View-menu
    shortcuts (Alt+1..4) and the discoverability story for the user —
    reordering this list is a deliberate API break, not a silent drift,
    and must be paired with an update to the saved-QSettings
    ``windowState`` expectation.

    1. Devices       (``self._device_panel``)        Alt+1 / Ctrl+Shift+1
    2. Stations      (``self._stations_dock``)       Alt+2 / Ctrl+Shift+2
    3. Spectrogram   (``self._spectrogram_dock``)    Alt+3 / Ctrl+Shift+3
    4. Log           (``self._log_dock``)            Alt+4 / Ctrl+Shift+4

    Devices + Stations sit tabbed in the left sidebar; Spectrogram + Log
    sit tabbed in the full-width bottom area.

    The shortcut contract for each dock at index N:

    * **Alt+N** — toggle the dock's *visibility* (View-menu toggle action).
    * **Ctrl+Shift+N** — toggle the dock's *floating* state (detach to /
      re-dock from a top-level OS window). Mirrored by the View ▸ Detach
      submenu and the ⧉ button in each dock's custom title bar.

    Two further window-wide shortcuts drive focus mode (M7 Stage A):

    * **F11** — toggle full-screen focus on the active dock (A1).
    * **Esc** — exit focus mode (only active while focused).

    The full shortcut catalogue is discoverable at runtime via Help ▸
    Keyboard shortcuts… (M7 Stage C3), which renders the data-driven
    :data:`~echosmonitor.gui.dialogs.shortcuts_dialog.SHORTCUT_GROUPS`.
    """

    # M11 B: deconvolution request, emitted from the GUI thread and
    # delivered to ``DeconvolutionWorker.compute`` across the thread
    # boundary (QueuedConnection). Carries
    # ``(token, device, nslc, output, samples, fs, start_epoch)``.
    _deconRequested = Signal(int, str, str, str, object, float, float)  # noqa: N815
    # M1-C: Echos poll-target set, emitted from the GUI thread and
    # delivered to ``EchosStatusWorker.configure`` across the thread
    # boundary (QueuedConnection). Carries tuple[EchosPollTarget, ...].
    _echosTargetsChanged = Signal(object)  # noqa: N815
    # M6.6-B: one-shot StationXML fetch request, delivered to
    # ``EchosStatusWorker.fetch_stationxml`` across the thread boundary
    # (QueuedConnection). Carries tuple[EchosPollTarget, ...].
    _stationXmlFetchRequested = Signal(object)  # noqa: N815
    # M6.6-C: the set of devices whose SeedLink stream is CONNECTED,
    # delivered to ``EchosStatusWorker.set_streaming`` (QueuedConnection)
    # so the poller backs off to the slow heartbeat cadence. Carries a
    # frozenset[str] of device names.
    _streamingDevicesChanged = Signal(object)  # noqa: N815

    def __init__(
        self,
        config: RootConfig,
        config_path: Path,
        parent: QWidget | None = None,
        log_sink: QtLogSink | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._config_path = config_path
        # M6.6-D: the in-app Log tab's source. None (e.g. headless tests)
        # keeps the dock a placeholder so the layout/objectName is stable.
        self._log_sink = log_sink

        self.setWindowTitle("EchosMonitor")
        self.resize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)
        self.setObjectName("MainWindow")

        # M2-C crash recovery: close-as-dirty every session left open by
        # a crash, BEFORE the engine exists (rule 13 keeps everything
        # idle at launch, so nothing in THIS process contends for the
        # DBs and the engine's own live row can never be swept).
        # Synchronous launch bootstrap, bounded by the project count;
        # per-DB failures are logged and skipped inside. A QLockFile on
        # the base root gates the sweep against OTHER EchosMonitor
        # instances: a second instance must not dirty-close a session
        # row the first is actively recording (it is indistinguishable
        # from a crash leftover). The lock is held for the app's
        # lifetime; a crashed holder's lock is auto-detected as stale
        # (dead pid) and reclaimed, so the sweep still runs after a
        # real crash — exactly when it matters.
        base_root = resolve_base_archive_root(config)
        base_root.mkdir(parents=True, exist_ok=True)
        # Kept for the Archive tab's session browser: discovery scans the
        # SAME base root the sweep (and the engine) use, so they can never
        # disagree about where sessions live (rule 14).
        self._base_archive_root = base_root
        self._instance_lock = QLockFile(str(base_root / ".echosmonitor.lock"))
        sweep_t0 = time.monotonic()
        _log.info("session_crash_recovery_sweep_started", root=str(base_root))
        if self._instance_lock.tryLock(0):
            swept = sweep_dirty_sessions(base_root)
            _log.info(
                "session_crash_recovery_sweep_done",
                swept=swept,
                elapsed_s=round(time.monotonic() - sweep_t0, 3),
            )
        else:
            _log.warning(
                "session_crash_recovery_sweep_skipped",
                root=str(base_root),
                reason="another EchosMonitor instance holds the archive lock",
            )

        # ConfigStore (M4 stage B) is the single writer of the user
        # YAML at runtime. The engine subscribes to it for hot-reload;
        # the GUI dialogs route every mutation through it. Constructed
        # before the engine so the engine can pick it up at start.
        self._store = ConfigStore(config, config_path)
        self._engine = StreamingEngine(config, parent=self, store=self._store)
        self._device_panel: DevicePanel | None = None
        # ``_live_tabs`` (M7 Stage B) is the facade all live data routes
        # through. ``_live_stack`` is kept pointing at its "All" overview
        # tab so any leftover references to the old central stack still
        # resolve to the global overview.
        self._live_tabs: LiveTabs | None = None
        self._live_stack: LiveStack | None = None
        self._station_browser: StationBrowser | None = None
        self._log_widget: LogWidget | None = None

        # --- Focus-mode (M7 A1) state ---------------------------------
        # ``_focus_active`` gates the Esc shortcut and tells callers
        # whether a focus session is in progress. ``_focus_target`` is the
        # currently-focused widget — a dock OR the central tabs.
        # ``_focus_target_is_central`` distinguishes the two: a dock focus
        # hides the central tabs and the other docks; a central focus
        # keeps the central tabs shown and hides all docks.
        # ``_focus_saved_state`` holds the ``saveState()`` bytes captured
        # at the *start* of the session — never overwritten while focus
        # stays active, so switching targets cannot nest.
        # ``_focus_was_floating`` / ``_focus_float_geometry`` remember a
        # focused dock's pre-focus floating flag + geometry to restore the
        # "was floating" edge case exactly. ``_focus_central_was_visible``
        # remembers whether the central widget needs re-showing on exit.
        self._focus_active = False
        self._focus_target: QWidget | None = None
        self._focus_target_is_central = False
        self._focus_saved_state: QByteArray | None = None
        self._focus_was_floating = False
        self._focus_float_geometry: QByteArray | None = None
        self._focus_central_was_visible = True
        self._last_interacted_dock: QDockWidget | None = None
        self._focus_banner: QLabel | None = None
        # Central tabs (set in ``_build_central``). Declared here so type
        # checkers see the attribute before construction.
        self._central_tabs: QTabWidget | None = None

        # Single shared InfoWorker on its own QThread (M4 stage A2).
        # Lives on MainWindow rather than inside the StreamingEngine
        # because the streaming worker's ``client.run()`` blocks the
        # device QThread's event loop while connected — a queued INFO
        # slot dispatched onto that thread would never fire. INFO is
        # also a UI concern (one click → one query) and serialises
        # nicely on a single dedicated thread.
        #
        # Note: NO ``thread.started → run`` wiring. The worker has no
        # ``run`` method; each request slot is dispatched directly by
        # the worker QThread's Qt event loop (started by ``thread.start()``
        # below). An earlier draft used a ``queue.Queue`` + ``run()``
        # consumer pattern and deadlocked because Qt's queued-connection
        # cannot dispatch to a thread parked inside ``queue.get()``.
        self._info_thread = QThread(self)
        self._info_thread.setObjectName("info-worker")
        self._info_worker = InfoWorker()
        self._info_worker.moveToThread(self._info_thread)
        self._info_thread.start()

        # M1-C: Echos status poller on its own QThread (one shared worker
        # for all Echos devices — public GETs only, no credentials, so it
        # can never trip the firmware's auth lockout). The timer is
        # constructed inside the queued ``start()`` slot so its thread
        # affinity is the worker thread (qt-worker-threading skill §5).
        # Polling is passive fleet status, not acquisition — rule 13's
        # "nothing starts without the user" applies to the engine, which
        # stays untouched here. Panel wiring + the initial target push
        # happen in ``_wire_engine`` (the panel exists by then).
        self._echos_thread = QThread(self)
        self._echos_thread.setObjectName("echos-status")
        self._echos_worker = EchosStatusWorker()
        self._echos_worker.moveToThread(self._echos_thread)
        self._echosTargetsChanged.connect(
            self._echos_worker.configure, type=Qt.ConnectionType.QueuedConnection
        )
        # M6.6-B: one-shot StationXML fetch on the same worker thread
        # (all device REST funnels through this one poller thread).
        self._stationXmlFetchRequested.connect(
            self._echos_worker.fetch_stationxml, type=Qt.ConnectionType.QueuedConnection
        )
        self._echos_worker.stationXmlReady.connect(
            self._on_stationxml_ready, type=Qt.ConnectionType.QueuedConnection
        )
        # M6.6-C: feed the streaming-device set to the poller so it backs
        # off to the slow heartbeat cadence while a stream is CONNECTED.
        self._streamingDevicesChanged.connect(
            self._echos_worker.set_streaming, type=Qt.ConnectionType.QueuedConnection
        )
        # Per-acquisition fetch de-dup + the latest blob per device (so a
        # monitoring fetch is reused when the device starts recording).
        self._stationxml_requested: set[str] = set()
        self._stationxml_blobs: dict[str, str] = {}
        # M6.6-C: devices currently CONNECTED at the SeedLink level.
        self._streaming_devices: set[str] = set()
        self._echos_thread.start()
        QMetaObject.invokeMethod(
            self._echos_worker, "start", Qt.ConnectionType.QueuedConnection
        )

        # M4: the ONE shared device-position resolver (rule 16) — the Map
        # tab and the multi-device HVSR (M5) both consume this instance.
        # Public credential-less GETs on its own worker thread (it can
        # never trip the auth lockout); like the status poller, position
        # resolution is passive fleet metadata, not acquisition, so rule
        # 13 is untouched. Query push + tab wiring happen in
        # ``_wire_engine`` (the Map tab exists by then).
        self._position_resolver = PositionResolver(parent=self)

        # M11 B: instrument-response deconvolution for the detail pane.
        # The provider is pure config-driven; the worker runs on its OWN
        # dedicated QThread — NOT the engine's science DSP thread (where
        # PsdWorker lives) — so a slow deconvolution can never
        # back-pressure live DSP/detection/storage (CLAUDE.md rule 11).
        self._response_provider = ResponseProvider(
            self._config.devices,
            self._config_path.parent if self._config_path else None,
        )
        self._decon_thread = QThread(self)
        self._decon_thread.setObjectName("deconvolution-worker")
        self._decon_worker = DeconvolutionWorker(self._response_provider)
        self._decon_worker.moveToThread(self._decon_thread)
        self._deconRequested.connect(
            self._decon_worker.compute, type=Qt.ConnectionType.QueuedConnection
        )
        self._decon_worker.deconvolved.connect(
            self._on_deconvolved, type=Qt.ConnectionType.QueuedConnection
        )
        self._decon_worker.failed.connect(
            self._on_deconvolution_failed, type=Qt.ConnectionType.QueuedConnection
        )
        self._decon_thread.start()
        # Current detail context fed to the worker + monotonic token so
        # superseded (stale) results are dropped (latest-wins).
        self._detail_ctx: dict[str, object] = {}
        self._decon_token = 0
        # Archive 3C decon routing: maps a (monotonic, unique) request token
        # to the component letter it deconvolves. Live single-trace decon
        # never populates this — so the live latest-wins path (and its tests)
        # are untouched; an archive result is recognised by its token being
        # present here. Cleared on each new selection / archive batch so a
        # superseded batch's results fall through and are dropped.
        self._decon_components: dict[int, str] = {}
        # Archive deconvolutions still in flight in the current batch; the
        # busy indicator clears only when this reaches 0 (not after the first).
        self._archive_decon_outstanding = 0

        # Off-GUI-thread archive detail loader. Reading a scrolled-out
        # detection's 3 components from the SDS archive (file discovery,
        # obspy.read, NaN-gap array build) is heavy enough that doing it on
        # the GUI thread would hold the GIL and starve the SeedLink worker —
        # the failure that reverted the first Archive Replay attempt (see
        # docs/POSTMORTEMS.md). The loader mirrors HvsrEngine: a parentless
        # worker on a dedicated thread, results delivered via QueuedConnection
        # so the GUI thread only ``setData``s. Read-only (rule 8); shares
        # nothing with the live data path (rule 11). No DAO is captured at
        # construction — since M2-B the engine's DAO is per-session-context,
        # so each request snapshots ``engine.archive_db_path()`` instead and
        # the worker opens it read-only per load (M3-A stale-reference fix).
        self._archive_loader = ArchiveDetailLoader(parent=self)
        self._archive_loader.loaded.connect(
            self._on_archive_detail_loaded, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_loader.failed.connect(
            self._on_archive_detail_failed, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_loader.empty.connect(
            self._on_archive_detail_empty, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_load_token = 0
        self._archive_load_detection: Detection | None = None
        # Display (inspect) window for the in-flight archive load. The READ
        # window can extend further left for the STA/LTA warm-up pre-roll; the
        # view stays anchored to [t_on - PRE, t_ref + POST] so the recomputed
        # ratio's converged peak lines up with the trigger window on screen.
        # ``None`` until a load is dispatched → the pane falls back to the
        # full data span.
        self._archive_view_window: tuple[float, float] | None = None
        # Components held from the most recent archive load, for per-component
        # deconvolution (keyed by component letter).
        self._archive_traces: dict[str, ComponentTrace] = {}

        # Archive tab: an independent off-thread loader (3C window + spectrogram)
        # mirroring the detail loader. Read-only (rule 8); shares nothing with
        # the live data path (rule 11). Its unit-switch deconvolution reuses the
        # same dedicated worker via a SEPARATE token map so it contends neither
        # the live nor the detail-pane decon. Like the detail loader, the
        # metadata index is snapshotted per request, never at construction.
        self._archive_window_loader = ArchiveWindowLoader(parent=self)

        # Session browser loader (M3-A): discovery + per-session trees for
        # the Archive tab, on its own worker thread. Owned here (shutdown
        # joins it with the other loaders); the tab only emits requests.
        self._archive_browser = ArchiveBrowserLoader(parent=self)

        # Data exports (M3-C): MiniSEED/CSV of an archive interval, re-read
        # from the session-scoped archive and written on the export
        # worker's thread (rule 1/8). A serial queue — exports never
        # supersede each other.
        self._archive_export_loader = ArchiveExportLoader(parent=self)
        self._archive_export_loader.exported.connect(
            self._on_archive_export_done, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_export_loader.failed.connect(
            self._on_archive_export_failed, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_export_loader.empty.connect(
            self._on_archive_export_empty, type=Qt.ConnectionType.QueuedConnection
        )
        # Re-indexer (M3-D): rebuild a project archive's DB from its SDS
        # tree on the re-index worker's thread (rules 1/8). The active-
        # session guard lives in the request handler below — the loader
        # never sees the engine (rule 4).
        self._archive_reindex_loader = ArchiveReindexLoader(parent=self)
        self._archive_reindex_token = 0
        # The root the UI's in-flight re-index targets (None when idle):
        # the session-start guard refuses recording into it — the engine
        # opening that archive.db mid-rebuild would be two writers on one
        # DB (rule 8; the inverse of the active-session guard below).
        self._archive_reindex_root: str | None = None
        self._archive_reindex_loader.progressed.connect(
            self._on_archive_reindex_progress, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_reindex_loader.finished.connect(
            self._on_archive_reindex_done, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_reindex_loader.failed.connect(
            self._on_archive_reindex_failed, type=Qt.ConnectionType.QueuedConnection
        )
        # The most recent Archive→HVSR hand-off as (device, session_root,
        # t_start, t_end), so "Run on archive" reads the SAME session the
        # user was browsing (rule 14) — a closed session's data is
        # unreachable via the live engine roots. Keyed on device AND
        # interval: a manual re-target of the HVSR widget (which can run
        # any device/interval without a hand-off) must fall back to the
        # live roots, not silently read a stale session (review finding).
        self._hvsr_archive_ctx: tuple[str, str, float, float] | None = None
        # closeEvent can run TWICE (explicit close + harness teardown);
        # the sessionChanged→refresh bridge is severed exactly once
        # (a second disconnect raises a libpyside RuntimeWarning).
        self._archive_bridge_severed = False
        self._archive_window_loader.loaded.connect(
            self._on_archive_window_loaded, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_window_loader.failed.connect(
            self._on_archive_window_failed, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_window_loader.empty.connect(
            self._on_archive_window_empty, type=Qt.ConnectionType.QueuedConnection
        )
        self._archive_window_token = 0
        self._archive_window_decon: dict[int, str] = {}
        self._archive_window_decon_outstanding = 0
        # Loaded window's components (keyed by letter), for per-component decon.
        self._archive_window_traces: dict[str, ComponentTrace] = {}

        # HVSR analysis engine (best-effort consumer, rule 11). Owns its own
        # dedicated compute thread internally; shares the response
        # provider so it can surface the same-response assumption.
        # Constructed AFTER ``_response_provider`` exists.
        self._hvsr_engine = HvsrEngine(self._engine, self._response_provider, parent=self)
        # M5: the multi-device peer — its own dedicated compute thread,
        # same best-effort contract (rule 11).
        self._hvsr_array_engine = HvsrArrayEngine(
            self._engine, self._response_provider, parent=self
        )

        self._build_central()
        self._build_docks()
        self._build_focus_machinery()
        self._build_menubar()
        self._build_status_bar()
        # M2-C: the session toolbar is the user's acquisition surface
        # (rule 13) — Monitor / Record… / Stop + live session status.
        self._session_toolbar = SessionToolbar(self._engine, self)
        self._session_toolbar.set_session_start_guard(self._session_start_guard)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._session_toolbar)
        self._wire_engine()
        self._restore_state()
        # Reopen the last-used Live tab (by device name). The target tab
        # may not exist yet — LiveTabs remembers it and switches when the
        # device's tab is created on first stream.
        assert self._live_tabs is not None
        self._live_tabs.restore_active_tab()

        # Rule 13: the engine NEVER autostarts. Every device launches
        # IDLE; acquisition begins only when the user acts on the
        # session toolbar (or the device panel). The recent-detections
        # prefill that used to follow autostart is gone with it — the
        # engine has no DAO until a device starts, so the table fills
        # from live sessions only. Historical WAVEFORMS are reachable
        # through the Archive tab's session browser (M3-A); a detection-
        # table history prefill across session DBs remains open (M3).
        _log.info(
            "streaming_engine_idle",
            reason="acquisition is user-controlled (rule 13)",
            device_count=len(self._config.devices),
        )

        _log.info(
            "main_window_ready",
            config_path=str(config_path),
            log_level=config.app.log_level,
            version=__version__,
        )

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_central(self) -> None:
        """Build the central :class:`QTabWidget` (Detections | Live | PSD |
        HVSR | Archive) and set it as the window's central widget.

        A solid central widget is the robust anchor :class:`QMainWindow`
        expects — it fixes the squeeze/freeze class of bug that the old
        weak/empty central placeholder produced. The analysis views
        (DetectionTable + DetectionDetailPane as a master-detail splitter,
        LiveTabs, PsdWidget, HvsrWidget) are constructed here — their
        engine dependencies (``self._engine`` / ``self._hvsr_engine``)
        all exist by the time this runs. The central tabs are
        intentionally not detachable (detach remains for the surviving
        docks).
        """
        self._live_tabs = LiveTabs(
            window_seconds=float(self._config.ui.default_window_seconds),
            cfg=self._config,
            settings_provider=self._settings,
            parent=self,
        )
        self._live_tabs.setObjectName("LiveTabs")
        # Keep _live_stack pointing at the global "All" overview so any
        # remaining references resolve to the same overview behaviour.
        self._live_stack = self._live_tabs.all_stack

        # The four analysis views that used to live in their own docks are
        # now central tabs. Constructed here (deps exist) and never
        # reparented again — their signal wiring binds to the contents
        # widgets, not dock objects, so this move preserves connections.
        self._detection_table = DetectionTable(self)
        self._psd_widget = PsdWidget(engine=self._engine, parent=self)
        self._hvsr_widget = HvsrWidget(self._engine, self._hvsr_engine, parent=self)
        self._hvsr_widget.set_archive_request_handler(self._run_hvsr_archive)
        # M5-B: multi-device HVSR. The geometry snapshot at measurement
        # start comes from the ONE shared PositionResolver (rule 16).
        self._hvsr_array_widget = HvsrArrayWidget(
            self._engine,
            self._hvsr_array_engine,
            self._position_resolver.geometry,
            parent=self,
        )
        self._hvsr_array_widget.set_archive_request_handler(self._run_hvsr_array_archive)
        # The Archive tab (M3-A): session browser + static window view.
        # Session discovery + per-session trees run on the browser loader's
        # worker thread; the tab carries each session's root + archive.db
        # explicitly so CLOSED sessions are readable with no live engine
        # context (the M2-B NOTE in ROADMAP).
        self._archive_tab = ArchiveTab(
            self._archive_browser, self._base_archive_root, parent=self
        )
        self._archive_tab.loadRequested.connect(self._on_archive_window_load_requested)
        self._archive_tab.unitChangeRequested.connect(self._on_archive_window_unit_change)
        self._archive_tab.hvsrRequested.connect(self._handoff_archive_to_hvsr)
        self._archive_tab.exportRequested.connect(self._on_archive_export_requested)
        self._archive_tab.reindexRequested.connect(self._on_archive_reindex_requested)
        # A started/ended session appears in the browser without a manual
        # refresh. Queued: the emit can originate inside engine transitions.
        self._engine.sessionChanged.connect(
            self._archive_tab.refresh_sessions, Qt.ConnectionType.QueuedConnection
        )

        # Detections is a master-detail splitter: the table on the left,
        # the "why did this fire?" detail pane on the right. The table's
        # minimum width is dropped to 0 so the splitter's aggregate
        # minimum tracks the (already-bounded) detail pane — keeping the
        # central width budget < 400 px (the dad5993 / m11 guard).
        self._detail_pane = DetectionDetailPane(self)
        self._detections_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._detections_splitter.setObjectName("DetectionsSplitter")
        self._detections_splitter.addWidget(self._detection_table)
        self._detections_splitter.addWidget(self._detail_pane)
        self._detections_splitter.setChildrenCollapsible(False)
        self._detections_splitter.setStretchFactor(0, 0)
        self._detections_splitter.setStretchFactor(1, 1)
        self._detections_splitter.setSizes([360, 900])
        # Drop the table's hard minimum AND make its width hint Ignored so a
        # wide content sizeHint (e.g. 200 pre-loaded rows) cannot pin the
        # splitter's aggregate minimum — keeping the central width budget
        # bounded (the dad5993 / m11 guard, applied at the host, not by
        # editing the reparented-as-is DetectionTable).
        self._detection_table.setMinimumWidth(0)
        table_policy = self._detection_table.sizePolicy()
        table_policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        self._detection_table.setSizePolicy(table_policy)

        # M4-B: device map. Pure consumer — positions arrive from the
        # shared PositionResolver, state colours from the engine, both
        # wired in ``_wire_engine``.
        self._map_widget = MapWidget(parent=self)

        self._central_tabs = QTabWidget(self)
        self._central_tabs.setObjectName("CentralTabs")
        self._central_tabs.addTab(self._detections_splitter, "Detections")
        self._central_tabs.addTab(self._live_tabs, "Live")
        self._central_tabs.addTab(self._psd_widget, "PSD")
        self._central_tabs.addTab(self._hvsr_widget, "HVSR")
        self._central_tabs.addTab(self._hvsr_array_widget, "HVSR Array")
        self._central_tabs.addTab(self._archive_tab, "Archive")
        self._central_tabs.addTab(self._map_widget, "Map")
        self._central_tabs.setCurrentWidget(self._detections_splitter)
        self.setCentralWidget(self._central_tabs)
        # Show the existing "Select a detection…" empty hint from the start.
        self._detail_pane.clear()

    def _build_docks(self) -> None:
        """Construct every dock instance, then place them via the default layout.

        Every dock is held on ``self._<name>_dock`` (or, for Devices, on
        ``self._device_panel``) so the View menu can iterate them
        deterministically and the Reset-window-layout flow has a stable
        re-place target. Layout placement is deferred to
        :meth:`_apply_default_dock_layout` — the same method that runs
        when the user resets the layout — so there is one source of
        truth for "the default layout".
        """
        self._device_panel = DevicePanel(self)
        # Stage-B injections: the panel needs the store for its toolbar
        # mutations and the engine for the Reconnect-now action. Both
        # methods are idempotent and tolerate being called before docks
        # are fully constructed.
        self._device_panel.set_config_store(self._store)
        self._device_panel.set_engine_for_actions(self._engine)

        self._stations_dock = QDockWidget(_DOCK_STATIONS, self)
        self._stations_dock.setObjectName(f"Dock_{_DOCK_STATIONS}")
        self._station_browser = StationBrowser(
            engine=self._engine,
            info_worker=self._info_worker,
            store=self._store,
            parent=self,
        )
        self._stations_dock.setWidget(self._station_browser)
        self._stations_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)

        # M6 stage 1: real Spectrogram dock (tabbed full-size view, one
        # tab per active stream, fed by the same signal as the inline
        # panes in LiveStack).
        self._spectrogram_dock = QDockWidget(_DOCK_SPECTROGRAM, self)
        self._spectrogram_dock.setObjectName(f"Dock_{_DOCK_SPECTROGRAM}")
        self._spectrogram_widget = SpectrogramDock(
            parent=self._spectrogram_dock,
            window_seconds=float(self._config.ui.default_window_seconds),
        )
        self._spectrogram_dock.setWidget(self._spectrogram_widget)
        self._spectrogram_dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self._log_dock = self._make_log_dock()

        self._apply_dock_minimum_sizes()
        self._install_title_bars()
        self._apply_default_dock_layout()

    def _apply_dock_minimum_sizes(self) -> None:
        """Set dock + central-page minimum sizes (M7 Stage C1).

        Applied to the contained widgets rather than the ``QDockWidget``
        so the minimums hold whether a dock is docked or floating. The
        ``_live_tabs`` (height) and ``_psd_widget`` (width) minimums are
        now central-page minimums; they stay valid — 300 < 400 keeps the
        central width budget safe and the height min is harmless. The
        values are deliberately modest (the whole set still fits 1080p)
        and do not block the all-docks-hidden fallback, focus mode (a
        focused target fills the window, which is larger than any
        minimum), or dock detach.
        """
        assert self._device_panel is not None
        assert self._live_tabs is not None
        self._device_panel.setMinimumWidth(_SIDE_DOCK_MIN_WIDTH_PX)
        if self._station_browser is not None:
            self._station_browser.setMinimumWidth(_SIDE_DOCK_MIN_WIDTH_PX)
        self._live_tabs.setMinimumHeight(_LIVE_MIN_HEIGHT_PX)
        self._spectrogram_widget.setMinimumHeight(_SPECTROGRAM_MIN_HEIGHT_PX)
        self._psd_widget.setMinimumWidth(_PSD_MIN_WIDTH_PX)

    def _install_title_bars(self) -> None:
        """Replace every dock's native title bar with a :class:`DockTitleBar`.

        The native title bar cannot host custom buttons, so each dock gets
        a custom title-bar widget carrying ⛶ (focus) and ⧉ (detach)
        controls plus native-equivalent float/close affordances. The bar's
        label area keeps default mouse handling so Qt's drag-to-move and
        double-click-to-float still work. The label text is sourced from
        the dock ``windowTitle`` so menus/tests that read titles are
        unaffected; ``objectName`` and ``toggleViewAction`` are untouched.
        """
        for dock in self._docks_in_order():
            bar = DockTitleBar(dock.windowTitle(), dock)
            # Late-bind the dock into each lambda via a default arg so the
            # loop variable is captured by value, not by reference.
            bar.focusRequested.connect(lambda d=dock: self._toggle_focus(d))
            bar.detachRequested.connect(lambda d=dock: self._toggle_dock_floating(d))
            bar.closeRequested.connect(dock.hide)
            dock.setTitleBarWidget(bar)

    def _make_placeholder_dock(self, name: str) -> QDockWidget:
        dock = QDockWidget(name, self)
        dock.setObjectName(f"Dock_{name}")
        body = QLabel(f"{name} (placeholder)")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dock.setWidget(body)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        return dock

    def _make_log_dock(self) -> QDockWidget:
        """The Log tab dock (M6.6-D).

        Same identity (``windowTitle``/``objectName``) as the prior
        placeholder so saved QSettings layouts and dock tests keep working;
        only the body changes. With no sink (headless tests) it degrades to
        the placeholder body.
        """
        if self._log_sink is None:
            return self._make_placeholder_dock(_DOCK_LOG)
        dock = QDockWidget(_DOCK_LOG, self)
        dock.setObjectName(f"Dock_{_DOCK_LOG}")
        self._log_widget = LogWidget(self._log_sink, parent=dock)
        dock.setWidget(self._log_widget)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        return dock

    def _docks_in_order(self) -> tuple[QDockWidget, ...]:
        """Four docks in the canonical Alt+1..4 order. See class docstring."""
        assert self._device_panel is not None
        return (
            self._device_panel,
            self._stations_dock,
            self._spectrogram_dock,
            self._log_dock,
        )

    def _apply_default_dock_layout(self) -> None:
        """Place every dock in its default area, tab grouping, and visibility.

        Idempotent — every dock is unconditionally re-added,
        re-tabified, and re-shown, so the call works from any starting
        state including "all docks closed" (the state the View → Reset
        window layout flow can be invoked from).
        """
        assert self._device_panel is not None
        left = Qt.DockWidgetArea.LeftDockWidgetArea
        bottom = Qt.DockWidgetArea.BottomDockWidgetArea

        # Left: Devices (front) + Stations tabbed behind it.
        self.addDockWidget(left, self._device_panel)
        self.addDockWidget(left, self._stations_dock)
        self.tabifyDockWidget(self._device_panel, self._stations_dock)

        # Bottom: Spectrogram + Log (Spectrogram front). The central
        # QTabWidget occupies the remaining centre.
        self.addDockWidget(bottom, self._spectrogram_dock)
        self.addDockWidget(bottom, self._log_dock)
        self.tabifyDockWidget(self._spectrogram_dock, self._log_dock)

        # ``addDockWidget`` alone does not flip a hidden dock back to
        # visible, nor un-detach a floating dock. The reset path may run
        # against a state where the user has hidden or floated docks, so
        # un-float and show() each unconditionally before raising the
        # front-of-tab widgets.
        for dock in self._docks_in_order():
            dock.setFloating(False)
            dock.show()
        self._device_panel.raise_()
        self._spectrogram_dock.raise_()

        # Keep the left sidebar secondary and the bottom Spectrogram row
        # modest so the central QTabWidget absorbs the bulk of the
        # horizontal and vertical space. resizeDocks treats the size lists
        # as relative weights.
        self.resizeDocks([self._device_panel], [_SIDE_DOCK_WIDTH_WEIGHT], Qt.Orientation.Horizontal)
        self.resizeDocks(
            [self._spectrogram_dock],
            [_SPECTROGRAM_DOCK_HEIGHT_WEIGHT],
            Qt.Orientation.Vertical,
        )

    # ------------------------------------------------------------------
    # Focus mode (M7 A1) + dock detach (M7 A2)
    # ------------------------------------------------------------------
    def _build_focus_machinery(self) -> None:
        """Create the window-wide F11 / Esc focus shortcuts.

        F11 toggles focus on the *active* target (see
        :meth:`_active_focus_target` for the deterministic selection
        rule). Esc exits focus mode and is gated by ``_focus_active`` so
        it never swallows Esc when no focus session is in progress.

        The Ctrl+Shift+N *detach* shortcuts are deliberately NOT created
        here as standalone ``QShortcut``s — they are owned by the View ▸
        Detach submenu's ``QAction``s (see :meth:`_build_menubar`), which
        both display the accelerator in the menu (discoverability) and
        dispatch window-wide. Binding the same sequence twice (a window
        ``QShortcut`` *and* a menu ``QAction``) registers two
        ``WindowShortcut``-context entries for one key and triggers Qt's
        "Ambiguous shortcut overload" — neither fires on a real key
        press. One owner, one binding.
        """
        focus_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F11), self)
        focus_shortcut.activated.connect(self._on_focus_shortcut)

        # Esc is wired window-wide but the handler is a no-op unless a
        # focus session is active, so it does not steal Esc from dialogs
        # or other consumers during normal use.
        esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc_shortcut.activated.connect(self._on_escape_shortcut)

    def _active_focus_target(self) -> QWidget:
        """Deterministically pick the widget F11 should act on.

        The target is either a dock or the central :class:`QTabWidget`.
        Rule, in priority order:

        1. The dock that is an ancestor of ``QApplication.focusWidget()``
           (i.e. currently holds keyboard focus).
        2. The central tabs, if they are an ancestor of the focus widget.
        3. The last dock the user interacted with via a title-bar button
           (if still visible).
        4. The first *visible* dock in canonical order.
        5. The central tabs (always exist — never ``None``).
        """
        assert self._central_tabs is not None
        focus_widget = QApplication.focusWidget()
        if focus_widget is not None:
            node: QWidget | None = focus_widget
            docks = set(self._docks_in_order())
            while node is not None:
                if isinstance(node, QDockWidget) and node in docks:
                    return node
                if node is self._central_tabs:
                    return self._central_tabs
                node = node.parentWidget()
        if self._last_interacted_dock is not None and not self._last_interacted_dock.isHidden():
            return self._last_interacted_dock
        for dock in self._docks_in_order():
            if not dock.isHidden():
                return dock
        return self._central_tabs

    def _on_focus_shortcut(self) -> None:
        target = self._active_focus_target()
        if target is None:  # pragma: no cover — defensive; central always exists
            _log.info("focus_toggle_no_active_target")
            return
        self._toggle_focus(target)

    def _on_escape_shortcut(self) -> None:
        if self._focus_active:
            self._exit_focus()

    def _toggle_focus(self, target: QWidget) -> None:
        """Enter focus on ``target``, switch target, or exit if re-toggled.

        ``target`` is a dock OR the central :class:`QTabWidget`. Switching
        is restore-then-enter so the original pre-focus ``saveState``
        bytes are preserved (never overwritten with a focus-mode state),
        which is what makes the round-trip exact.
        """
        if isinstance(target, QDockWidget):
            self._last_interacted_dock = target
        if not self._focus_active:
            self._enter_focus(target)
        elif self._focus_target is target:
            self._exit_focus()
        else:
            # Switch target: restore the original layout first, then
            # re-enter on the new target. This re-captures saved state from
            # the genuine pre-focus layout, never from a focus-mode one.
            self._exit_focus()
            self._enter_focus(target)

    def _enter_focus(self, target: QWidget) -> None:
        """Promote ``target`` to fill the window; hide all else.

        For a dock target: capture ``saveState()`` bytes plus the dock's
        pre-focus floating flag/geometry, hide every other dock and the
        central tabs, then show+raise the dock (un-floating it first so it
        can fill the window). For the central-tabs target: hide all docks
        but keep the central tabs shown — it already fills the centre.
        """
        self._focus_saved_state = self.saveState()
        self._focus_target = target
        self._focus_active = True

        if isinstance(target, QDockWidget):
            self._focus_target_is_central = False
            self._focus_was_floating = target.isFloating()
            self._focus_float_geometry = (
                QByteArray(target.saveGeometry()) if self._focus_was_floating else None
            )
            if self._focus_was_floating:
                target.setFloating(False)

            central = self.centralWidget()
            self._focus_central_was_visible = central is not None and not central.isHidden()
            if central is not None:
                central.hide()

            for other in self._docks_in_order():
                if other is not target:
                    other.hide()
            target.show()
            target.raise_()
            self._show_focus_banner(target.windowTitle())
            _log.info("focus_mode_enter", target=target.objectName())
        else:
            # Central-tabs target: do not hide the central widget; hide all
            # docks so the tabs maximise.
            assert self._central_tabs is not None
            self._focus_target_is_central = True
            self._focus_was_floating = False
            self._focus_float_geometry = None
            for other in self._docks_in_order():
                other.hide()
            self._central_tabs.raise_()
            banner = self._central_tabs.tabText(self._central_tabs.currentIndex())
            self._show_focus_banner(banner)
            _log.info("focus_mode_enter", target="CentralTabs")

    def _exit_focus(self) -> None:
        """Restore the exact pre-focus layout captured in :meth:`_enter_focus`."""
        if not self._focus_active:
            return
        saved = self._focus_saved_state
        focus_target = self._focus_target

        if not self._focus_target_is_central:
            central = self.centralWidget()
            if central is not None and self._focus_central_was_visible:
                central.show()

            if saved is not None:
                self.restoreState(saved)

            # Restore the "was floating" edge case. ``saveState`` persists a
            # dock's floating flag + geometry, so ``restoreState`` normally
            # re-floats it on its own. We only step in if the restore left
            # the dock docked (some platforms / degenerate states don't
            # replay the float), re-floating from the remembered geometry as
            # a safety net. Re-applying geometry when restoreState already
            # floated the dock would perturb the saved-state bytes, so it is
            # intentionally skipped in the common path.
            if (
                isinstance(focus_target, QDockWidget)
                and self._focus_was_floating
                and not focus_target.isFloating()
            ):
                focus_target.setFloating(True)
                if self._focus_float_geometry is not None:
                    focus_target.restoreGeometry(self._focus_float_geometry)
        else:
            # Central-tabs focus left the central widget untouched; just
            # restore the docks' pre-focus state.
            if saved is not None:
                self.restoreState(saved)

        self._hide_focus_banner()
        self._focus_active = False
        self._focus_target = None
        self._focus_target_is_central = False
        self._focus_saved_state = None
        self._focus_was_floating = False
        self._focus_float_geometry = None
        _log.info("focus_mode_exit")

    def _show_focus_banner(self, widget_title: str) -> None:
        """Show the thin focus-mode banner at the top of the window.

        Implemented as a child :class:`QLabel` raised above the dock area
        and repositioned in :meth:`resizeEvent`. It is purely decorative
        and never receives focus, so it does not interfere with the
        focused dock's input.
        """
        if self._focus_banner is None:
            banner = QLabel(self)
            banner.setObjectName("FocusModeBanner")
            banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            banner.setStyleSheet(
                "QLabel#FocusModeBanner {"
                " background-color: rgba(40, 110, 160, 220);"
                " color: white; font-weight: bold; }"
            )
            banner.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._focus_banner = banner
        self._focus_banner.setText(f"{widget_title} — focus mode · press ESC or F11 to exit")
        self._reposition_focus_banner()
        self._focus_banner.show()
        self._focus_banner.raise_()

    def _hide_focus_banner(self) -> None:
        if self._focus_banner is not None:
            self._focus_banner.hide()

    def _reposition_focus_banner(self) -> None:
        if self._focus_banner is None:
            return
        # x, y, width, height form (NOT the two-point QRect ctor, which is
        # corner-inclusive and would over-size the banner by one pixel).
        self._focus_banner.setGeometry(QRect(0, 0, self.width(), _FOCUS_BANNER_HEIGHT_PX))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        if self._focus_active:
            self._reposition_focus_banner()

    # --- Dock detach (A2) ---------------------------------------------
    def _float_geometry_key(self, dock: QDockWidget) -> str:
        """QSettings key for ``dock``'s last floating geometry.

        Keyed by objectName so it round-trips through the same
        ``self._settings()`` path the tests' ``isolated_settings`` fixture
        redirects. Example: ``floatGeometry/Dock_Live``.
        """
        return f"{_FLOAT_GEOMETRY_GROUP}/{dock.objectName()}"

    def _toggle_dock_floating(self, dock: QDockWidget) -> None:
        """Toggle ``dock`` between docked and floating (top-level window).

        On float: ensure the dock is visible, restore its last remembered
        floating geometry (if any), then save geometry. On re-dock: persist
        the current floating geometry first so the next detach restores it.
        """
        self._last_interacted_dock = dock
        settings = self._settings()
        if dock.isFloating():
            # About to re-dock — remember where it was.
            settings.setValue(self._float_geometry_key(dock), dock.saveGeometry())
            dock.setFloating(False)
            _log.info("dock_redocked", dock=dock.objectName())
        else:
            if dock.isHidden():
                dock.show()
            dock.setFloating(True)
            saved = settings.value(self._float_geometry_key(dock))
            if isinstance(saved, (bytes, bytearray, QByteArray)):
                dock.restoreGeometry(saved)
            # Persist immediately so a detach→redock→detach cycle that
            # never moves the window still round-trips a known geometry.
            settings.setValue(self._float_geometry_key(dock), dock.saveGeometry())
            _log.info("dock_detached", dock=dock.objectName())

    # ------------------------------------------------------------------
    # Menubar
    # ------------------------------------------------------------------
    def _build_menubar(self) -> None:
        """Wire the File / View / Help menus.

        Runs after :meth:`_build_docks` so dock toggle-actions can be
        bound to real :class:`QDockWidget` instances. The File menu's
        device actions delegate to the same QActions the DevicePanel's
        toolbar uses (via the ``add_action`` / ``edit_action`` /
        ``remove_action`` properties) — there is no parallel handler
        path, so enabled-state, tooltips, and slot logic stay in one
        place.
        """
        assert self._device_panel is not None
        bar = self.menuBar()

        # File ----------------------------------------------------------
        file_menu = bar.addMenu("&File")
        new_action = QAction("&New device…", self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(self._device_panel.add_action.trigger)
        # Defence in depth: ``add_action`` is gated on the panel having
        # a ConfigStore. Mirror that enabled-state to the File menu so a
        # future re-ordering of store-injection cannot leave a menu
        # entry whose underlying handler is a no-op.
        self._mirror_enabled(self._device_panel.add_action, new_action)
        file_menu.addAction(new_action)

        edit_action = QAction("&Edit device…", self)
        edit_action.setShortcut("Ctrl+E")
        edit_action.triggered.connect(self._device_panel.edit_action.trigger)
        self._mirror_enabled(self._device_panel.edit_action, edit_action)
        file_menu.addAction(edit_action)

        remove_action = QAction("&Remove device…", self)
        remove_action.setShortcut("Del")
        remove_action.triggered.connect(self._device_panel.remove_action.trigger)
        self._mirror_enabled(self._device_panel.remove_action, remove_action)
        file_menu.addAction(remove_action)

        file_menu.addSeparator()
        settings_action = QAction("&Settings…", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._on_show_settings)
        file_menu.addAction(settings_action)

        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # View ----------------------------------------------------------
        view_menu = bar.addMenu("&View")
        for index, dock in enumerate(self._docks_in_order(), start=1):
            toggle = dock.toggleViewAction()
            # Use the dock's window title verbatim so the menu reads
            # "Devices", "Stations", etc.
            toggle.setText(dock.windowTitle())
            toggle.setShortcut(f"Alt+{index}")
            view_menu.addAction(toggle)

        # Detach submenu: one entry per dock (canonical order) that
        # toggles its floating state. Mirrors the Ctrl+Shift+N shortcuts
        # and the ⧉ title-bar button — three discoverability paths for the
        # same action.
        view_menu.addSeparator()
        detach_menu = view_menu.addMenu("&Detach")
        for index, dock in enumerate(self._docks_in_order(), start=1):
            action = QAction(dock.windowTitle(), self)
            action.setShortcut(f"Ctrl+Shift+{index}")
            action.triggered.connect(lambda _checked=False, d=dock: self._toggle_dock_floating(d))
            detach_menu.addAction(action)

        view_menu.addSeparator()
        self._markers_action = QAction("Show &detection markers", self)
        self._markers_action.setCheckable(True)
        self._markers_action.setChecked(True)
        self._markers_action.toggled.connect(self._on_toggle_markers)
        view_menu.addAction(self._markers_action)

        view_menu.addSeparator()
        reset_action = QAction("&Reset window layout…", self)
        reset_action.triggered.connect(self._on_reset_window_layout)
        view_menu.addAction(reset_action)

        # Help ----------------------------------------------------------
        help_menu = bar.addMenu("&Help")
        wizard_action = QAction("&First-run wizard…", self)
        wizard_action.triggered.connect(self._on_show_first_run_wizard)
        help_menu.addAction(wizard_action)
        manual_action = QAction("&Manual tests…", self)
        manual_action.triggered.connect(self._on_open_manual_tests)
        help_menu.addAction(manual_action)
        shortcuts_action = QAction("&Keyboard shortcuts…", self)
        shortcuts_action.triggered.connect(self._on_show_shortcuts)
        help_menu.addAction(shortcuts_action)
        help_menu.addSeparator()
        about_action = QAction("&About EchosMonitor", self)
        about_action.triggered.connect(self._on_show_about)
        help_menu.addAction(about_action)

    def _mirror_enabled(self, source: QAction, target: QAction) -> None:
        """Keep ``target.enabled`` synchronised with ``source.enabled``.

        QAction emits ``changed`` on every property update — including
        enabled-state — so a single connection is enough to mirror the
        DevicePanel's selection-gated toolbar QActions onto the File
        menu's wrapper actions.
        """
        target.setEnabled(source.isEnabled())
        source.changed.connect(lambda: target.setEnabled(source.isEnabled()))

    def _on_reset_window_layout(self) -> None:
        """View → Reset window layout. Confirm, clear, re-apply, resize."""
        answer = QMessageBox.question(
            self,
            "Reset window layout?",
            "Reset all window layout to defaults? Your geometry, dock "
            "positions, and visibility will be reset. Open devices and "
            "config are unaffected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # Reset must exit focus first: a default layout applied underneath
        # an active focus session would leave hidden docks and a stale
        # banner. Exiting clears both before re-placing docks.
        if self._focus_active:
            self._exit_focus()
        settings = self._settings()
        settings.remove("geometry")
        settings.remove("windowState")
        self._apply_default_dock_layout()
        self.resize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)
        _log.info("window_layout_reset")

    def _on_show_settings(self) -> None:
        """File → Settings… (M6): archive root, theme, display caps."""
        from echosmonitor.gui.dialogs.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self, self._store)
        dialog.exec()
        dialog.deleteLater()

    def _on_show_first_run_wizard(self) -> None:
        """Help → First-run wizard. Safe to call against a populated config."""
        wizard = FirstRunWizard(
            store=self._store,
            parent=self,
        )
        wizard.exec()
        wizard.deleteLater()

    def _on_show_shortcuts(self) -> None:
        """Help → Keyboard shortcuts. Open the read-only reference modal."""
        dialog = ShortcutsDialog(self)
        dialog.exec()

    def _on_open_manual_tests(self) -> None:
        """Help → Manual tests. Open MANUAL_TESTS.md in the system viewer."""
        path = find_manual_tests()
        if path is None:
            QMessageBox.information(
                self,
                "Manual tests",
                "Manual tests document is not bundled in this install.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_show_about(self) -> None:
        QMessageBox.about(
            self,
            "About EchosMonitor",
            f"<b>EchosMonitor</b> v{__version__}<br>"
            "Real-time seismic streaming and analysis.<br>"
            "<a href='—'>Project repository</a>",
        )

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        self.setStatusBar(bar)

        # Persistent left-side tip (M4 stage C). Mounted via
        # ``addWidget`` (not ``showMessage``) so it survives the
        # transient ``showMessage(error, 5000)`` calls in
        # ``_on_engine_error``. Qt resets a temporary message after
        # its timeout expires; permanent widgets re-emerge automatically
        # whereas a ``showMessage(text, 0)`` tip would disappear with
        # no auto-restore. Original Stage C draft used the timeout=0
        # form; code-reviewer caught the auto-restore failure.
        self._devices_tip_label = QLabel("")
        self._devices_tip_label.setObjectName("StatusBarDevicesTip")
        self._devices_tip_label.setStyleSheet(
            "QLabel#StatusBarDevicesTip { color: #888; font-style: italic; }"
        )
        bar.addWidget(self._devices_tip_label, stretch=1)

        bar.addPermanentWidget(QLabel(f"cfg → {self._config_path}"))
        bar.addPermanentWidget(QLabel(f"log → {self._config.app.log_level}"))
        bar.addPermanentWidget(QLabel(f"v{__version__}"))

        # Dim italic note when one or more configured devices have no
        # `dsp_chain`. Tooltip lists the device names. Informational, not
        # a warning colour — empty chain is a valid configuration.
        no_chain_devices = [d.name for d in self._config.devices if not d.dsp_chain]
        self._no_chain_label = QLabel("")
        self._no_chain_label.setObjectName("StatusBarNoChainNote")
        self._no_chain_label.setStyleSheet(
            "QLabel#StatusBarNoChainNote { color: #888; font-style: italic; }"
        )
        bar.addPermanentWidget(self._no_chain_label)
        self._update_no_chain_note(no_chain_devices)

        # Initialise the tip + subscribe to the store. Engine's
        # ``devicesChanged`` would also fire on the same mutation but
        # ``store.configChanged`` is the source of truth for the
        # device list, so subscribing to one is sufficient and avoids
        # duplicate work.
        self._refresh_devices_tip()
        self._store.configChanged.connect(self._refresh_devices_tip)

    def _refresh_devices_tip(self) -> None:
        """Show / hide the empty-config tip on the status bar.

        Mutates the dedicated permanent QLabel so engine-error
        ``showMessage`` calls don't fight with this state.
        """
        if not self._store.root.devices:
            self._devices_tip_label.setText("Tip: open the Devices dock to add your first server.")
        else:
            self._devices_tip_label.setText("")

    def _update_no_chain_note(self, no_chain_devices: list[str]) -> None:
        """Update the dim italic suffix in the status bar when one or more
        configured devices declare an empty (or missing) ``dsp_chain``.

        The label is permanently mounted so its visibility is toggled via
        text and tooltip rather than show/hide cycles."""
        if no_chain_devices:
            self._no_chain_label.setText(f" · {len(no_chain_devices)} device(s) without DSP chain")
            self._no_chain_label.setToolTip(
                "Devices without a dsp_chain show only the raw trace.\n"
                + "\n".join(f"• {name}" for name in no_chain_devices)
            )
        else:
            self._no_chain_label.setText("")
            self._no_chain_label.setToolTip("")

    # ------------------------------------------------------------------
    # Engine wiring
    # ------------------------------------------------------------------
    def _wire_engine(self) -> None:
        assert self._device_panel is not None
        assert self._live_tabs is not None
        # Same-thread connections (Auto resolves to Direct); cross-thread is
        # already Queued inside the engine. No explicit Queued needed here.
        self._engine.deviceStateChanged.connect(self._device_panel.on_device_state)
        # M6.6-C: track SeedLink CONNECTED state to drive the poll backoff.
        self._engine.deviceStateChanged.connect(self._on_device_state_streaming)
        # M2-C acquisition badges (rule 13): queued so the panel's
        # handler never runs re-entrantly inside an engine emit.
        self._engine.acquisitionStateChanged.connect(
            self._device_panel.on_acquisition_state,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # M6.6-B: auto-fetch + persist the device StationXML when a device
        # enters monitoring/recording (rule 13 — user-triggered, off the
        # GUI thread via the status-poller worker).
        self._engine.acquisitionStateChanged.connect(
            self._on_acquisition_stationxml,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._engine.deviceStateChanged.connect(self._live_tabs.set_device_state)
        self._engine.newStreamSeen.connect(self._device_panel.on_new_stream)
        self._engine.newStreamSeen.connect(self._on_new_stream)
        # Render path is best-effort (CLAUDE.md rule 11). The engine lives on
        # the GUI thread, so the engine re-emits ``traceReady`` via a cheap
        # same-thread DirectConnection *inside* ``_flush_all``. To keep the
        # slow widget ``setData`` out of that tick, the WIDGET render slots
        # are wired QueuedConnection: even on the same thread this defers the
        # render to a later event-loop turn, so it never runs synchronously
        # inside the flush/drain tick that feeds DSP/detection and storage.
        self._engine.traceReady.connect(
            self._on_trace_ready, type=Qt.ConnectionType.QueuedConnection
        )
        self._engine.processedTraceReady.connect(
            self._on_processed_trace_ready, type=Qt.ConnectionType.QueuedConnection
        )
        self._engine.processedStreamMeta.connect(self._on_processed_stream_meta)
        self._engine.streamMeta.connect(self._on_stream_meta)
        self._engine.chainDropped.connect(self._on_chain_dropped)
        self._engine.triggerFired.connect(self._on_trigger_fired)
        # M8 detections: feed the table (persisted-before-announced, so the
        # row is durable by the time these fire). The table is the source of
        # truth; selection drives the central detail pane.
        self._engine.detectionRecorded.connect(self._detection_table.on_detection_recorded)
        self._engine.detectionUpdated.connect(self._detection_table.on_detection_updated)
        # Markers on trace plots + spectrograms (C1/C2).
        self._engine.detectionRecorded.connect(self._on_detection_markers)
        self._engine.detectionUpdated.connect(self._on_detection_update_markers)
        self._detection_table.detectionSelected.connect(self._on_detection_selected)
        self._detection_table.focusDetectionRequested.connect(self._on_focus_detection_requested)
        # M11 B: physical-unit inspection. The detail pane requests a unit
        # change; the context-menu "Inspect in …" pre-selects a detection
        # then drives the same unit-change path.
        self._detail_pane.unitChangeRequested.connect(self._on_unit_change_requested)
        self._detail_pane.componentLayoutChanged.connect(self._on_component_layout_changed)
        self._detection_table.inspectInUnitRequested.connect(self._on_inspect_in_unit)
        self._engine.errorOccurred.connect(self._on_engine_error)
        # M6 stage 1: spectrogram fan-out. The engine emits one column
        # per stream per STFT step; both the inline LiveStack pane and
        # the tabbed Spectrogram dock consume the same signal — no
        # double computation.
        self._engine.spectrogramColumnReady.connect(self._live_tabs.on_spectrogram_column)
        self._engine.spectrogramColumnReady.connect(self._spectrogram_widget.on_column)
        # M7 Stage B: prune device tabs when a device is removed from
        # config. configChanged is the source of truth for the device set
        # (the engine's devicesChanged fires on the same mutation).
        self._store.configChanged.connect(self._on_config_devices_changed)
        # 1 Hz packet/byte counters in the Devices dock are driven by a
        # poll closure over the engine's status snapshot — no extra
        # signals needed. The same poll feeds the Diagnostics column
        # (attempt counter / last failure / next retry seconds), so we
        # also push per-device endpoints + connect timeouts so the panel
        # can render the manual-test tooltip text correctly.
        self._device_panel.set_status_provider(self._engine.device_status)
        self._device_panel.set_device_endpoints(
            {d.name: (d.host, d.port) for d in self._config.devices}
        )
        self._device_panel.set_connect_timeouts(
            {d.name: float(d.reconnect.connect_timeout_s) for d in self._config.devices}
        )
        # M1-C: Echos status snapshots into the panel's Echos column.
        # Worker → GUI is cross-thread, so both connections are
        # explicitly Queued; payload isinstance-guarded in the slot.
        self._echos_worker.snapshotReady.connect(
            self._device_panel.on_echos_snapshot, type=Qt.ConnectionType.QueuedConnection
        )
        self._echos_worker.pollFailed.connect(
            self._device_panel.on_echos_poll_failed, type=Qt.ConnectionType.QueuedConnection
        )
        # Target set follows the config: push now and on every change
        # (devices added/removed/edited in the dialog).
        self._push_echos_targets()
        self._store.configChanged.connect(self._push_echos_targets)
        # M4-B: Map tab wiring. The resolver re-emits on the GUI thread,
        # so widget connections are same-thread; the engine's
        # acquisition-state emit is wired Queued for the same reentrancy
        # reason as the panel badge above.
        self._position_resolver.positionResolved.connect(self._map_widget.on_position)
        self._position_resolver.positionFailed.connect(self._map_widget.on_position_failed)
        self._map_widget.refreshRequested.connect(self._position_resolver.refresh)
        self._map_widget.deviceSelected.connect(self._device_panel.select_device)
        self._engine.acquisitionStateChanged.connect(
            self._map_widget.on_acquisition_state,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._engine.deviceStateChanged.connect(self._map_widget.on_device_state)
        self._push_position_queries()
        self._store.configChanged.connect(self._push_position_queries)
        # M5-B: array-HVSR f0 → map overlay. A new array run clears the
        # previous overlay (stale colouring must not blend with fresh
        # results); the overlay itself persists across stop — the last
        # result stays valid until cleared or superseded.
        self._hvsr_array_engine.arrayUpdated.connect(self._on_array_hvsr_updated)
        self._hvsr_array_engine.arrayMeasurementStarted.connect(self._on_array_hvsr_started)

    def _on_array_hvsr_started(self, _measurement_id: str, _summary: object) -> None:
        self._map_widget.clear_f0_overlay()

    def _on_array_hvsr_updated(self, payload: object) -> None:
        """Colour the map markers by each station's f0 (M5-B, rule 4 guard)."""
        if not isinstance(payload, ArrayHvsrResult):
            return
        overlay = {
            device: result.f0_hz
            for device, result in payload.results.items()
            if result.n_windows_valid > 0 and math.isfinite(result.f0_hz) and result.f0_hz > 0
        }
        # Unconditional: each cycle is self-contained (ArrayHvsrResult
        # contract), so a no-f0 cycle honestly clears the colouring rather
        # than letting a previous cycle's colours linger over fresh errors.
        self._map_widget.set_f0_overlay(overlay)

    def _push_position_queries(self) -> None:
        """Rebuild the position-resolver query set from config (M4, rule 16).

        Every configured device is queried: Echos nodes resolve via
        override/StationXML/GNSS; generic SeedLink devices (no ``echos``
        section, hence no override either) are honestly ``unavailable``.
        The Map tab learns the device set FIRST so results arriving on
        later event-loop turns always find their device known, then gets
        the cache snapshot configure() preserved.
        """
        devices = self._store.root.devices
        queries = tuple(
            PositionQuery(
                name=d.name,
                host=d.host,
                http_port=d.echos.http_port if d.echos is not None else 80,
                has_rest=d.echos is not None,
                override=(
                    (
                        d.echos.position_override.lat,
                        d.echos.position_override.lon,
                        d.echos.position_override.elev_m,
                    )
                    if d.echos is not None and d.echos.position_override is not None
                    else None
                ),
            )
            for d in devices
        )
        self._map_widget.set_devices(tuple(d.name for d in devices))
        self._position_resolver.configure(queries)
        self._map_widget.set_positions(self._position_resolver.positions())

    def _push_echos_targets(self) -> None:
        """Rebuild the Echos poll-target set from config (M1-C).

        Only devices with an ``echos:`` section are polled; the rest are
        generic SeedLink servers with no REST API. The full tuple is the
        payload — the worker treats it as a replacement set.
        """
        targets = tuple(
            EchosPollTarget(
                name=d.name,
                host=d.host,
                http_port=d.echos.http_port,
                poll_interval_s=d.echos.poll_interval_s,
                poll_interval_streaming_s=d.echos.poll_interval_streaming_s,
            )
            for d in self._store.root.devices
            if d.echos is not None
        )
        self._echosTargetsChanged.emit(targets)

    def _echos_target_for(self, device_name: str) -> EchosPollTarget | None:
        """Build a one-shot poll target for an Echos device, or None.

        None when the device has no ``echos:`` section (a generic SeedLink
        server with no REST API).
        """
        for d in self._store.root.devices:
            if d.name == device_name and d.echos is not None:
                return EchosPollTarget(
                    name=d.name,
                    host=d.host,
                    http_port=d.echos.http_port,
                    poll_interval_s=d.echos.poll_interval_s,
                    poll_interval_streaming_s=d.echos.poll_interval_streaming_s,
                )
        return None

    def _on_device_state_streaming(self, device_name: str, conn_int: int) -> None:
        """Track SeedLink CONNECTED devices for the poll backoff (M6.6-C).

        Keyed off ``ConnState.CONNECTED``: while the socket is up the data
        stream proves the device is alive, so the status poller backs off to
        the slow heartbeat cadence. Any other state (drop/reconnect) removes
        the device, resuming full-cadence polling — exactly when REST is
        useful. Pushes to the worker only on an actual set change.
        """
        streaming = conn_int == int(ConnState.CONNECTED)
        was_streaming = device_name in self._streaming_devices
        if streaming == was_streaming:
            return
        if streaming:
            self._streaming_devices.add(device_name)
        else:
            self._streaming_devices.discard(device_name)
        self._streamingDevicesChanged.emit(frozenset(self._streaming_devices))

    def _on_acquisition_stationxml(self, device_name: str, state_int: int) -> None:
        """Fetch + persist the device StationXML across acquisition changes.

        Idle→(Monitoring|Recording): request one off-thread fetch (de-duped
        per acquisition). On entering Recording, persist any already-fetched
        blob immediately (covers Monitoring→Recording, where no new fetch is
        issued). Returning to Idle clears the de-dup flag so the next
        acquisition re-fetches fresh metadata (rule 13).
        """
        state = AcquisitionState(state_int)
        if state is AcquisitionState.IDLE:
            self._stationxml_requested.discard(device_name)
            return
        if device_name not in self._stationxml_requested:
            target = self._echos_target_for(device_name)
            if target is not None:
                self._stationxml_requested.add(device_name)
                self._stationXmlFetchRequested.emit((target,))
        if state is AcquisitionState.RECORDING:
            blob = self._stationxml_blobs.get(device_name)
            if blob is not None:
                self._engine.persist_session_stationxml(device_name, blob)

    def _on_stationxml_ready(self, device_name: str, xml: object) -> None:
        """Receive a fetched StationXML blob (worker thread → GUI, queued).

        Payload guarded per rule 4. ``None`` means the fetch failed — the
        analysis path degrades to counts (the helper already logged one
        warning). On success, register the blob for live deconvolution and
        persist it if the device is currently recording.
        """
        if not isinstance(xml, str) or not xml:
            return
        self._stationxml_blobs[device_name] = xml
        self._response_provider.set_stationxml_blob(device_name, xml)
        if self._engine.acquisition_state(device_name) is AcquisitionState.RECORDING:
            self._engine.persist_session_stationxml(device_name, xml)

    def _on_new_stream(self, device_name: str, nslc: str) -> None:
        assert self._live_tabs is not None
        # streamMeta will resize the buffer as soon as the first packet's
        # rate arrives. LiveTabs fans the stream into both the "All" tab
        # and the device tab (creating the device tab on first sight).
        self._live_tabs.add_stream(device_name, nslc, _PROVISIONAL_FS_HZ)
        # Mirror the new stream into the Spectrogram dock so the user
        # has a full-size view available immediately. The dock's
        # add_stream is idempotent — a duplicate signal during chain
        # hot-reload is a no-op.
        self._spectrogram_widget.add_stream(device_name, nslc, _PROVISIONAL_FS_HZ)

    def _on_trace_ready(self, device_name: str, nslc: str, samples: object) -> None:
        assert self._live_tabs is not None
        # samples: np.ndarray[float32]; type-erased through Signal(object)
        self._live_tabs.push_raw(device_name, nslc, samples)

    def _on_processed_trace_ready(self, device_name: str, nslc: str, samples: object) -> None:
        assert self._live_tabs is not None
        self._live_tabs.push_processed(device_name, nslc, samples)

    def _on_processed_stream_meta(self, device_name: str, nslc: str, fs_out: float) -> None:
        assert self._live_tabs is not None
        # Resets the inline / per-tab spectrograms to the new fs_out — a
        # chain hot-reload may decimate, so the old fs's frequency axis is
        # no longer correct.
        self._live_tabs.update_processed_meta(device_name, nslc, fs_out)
        self._spectrogram_widget.update_meta(device_name, nslc, fs_out)

    def _on_stream_meta(self, device_name: str, nslc: str, fs: float, starttime_iso: str) -> None:
        assert self._live_tabs is not None
        # First-packet fs may differ from the provisional fs we used on
        # newStreamSeen — surface the correct value on the trace + inline
        # spectrogram widgets so titles match the underlying signal.
        self._live_tabs.update_meta(device_name, nslc, fs, starttime_iso)
        self._spectrogram_widget.update_meta(device_name, nslc, fs)

    def _on_chain_dropped(self, device_name: str, nslc: str, count: int) -> None:
        assert self._live_tabs is not None
        self._live_tabs.set_drop_count(device_name, nslc, count)

    def _on_config_devices_changed(self) -> None:
        """Prune Live device tabs for devices removed from config."""
        assert self._live_tabs is not None
        device_names = {d.name for d in self._store.root.devices}
        self._live_tabs.prune_to(device_names)

    def _on_trigger_fired(self, trigger: object, device_name: str, nslc: str) -> None:
        # The detection table is fed by ``detectionRecorded`` (after the DB
        # commit), not from here; this log line is kept for trace-level
        # debugging of the raw trigger stream.
        _log.debug("dsp_trigger", device=device_name, nslc=nslc, trigger=str(trigger))

    def _on_detection_selected(self, detection: object) -> None:
        """Show the selected detection in the master-detail detail pane, or
        the empty hint when nothing is selected.

        In-buffer detections render from the live ring buffer (fast path —
        a live-memory read, never waveforms from disk; rule 8). Out-of-buffer
        detections are loaded from the SDS archive (Z/N/E components) **off
        the GUI thread** via :class:`ArchiveDetailLoader`; the read + array
        prep must never run on the GUI thread or it would starve the SeedLink
        worker (docs/POSTMORTEMS.md — the reverted Archive Replay). The pane
        shows a brief loading state until the components arrive.
        """
        from echosmonitor.core.models import Detection

        # Every new selection supersedes any in-flight deconvolution and
        # archive load (latest-wins): bump the decon token, drop the archive
        # component routing, and cancel the loader so a stale result cannot
        # land in the pane for a different detection.
        self._decon_token += 1
        self._decon_components = {}

        if not isinstance(detection, Detection):
            self._archive_loader.cancel()
            self._archive_load_detection = None
            self._archive_traces = {}
            self._detail_ctx = {}
            self._detail_pane.clear()
            return

        from obspy import UTCDateTime

        # Request a window from a little before the onset up to now so the
        # ring-buffer tail (capped at ~2x default_window_seconds) includes
        # the detection when it is recent enough to still be in memory.
        from_onset = float(UTCDateTime() - detection.t_on)
        seconds = max(float(self._config.ui.default_window_seconds), from_onset + 15.0)
        samples, fs, latest_t = self._engine.read_recent(detection.device, detection.nslc, seconds)

        assert self._central_tabs is not None
        self._central_tabs.setCurrentWidget(self._detections_splitter)

        if not self._is_scrolled_out(detection, samples, fs, latest_t):
            # IN-BUFFER fast path — live ring-buffer render (unchanged).
            self._archive_loader.cancel()
            self._archive_load_detection = None
            self._archive_traces = {}
            self._detail_pane.show_detection(detection, samples, fs, latest_t)
            self._apply_response_availability(detection)
            return

        # OUT-OF-BUFFER — dispatch a 3C archive load off the GUI thread. The
        # pane shows a loading state; the result arrives via QueuedConnection.
        self._detail_ctx = {}
        self._archive_traces = {}
        self._detail_pane.set_loading(detection)
        components = self._derive_components(detection)
        t_on = detection.t_on
        t_ref = detection.t_off if detection.t_off is not None else t_on
        # The on-screen (inspect) window. The recomputed STA/LTA ratio's peak
        # must line up with the trigger window inside THIS span.
        view_start = float(t_on - _ARCHIVE_INSPECT_PRE_S)
        view_end = float(t_ref + _ARCHIVE_INSPECT_POST_S)
        # Read extra pre-roll ahead of the inspect window so a recomputed
        # recursive STA/LTA has a converged LTA by the onset (H3 — see
        # _archive_ratio_warmup_s). For other kinds this is 0, so the read
        # and view windows coincide (display unchanged).
        warmup_s = self._archive_ratio_warmup_s(detection)
        t_start = float(t_on - _ARCHIVE_INSPECT_PRE_S - warmup_s)
        t_end = view_end
        self._archive_load_detection = detection
        self._archive_view_window = (view_start, view_end)
        db_path = self._engine.archive_db_path()
        self._archive_load_token = self._archive_loader.request(
            detection.device,
            detection.nslc,
            components,
            t_start,
            t_end,
            str(self._engine.archive_root(detection.device)),
            db_path=str(db_path) if db_path is not None else None,
        )

    def _apply_response_availability(self, detection: Detection) -> None:
        """Rebuild ``_detail_ctx`` from the rendered window + set unit avail.

        Works for both the live single-trace render and an archive-loaded 3C
        window — ``rendered_counts_context`` returns the trigger component's
        ``(fs, start_epoch)`` and ``counts_samples`` its counts. Call after
        the pane shows a real window; clears the context if none is up.
        """
        from obspy import UTCDateTime

        ctx = self._detail_pane.rendered_counts_context()
        if ctx is None:
            self._detail_ctx = {}
            return
        ctx_fs, start_epoch = ctx
        self._detail_ctx = {
            "device": detection.device,
            "nslc": detection.nslc,
            "fs": float(ctx_fs),
            "start_epoch": float(start_epoch),
            "samples": self._detail_pane.counts_samples(),
        }
        available = self._response_provider.available_for(
            detection.device, detection.nslc, UTCDateTime(start_epoch)
        )
        tooltip = "" if available else NO_RESPONSE_TOOLTIP
        self._detail_pane.set_response_available(available, tooltip)

    @staticmethod
    def _is_scrolled_out(
        detection: Detection,
        samples: np.ndarray,
        fs: float,
        latest_t: UTCDateTime | None,
    ) -> bool:
        """Replicate the pane's scrolled-out test against a ring read."""
        n = int(samples.shape[0])
        if n == 0 or fs <= 0 or latest_t is None:
            return True
        window_start = float(latest_t) - (n - 1) / fs
        return float(detection.t_on) < window_start

    @staticmethod
    def _archive_ratio_warmup_s(detection: Detection) -> float:
        """Extra archive pre-roll (seconds) for the STA/LTA recompute.

        The detail pane recomputes the recursive STA/LTA ratio from the
        loaded waveform. A recursive LTA has a time constant of ``lta_s``;
        with only the inspect pre-roll (10 s) ahead of the onset the LTA is
        still in its warm-up transient at the event, so the ratio is flat
        through the onset and peaks spuriously near the right edge. Reading
        ``LTA_MULT * lta_s`` of extra pre-roll lets the LTA converge before
        the onset; the warm-up region is rendered off-screen.

        Returns ``0.0`` for non-STA/LTA kinds (nothing is recomputed for
        them, so they need no warm-up) and for STA/LTA detections that
        recorded no usable ``lta_s`` in meta.
        """
        if detection.kind != _STA_LTA_DETECTION_KIND:
            return 0.0
        meta = detection.meta if isinstance(detection.meta, dict) else {}
        lta_s = meta.get("lta_s")
        if isinstance(lta_s, (int, float)) and lta_s > 0:
            return _ARCHIVE_RATIO_WARMUP_LTA_MULT * float(lta_s)
        return 0.0

    @staticmethod
    def _derive_components(detection: Detection) -> dict[str, str]:
        """Map component letters to NSLCs for the detection's station.

        Swaps the trigger channel's orientation code (last char) to Z/N/E,
        keeping net.sta.loc and the band+instrument prefix. A trigger channel
        that does not end in Z/N/E degrades to a single-component view on the
        trigger NSLC itself (logged) rather than guessing. The loader skips
        components with no archived data, so requesting all three is safe.
        """
        from echosmonitor.core.models import StreamID

        sid = StreamID.from_trace_id(detection.nslc)
        cha = sid.channel
        if len(cha) == 3 and cha[-1].upper() in ("Z", "N", "E"):
            base = f"{sid.network}.{sid.station}.{sid.location}.{cha[:2]}"
            return {"Z": f"{base}Z", "N": f"{base}N", "E": f"{base}E"}
        _log.warning(
            "archive_detail_non_zne_channel",
            nslc=detection.nslc,
            message="trigger channel does not end in Z/N/E; loading single component",
        )
        comp = cha[-1].upper() if cha else "Z"
        if comp not in ("Z", "N", "E"):
            comp = "Z"
        return {comp: detection.nslc}

    def _on_archive_detail_loaded(self, payload: object) -> None:
        """A 3C archive load arrived on the GUI thread — render unless stale.

        The GUI thread only ``setData``s here; all the reading happened on
        the loader's worker thread.
        """
        if not isinstance(payload, ArchiveDetailResult):
            return
        if payload.token != self._archive_load_token:
            return  # superseded by a newer selection (latest-wins)
        detection = self._archive_load_detection
        if detection is None:
            return
        self._archive_traces = {t.comp: t for t in payload.traces}
        view = self._archive_view_window
        view_start, view_end = view if view is not None else (None, None)
        self._detail_pane.show_archive_3c(
            detection,
            payload.traces,
            payload.trigger_comp,
            view_start_epoch=view_start,
            view_end_epoch=view_end,
        )
        self._apply_response_availability(detection)

    def _on_archive_detail_empty(self, token: int) -> None:
        """The archive had no data for this window — honest message."""
        if token != self._archive_load_token:
            return
        self._archive_traces = {}
        self._detail_ctx = {}
        detection = self._archive_load_detection
        if detection is not None:
            self._detail_pane.show_no_archive_data(detection)

    def _on_archive_detail_failed(self, token: int, message: object) -> None:
        """The archive read failed — honest message + status-bar note."""
        if token != self._archive_load_token:
            return
        self._archive_traces = {}
        self._detail_ctx = {}
        detection = self._archive_load_detection
        if detection is not None:
            self._detail_pane.show_no_archive_data(detection)
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(f"Archive read failed: {message}", 6000)

    # ------------------------------------------------------------------
    # Archive tab (browse + static view + hand-off)
    # ------------------------------------------------------------------
    def _on_archive_window_load_requested(
        self, device: object, group: object, t_start_epoch: float, t_end_epoch: float
    ) -> None:
        """Dispatch an Archive-tab window load off the GUI thread.

        Latest-wins: the loader supersedes any in-flight load; only the latest
        token's result is rendered. The read + spectrogram build run on the
        loader's worker thread (rule 11). The archive root + index are
        resolved here on the GUI thread and snapshotted into the request
        (rule 8) — from the tab's SELECTED SESSION, so a closed session's
        project root is read even though the live engine only exposes the
        bare base root between sessions (rule 14 / the M2-B NOTE). The
        engine context is only a fallback for a load with no session
        selected (not reachable through the tab's UI; defensive).
        """
        if not isinstance(device, str) or not isinstance(group, dict):
            return
        archive_root, db_path = self._archive_session_context(device)
        # Supersede any prior unit batch + clear the prior window's components
        # so an in-flight decon result for the old window can't land on the new.
        self._archive_window_decon = {}
        self._archive_window_decon_outstanding = 0
        self._archive_window_traces = {}
        self._archive_window_token = self._archive_window_loader.request(
            device,
            {str(k): str(v) for k, v in group.items()},
            float(t_start_epoch),
            float(t_end_epoch),
            archive_root,
            db_path=db_path,
        )

    def _load_archive_stationxml(self, device: str, entry: SessionEntry) -> None:
        """Preload the selected archive session's StationXML into the provider.

        Makes archive HVSR/deconvolution resolve the real instrument
        response from the persisted blob (M6.6-B) with NO live device call.
        ``None`` (pre-v6 DB or no row) clears any stale blob so the archive
        analysis honestly falls back to counts. Read-only DB read, bounded
        and small (rule 6) — safe on the GUI thread.
        """
        if entry.record.id is None:
            return
        from echosmonitor.storage.archive_reader import read_session_stationxml

        blob = read_session_stationxml(Path(entry.db_path), entry.record.id, device)
        self._response_provider.set_stationxml_blob(device, blob)

    def _archive_session_context(self, device: str) -> tuple[str, str | None]:
        """``(archive_root, db_path)`` for an Archive-tab data request.

        The tab's SELECTED SESSION is the truth (rule 14 — only its root
        reaches a closed session's data); the live engine context is a
        defensive fallback for a request with no session selected (not
        reachable through the tab's UI).
        """
        entry = self._archive_tab.selected_session_entry()
        if entry is not None:
            self._load_archive_stationxml(device, entry)
            return entry.session_root, entry.db_path
        engine_db = self._engine.archive_db_path()
        return (
            str(self._engine.archive_root(device)),
            str(engine_db) if engine_db is not None else None,
        )

    # ------------------------------------------------------------------
    # Archive data exports (M3-C)
    # ------------------------------------------------------------------
    def _on_archive_export_requested(
        self,
        fmt: object,
        device: object,
        group: object,
        t_start_epoch: float,
        t_end_epoch: float,
        dest_path: object,
    ) -> None:
        """Queue a MiniSEED/CSV export of the selected interval.

        The session context is snapshotted HERE, at request time (rule 8)
        — the export then runs independently on the worker's serial
        queue; a later session selection cannot redirect it.
        """
        if not isinstance(fmt, str) or not isinstance(device, str) or not isinstance(group, dict):
            return
        if not isinstance(dest_path, str) or not dest_path:
            return
        archive_root, db_path = self._archive_session_context(device)
        self._archive_export_loader.request(
            fmt,
            device,
            {str(k): str(v) for k, v in group.items()},
            float(t_start_epoch),
            float(t_end_epoch),
            archive_root,
            db_path,
            dest_path,
        )

    def _on_archive_export_done(self, payload: object) -> None:
        if not isinstance(payload, ArchiveExportResult):
            return
        self._archive_tab.show_export_done(payload.fmt, payload.dest_path, payload.n_bytes)
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(f"Exported {payload.fmt.upper()} to {payload.dest_path}", 6000)

    def _on_archive_export_failed(self, _token: int, message: object) -> None:
        self._archive_tab.show_export_failed(str(message))
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(f"Archive export failed: {message}", 6000)

    def _on_archive_export_empty(self, _token: int) -> None:
        self._archive_tab.show_export_empty()

    def _on_archive_reindex_requested(self, session_root: object) -> None:
        """Guard + dispatch a re-index of a project directory (M3-D).

        The guard: a re-index is a WRITE path into ``archive.db``, and
        the engine holds the ACTIVE session's DB open and is writing it
        — re-indexing that one would race the live writer (rule 8).
        Refused honestly rather than queued. Cross-process safety is the
        app-lifetime ``QLockFile`` on the base root: another instance
        cannot be recording under this root at all.
        """
        import socket
        from pathlib import Path

        from echosmonitor import __version__

        if not isinstance(session_root, str) or not session_root:
            return
        target_db = Path(session_root) / "archive.db"
        active_db = self._engine.archive_db_path()
        if active_db is not None and target_db.resolve() == active_db.resolve():
            session = self._engine.active_session()
            project = session.project_name if session is not None else "(monitoring)"
            _log.warning(
                "archive_reindex_refused_active_session",
                root=session_root,
                project=project,
            )
            self._archive_tab.show_reindex_failed(
                f"'{project}' is the ACTIVE session — stop it first, then"
                " re-index."
            )
            return
        self._archive_tab.set_reindex_busy(True)
        self._archive_reindex_root = session_root
        self._archive_reindex_token = self._archive_reindex_loader.request(
            session_root,
            host=socket.gethostname(),
            version=__version__,
        )

    def _session_start_guard(self, project_name: str) -> str | None:
        """Toolbar veto (M3-D, the inverse of the active-session guard):
        starting a recording session whose root is being RE-INDEXED would
        put the engine's DAO and the re-index DAO on one archive.db
        concurrently (rule 8) — refuse until the re-index finishes."""
        from pathlib import Path

        from echosmonitor.core.session import session_archive_root

        root = self._archive_reindex_root
        if root is None:
            return None
        target = session_archive_root(self._base_archive_root, project_name)
        if target.resolve() == Path(root).resolve():
            return (
                f"The archive of '{project_name}' is being re-indexed — wait"
                " for it to finish, then start the session."
            )
        return None

    def _on_archive_reindex_progress(self, payload: object) -> None:
        if self._archive_bridge_severed:
            return  # closing — the tab/loaders are torn down (auditor F1 class)
        if not isinstance(payload, ArchiveReindexProgressEvent):
            return
        if payload.token != self._archive_reindex_token:
            return  # a stale queued request's beats — not the one the UI tracks
        self._archive_tab.show_reindex_progress(payload.files_done, payload.files_total)

    def _on_archive_reindex_done(self, payload: object) -> None:
        # A re-index finishing while closeEvent's bounded joins run posts
        # this queued — it dispatches AFTER closeEvent returns, and the
        # refresh below would lazily RESTART the just-joined browser
        # thread (nothing joins it again; Qt aborts at exit — the M3-A
        # F1 class, re-found by the qt-concurrency-auditor on this path).
        if self._archive_bridge_severed:
            return
        if not isinstance(payload, ArchiveReindexResult):
            return
        if payload.token == self._archive_reindex_token:
            self._archive_reindex_root = None
            self._archive_tab.set_reindex_busy(False)
            report = payload.report
            self._archive_tab.show_reindex_done(
                report.files_indexed, report.files_skipped, report.files_pruned
            )
        # The index changed on disk either way — re-discover sessions.
        self._archive_tab.refresh_sessions()

    def _on_archive_reindex_failed(self, token: int, message: object) -> None:
        if self._archive_bridge_severed:
            return  # closing — see _on_archive_reindex_done
        if token != self._archive_reindex_token:
            return
        self._archive_reindex_root = None
        self._archive_tab.set_reindex_busy(False)
        self._archive_tab.show_reindex_failed(str(message))

    def _on_archive_window_loaded(self, payload: object) -> None:
        from obspy import UTCDateTime

        if not isinstance(payload, ArchiveWindowResult):
            return
        if payload.token != self._archive_window_token:
            return  # stale (latest-wins) — drop
        self._archive_window_traces = {t.comp: t for t in payload.traces}
        self._archive_tab.show_result(payload)
        # Resolve physical-unit availability for the primary (Z) component.
        device, group, t_start, _t_end = self._archive_tab.current_window()
        z_nslc = group.get("Z")
        if z_nslc:
            available = self._response_provider.available_for(device, z_nslc, UTCDateTime(t_start))
            tooltip = "" if available else NO_RESPONSE_TOOLTIP
            self._archive_tab.set_response_available(available, tooltip)

    def _on_archive_window_empty(self, token: int) -> None:
        if token != self._archive_window_token:
            return
        self._archive_tab.show_empty()

    def _on_archive_window_failed(self, token: int, message: object) -> None:
        if token != self._archive_window_token:
            return
        self._archive_tab.show_failed(str(message))

    def _on_archive_window_unit_change(self, unit_code: object) -> None:
        """Deconvolve each gap-free archive-window component to ``unit_code``.

        Reuses the dedicated decon worker (rule 11) with a SEPARATE token map
        (``_archive_window_decon``) so it contends neither the live nor the
        detail-pane decon. Components carrying NaN gaps are skipped (left in
        counts) — an FFT response removal would smear NaN across the window.
        """
        import numpy as np

        code = str(unit_code)
        self._archive_window_decon = {}  # supersede any prior batch
        if code == "COUNTS":
            self._archive_tab.revert_to_counts()
            return
        device, group, _t_start, _t_end = self._archive_tab.current_window()
        dispatched = 0
        skipped_gappy: list[str] = []
        for comp, nslc in group.items():
            # Always deconvolve from the COUNTS samples the loader produced
            # (``_archive_window_traces``), never the currently-displayed curve
            # — which may already hold a previous unit's physical values.
            tr = self._archive_window_traces.get(comp)
            if tr is None:
                continue
            arr = np.asarray(tr.y, dtype=np.float64)
            if not np.all(np.isfinite(arr)):
                skipped_gappy.append(comp)
                continue  # gappy component — cannot deconvolve cleanly
            self._decon_token += 1
            self._archive_window_decon[self._decon_token] = comp
            self._deconRequested.emit(
                self._decon_token,
                device,
                str(nslc),
                code,
                arr,
                float(tr.fs),
                float(tr.start_epoch),
            )
            dispatched += 1
        self._archive_window_decon_outstanding = dispatched
        if not dispatched:
            self._archive_tab.revert_to_counts()
            bar = self.statusBar()
            if bar is not None:
                bar.showMessage("Cannot deconvolve: all window components have gaps", 6000)
        elif skipped_gappy:
            # Partial dispatch: the siblings switch units while these stay
            # in counts — say so on the plot labels + status (M3-B; a
            # silent mixed-unit display lies about whichever side it
            # doesn't match).
            self._archive_tab.mark_components_left_in_counts(skipped_gappy)

    def _handoff_archive_to_hvsr(
        self, device: object, group: object, t_start_epoch: float, t_end_epoch: float
    ) -> None:
        """Switch to the HVSR tab, prefilled with the Archive selection.

        Prefill only — the user reviews the HVSR settings and clicks "Run on
        archive". The interval round-trips exactly. The browsed session's
        root is remembered (keyed to the handed-off device) so the run
        reads the SAME session-rooted archive — including closed sessions
        the live engine roots cannot reach (M3-E seam, rule 14).
        """
        if not isinstance(device, str) or not isinstance(group, dict):
            return
        grp = {str(k): str(v) for k, v in group.items()}
        entry = self._archive_tab.selected_session_entry()
        if entry is not None:
            self._hvsr_archive_ctx = (
                device,
                entry.session_root,
                float(t_start_epoch),
                float(t_end_epoch),
            )
        else:
            self._hvsr_archive_ctx = None
        if self._central_tabs is not None:
            self._central_tabs.setCurrentWidget(self._hvsr_widget)
        self._hvsr_widget.prefill_archive(device, grp, float(t_start_epoch), float(t_end_epoch))

    def _on_component_layout_changed(self, layout: object) -> None:
        """User toggled Stacked/Overlaid for the 3C archive view."""
        self._detail_pane.set_component_layout(str(layout))

    def _on_unit_change_requested(self, unit_code: object) -> None:
        """Handle a unit selection from the detail pane.

        ``COUNTS`` reverts to counts. A physical code dispatches deconvolution
        to the dedicated worker thread (off the science thread, rule 11;
        latest-wins). In the 3C archive view one request is issued per
        present, gap-free component, routed back by token; the live
        single-trace path is unchanged.
        """
        code = str(unit_code)
        if self._detail_pane.is_showing_archive():
            if code == "COUNTS":
                self._detail_pane.revert_archive_to_counts()
                return
            self._dispatch_archive_decon(code)
            return
        if code == "COUNTS":
            self._detail_pane.revert_to_counts()
            return
        if not self._detail_ctx:
            self._detail_pane.revert_to_counts()
            return
        self._decon_token += 1
        self._detail_pane.set_computing(True)
        self._deconRequested.emit(
            self._decon_token,
            str(self._detail_ctx["device"]),
            str(self._detail_ctx["nslc"]),
            code,
            self._detail_ctx["samples"],
            float(self._detail_ctx["fs"]),  # type: ignore[arg-type]
            float(self._detail_ctx["start_epoch"]),  # type: ignore[arg-type]
        )

    def _dispatch_archive_decon(self, code: str) -> None:
        """Deconvolve each present, gap-free archive component to ``code``.

        Reuses the dedicated deconvolution worker (off the science thread,
        rule 11); the (≤3) one-shot jobs run serially. Components carrying
        NaN gaps are skipped (left in counts): an FFT-based response removal
        would smear NaN across the whole window. Tokens are unique +
        monotonic; ``_decon_components`` maps each to its component so the
        result routes to the right curve, and is cleared first so a
        superseded batch's results fall through and are dropped.
        """
        import numpy as np

        device = self._archive_load_detection.device if self._archive_load_detection else ""
        self._decon_components = {}  # supersede any prior archive batch
        dispatched = 0
        for comp, trace in self._archive_traces.items():
            y = np.asarray(trace.y, dtype=np.float64)
            if not np.all(np.isfinite(y)):
                continue  # gappy component — cannot deconvolve cleanly
            self._decon_token += 1
            self._decon_components[self._decon_token] = comp
            self._deconRequested.emit(
                self._decon_token,
                device,
                str(trace.nslc),
                code,
                y,
                float(trace.fs),
                float(trace.start_epoch),
            )
            dispatched += 1
        self._archive_decon_outstanding = dispatched
        if dispatched:
            self._detail_pane.set_computing(True)
        else:
            # Every present component has gaps — nothing to deconvolve. Revert
            # the selector to counts so the visible unit matches reality.
            self._detail_pane.revert_archive_to_counts()
            bar = self.statusBar()
            if bar is not None:
                bar.showMessage("Cannot deconvolve: all archived components have gaps", 6000)

    def _on_inspect_in_unit(self, detection: object, unit_code: object) -> None:
        """Context-menu "Inspect in <unit>": select then drive the unit path.

        Selecting the detection renders counts + resolves availability; we
        then emit the unit-change flow directly (avoids racing on
        programmatically pre-selecting the combo item)."""
        self._on_detection_selected(detection)
        if self._detail_ctx:
            self._on_unit_change_requested(unit_code)

    def _on_deconvolved(self, token: int, label: object, samples: object) -> None:
        """Worker result on the GUI thread: render unless superseded.

        An archive per-component result (token in ``_decon_components``)
        routes to that component's curve; otherwise it is the live
        single-trace path (latest-wins on ``_decon_token``).
        """
        import numpy as np

        arr = samples if isinstance(samples, np.ndarray) else np.asarray(samples)
        win_comp = self._archive_window_decon.pop(token, None)
        if win_comp is not None:
            self._archive_tab.show_physical_component(win_comp, str(label), arr)
            self._archive_window_decon_outstanding = max(
                0, self._archive_window_decon_outstanding - 1
            )
            return
        comp = self._decon_components.get(token)
        if comp is not None:
            self._detail_pane.show_physical_component(comp, str(label), arr)
            self._archive_decon_outstanding = max(0, self._archive_decon_outstanding - 1)
            if self._archive_decon_outstanding == 0:
                self._detail_pane.set_computing(False)  # whole batch landed
            return
        if token != self._decon_token:
            return  # stale (latest-wins) — drop
        self._detail_pane.show_physical_trace(str(label), arr)

    def _on_deconvolution_failed(self, token: int, message: object) -> None:
        """Worker failure on the GUI thread: revert + status-bar message.

        An archive component failure leaves that component in counts (the
        others may still convert); the live path reverts the top trace.
        """
        win_comp = self._archive_window_decon.pop(token, None)
        if win_comp is not None:
            self._archive_window_decon_outstanding = max(
                0, self._archive_window_decon_outstanding - 1
            )
            bar = self.statusBar()
            if bar is not None:
                bar.showMessage(f"Physical units unavailable for {win_comp}: {message}", 6000)
            return
        comp = self._decon_components.get(token)
        if comp is not None:
            self._archive_decon_outstanding = max(0, self._archive_decon_outstanding - 1)
            if self._archive_decon_outstanding == 0:
                self._detail_pane.set_computing(False)
            bar = self.statusBar()
            if bar is not None:
                bar.showMessage(f"Physical units unavailable for {comp}: {message}", 6000)
            return
        if token != self._decon_token:
            return
        self._detail_pane.revert_to_counts()
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(f"Physical units unavailable: {message}", 6000)

    def _on_focus_detection_requested(self, detection: object) -> None:
        """Row double-clicked: switch the Live view to the detection's
        device tab. (Selection — which a double-click also triggers —
        drives the central detail pane separately.)"""
        from echosmonitor.core.models import Detection

        if not isinstance(detection, Detection) or self._live_tabs is None:
            return
        self._live_tabs.focus_device(detection.device)
        # Bring the Live tab forward so "double-click row → see it live"
        # lands on the now-focused device tab.
        assert self._central_tabs is not None
        self._central_tabs.setCurrentWidget(self._live_tabs)

    def _run_hvsr_archive(
        self,
        device: str,
        group: dict[str, str],
        t_start: object,
        t_end: object,
        settings: object,
    ) -> str:
        """Run HVSR over an archived range (the HVSR widget's archive handler).

        A deliberate one-shot inline read bounded by the reader's day-scan cap
        (not the live data path). Builds the ``ArchiveReader`` for the device
        and hands it to the HVSR engine, which slices the windows and runs one
        off-thread compute. Returns the measurement id, or ``""`` when the
        range holds no gap-free 3-component window.

        Root resolution (M3-E seam, rule 14): a run that follows an
        Archive-tab hand-off reads the handed-off SESSION's root — the
        only way to reach a closed session's data. The context is keyed
        to the handed-off device AND interval (±1 s — the prefill
        round-trips at second resolution), so any manual re-target of
        the widget falls back to the live engine roots instead of
        silently reading a stale session.
        """
        from pathlib import Path

        from obspy import UTCDateTime

        from echosmonitor.core.hvsr import HvsrSettings
        from echosmonitor.storage.archive_reader import ArchiveReader

        assert isinstance(settings, HvsrSettings)
        t0 = UTCDateTime(str(t_start))
        t1 = UTCDateTime(str(t_end))
        ctx = self._hvsr_archive_ctx
        if (
            ctx is not None
            and ctx[0] == device
            and abs(float(t0.timestamp) - ctx[2]) < 1.0
            and abs(float(t1.timestamp) - ctx[3]) < 1.0
        ):
            root = Path(ctx[1])
        else:
            root = self._engine.archive_root(device)
        # No DAO: the reader's canonical SDS day-scan finds every file the
        # writer lays down, and a DAO would open a connection the engine's
        # per-context close() never reaches (the M2-B leak note — same
        # class the archive loaders fixed). NOTE the slice read itself is
        # the documented one-shot INLINE read on the calling thread
        # (hvsr_engine.start_archive_measurement) — only the HVSR compute
        # runs off-thread; this call does not change that.
        reader = ArchiveReader(root)
        return self._hvsr_engine.start_archive_measurement(
            device,
            group,
            t0,
            t1,
            settings,
            reader,
        )

    def _run_hvsr_array_archive(
        self,
        groups: dict[str, dict[str, str]],
        t_start: object,
        t_end: object,
        settings: object,
        geometry: object,
    ) -> str:
        """Run the ARRAY analysis over an archived range (M5-D).

        Root resolution (rule 14): when a session is selected in the
        Archive tab, every checked device reads from that SESSION's root
        (one shared reader — the per-device SDS trees live below it);
        otherwise each device falls back to its live engine root. The
        Archive-tab selection is the pull-based counterpart of the
        single-station hand-off context — the user picks the session
        first, then runs the array over it.

        The slicing runs on the array worker (M6): this returns the id
        immediately, and a range with no data is announced asynchronously
        via ``arrayArchiveNoData`` together with the searched roots (the
        readers' ``root`` is where the message text comes from).
        """
        from pathlib import Path

        from obspy import UTCDateTime

        from echosmonitor.core.hvsr import HvsrSettings
        from echosmonitor.core.positions import StationGeometry
        from echosmonitor.storage.archive_reader import ArchiveReader

        assert isinstance(settings, HvsrSettings)
        assert isinstance(geometry, StationGeometry)
        t0 = UTCDateTime(str(t_start))
        t1 = UTCDateTime(str(t_end))
        entry = self._archive_tab.selected_session_entry() if self._archive_tab else None
        readers: dict[str, ArchiveReader] = {}
        if entry is not None:
            shared = ArchiveReader(Path(entry.session_root))
            readers = dict.fromkeys(groups, shared)
            _log.info(
                "hvsr_array_archive_session_root",
                session_root=str(entry.session_root),
                devices=sorted(groups),
            )
        else:
            for device in groups:
                readers[device] = ArchiveReader(self._engine.archive_root(device))
        return self._hvsr_array_engine.start_archive_measurement(
            groups, t0, t1, settings, geometry, readers
        )

    def _on_detection_markers(self, detection: object) -> None:
        """Draw markers for a newly-recorded detection on its trace + the
        wall-clock spectrogram (C1/C2)."""
        from echosmonitor.core.models import Detection

        if not isinstance(detection, Detection) or detection.id is None:
            return
        if self._live_tabs is None:
            return
        t_on = float(detection.t_on)
        t_off = float(detection.t_off) if detection.t_off is not None else None
        self._live_tabs.add_detection_marker(
            detection.device, detection.nslc, detection.id, t_on, t_off, detection.score
        )
        self._spectrogram_widget.add_detection_marker(
            detection.device, detection.nslc, detection.id, t_on
        )

    def _on_detection_update_markers(self, detection: object) -> None:
        """Close an open trace marker into a shaded region (C1)."""
        from echosmonitor.core.models import Detection

        if not isinstance(detection, Detection) or detection.id is None:
            return
        if self._live_tabs is None or detection.t_off is None:
            return
        self._live_tabs.update_detection_marker(
            detection.device, detection.nslc, detection.id, float(detection.t_off)
        )

    def _on_toggle_markers(self, checked: bool) -> None:
        """View ▸ Show detection markers (global toggle for all plots)."""
        if self._live_tabs is not None:
            self._live_tabs.set_markers_visible(checked)
        self._spectrogram_widget.set_markers_visible(checked)

    def _on_engine_error(self, device_name: str, message: str) -> None:
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(f"{device_name}: {message}", 5000)
        _log.warning("streaming_engine_error", device=device_name, message=message)

    # ------------------------------------------------------------------
    # Persistence / lifecycle
    # ------------------------------------------------------------------
    def _settings(self) -> QSettings:
        return QSettings(_ORG_NAME, _APP_NAME)

    def _restore_state(self) -> None:
        settings = self._settings()
        if not settings.allKeys() and QSettings(_LEGACY_ORG_NAME, _LEGACY_APP_NAME).allKeys():
            _log.info(
                "qsettings_reset_after_rename",
                legacy_org=_LEGACY_ORG_NAME,
                org=_ORG_NAME,
            )
        binary = (bytes, bytearray, QByteArray)
        geometry = settings.value("geometry")
        if isinstance(geometry, binary):
            self.restoreGeometry(geometry)
        window_state = settings.value("windowState")
        if isinstance(window_state, binary):
            # ``restoreState`` returns False on a malformed / version-
            # mismatched buffer. We intentionally ignore the return:
            # ``_apply_default_dock_layout`` (called inside
            # ``_build_docks`` before this method runs) has already
            # placed every dock in the recoverable default layout, so
            # a no-op restore leaves the user in a usable state. Any
            # successful-but-degenerate restore where every dock ends
            # up hidden is caught by ``_apply_empty_layout_fallback``
            # below.
            self.restoreState(window_state)
        self._apply_empty_layout_fallback()

    def _apply_empty_layout_fallback(self) -> None:
        """Re-show Devices if every dock is hidden after restoring state.

        QSettings can faithfully restore a saved state where the user
        closed every dock and quit, leaving the next launch with a
        blank MainWindow whose only recovery affordance would be
        editing the QSettings file by hand. With this fallback the
        title-bar context menu and the View menu always have a visible
        dock to attach to.
        """
        assert self._device_panel is not None
        if all(dock.isHidden() for dock in self._docks_in_order()):
            self._device_panel.show()
            _log.info(
                "all_docks_hidden_fallback",
                message=(
                    "all docks were hidden in saved state; "
                    "restored Devices dock as a safety fallback"
                ),
            )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        # Persist child-widget state BEFORE we tear anything down. Qt
        # does not propagate ``closeEvent`` from a closing QMainWindow
        # to its docked children, so the StationBrowser's own
        # closeEvent does not fire on a normal app-quit.
        if self._station_browser is not None:
            self._station_browser.persist_state()
        # Stop the InfoWorker before the engine: it's a leaf consumer
        # so order doesn't strictly matter, but tearing it down first
        # means a late ``stationsReceived`` queued onto a dying browser
        # cannot fire after the dock is gone.
        self._info_worker.stop()
        self._info_thread.quit()
        if not self._info_thread.wait(_INFO_THREAD_JOIN_MS):
            _log.warning("info_thread_join_timeout")
        # M1-C: stop the Echos status poller. stop() is a plain method
        # (not a queued slot) so it interrupts an in-flight poll via the
        # asyncio task-cancel path; the blocking-queued release() is the
        # skill §3 barrier that stops the worker-thread QTimer on its own
        # thread before quit (safe: emitter thread ≠ receiver thread).
        # The isRunning() guard matters: closeEvent can run TWICE (an
        # explicit close + the test-harness teardown close), and a
        # BlockingQueuedConnection into an already-finished thread never
        # dispatches — it would hang the GUI forever. Nothing else quits
        # this thread, so "running here" ⇒ the barrier will dispatch.
        self._echos_worker.stop()
        if self._echos_thread.isRunning():
            QMetaObject.invokeMethod(
                self._echos_worker, "release", Qt.ConnectionType.BlockingQueuedConnection
            )
        self._echos_thread.quit()
        if not self._echos_thread.wait(_ECHOS_THREAD_JOIN_MS):
            _log.warning("echos_thread_join_timeout")
        # The bounded join above is what makes leaving snapshotReady /
        # stationXmlReady connected safe (skill §3): the emitter thread is
        # fully stopped before MainWindow finishes tearing down, so no
        # queued worker→GUI signal can fire post-teardown.
        # M4: the position resolver owns its thread + bounded join; its
        # stop() cancels an in-flight fetch via the asyncio task nudge.
        # Terminal — a later configChanged push is refused, not revived.
        self._position_resolver.shutdown()
        # M6.5-D: the Map tab's tile worker (bounded join; no-op when
        # the Satellite layer was never toggled).
        self._map_widget.shutdown_basemap()
        # M11 B: stop the dedicated deconvolution thread. It runs a plain
        # Qt event loop dispatching one-shot ``compute`` slots (no parked
        # blocking loop), so ``quit()`` returns it promptly.
        self._decon_thread.quit()
        if not self._decon_thread.wait(_DECON_THREAD_JOIN_MS):
            _log.warning("decon_thread_join_timeout")
        # Shut the HVSR engines down BEFORE the streaming engine: their
        # worker threads read the engine's ring buffers, so they must be
        # joined before the engine they read from goes away.
        self._hvsr_engine.shutdown()
        self._hvsr_array_engine.shutdown()
        # Archive loaders: their workers hold per-request read-only DB
        # connections and read SDS files; join them before the engine
        # (and its writers) tear down. Sever the sessionChanged →
        # refresh_sessions bridge FIRST (skill §3: disconnect at the
        # join): engine.stop() below emits sessionChanged(None) queued,
        # and a refresh dispatched after the join would lazily RESTART
        # the just-joined browser thread mid-teardown — nothing joins it
        # again and Qt aborts at exit (qt-concurrency-auditor F1).
        if not self._archive_bridge_severed:
            self._archive_bridge_severed = True
            with contextlib.suppress(RuntimeError, TypeError):
                self._engine.sessionChanged.disconnect(self._archive_tab.refresh_sessions)
        self._archive_loader.shutdown()
        self._archive_window_loader.shutdown()
        self._archive_browser.shutdown()
        # The export worker aborts cooperatively: an in-flight export
        # removes its temp file (never a half-written destination) and
        # queued exports are dropped with a log line.
        self._archive_export_loader.shutdown()
        # An in-flight re-index aborts at its next per-file poll; the
        # partial index is safe (files win over it — rule 8) and a
        # re-run converges.
        self._archive_reindex_loader.shutdown()
        self._engine.stop()
        if self._live_tabs is not None:
            self._live_tabs.save_active_tab()
        settings = self._settings()
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("windowState", self.saveState())
        _log.info("main_window_closing")
        super().closeEvent(event)
