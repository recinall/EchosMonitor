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

from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import QByteArray, QRect, QSettings, Qt, QThread, QUrl, Signal
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
from echosmonitor.core.archive_detail_loader import (
    ArchiveDetailLoader,
    ArchiveDetailResult,
    ComponentTrace,
)
from echosmonitor.core.archive_window_loader import (
    ArchiveWindowLoader,
    ArchiveWindowResult,
)
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.core.deconvolution_worker import DeconvolutionWorker
from echosmonitor.core.hvsr_engine import HvsrEngine
from echosmonitor.core.info_worker import InfoWorker
from echosmonitor.core.response import ResponseProvider
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
from echosmonitor.gui.widgets.hvsr_widget import HvsrWidget
from echosmonitor.gui.widgets.live_stack import LiveStack
from echosmonitor.gui.widgets.live_tabs import LiveTabs
from echosmonitor.gui.widgets.psd_widget import PsdWidget
from echosmonitor.gui.widgets.spectrogram_dock import SpectrogramDock
from echosmonitor.gui.widgets.station_browser import StationBrowser
from echosmonitor.utils.docs import find_manual_tests

if TYPE_CHECKING:
    import numpy as np
    from obspy import UTCDateTime

    from echosmonitor.core.models import Detection

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

    def __init__(
        self,
        config: RootConfig,
        config_path: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._config_path = config_path

        self.setWindowTitle("EchosMonitor")
        self.resize(_DEFAULT_WIDTH, _DEFAULT_HEIGHT)
        self.setObjectName("MainWindow")

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
        # nothing with the live data path (rule 11). ``archive_dao()`` is read
        # once here on the GUI thread (the DAO is thread-safe).
        self._archive_loader = ArchiveDetailLoader(self._engine.archive_dao(), parent=self)
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
        # the live nor the detail-pane decon.
        self._archive_window_loader = ArchiveWindowLoader(self._engine.archive_dao(), parent=self)
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

        self._build_central()
        self._build_docks()
        self._build_focus_machinery()
        self._build_menubar()
        self._build_status_bar()
        self._wire_engine()
        self._restore_state()
        # Reopen the last-used Live tab (by device name). The target tab
        # may not exist yet — LiveTabs remembers it and switches when the
        # device's tab is created on first stream.
        assert self._live_tabs is not None
        self._live_tabs.restore_active_tab()

        if self._config.devices:
            self._engine.start()
            _log.info("streaming_engine_autostart", device_count=len(self._config.devices))
            # Recent-detections historical taste (C3): pre-fill the table
            # from the DB index now that the engine (and its DAO) is up.
            self._load_recent_detections()
        else:
            _log.info("streaming_engine_idle", reason="no devices in config")

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
        # The Archive tab: browse the SDS archive, view a window statically,
        # measure on it, and hand the interval to HVSR. The DAO is read
        # once here on the GUI thread (thread-safe, read-only — rule 8).
        self._archive_tab = ArchiveTab(self._engine, self._engine.archive_dao(), parent=self)
        self._archive_tab.loadRequested.connect(self._on_archive_window_load_requested)
        self._archive_tab.unitChangeRequested.connect(self._on_archive_window_unit_change)
        self._archive_tab.hvsrRequested.connect(self._handoff_archive_to_hvsr)

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

        self._central_tabs = QTabWidget(self)
        self._central_tabs.setObjectName("CentralTabs")
        self._central_tabs.addTab(self._detections_splitter, "Detections")
        self._central_tabs.addTab(self._live_tabs, "Live")
        self._central_tabs.addTab(self._psd_widget, "PSD")
        self._central_tabs.addTab(self._hvsr_widget, "HVSR")
        self._central_tabs.addTab(self._archive_tab, "Archive")
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
        self._log_dock = self._make_placeholder_dock(_DOCK_LOG)

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

    def _on_show_first_run_wizard(self) -> None:
        """Help → First-run wizard. Safe to call against a populated config."""
        wizard = FirstRunWizard(
            store=self._store,
            info_worker=self._info_worker,
            parent=self,
        )
        wizard.exec()

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
        self._archive_load_token = self._archive_loader.request(
            detection.device,
            detection.nslc,
            components,
            t_start,
            t_end,
            str(self._engine.archive_root(detection.device)),
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
        loader's worker thread (rule 11). ``archive_root`` is resolved here on
        the GUI thread and snapshotted into the request (rule 8).
        """
        if not isinstance(device, str) or not isinstance(group, dict):
            return
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
            str(self._engine.archive_root(device)),
        )

    def _on_archive_window_loaded(self, payload: object) -> None:
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
        for comp, nslc in group.items():
            # Always deconvolve from the COUNTS samples the loader produced
            # (``_archive_window_traces``), never the currently-displayed curve
            # — which may already hold a previous unit's physical values.
            tr = self._archive_window_traces.get(comp)
            if tr is None:
                continue
            arr = np.asarray(tr.y, dtype=np.float64)
            if not np.all(np.isfinite(arr)):
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

    def _handoff_archive_to_hvsr(
        self, device: object, group: object, t_start_epoch: float, t_end_epoch: float
    ) -> None:
        """Switch to the HVSR tab, prefilled with the Archive selection.

        Prefill only — the user reviews the HVSR settings and clicks "Run on
        archive". The interval round-trips exactly.
        """
        if not isinstance(device, str) or not isinstance(group, dict):
            return
        grp = {str(k): str(v) for k, v in group.items()}
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
        """
        from obspy import UTCDateTime

        from echosmonitor.core.hvsr import HvsrSettings
        from echosmonitor.storage.archive_reader import ArchiveReader

        assert isinstance(settings, HvsrSettings)
        reader = ArchiveReader(self._engine.archive_root(device), self._engine.archive_dao())
        return self._hvsr_engine.start_archive_measurement(
            device,
            group,
            UTCDateTime(str(t_start)),
            UTCDateTime(str(t_end)),
            settings,
            reader,
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

    def _load_recent_detections(self) -> None:
        """Pre-populate the table from the DB index (C3): the last 24 h of
        detections, capped by ``ui.recent_detections_limit``. A bounded
        index read — no waveforms are loaded."""
        from obspy import UTCDateTime

        limit = int(self._config.ui.recent_detections_limit)
        if limit <= 0:
            return
        since = UTCDateTime() - 24 * 3600
        recent = self._engine.recent_detections(limit, since)
        if recent:
            self._detection_table.load_historical(recent)
            _log.info("detections_recent_loaded", count=len(recent), limit=limit)

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
        # M11 B: stop the dedicated deconvolution thread. It runs a plain
        # Qt event loop dispatching one-shot ``compute`` slots (no parked
        # blocking loop), so ``quit()`` returns it promptly.
        self._decon_thread.quit()
        if not self._decon_thread.wait(_DECON_THREAD_JOIN_MS):
            _log.warning("decon_thread_join_timeout")
        # Shut the HVSR engine down BEFORE the streaming engine: its worker
        # thread reads the engine's ring buffers, so it must be joined
        # before the engine it reads from goes away.
        self._hvsr_engine.shutdown()
        # Archive detail loader: its worker consults the engine's DAO
        # (thread-safe, read-only), so join it before the engine tears down.
        self._archive_loader.shutdown()
        self._archive_window_loader.shutdown()
        self._engine.stop()
        if self._live_tabs is not None:
            self._live_tabs.save_active_tab()
        settings = self._settings()
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("windowState", self.saveState())
        _log.info("main_window_closing")
        super().closeEvent(event)
