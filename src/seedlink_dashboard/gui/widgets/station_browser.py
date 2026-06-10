"""Stations dock body — browse the SeedLink server catalog.

Layout:

    [Device ▼] [Refresh] ........................ [⠋]
    ┌────────────────────────┬─────────────────────────────┐
    │ ▼ IU                   │ ☐ NSLC          Type fs Per │
    │   • ANMO  Albuquerque  │ ☐ IU.ANMO.00.BHZ  D 100 …   │
    │   • COLA  College AK   │ ☐ IU.ANMO.00.BHN  D 100 …   │
    │ ▼ IV                   │                             │
    │   • MILN  Milan        │                             │
    └────────────────────────┴─────────────────────────────┘
    0 streams selected                  [Add to device…]

Two non-obvious decisions are baked in:

1. **Request-id filtering.** Every Refresh and station-click generates a
   fresh UUID stored as ``self._pending_*_request``. Replies whose
   ``request_id`` does not match are silently discarded. This avoids a
   stale stations response landing on the right pane after the user has
   moved on, *without* needing cross-thread cancellation.
2. **Per-(device, net, sta) cache.** Re-clicking a station the user
   already opened skips the network round-trip — the streams table
   repopulates from the cached :class:`StreamInfo` list. The cache is
   in-memory only; closing and re-opening the dock starts fresh.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import (
    QByteArray,
    QPoint,
    QSettings,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from seedlink_dashboard.config.schema import (
    DeviceConfig,
    StreamSelectorConfig,
)
from seedlink_dashboard.core.exceptions import ConfigError
from seedlink_dashboard.core.models import ConnState

if TYPE_CHECKING:
    from PySide6.QtGui import QCloseEvent

    from seedlink_dashboard.core.config_store import ConfigStore
    from seedlink_dashboard.core.info import StationInfo, StreamInfo
    from seedlink_dashboard.core.info_worker import InfoWorker
    from seedlink_dashboard.core.streaming_engine import StreamingEngine

_log = structlog.get_logger(__name__)

# QSettings keys — namespaced under ``StationBrowser/`` so they don't
# collide with other widgets persisting state under the shared org/app.
_SETTINGS_LAST_DEVICE = "StationBrowser/lastDeviceId"
_SETTINGS_EXPANDED = "StationBrowser/expandedNetworks"
_SETTINGS_TREE_SCROLL = "StationBrowser/treeScroll"
_SETTINGS_SPLITTER = "StationBrowser/splitterSizes"

# QSettings is shared across the app; use the same keys MainWindow uses.
_ORG_NAME = "SeedLinkDashboard"
_APP_NAME = "SeedLinkDashboard"

# Spinner cadence. 100 ms keeps the rotation visibly smooth without
# adding meaningful GUI-thread load — the Qt timer wakes once per
# frame at 60 Hz anyway, so a 10 Hz tick is well under that.
_SPINNER_INTERVAL_MS = 100
# Animated spinner glyphs (Braille dot pattern, common across
# monospace fonts). Cycled in order on each timer tick.
_SPINNER_GLYPHS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_IDLE_GLYPH = "↻"

# Stack indices for the empty-state QLabel switch. Order must match
# the ``_empty_state_*`` insertions in :meth:`_build_empty_state`.
_EMPTY_NO_DEVICES = 0
_EMPTY_NO_DATA = 1
_EMPTY_DEVICE_OFFLINE = 2
_EMPTY_ZERO_STATIONS = 3

_EMPTY_TEXT = (
    "No devices configured — add one in the Devices dock.",
    "No data yet — click Refresh to query the server.",
    "Device offline — refresh to retry.",
    "Server returned 0 stations.",
)

# QStackedWidget pages for the main body.
_PAGE_EMPTY = 0
_PAGE_BROWSER = 1

# Streams table column layout.
_COL_CHECK = 0
_COL_NSLC = 1
_COL_TYPE = 2
_COL_RATE = 3
_COL_PERIOD = 4
_TABLE_HEADERS = ("", "NSLC", "Type", "Sampling rate", "Active period")

# Tree column layout.
_TREE_COL_STATION = 0
_TREE_COL_DESC = 1
_TREE_HEADERS = ("Station", "Description")

# Tooltip on the disabled "Add to device…" button when no ConfigStore
# is wired (i.e. the existing test stubs). Real users always run with a
# store wired, so they see this only if an upstream change drops the
# store wiring.
_ADD_DISABLED_TOOLTIP = "Available in M4 stage B."

# Tooltip on the button when it's enabled-eligible but no streams are
# checked yet. Differentiates "no streams selected" from "feature off".
_ADD_NO_SELECTION_TOOLTIP = "Check at least one stream to enable."


class StationBrowser(QWidget):
    """Body widget for the Stations dock. Owns no thread; talks to ``InfoWorker``.

    Wires across thread boundaries via :class:`Qt.ConnectionType.QueuedConnection`
    where appropriate. Engine signals (``devicesChanged`` /
    ``deviceStateChanged``) come from the same GUI thread the browser
    lives on — Auto resolves to Direct.
    """

    # Internal: re-emit the request slots the GUI calls, with a queued
    # connection so the underlying ``InfoWorker.requestX`` slots run on
    # the worker thread. Routing through these signals avoids manually
    # specifying a connection type at every call site.
    _stationsRequested = Signal(str, str, str, int)  # noqa: N815
    _streamsRequested = Signal(str, str, str, int, str, str)  # noqa: N815

    def __init__(
        self,
        *,
        engine: StreamingEngine,
        info_worker: InfoWorker,
        store: ConfigStore | None = None,
        parent: QWidget | None = None,
    ) -> None:
        """Construct the browser.

        Args:
            engine: Source of truth for the configured device list; the
                browser subscribes to ``devicesChanged`` so the combo
                refreshes when devices are added/removed (Stage B).
            info_worker: Background INFO-fetch worker. The browser
                connects to its three reply signals and emits
                ``request*`` slots into it via queued connection.
            store: Optional :class:`ConfigStore`. When provided, the
                bottom toolbar's "Add to device..." button becomes
                interactive and routes the user through a small popup
                that adds the checked streams either to an existing
                device on the same host:port, or as a new device. The
                Stage-A test stubs pass ``None`` here.
            parent: Standard Qt parent. Optional.
        """
        super().__init__(parent)
        self._engine = engine
        self._info_worker = info_worker
        self._store = store

        # State -------------------------------------------------------
        # Most recent request_id we issued; replies with a different id
        # are stale and dropped. Two pending fields because Refresh and
        # station-click race independently.
        self._pending_stations_request: str | None = None
        self._pending_streams_request: str | None = None
        # Cached StationInfo list per device, populated on every
        # successful stationsReceived. Re-renders the tree without a
        # network round-trip when the user re-selects the device.
        self._stations_by_device: dict[str, list[StationInfo]] = {}
        # Cached StreamInfo list per (device_id, network, station).
        # Re-clicking a station shortcuts straight to populating the
        # right-hand table.
        self._streams_cache: dict[tuple[str, str, str], list[StreamInfo]] = {}
        # Track currently-selected device id so empty-state logic can
        # branch on whether a fetch has happened for it yet.
        self._fetched_devices: set[str] = set()
        # Live device-state mirror so the empty-state widget can show
        # "Device offline — refresh to retry." when the engine reports
        # anything other than CONNECTED.
        self._device_states: dict[str, ConnState] = {}
        # Spinner animation state.
        self._spinner_active = False
        self._spinner_index = 0

        # Layout root -------------------------------------------------
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Top row -----------------------------------------------------
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(QLabel("Device:"))
        self._device_combo = QComboBox(self)
        self._device_combo.setMinimumWidth(220)
        top.addWidget(self._device_combo)
        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_button.setEnabled(False)
        top.addWidget(self._refresh_button)
        top.addStretch(1)
        self._spinner_label = QLabel(_SPINNER_IDLE_GLYPH, self)
        self._spinner_label.setFixedSize(16, 16)
        self._spinner_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self._spinner_label)
        root.addLayout(top)

        # Spinner timer (started on demand). Driven entirely on the GUI
        # thread; cycles a Braille glyph through ``_spinner_label``.
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(_SPINNER_INTERVAL_MS)
        self._spinner_timer.timeout.connect(self._on_spinner_tick)

        # Stacked main body ------------------------------------------
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_empty_state())
        self._stack.addWidget(self._build_browser())
        root.addWidget(self._stack, 1)

        # Bottom row --------------------------------------------------
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self._selection_label = QLabel("0 streams selected", self)
        bottom.addWidget(self._selection_label)
        bottom.addStretch(1)
        self._add_to_device_button = QPushButton("Add to device…", self)
        # Initial enable state depends on whether a ConfigStore is
        # wired AND any streams are checked. Tooltip distinguishes the
        # two "off" cases so the user knows what to do.
        self._add_to_device_button.setEnabled(False)
        if self._store is None:
            self._add_to_device_button.setToolTip(_ADD_DISABLED_TOOLTIP)
        else:
            self._add_to_device_button.setToolTip(_ADD_NO_SELECTION_TOOLTIP)
        self._add_to_device_button.clicked.connect(self._on_add_to_device_clicked)
        bottom.addWidget(self._add_to_device_button)
        root.addLayout(bottom)

        # Wiring ------------------------------------------------------
        self._engine.devicesChanged.connect(self._refresh_device_combo)
        self._engine.deviceStateChanged.connect(self._on_device_state_changed)
        # Cross-thread: results land on this thread via queued conn.
        self._info_worker.stationsReceived.connect(
            self._on_stations_received,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._info_worker.streamsReceived.connect(
            self._on_streams_received,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._info_worker.infoFailed.connect(
            self._on_info_failed,
            type=Qt.ConnectionType.QueuedConnection,
        )
        # Internal request signals → worker slots (queued so the
        # underlying enqueue runs on the worker thread).
        self._stationsRequested.connect(
            self._info_worker.requestStations,
            type=Qt.ConnectionType.QueuedConnection,
        )
        self._streamsRequested.connect(
            self._info_worker.requestStreams,
            type=Qt.ConnectionType.QueuedConnection,
        )

        self._refresh_button.clicked.connect(self._on_refresh_clicked)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._tree.itemSelectionChanged.connect(self._on_station_selected)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._streams_table.itemChanged.connect(self._on_check_toggled)

        # Initial population ------------------------------------------
        self._refresh_device_combo()
        self._restore_settings()
        self._update_empty_state()

    # ------------------------------------------------------------------
    # UI construction helpers
    # ------------------------------------------------------------------
    def _build_empty_state(self) -> QWidget:
        """Wrap the four empty-state messages in a centred QStackedWidget."""
        wrapper = QWidget(self)
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)
        self._empty_stack = QStackedWidget(wrapper)
        for text in _EMPTY_TEXT:
            lbl = QLabel(text, wrapper)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("QLabel { color: #888; font-style: italic; }")
            self._empty_stack.addWidget(lbl)
        outer.addWidget(self._empty_stack)
        outer.addStretch(1)
        return wrapper

    def _build_browser(self) -> QWidget:
        """Build the splitter holding the station tree + streams table."""
        wrapper = QWidget(self)
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        self._splitter = QSplitter(Qt.Orientation.Horizontal, wrapper)

        # Tree --------------------------------------------------------
        self._tree = QTreeWidget(self._splitter)
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(list(_TREE_HEADERS))
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree_header = self._tree.header()
        tree_header.setSectionResizeMode(_TREE_COL_STATION, QHeaderView.ResizeMode.ResizeToContents)
        tree_header.setSectionResizeMode(_TREE_COL_DESC, QHeaderView.ResizeMode.Stretch)
        self._splitter.addWidget(self._tree)

        # Streams table ----------------------------------------------
        self._streams_table = QTableWidget(self._splitter)
        self._streams_table.setColumnCount(len(_TABLE_HEADERS))
        self._streams_table.setHorizontalHeaderLabels(list(_TABLE_HEADERS))
        self._streams_table.verticalHeader().setVisible(False)
        self._streams_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._streams_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table_header = self._streams_table.horizontalHeader()
        table_header.setSectionResizeMode(_COL_CHECK, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(_COL_NSLC, QHeaderView.ResizeMode.Stretch)
        table_header.setSectionResizeMode(_COL_TYPE, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(_COL_RATE, QHeaderView.ResizeMode.ResizeToContents)
        table_header.setSectionResizeMode(_COL_PERIOD, QHeaderView.ResizeMode.ResizeToContents)
        self._splitter.addWidget(self._streams_table)

        layout.addWidget(self._splitter)
        return wrapper

    # ------------------------------------------------------------------
    # Slots / handlers
    # ------------------------------------------------------------------
    @Slot()
    def _refresh_device_combo(self) -> None:
        """Repopulate the device combo from ``engine.devices()``.

        Preserves the previously-selected device if it still exists.
        Disables Refresh when the combo is empty.
        """
        previous = self._current_device_id()
        self._device_combo.blockSignals(True)
        try:
            self._device_combo.clear()
            for dev in self._engine.devices():
                self._device_combo.addItem(dev.name, dev.name)
            if previous is not None:
                idx = self._device_combo.findData(previous)
                if idx >= 0:
                    self._device_combo.setCurrentIndex(idx)
        finally:
            self._device_combo.blockSignals(False)

        has_device = self._device_combo.count() > 0
        self._refresh_button.setEnabled(has_device)
        self._update_empty_state()

    @Slot(str, int)
    def _on_device_state_changed(self, device_name: str, state: int) -> None:
        try:
            self._device_states[device_name] = ConnState(state)
        except ValueError:
            self._device_states[device_name] = ConnState.DISCONNECTED
        self._update_empty_state()

    @Slot(int)
    def _on_device_changed(self, _index: int) -> None:
        device_id = self._current_device_id()
        if device_id is None:
            return
        # If we have cached stations for this device, render them so
        # the user does not see an empty state after switching back to
        # an already-fetched device.
        cached = self._stations_by_device.get(device_id)
        if cached is not None:
            self._render_stations(device_id, cached)
        self._update_empty_state()

    @Slot()
    def _on_refresh_clicked(self) -> None:
        device_id = self._current_device_id()
        if device_id is None:
            return
        host, port = self._endpoint_for(device_id)
        if host is None or port is None:
            return
        request_id = uuid.uuid4().hex
        self._pending_stations_request = request_id
        self._start_spinner()
        _log.info(
            "station_browser_refresh",
            request_id=request_id,
            device_id=device_id,
            host=host,
            port=port,
        )
        self._stationsRequested.emit(request_id, device_id, host, port)

    @Slot(str, str, object)
    def _on_stations_received(
        self,
        request_id: str,
        device_id: str,
        stations: object,
    ) -> None:
        # Drop stale replies — the user has moved on (or this is a
        # response to an old refresh from a different device).
        if request_id != self._pending_stations_request:
            _log.debug(
                "station_browser_stations_stale",
                request_id=request_id,
                pending=self._pending_stations_request,
            )
            return
        self._pending_stations_request = None
        self._stop_spinner()
        # PySide6 forwards lists as ``object`` through Signal(object);
        # narrow defensively in case a future signal change breaks the
        # invariant. Empty list is a valid result (server returned 0).
        if not isinstance(stations, list):
            _log.warning(
                "station_browser_stations_bad_payload",
                payload_type=type(stations).__name__,
            )
            return
        self._stations_by_device[device_id] = list(stations)
        self._fetched_devices.add(device_id)
        # If the user has already switched away, defer rendering: the
        # cache is now warm so the next select-this-device path will
        # repopulate the tree without another network round-trip.
        if device_id == self._current_device_id():
            self._render_stations(device_id, list(stations))
        self._update_empty_state()

    @Slot(str, str, object)
    def _on_streams_received(
        self,
        request_id: str,
        device_id: str,
        streams: object,
    ) -> None:
        if request_id != self._pending_streams_request:
            return
        self._pending_streams_request = None
        if not isinstance(streams, list):
            _log.warning(
                "station_browser_streams_bad_payload",
                payload_type=type(streams).__name__,
            )
            return
        net, sta = self._current_station()
        if net and sta:
            self._streams_cache[(device_id, net, sta)] = list(streams)
        self._render_streams(list(streams))

    @Slot(str, str, str, str)
    def _on_info_failed(
        self,
        request_id: str,
        device_id: str,
        kind: str,
        reason: str,
    ) -> None:
        # A failed reply clears the matching pending id so the spinner
        # stops and a fresh Refresh can be issued without ambiguity.
        cleared = False
        if request_id == self._pending_stations_request:
            self._pending_stations_request = None
            cleared = True
        if request_id == self._pending_streams_request:
            self._pending_streams_request = None
            cleared = True
        if cleared:
            self._stop_spinner()
        _log.warning(
            "station_browser_info_failed",
            request_id=request_id,
            device_id=device_id,
            kind=kind,
            reason=reason,
        )

    @Slot()
    def _on_station_selected(self) -> None:
        device_id = self._current_device_id()
        if device_id is None:
            return
        net, sta = self._current_station()
        if not net or not sta:
            self._streams_table.setRowCount(0)
            return
        cached = self._streams_cache.get((device_id, net, sta))
        if cached is not None:
            self._render_streams(cached)
            return
        host, port = self._endpoint_for(device_id)
        if host is None or port is None:
            return
        request_id = uuid.uuid4().hex
        self._pending_streams_request = request_id
        _log.info(
            "station_browser_streams_request",
            request_id=request_id,
            device_id=device_id,
            network=net,
            station=sta,
        )
        self._streamsRequested.emit(request_id, device_id, host, port, net, sta)

    @Slot(QPoint)
    def _on_tree_context_menu(self, pos: QPoint) -> None:
        item = self._tree.itemAt(pos)
        menu = QMenu(self)
        copy_action = QAction("Copy NSLC", menu)
        net, sta = "", ""
        if item is not None:
            data = item.data(_TREE_COL_STATION, Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) == 2:
                net, sta = str(data[0]), str(data[1])
        copy_action.setEnabled(bool(net and sta))
        copy_action.triggered.connect(lambda: self._copy_nslc_to_clipboard(net, sta))
        menu.addAction(copy_action)
        # "Subscribe in Live" routes through the same flow as the
        # checkbox+button pair — but it scopes the selection to the
        # right-clicked station, so the user can subscribe without
        # touching the streams table at all. The single-station
        # selectors are wildcarded on location/channel so the resulting
        # device subscription pulls every channel from that station.
        subscribe_action = QAction("Subscribe in Live", menu)
        if self._store is not None and net and sta:
            subscribe_action.setEnabled(True)
            subscribe_action.triggered.connect(
                lambda: self._open_add_to_device([StreamSelectorConfig(network=net, station=sta)])
            )
        else:
            subscribe_action.setEnabled(False)
            if self._store is None:
                subscribe_action.setToolTip(_ADD_DISABLED_TOOLTIP)
        menu.addAction(subscribe_action)
        viewport = self._tree.viewport()
        if viewport is not None:
            menu.exec(viewport.mapToGlobal(pos))

    @Slot()
    def _on_spinner_tick(self) -> None:
        self._spinner_index = (self._spinner_index + 1) % len(_SPINNER_GLYPHS)
        self._spinner_label.setText(_SPINNER_GLYPHS[self._spinner_index])

    @Slot(QTableWidgetItem)
    def _on_check_toggled(self, _item: QTableWidgetItem) -> None:
        self._update_selection_label()

    # ------------------------------------------------------------------
    # Render helpers
    # ------------------------------------------------------------------
    def _render_stations(self, device_id: str, stations: list[StationInfo]) -> None:
        """Repopulate the tree with one network parent per distinct net code."""
        # Snapshot what was expanded before rebuilding so the
        # restore-from-settings path can also fall back to an in-session
        # expansion state. The child item list is replaced wholesale
        # because StationInfo is immutable and re-creation is cheap at
        # this scale (a few thousand rows max).
        self._tree.blockSignals(True)
        try:
            self._tree.clear()
            by_network: dict[str, list[StationInfo]] = {}
            for sta in stations:
                by_network.setdefault(sta.network, []).append(sta)
            settings = QSettings(_ORG_NAME, _APP_NAME)
            saved_expanded_raw = settings.value(_SETTINGS_EXPANDED, "")
            saved_last_device = settings.value(_SETTINGS_LAST_DEVICE, "")
            saved_expanded = (
                {n for n in str(saved_expanded_raw).split(",") if n}
                if str(saved_last_device) == device_id
                else set()
            )
            for net in sorted(by_network):
                parent = QTreeWidgetItem([net, ""])
                # Tag networks with their own UserRole so right-click
                # context detection can distinguish "user clicked a
                # network" from "user clicked a station".
                parent.setData(_TREE_COL_STATION, Qt.ItemDataRole.UserRole, (net, ""))
                self._tree.addTopLevelItem(parent)
                for sta in sorted(by_network[net], key=lambda s: s.station):
                    child = QTreeWidgetItem(
                        [sta.station, sta.description or ""],
                    )
                    child.setData(
                        _TREE_COL_STATION,
                        Qt.ItemDataRole.UserRole,
                        (net, sta.station),
                    )
                    parent.addChild(child)
                if not saved_expanded or net in saved_expanded:
                    parent.setExpanded(True)
        finally:
            self._tree.blockSignals(False)
        # Force the browser page so the empty-state widget is hidden
        # whenever there is at least something to render. Zero-station
        # responses still go through ``_update_empty_state`` below.
        self._update_empty_state()

    def _render_streams(self, streams: list[StreamInfo]) -> None:
        """Populate the streams table from the supplied list."""
        self._streams_table.blockSignals(True)
        try:
            self._streams_table.setRowCount(0)
            self._streams_table.setRowCount(len(streams))
            for row, stream in enumerate(streams):
                check_item = QTableWidgetItem("")
                check_item.setFlags(
                    Qt.ItemFlag.ItemIsUserCheckable
                    | Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable,
                )
                check_item.setCheckState(Qt.CheckState.Unchecked)
                self._streams_table.setItem(row, _COL_CHECK, check_item)
                nslc = f"{stream.network}.{stream.station}.{stream.location}.{stream.channel}"
                nslc_item = QTableWidgetItem(nslc)
                nslc_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable,
                )
                self._streams_table.setItem(row, _COL_NSLC, nslc_item)
                self._streams_table.setItem(
                    row,
                    _COL_TYPE,
                    QTableWidgetItem(stream.type or ""),
                )
                rate_text = ""
                if stream.sampling_rate is not None:
                    rate_text = f"{stream.sampling_rate:g} Hz"
                self._streams_table.setItem(row, _COL_RATE, QTableWidgetItem(rate_text))
                period_parts: list[str] = []
                if stream.begin:
                    period_parts.append(stream.begin)
                if stream.end:
                    period_parts.append(stream.end)
                self._streams_table.setItem(
                    row,
                    _COL_PERIOD,
                    QTableWidgetItem(" → ".join(period_parts)),
                )
        finally:
            self._streams_table.blockSignals(False)
        self._update_selection_label()

    def _update_selection_label(self) -> None:
        count = 0
        for row in range(self._streams_table.rowCount()):
            item = self._streams_table.item(row, _COL_CHECK)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                count += 1
        self._selection_label.setText(f"{count} streams selected")
        # Stage B: enable Add-to-device only if at least one stream is
        # checked AND a ConfigStore is wired. The two-tooltip dance
        # explains both possible disable reasons to the user.
        if self._store is None:
            self._add_to_device_button.setEnabled(False)
            self._add_to_device_button.setToolTip(_ADD_DISABLED_TOOLTIP)
        elif count == 0:
            self._add_to_device_button.setEnabled(False)
            self._add_to_device_button.setToolTip(_ADD_NO_SELECTION_TOOLTIP)
        else:
            self._add_to_device_button.setEnabled(True)
            self._add_to_device_button.setToolTip("")

    def _update_empty_state(self) -> None:
        """Decide which page (empty / browser) and which empty-state label to show.

        Priority (highest first):

        1. No devices at all → "No devices configured…".
        2. Selected device has cached stations → browser page.
        3. Selected device has been fetched and returned 0 → "Server returned 0 stations.".
        4. Device has not been fetched yet AND state != CONNECTED → "Device offline".
        5. Otherwise → "No data yet — click Refresh".
        """
        if self._device_combo.count() == 0:
            self._stack.setCurrentIndex(_PAGE_EMPTY)
            self._empty_stack.setCurrentIndex(_EMPTY_NO_DEVICES)
            return
        device_id = self._current_device_id()
        if device_id is None:
            self._stack.setCurrentIndex(_PAGE_EMPTY)
            self._empty_stack.setCurrentIndex(_EMPTY_NO_DATA)
            return
        cached = self._stations_by_device.get(device_id)
        if cached is not None and cached:
            self._stack.setCurrentIndex(_PAGE_BROWSER)
            return
        if device_id in self._fetched_devices and not cached:
            self._stack.setCurrentIndex(_PAGE_EMPTY)
            self._empty_stack.setCurrentIndex(_EMPTY_ZERO_STATIONS)
            return
        state = self._device_states.get(device_id)
        if state is not None and state != ConnState.CONNECTED:
            self._stack.setCurrentIndex(_PAGE_EMPTY)
            self._empty_stack.setCurrentIndex(_EMPTY_DEVICE_OFFLINE)
            return
        self._stack.setCurrentIndex(_PAGE_EMPTY)
        self._empty_stack.setCurrentIndex(_EMPTY_NO_DATA)

    # ------------------------------------------------------------------
    # Spinner control
    # ------------------------------------------------------------------
    def _start_spinner(self) -> None:
        if self._spinner_active:
            return
        self._spinner_active = True
        self._spinner_index = 0
        self._spinner_label.setText(_SPINNER_GLYPHS[0])
        self._spinner_timer.start()

    def _stop_spinner(self) -> None:
        if not self._spinner_active:
            # Multiple replies might land — only the first one stops it.
            return
        self._spinner_active = False
        self._spinner_timer.stop()
        self._spinner_label.setText(_SPINNER_IDLE_GLYPH)

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------
    def _current_device_id(self) -> str | None:
        idx = self._device_combo.currentIndex()
        if idx < 0:
            return None
        data = self._device_combo.itemData(idx)
        if data is None:
            return None
        return str(data)

    def _current_station(self) -> tuple[str, str]:
        items = self._tree.selectedItems()
        if not items:
            return "", ""
        data = items[0].data(_TREE_COL_STATION, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and len(data) == 2:
            return str(data[0]), str(data[1])
        return "", ""

    def _endpoint_for(self, device_id: str) -> tuple[str | None, int | None]:
        for dev in self._engine.devices():
            if dev.name == device_id:
                return dev.host, int(dev.port)
        return None, None

    def _copy_nslc_to_clipboard(self, network: str, station: str) -> None:
        clip = QGuiApplication.clipboard()
        if clip is None:
            return
        clip.setText(f"{network}.{station}.*.*")

    # ------------------------------------------------------------------
    # Add-to-device flow (Stage B)
    # ------------------------------------------------------------------
    def _checked_selectors(self) -> list[StreamSelectorConfig]:
        """Build :class:`StreamSelectorConfig` from every checked stream row.

        The streams table renders one row per :class:`StreamInfo`; the
        NSLC string in column 1 is the canonical form. We round-trip
        through the dotted form so a user-visible inconsistency between
        the table and the resulting selector entries is impossible.
        """
        out: list[StreamSelectorConfig] = []
        for row in range(self._streams_table.rowCount()):
            check_item = self._streams_table.item(row, _COL_CHECK)
            if check_item is None or check_item.checkState() != Qt.CheckState.Checked:
                continue
            nslc_item = self._streams_table.item(row, _COL_NSLC)
            if nslc_item is None:
                continue
            parts = nslc_item.text().split(".")
            if len(parts) != 4:
                _log.warning("station_browser_invalid_nslc", text=nslc_item.text())
                continue
            net, sta, loc, cha = parts
            out.append(
                StreamSelectorConfig(
                    network=net,
                    station=sta,
                    location=loc,
                    channel=cha,
                )
            )
        return out

    def _clear_stream_checkboxes(self) -> None:
        """Uncheck every row so a successful add doesn't double-fire on accident.

        Blocks the table's ``itemChanged`` signal during the bulk uncheck
        so :meth:`_update_selection_label` runs once at the end rather
        than once per row.
        """
        self._streams_table.blockSignals(True)
        try:
            for row in range(self._streams_table.rowCount()):
                item = self._streams_table.item(row, _COL_CHECK)
                if item is not None:
                    item.setCheckState(Qt.CheckState.Unchecked)
        finally:
            self._streams_table.blockSignals(False)
        self._update_selection_label()

    @Slot()
    def _on_add_to_device_clicked(self) -> None:
        if self._store is None:
            return
        selectors = self._checked_selectors()
        if not selectors:
            return
        self._open_add_to_device(selectors)

    def _open_add_to_device(self, selectors: list[StreamSelectorConfig]) -> None:
        """Run the small "add to existing / add as new" popup.

        Args:
            selectors: The :class:`StreamSelectorConfig` list to attach.
                For the toolbar-button path this is every checked row;
                for the right-click "Subscribe in Live" path this is a
                single wildcard selector for the right-clicked station.
        """
        if self._store is None or not selectors:
            return
        device_id = self._current_device_id()
        if device_id is None:
            return
        host, port = self._endpoint_for(device_id)
        if host is None or port is None:
            return
        # Devices on the same host:port are eligible for "Add to existing".
        same_endpoint = [
            d for d in self._store.root.devices if d.host == host and int(d.port) == int(port)
        ]
        dialog = _AddToDeviceDialog(
            parent=self,
            selectors=list(selectors),
            host=host,
            port=int(port),
            source_device_label=device_id,
            existing_same_endpoint=same_endpoint,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        choice = dialog.choice()
        if choice == _AddToDeviceDialog.CHOICE_EXISTING:
            target = dialog.target_existing_name()
            if target is None:
                return
            try:
                self._store.add_selectors(target, list(selectors))
            except ConfigError as exc:
                QMessageBox.critical(self, "Add failed", str(exc))
                _log.warning("station_browser_add_failed", device=target, error=str(exc))
                return
            self._clear_stream_checkboxes()
        elif choice == _AddToDeviceDialog.CHOICE_NEW:
            # Local import: Stage B's add-as-new dialog is the same
            # DeviceDialog the device panel opens, so circulating it
            # at module load would couple browser → dialogs even when
            # the browser path isn't used.
            from seedlink_dashboard.gui.dialogs.device_dialog import DeviceDialog

            # Schema rejects ``name=""`` (Field min_length=1), so seed
            # a placeholder name derived from the source device's id.
            # The user is expected to overwrite it before accepting —
            # the DeviceForm's name validator highlights non-conforming
            # characters anyway, so the placeholder visibly differs
            # from the destination.
            placeholder_name = self._propose_new_device_name(host, int(port))
            prefill = DeviceConfig(
                name=placeholder_name,
                host=host,
                port=int(port),
                selectors=list(selectors),
            )
            result = DeviceDialog.add(self, self._store, prefill=prefill)
            if result == QDialog.DialogCode.Accepted:
                self._clear_stream_checkboxes()

    def _propose_new_device_name(self, host: str, port: int) -> str:
        """Generate a placeholder device name for the "add as new" prefill.

        Stage B caveat: the design plan calls for ``name=""`` here, but
        the schema enforces ``min_length=1`` so a literal empty string
        would fail to construct the prefill. We instead fabricate a
        slug like ``new-rtserve.iris.washington.edu-18000`` that is:

        * unique against the current device list (so the duplicate-name
          validator stays silent until the user has committed to a name);
        * visibly placeholder-y so the user knows to replace it.
        """
        if self._store is None:
            return "new-device"
        existing = {d.name for d in self._store.root.devices}
        base = f"new-{host}-{port}"
        # Strip characters the name validator rejects so the prefill
        # is at least valid against the regex; keep the host slug
        # roughly readable rather than aggressively normalised.
        cleaned = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in base)
        if cleaned not in existing:
            return cleaned
        # Append a numeric suffix to deconflict.
        for i in range(2, 100):
            candidate = f"{cleaned}-{i}"
            if candidate not in existing:
                return candidate
        return cleaned  # extreme fallback — duplicate; user will edit.

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _settings(self) -> QSettings:
        return QSettings(_ORG_NAME, _APP_NAME)

    def _restore_settings(self) -> None:
        settings = self._settings()
        last_device = settings.value(_SETTINGS_LAST_DEVICE, "")
        if isinstance(last_device, str) and last_device:
            idx = self._device_combo.findData(last_device)
            if idx >= 0:
                self._device_combo.setCurrentIndex(idx)
        splitter_state = settings.value(_SETTINGS_SPLITTER)
        if isinstance(splitter_state, (bytes, bytearray, QByteArray)):
            self._splitter.restoreState(splitter_state)
        scroll_value = settings.value(_SETTINGS_TREE_SCROLL, 0)
        # QSettings round-trips integers as ``str`` under the IniFormat
        # backend used by the test harness, so coerce defensively. Any
        # garbage in the store falls back to 0 rather than raising.
        try:
            scroll_int = int(str(scroll_value)) if scroll_value is not None else 0
        except (TypeError, ValueError):
            scroll_int = 0
        scrollbar = self._tree.verticalScrollBar()
        if scrollbar is not None:
            scrollbar.setValue(scroll_int)

    def persist_state(self) -> None:
        """Save QSettings state (last device, expansion, scroll, splitter).

        Public so :class:`~seedlink_dashboard.gui.main_window.MainWindow`
        can call it from its ``closeEvent`` — Qt does NOT propagate
        ``closeEvent`` from a closing ``QMainWindow`` to its docked
        child widgets, so relying on this widget's own ``closeEvent``
        loses state on a normal app-quit.
        """
        settings = self._settings()
        device_id = self._current_device_id()
        if device_id is not None:
            settings.setValue(_SETTINGS_LAST_DEVICE, device_id)
        expanded: list[str] = []
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if top is not None and top.isExpanded():
                expanded.append(top.text(_TREE_COL_STATION))
        settings.setValue(_SETTINGS_EXPANDED, ",".join(expanded))
        scrollbar = self._tree.verticalScrollBar()
        if scrollbar is not None:
            settings.setValue(_SETTINGS_TREE_SCROLL, int(scrollbar.value()))
        settings.setValue(_SETTINGS_SPLITTER, self._splitter.saveState())

    # Backwards-compatible alias so the existing test suite (and any
    # internal caller) keeps working without churn. The leading-
    # underscore form was used during Stage A development.
    _persist_settings = persist_state

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        self.persist_state()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Test-only accessors
    # ------------------------------------------------------------------
    def _empty_state_index_for_test(self) -> int:
        if self._stack.currentIndex() != _PAGE_EMPTY:
            return -1
        return self._empty_stack.currentIndex()

    def _network_count_for_test(self) -> int:
        return self._tree.topLevelItemCount()

    def _station_count_for_test(self, network: str) -> int:
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if top is not None and top.text(_TREE_COL_STATION) == network:
                return top.childCount()
        return 0

    def _streams_row_count_for_test(self) -> int:
        return self._streams_table.rowCount()

    def _set_check_state_for_test(self, row: int, checked: bool) -> None:
        item = self._streams_table.item(row, _COL_CHECK)
        if item is None:
            return
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _select_station_for_test(self, network: str, station: str) -> None:
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if top is None or top.text(_TREE_COL_STATION) != network:
                continue
            for j in range(top.childCount()):
                child = top.child(j)
                if child is not None and child.text(_TREE_COL_STATION) == station:
                    self._tree.setCurrentItem(child)
                    return


class _AddToDeviceDialog(QDialog):
    """Tiny "Add selected streams to which device?" popup.

    Two radio buttons:

    * "Add to existing device" — only selectable when at least one
      device on the same host:port already exists. The combo lists
      those devices (filtered by endpoint).
    * "Add as new device..." — always selectable. On accept, the
      caller opens :class:`DeviceDialog.add` prefilled with the
      selectors / host / port.

    Returns Accepted when the user clicks OK with a valid choice.
    Stays open if the user picks "existing" with no selection — but
    the radio is auto-disabled when that branch has no candidates,
    so the case is degenerate.
    """

    CHOICE_EXISTING = "existing"
    CHOICE_NEW = "new"

    def __init__(
        self,
        *,
        parent: QWidget | None,
        selectors: list[StreamSelectorConfig],
        host: str,
        port: int,
        source_device_label: str,
        existing_same_endpoint: list[DeviceConfig],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add to device")
        self.setModal(True)
        self._selectors = selectors

        layout = QVBoxLayout(self)
        # Header line that names the source endpoint and the selector
        # count so the user knows what they're about to commit to.
        header = QLabel(
            f"Selected: {len(selectors)} stream(s) from {source_device_label} ({host}:{port})",
            self,
        )
        layout.addWidget(header)

        # Existing radio + combo --------------------------------------
        existing_row = QHBoxLayout()
        self._existing_radio = QRadioButton("Add to existing device:", self)
        existing_row.addWidget(self._existing_radio)
        self._existing_combo = QComboBox(self)
        self._existing_combo.setMinimumWidth(220)
        for dev in existing_same_endpoint:
            self._existing_combo.addItem(dev.name, dev.name)
        existing_row.addWidget(self._existing_combo, 1)
        layout.addLayout(existing_row)
        # Disable the whole "existing" branch when the endpoint has no
        # devices yet — the user can only choose "new" in that case.
        if not existing_same_endpoint:
            self._existing_radio.setEnabled(False)
            self._existing_combo.setEnabled(False)

        self._new_radio = QRadioButton("Add as new device...", self)
        layout.addWidget(self._new_radio)

        # Group the radios so only one is selected at a time. Default
        # to "existing" when available; otherwise "new".
        self._radio_group = QButtonGroup(self)
        self._radio_group.addButton(self._existing_radio)
        self._radio_group.addButton(self._new_radio)
        if existing_same_endpoint:
            self._existing_radio.setChecked(True)
        else:
            self._new_radio.setChecked(True)

        # Buttons -----------------------------------------------------
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        layout.addWidget(buttons)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

    def choice(self) -> str:
        if self._existing_radio.isChecked():
            return self.CHOICE_EXISTING
        return self.CHOICE_NEW

    def target_existing_name(self) -> str | None:
        idx = self._existing_combo.currentIndex()
        if idx < 0:
            return None
        data = self._existing_combo.itemData(idx)
        if data is None:
            return None
        return str(data)


__all__ = ["StationBrowser"]
