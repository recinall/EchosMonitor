"""Devices dock: a tree of (device, NSLC streams) with state badges + counters.

State updates arrive immediately via ``on_device_state`` (engine signal,
sub-millisecond). Packet / byte counters AND the diagnostics column
(attempt counter / last failure / next retry seconds) refresh on a 1 Hz
``QTimer`` poll of the engine's status snapshot — they tick fast enough
to feel live without flooding the GUI thread with per-packet repaint
work, and use the same status feed so there's only one polling timer.

Stage B (M4) adds a toolbar above the tree with Add / Edit / Remove /
Reconnect actions wired to a :class:`ConfigStore` and the streaming
engine. When the configured device list is empty, a centred "No devices
configured." panel replaces the tree (a :class:`QStackedWidget` swaps
between the two so the tree's columns survive empty-state cycles).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QPoint, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QBrush, QColor
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.exceptions import ConfigError
from echosmonitor.core.models import (
    AcquisitionState,
    ClockHealth,
    ConnState,
    EchosDeviceSnapshot,
)

if TYPE_CHECKING:
    from echosmonitor.core.config_store import ConfigStore
    from echosmonitor.core.models import DeviceStatus
    from echosmonitor.core.streaming_engine import StreamingEngine

_log = structlog.get_logger(__name__)

_STATE_COLORS: dict[int, str] = {
    int(ConnState.DISCONNECTED): "#888888",
    int(ConnState.CONNECTING): "#d9a441",
    int(ConnState.CONNECTED): "#3aa371",
    int(ConnState.RECONNECTING): "#d9a441",
    # Distinct darker amber for the backoff-sleep state. Pairs with the
    # same colour in ``live_stack._STATE_COLORS`` so the tree row and the
    # plot-area badge agree at a glance.
    int(ConnState.WAITING_RETRY): "#c98f2a",
    int(ConnState.STOPPED): "#666666",
}

# Counter refresh cadence. 1 Hz is plenty: counters change monotonically
# and the user reads them as a sanity check, not a real-time display.
_STATS_REFRESH_MS = 1000

# Default we fall back to when the panel hasn't been told a device's
# configured ``connect_timeout_s`` yet. The tooltip "I'm worried"
# threshold uses ``2 * connect_timeout`` so this value matches the
# schema default at the time of writing — keeps cold-start tooltips
# from triggering early before main_window pushes the real values in.
_DEFAULT_CONNECT_TIMEOUT_S = 10.0

# Human-readable expansion of the closed FailureKind set used in the
# tooltip body. Adding a new ``FailureKind`` literal in models.py
# requires a corresponding entry here.
_FAILURE_KIND_HUMANIZED: dict[str, str] = {
    "timeout": "TCP connect timed out",
    "refused": "connection refused",
    "dns": "DNS lookup failed",
    "unknown": "unknown error",
    "protocol_rejected": "server rejected the requested stations",
    "protocol_unsupported": "server lacks a required capability",
}

# Suffix appended to the state badge in ``_refresh_stats`` when the
# device is in WAITING_RETRY because of a misconfiguration (rejected
# selectors) rather than a transient outage. The marker tells the user
# at a glance that the retry loop is futile until they fix the config.
_MISCONFIG_BADGE_SUFFIX = " (!)"

_COL_NAME = 0
# M2-C: per-device acquisition badge (rule 13 — the user state must be
# unmistakable). Idle / Monitoring / ● REC, driven by the engine's
# acquisitionStateChanged signal + the 1 Hz stats refresh.
_COL_ACQ = 1
_COL_STATE = 2
_COL_DIAG = 3
_COL_STATS = 4
# M1-C: Echos firmware status (fw / uptime / clients / ring / GNSS /
# calibration) fed by the EchosStatusWorker poller. Empty for generic
# SeedLink devices (no ``echos:`` config section).
_COL_ECHOS = 5

# M2-C acquisition badges (rule 13): the Recording badge is loud red,
# Idle is dim — the current user state must be unmistakable at a glance.
_ACQ_BADGES = {
    AcquisitionState.IDLE: "Idle",
    AcquisitionState.MONITORING: "Monitoring",
    AcquisitionState.RECORDING: "\u25cf REC",
}
_ACQ_REC_COLOR = "#d04040"
_ACQ_IDLE_COLOR = "#808080"

# Echos column render colours: healthy text uses the default palette;
# a failed poll renders dim amber so it reads as "stale/unreachable",
# not as a device-down alarm (the SeedLink State column owns that).
_ECHOS_FAIL_COLOR = "#c98f2a"

# QStackedWidget pages for the body. Page 0 is the populated tree;
# page 1 is the centred empty-state with the inline "Add device..."
# button. We swap between them based on whether the store has any
# devices configured (Stage B) — the tree alone can be empty without
# implying "no devices" if the engine's emit lag has not caught up,
# so the swap is gated on ``store.root.devices`` not on the tree's
# ``topLevelItemCount`` directly.
_PAGE_TREE = 0
_PAGE_EMPTY = 1

# Toolbar action labels — pulled out so the test that asserts on them
# stays robust against stylistic copy-edits.
_ACTION_ADD = "+ Add device"
_ACTION_EDIT = "Edit"
_ACTION_REMOVE = "Remove"
_ACTION_RECONNECT = "Reconnect now"


def _format_count(n: int) -> str:
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / (1024**2):.1f} MB"
    return f"{n / (1024**3):.1f} GB"


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 48 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


# Compact clock-health tokens for the Echos column (M6). UNSYNCED carries
# the same "(!)" attention suffix the Stats column uses for archive errors.
_CLOCK_TOKENS = {
    ClockHealth.PPS: "clk PPS",
    ClockHealth.GNSS: "clk GNSS",
    ClockHealth.NTP: "clk NTP",
    ClockHealth.HOLDOVER: "clk hold (!)",
    ClockHealth.UNSYNCED: "clk none (!)",
}

# One honest sentence per verdict for the tooltip (M6): what the clock
# discipline means for the recorded timestamps.
_CLOCK_DETAILS = {
    ClockHealth.PPS: "GNSS-disciplined, PPS locked — sample-accurate timestamps",
    ClockHealth.GNSS: "GNSS time, PPS not locked — second-accurate timestamps",
    ClockHealth.NTP: "NTP-synchronized (no GNSS) — network-accuracy timestamps",
    ClockHealth.HOLDOVER: (
        "previously synchronized, all time sources lost — clock in holdover, drifting"
    ),
    ClockHealth.UNSYNCED: "NOT SYNCHRONIZED — timestamps unreliable",
}


def _format_echos_text(snapshot: EchosDeviceSnapshot) -> str:
    """Compose the compact Echos column text for one device.

    ``cal <state>`` appears only while a calibration is in flight or
    after a failure — the attention states. ``idle`` is steady-state
    noise and ``done`` persists in device RAM until reboot, so it would
    sit in the column for days; both are visible in the tooltip instead.
    (The real firmware's in-sweep vocabulary is not pinned yet, so the
    filter is an exclusion list, not an allowlist.)

    The clock token (M6) is always present: on a seismic node the clock
    discipline is first-class health, not tooltip trivia.
    """
    gnss = f"GNSS {snapshot.gnss_satellites}sat" if snapshot.gnss_fix else "GNSS no fix"
    parts = [
        f"fw {snapshot.firmware_version}",
        f"up {_format_uptime(snapshot.uptime_s)}",
        f"{snapshot.clients_connected} cli",
        f"ring {snapshot.ring_used_pct:.0f}%",
        gnss,
        _CLOCK_TOKENS[snapshot.clock_health()],
    ]
    if snapshot.calibration_state not in ("idle", "done"):
        parts.append(f"cal {snapshot.calibration_state}")
    return " · ".join(parts)


def _format_echos_tooltip(snapshot: EchosDeviceSnapshot) -> str:
    gnss_line = (
        f"GNSS: fix, {snapshot.gnss_satellites} satellites"
        if snapshot.gnss_fix
        else "GNSS: no fix"
    )
    pps = "locked" if snapshot.pps_locked else "not locked"
    clock_line = f"Clock: {_CLOCK_DETAILS[snapshot.clock_health()]}"
    if snapshot.time_sync_type:
        # Free-form firmware string ("RMC+PPS+NTP") — shown verbatim.
        clock_line += f" · sync {snapshot.time_sync_type}"
    if snapshot.pps_locked:
        clock_line += f" · PPS offset {snapshot.pps_offset_us} µs"
    return (
        f"Firmware {snapshot.firmware_version} · up {_format_uptime(snapshot.uptime_s)}\n"
        f"{gnss_line} · PPS {pps}\n"
        f"{clock_line}\n"
        f"SeedLink clients: {snapshot.clients_connected} · "
        f"ring {snapshot.ring_used_pct:.1f}% used\n"
        f"Calibration: {snapshot.calibration_state}"
    )


def _format_stats_text(status: DeviceStatus) -> str:
    """Compose the ``Stats`` column text for one device.

    The base form is ``"<pkts> pkts / <bytes>"`` (M2). When the device
    is configured with archive enabled, an inline second segment shows
    the archive footprint and file count; an active error gets a
    ``(!)`` suffix so the user can spot it in the tree at a glance.
    """
    base = f"{_format_count(status.packets_received)} pkts / {_format_bytes(status.bytes_received)}"
    if status.archive_enabled or status.archive_bytes_written > 0 or status.archive_files_open > 0:
        base = (
            f"{base} · arch {_format_bytes(status.archive_bytes_written)} "
            f"· {status.archive_files_open} files"
        )
        if status.archive_gaps_total > 0:
            base += f" · {status.archive_gaps_total} gaps"
        if status.archive_last_error:
            base += " (!)"
    return base


class DevicePanel(QDockWidget):
    """Dockable tree of devices and their per-NSLC child rows.

    Slots:
        on_device_state(name, state_int) — wires to engine.deviceStateChanged.
        on_new_stream(device_name, nslc) — wires to engine.newStreamSeen.
    Stats refresh is driven internally once a status provider is wired
    via ``set_status_provider``.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Devices", parent)
        self.setObjectName("Dock_Devices")
        self.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)

        # Container widget so we can stack a toolbar above the tree
        # without losing the dock's resize behaviour. ``QDockWidget``
        # only accepts a single child, so we wrap the toolbar + body
        # in a :class:`QWidget` and set THAT as the dock's widget.
        self._body = QWidget(self)
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        self._toolbar = QToolBar("Device actions", self._body)
        # ``setMovable(False)`` keeps the toolbar pinned above the tree
        # — letting the user drag it out of the dock makes for a poor
        # UX since the actions are scoped to the tree's selection.
        self._toolbar.setMovable(False)
        self._action_add = QAction(_ACTION_ADD, self)
        self._action_edit = QAction(_ACTION_EDIT, self)
        self._action_remove = QAction(_ACTION_REMOVE, self)
        self._action_reconnect = QAction(_ACTION_RECONNECT, self)
        for action in (
            self._action_add,
            self._action_edit,
            self._action_remove,
            self._action_reconnect,
        ):
            self._toolbar.addAction(action)
        # Add is unconditionally enabled — the user always has the
        # option to add a device. The other three are gated on the
        # tree's selection state.
        self._action_edit.setEnabled(False)
        self._action_remove.setEnabled(False)
        self._action_reconnect.setEnabled(False)
        body_layout.addWidget(self._toolbar)

        # Stack: page 0 is the tree, page 1 is the empty-state body.
        self._stack = QStackedWidget(self._body)

        self._tree = QTreeWidget(self._stack)
        self._tree.setColumnCount(6)
        self._tree.setHeaderLabels(["Stream", "Acq", "State", "Diagnostics", "Stats", "Echos"])
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header = self._tree.header()
        # BUG 1 fix: column 0 holds the stream identity (device name on the
        # top-level row, NSLC on the child rows) and MUST stay readable. It
        # used to be ``Stretch`` while State/Diagnostics/Stats were
        # ``ResizeToContents``; the wide Stats text ("<pkts> / <bytes> · arch
        # …") then starved column 0 down to ~38px when the dock was narrow,
        # clipping the NSLC to nothing so the child rows looked absent. Make
        # the Stream column fit its content (so the NSLC is always visible,
        # leftmost) and let the expendable Stats column be the elastic one
        # that absorbs/yields width instead.
        header.setSectionResizeMode(_COL_NAME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_ACQ, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_STATE, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_DIAG, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_COL_STATS, QHeaderView.ResizeMode.Stretch)
        # Echos status is compact, fixed-shape text; sizing to contents
        # keeps the elastic Stats column as the only width absorber.
        header.setSectionResizeMode(_COL_ECHOS, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        self._stack.addWidget(self._tree)

        self._empty_state = self._build_empty_state()
        self._stack.addWidget(self._empty_state)

        body_layout.addWidget(self._stack, 1)
        self.setWidget(self._body)

        self._device_items: dict[str, QTreeWidgetItem] = {}
        self._stream_items: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._status_provider: Callable[[], dict[str, DeviceStatus]] | None = None
        # Per-device cache of (host, port, connect_timeout_s) used to render
        # the diagnostics tooltip body. Pushed in by ``main_window`` after
        # the panel is built; falls back to ``_DEFAULT_CONNECT_TIMEOUT_S``
        # for any device the panel sees a status for before the cache is
        # populated.
        self._device_endpoints: dict[str, tuple[str, int]] = {}
        self._connect_timeouts: dict[str, float] = {}
        # Stage-B injections: optional ConfigStore (toolbar mutations)
        # and engine (Reconnect-now). ``None`` keeps the panel useable
        # in test contexts that bypass the dialogs entirely.
        self._store: ConfigStore | None = None
        self._engine: StreamingEngine | None = None
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(_STATS_REFRESH_MS)
        self._stats_timer.timeout.connect(self._refresh_stats)

        # Wiring -----------------------------------------------------
        self._action_add.triggered.connect(self._on_add_clicked)
        self._action_edit.triggered.connect(self._on_edit_clicked)
        self._action_remove.triggered.connect(self._on_remove_clicked)
        self._action_reconnect.triggered.connect(self._on_reconnect_clicked)
        self._tree.itemSelectionChanged.connect(self._update_action_enabled)
        self._tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self._tree.itemDoubleClicked.connect(self._on_tree_double_clicked)

        # Empty-state default: visible until `set_config_store` is
        # called. Tests that don't wire a store stay on page 0 (the
        # tree) so the existing `on_device_state` / `on_new_stream`
        # paths still populate the right widget.
        self._stack.setCurrentIndex(_PAGE_TREE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @property
    def add_action(self) -> QAction:
        """The toolbar's Add-device QAction.

        Returns the *live* QAction object — not a copy. External code
        that mutates this action (text, icon, ``triggered`` connections,
        enabled-state) sees those mutations on the toolbar surface and
        on any other widget that also embeds this QAction. Used by
        MainWindow's File menu to reuse the same handler logic without
        duplicating it.
        """
        return self._action_add

    @property
    def edit_action(self) -> QAction:
        """The toolbar's Edit-device QAction (selection-gated).

        Returns the live QAction; see :attr:`add_action` for caveats.
        """
        return self._action_edit

    @property
    def remove_action(self) -> QAction:
        """The toolbar's Remove-device QAction (selection-gated).

        Returns the live QAction; see :attr:`add_action` for caveats.
        """
        return self._action_remove

    def set_status_provider(
        self,
        provider: Callable[[], dict[str, DeviceStatus]],
    ) -> None:
        """Inject a callable that returns a fresh ``DeviceStatus`` snapshot
        per device. Starts the 1 Hz refresh timer the first time it's set.
        Call again with the same callable to leave the timer running; pass
        a fresh closure to re-target a different engine instance."""
        self._status_provider = provider
        if not self._stats_timer.isActive():
            self._stats_timer.start()
            # One immediate tick so the counters appear within the
            # interval rather than waiting a full second.
            self._refresh_stats()

    def set_device_endpoints(self, endpoints: dict[str, tuple[str, int]]) -> None:
        """Push per-device ``(host, port)`` tuples for tooltip rendering.

        The diagnostics tooltip suggests ``nc -vz {host} {port}`` as a
        manual reproduction step, so the panel needs to know each
        device's endpoint. Wired from ``main_window`` after the engine
        config is loaded — the panel has no other source of truth.
        """
        self._device_endpoints = dict(endpoints)

    def set_connect_timeouts(self, timeouts: dict[str, float]) -> None:
        """Push per-device ``connect_timeout_s`` values for the tooltip
        threshold (``CONNECTING`` longer than ``2 * connect_timeout`` is
        when the tooltip should appear, since the worker should have
        either succeeded or moved to ``WAITING_RETRY`` by then)."""
        self._connect_timeouts = dict(timeouts)

    def set_config_store(self, store: ConfigStore) -> None:
        """Inject the :class:`ConfigStore` used by the toolbar's mutations.

        Without a store, the Add/Edit/Remove actions raise on click —
        injecting it here keeps the panel ergonomic in tests that don't
        exercise mutations. The panel re-renders the tree+empty-state
        whenever the store reports a change, so call this AFTER the
        engine wiring so a queued ``configChanged`` cannot fire onto
        an un-rendered tree.
        """
        self._store = store
        self._refresh_from_store()
        # ``configChanged`` is the canonical "something changed" signal.
        # We intentionally connect via the default (Auto) connection
        # type — the ConfigStore lives on whichever thread the GUI does
        # in this app, so Auto resolves to Direct. If a future change
        # moves the store to a worker thread, this will need an
        # explicit Queued.
        store.configChanged.connect(self._refresh_from_store)

    def set_engine_for_actions(self, engine: StreamingEngine) -> None:
        """Inject the :class:`StreamingEngine` for the Reconnect-now action.

        The panel calls :meth:`engine.reconnect_device` directly when
        the user clicks Reconnect now. Without an engine the action
        stays disabled; once injected, enablement tracks the selected
        device's state (anything other than CONNECTED).
        """
        self._engine = engine
        # ``devicesChanged`` fires after each ConfigStore-driven diff
        # the engine applies. The panel itself may have already updated
        # via the store's ``configChanged`` signal, but the engine
        # signal is what the test in test_main_window asserts on so
        # we wire both for symmetry.
        engine.devicesChanged.connect(self._refresh_from_store)
        self._update_action_enabled()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    @Slot(str, int)
    def on_acquisition_state(self, device_name: str, state: int) -> None:
        """Render the per-device acquisition badge (M2-C, rule 13).

        Wired QueuedConnection from ``engine.acquisitionStateChanged``;
        the 1 Hz stats refresh re-asserts the badge so a missed emit
        self-heals within a tick.
        """
        try:
            acq = AcquisitionState(state)
        except ValueError:
            return
        item = self._device_items.get(device_name)
        if item is None:
            # Do NOT create a row here. The engine's removal path emits
            # a queued IDLE for the dying device BEFORE devicesChanged
            # tears the row down via the direct _refresh_from_store —
            # FIFO inversion means this slot can run after the removal,
            # and resurrecting the row would leave a permanent phantom
            # (POSTMORTEMS 2026-06-01 class: a queued meta-call posted
            # before teardown still dispatches after it). A device the
            # panel doesn't know yet gets its badge on the next
            # on_device_state / stats tick instead.
            return
        self._set_acq_badge(item, acq)

    @Slot(str, int)
    def on_device_state(self, device_name: str, state: int) -> None:
        item = self._device_items.get(device_name)
        if item is None:
            item = self._add_device_row(device_name)
        try:
            label = ConnState(state).name
        except ValueError:
            label = str(state)
        item.setText(_COL_STATE, label)
        color_hex = _STATE_COLORS.get(state, "#888888")
        item.setForeground(_COL_STATE, QBrush(QColor(color_hex)))

    @Slot(object)
    def on_echos_snapshot(self, snapshot: object) -> None:
        """Render one Echos status poll into the Echos column (M1-C).

        Wired QueuedConnection from ``EchosStatusWorker.snapshotReady``;
        the payload is type-erased through ``Signal(object)`` so it gets
        the standard isinstance guard (rule 4).
        """
        if not isinstance(snapshot, EchosDeviceSnapshot):
            return
        item = self._device_items.get(snapshot.device)
        if item is None:
            # Stale cross-thread delivery: rows are pre-created from the
            # store, so an unknown device here means it was just REMOVED
            # while a poll was in flight. Creating a row would resurrect
            # it as a ghost (M1-C review finding) — drop the payload.
            return
        item.setText(_COL_ECHOS, _format_echos_text(snapshot))
        item.setForeground(_COL_ECHOS, QBrush(self.palette().text().color()))
        item.setToolTip(_COL_ECHOS, _format_echos_tooltip(snapshot))

    @Slot(str, str, str)
    def on_echos_poll_failed(self, device_name: str, kind: str, message: str) -> None:
        """Mark the Echos column stale after a failed poll.

        ``kind`` is the closed ``EchosErrorKind`` set from the REST
        client; the column shows it compactly and the tooltip carries
        the full message. The last good snapshot text is replaced —
        showing stale numbers as if they were live would be worse than
        showing "unreachable".
        """
        item = self._device_items.get(device_name)
        if item is None:
            # Same stale-delivery guard as ``on_echos_snapshot``.
            return
        item.setText(_COL_ECHOS, f"({kind})")
        item.setForeground(_COL_ECHOS, QBrush(QColor(_ECHOS_FAIL_COLOR)))
        item.setToolTip(_COL_ECHOS, f"Echos status poll failed: {message}")

    @Slot(str, str)
    def on_new_stream(self, device_name: str, nslc: str) -> None:
        device_item = self._device_items.get(device_name)
        if device_item is None:
            device_item = self._add_device_row(device_name)
        key = (device_name, nslc)
        if key in self._stream_items:
            return
        child = QTreeWidgetItem(device_item, [nslc, "", "", "", ""])
        device_item.addChild(child)
        device_item.setExpanded(True)
        self._stream_items[key] = child

    @Slot(str)
    def select_device(self, device_name: str) -> None:
        """Select + reveal a device row (Map tab marker click, M4-B).

        Unknown names are a no-op — a marker click can race a config
        removal, and resurrecting a row here would be the same ghost
        class the echos-snapshot guard exists for.
        """
        item = self._device_items.get(device_name)
        if item is None:
            return
        self._tree.setCurrentItem(item)
        self._tree.scrollToItem(item)
        self.raise_()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _add_device_row(self, device_name: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(
            self._tree,
            [device_name, _ACQ_BADGES[AcquisitionState.IDLE], ConnState.DISCONNECTED.name, "", "", ""],
        )
        item.setForeground(_COL_STATE, QBrush(QColor(_STATE_COLORS[int(ConnState.DISCONNECTED)])))
        self._tree.addTopLevelItem(item)
        self._device_items[device_name] = item
        if self._engine is not None:
            self._set_acq_badge(item, self._engine.acquisition_state(device_name))
        return item

    def _set_acq_badge(self, item: QTreeWidgetItem, state: AcquisitionState) -> None:
        item.setText(_COL_ACQ, _ACQ_BADGES[state])
        if state is AcquisitionState.RECORDING:
            item.setForeground(_COL_ACQ, QBrush(QColor(_ACQ_REC_COLOR)))
        elif state is AcquisitionState.MONITORING:
            item.setForeground(_COL_ACQ, QBrush(self.palette().text().color()))
        else:
            item.setForeground(_COL_ACQ, QBrush(QColor(_ACQ_IDLE_COLOR)))

    @Slot()
    def _refresh_stats(self) -> None:
        if self._status_provider is None:
            return
        statuses = self._status_provider()
        for name, status in statuses.items():
            item = self._device_items.get(name)
            if item is None:
                # The status provider sees a device the panel hasn't been
                # told about yet — common during startup. Add a row so
                # stats don't disappear; state will catch up on the next
                # ``on_device_state`` emission.
                item = self._add_device_row(name)
            item.setText(_COL_STATS, _format_stats_text(status))
            item.setText(_COL_DIAG, self._format_diagnostics(status))
            tooltip = self._format_tooltip(status)
            # Apply the same tooltip to the Stream and Diagnostics
            # columns so the user picks it up wherever they hover. An
            # empty string clears the tooltip on healthy rows.
            item.setToolTip(_COL_NAME, tooltip)
            item.setToolTip(_COL_DIAG, tooltip)
            self._apply_misconfig_suffix(item, status)
            if self._engine is not None:
                self._set_acq_badge(item, self._engine.acquisition_state(name))

    def _apply_misconfig_suffix(self, item: QTreeWidgetItem, status: DeviceStatus) -> None:
        """Append/strip a ``(!)`` suffix on the state badge for misconfig states.

        WAITING_RETRY plus a misconfiguration kind (currently only
        ``protocol_rejected``) is qualitatively different from
        WAITING_RETRY caused by a transient outage: no amount of
        retrying will help. The suffix tells the user at a glance to
        stop waiting and open the Stations browser.

        Idempotent: refreshing the suffix every tick is cheap and
        survives any out-of-band ``on_device_state`` call that might
        have rewritten the badge in between.
        """
        label = item.text(_COL_STATE)
        misconfig = (
            status.state == ConnState.WAITING_RETRY
            and status.last_failure_kind == "protocol_rejected"
        )
        if misconfig:
            if not label.endswith(_MISCONFIG_BADGE_SUFFIX):
                item.setText(_COL_STATE, f"{label}{_MISCONFIG_BADGE_SUFFIX}")
        else:
            if label.endswith(_MISCONFIG_BADGE_SUFFIX):
                item.setText(_COL_STATE, label[: -len(_MISCONFIG_BADGE_SUFFIX)])

    def _format_diagnostics(self, status: DeviceStatus) -> str:
        """Compact one-liner for the Diagnostics column.

        Returns the empty string for healthy / idle states so the column
        stays uncluttered for the common case. Populates only while the
        worker is actively struggling — i.e. ``CONNECTING`` after a
        prior failure or in ``WAITING_RETRY``. The "next: Ns" component
        counts down each tick because ``next_attempt_at`` is an absolute
        timestamp and we recompute the delta on every refresh.

        ``protocol_rejected`` is shown with a different shape — the
        attempt counter is unhelpful (every attempt rejects) but the
        rejected-selector count IS actionable, so it takes the slot.
        """
        if status.attempt_count <= 0:
            return ""
        if status.state not in (ConnState.CONNECTING, ConnState.WAITING_RETRY):
            return ""
        next_part = "?"
        if status.next_attempt_at is not None:
            try:
                delta = float(status.next_attempt_at - UTCDateTime())
            except Exception:
                delta = 0.0
            next_part = f"{max(0, round(delta))}s"
        if status.last_failure_kind == "protocol_rejected":
            n = "?"
            if status.last_failure_detail:
                sels = status.last_failure_detail.get("rejected_selectors")
                if isinstance(sels, list):
                    n = str(len(sels))
            return f"rejected: {n} selectors · next: {next_part}"
        kind = status.last_failure_kind or "—"
        # Fallback for the legacy "—" placeholder when next_attempt_at
        # is missing — preserves the prior column shape that some tests
        # assert on directly.
        if status.next_attempt_at is None:
            next_part = "—"
        return f"attempt {status.attempt_count} · last fail: {kind} · next: {next_part}"

    def _format_tooltip(self, status: DeviceStatus) -> str:
        """Multi-line tooltip body when the row warrants attention.

        Triggers on:

        - ``WAITING_RETRY`` — explicitly told "we are sleeping until next try"
        - ``CONNECTING`` longer than ``2 * connect_timeout_s`` — the worker
          *should* have already moved to WAITING_RETRY by then; if not,
          something is sufficiently wrong that the user benefits from the
          manual-test hint anyway.

        Includes a copy-pasteable ``nc -vz`` reproduction command so the
        operator can confirm whether the issue is local network reach
        vs. the dashboard.
        """
        if status.attempt_count <= 0:
            return ""
        timeout_s = self._connect_timeouts.get(status.name, _DEFAULT_CONNECT_TIMEOUT_S)
        show = False
        if status.state == ConnState.WAITING_RETRY:
            show = True
        elif status.state == ConnState.CONNECTING and status.since_first_attempt_at is not None:
            try:
                elapsed = float(UTCDateTime() - status.since_first_attempt_at)
            except Exception:
                elapsed = 0.0
            if elapsed > 2.0 * timeout_s:
                show = True
        if not show:
            return ""
        kind = status.last_failure_kind or "unknown"
        humanized = _FAILURE_KIND_HUMANIZED.get(kind, "unknown error")
        if kind == "timeout":
            humanized = f"{humanized} after {timeout_s:.1f}s"
        endpoint = self._device_endpoints.get(status.name)
        host_port = "<host> <port>" if endpoint is None else f"{endpoint[0]} {endpoint[1]}"
        if kind == "protocol_rejected":
            # Pivot from the network-diagnosis hint (nc -vz) to the
            # workflow hint that actually fixes the problem: the
            # Stations browser. The rejected-selector count, when
            # available, makes the message specific.
            n_part = ""
            if status.last_failure_detail:
                sels = status.last_failure_detail.get("rejected_selectors")
                if isinstance(sels, list):
                    n_part = f" ({len(sels)} selector{'s' if len(sels) != 1 else ''})"
            return (
                f"Server rejected the requested stations{n_part}.\n"
                f"Last: {humanized}.\n"
                "Try: open the Stations browser, pick this device, hit Refresh,\n"
                "and subscribe to a station that exists on this server."
            )
        return (
            f"Connection failed {status.attempt_count} times.\n"
            f"Last: {humanized}.\n"
            f"Try: nc -vz {host_port} from your terminal to verify reachability."
        )

    # ------------------------------------------------------------------
    # Stage-B toolbar / empty-state internals
    # ------------------------------------------------------------------
    def _build_empty_state(self) -> QWidget:
        """Construct the centred "No devices configured." panel.

        The inline button mirrors the toolbar's Add action so the user
        can add a device without first locating the toolbar — handy on
        a fresh install where the dock is empty.
        """
        wrapper = QWidget(self._stack)
        layout = QVBoxLayout(wrapper)
        layout.addStretch(1)
        label = QLabel("No devices configured.", wrapper)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("QLabel { color: #888; font-style: italic; }")
        layout.addWidget(label)
        button = QPushButton("Add device...", wrapper)
        button.setMaximumWidth(200)
        button_row = QVBoxLayout()
        button_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        button_row.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addLayout(button_row)
        layout.addStretch(1)
        # Reuse the same slot the toolbar's Add action triggers so
        # there is exactly one code path that opens the dialog.
        button.clicked.connect(self._on_add_clicked)
        return wrapper

    def _refresh_from_store(self) -> None:
        """Re-sync the tree contents and visibility against the store.

        Called on store mutations (``configChanged``) and engine
        ``devicesChanged``. Adds rows for new devices, removes rows
        for devices no longer in the store, and swaps the stack page
        between tree and empty-state based on the device count.
        """
        if self._store is None:
            return
        names_in_store = {d.name for d in self._store.root.devices}
        # Remove rows for devices that the store no longer contains.
        for name in list(self._device_items.keys()):
            if name not in names_in_store:
                item = self._device_items.pop(name)
                idx = self._tree.indexOfTopLevelItem(item)
                if idx >= 0:
                    self._tree.takeTopLevelItem(idx)
                # Drop child stream entries that pointed at this row.
                for key in [k for k in self._stream_items if k[0] == name]:
                    self._stream_items.pop(key, None)
        # Add rows for newly-known devices so they appear in the tree
        # even before the engine has dispatched a state for them.
        for name in names_in_store:
            if name not in self._device_items:
                self._add_device_row(name)
        # Swap the stack page based on whether the store has any
        # devices at all. Use the store as the source of truth — the
        # tree's count can lag a queued ``devicesChanged`` emit.
        page = _PAGE_TREE if names_in_store else _PAGE_EMPTY
        self._stack.setCurrentIndex(page)
        self._update_action_enabled()

    def _selected_device_name(self) -> str | None:
        """Return the device name for the single selected top-level row.

        Returns ``None`` when no row is selected, when a child (stream)
        row is selected, or when more than one top-level row is
        selected. The toolbar's Edit/Remove/Reconnect actions all act
        on a single device, so multi-select stays disabled rather than
        ambiguously acting on the first item.
        """
        items = self._tree.selectedItems()
        if len(items) != 1:
            return None
        item = items[0]
        # Top-level only: child rows represent NSLC streams, not devices.
        if item.parent() is not None:
            return None
        # Reverse-lookup the device name from the items dict so we
        # don't have to trust the column-0 text (which is set during
        # row construction but could in principle be edited).
        for name, mapped in self._device_items.items():
            if mapped is item:
                return name
        return None

    @Slot()
    def _update_action_enabled(self) -> None:
        """Recompute the toolbar action enable states.

        Add: always enabled (when a store is wired).
        Edit / Remove: exactly one device selected.
        Reconnect: exactly one device selected AND its state is not
        CONNECTED — reconnecting an already-connected device is a no-op
        with confusing semantics.
        """
        has_store = self._store is not None
        self._action_add.setEnabled(has_store)
        name = self._selected_device_name()
        single = name is not None and has_store
        self._action_edit.setEnabled(single)
        self._action_remove.setEnabled(single)
        # Reconnect needs the engine AND a non-CONNECTED state.
        reconnect_enabled = False
        if single and self._engine is not None:
            statuses = self._engine.device_status()
            status = statuses.get(name) if name is not None else None
            if status is None or status.state != ConnState.CONNECTED:
                reconnect_enabled = True
        self._action_reconnect.setEnabled(reconnect_enabled)

    @Slot()
    def _on_add_clicked(self) -> None:
        if self._store is None:
            _log.debug("device_panel_add_no_store")
            return
        # Local import to avoid circulating ``DeviceDialog → DevicePanel``
        # at module load. The dialog imports nothing from this module
        # and the panel only needs the dialog at click time.
        from echosmonitor.gui.dialogs.device_dialog import DeviceDialog

        DeviceDialog.add(self, self._store, engine=self._engine)

    @Slot()
    def _on_edit_clicked(self) -> None:
        if self._store is None:
            return
        name = self._selected_device_name()
        if name is None:
            return
        from echosmonitor.gui.dialogs.device_dialog import DeviceDialog

        try:
            DeviceDialog.edit(self, self._store, name, engine=self._engine)
        except ConfigError as exc:
            QMessageBox.warning(self, "Edit failed", str(exc))

    @Slot(object, int)
    def _on_tree_double_clicked(self, item: object, _column: int) -> None:
        """Double-click on a device row opens the Edit-device dialog.

        Stream rows are a deliberate no-op: DSP chains are per-device in
        the schema, so opening the chain editor scoped to a single
        NSLC would mislead users into thinking per-stream chains are a
        thing. A future "per-channel chain" feature can revisit this.
        """
        if self._store is None:
            return
        # Top-level items are devices; child items are stream rows.
        # ``QTreeWidgetItem.parent()`` is ``None`` for top-level rows
        # which is the device case we want.
        if not isinstance(item, QTreeWidgetItem):
            return
        if item.parent() is not None:
            return  # stream row: no-op
        name = item.text(0).strip()
        if not name:
            return
        from echosmonitor.gui.dialogs.device_dialog import DeviceDialog

        try:
            DeviceDialog.edit(self, self._store, name, engine=self._engine)
        except ConfigError as exc:
            QMessageBox.warning(self, "Edit failed", str(exc))

    @Slot()
    def _on_remove_clicked(self) -> None:
        if self._store is None:
            return
        name = self._selected_device_name()
        if name is None:
            return
        from echosmonitor.gui.dialogs.device_dialog import ConfirmRemoveDialog

        confirm = ConfirmRemoveDialog(name, self)
        if confirm.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._store.remove_device(name)
        except ConfigError as exc:
            QMessageBox.warning(self, "Remove failed", str(exc))
            _log.warning("device_panel_remove_failed", device=name, error=str(exc))

    @Slot()
    def _on_reconnect_clicked(self) -> None:
        if self._engine is None:
            return
        name = self._selected_device_name()
        if name is None:
            return
        try:
            self._engine.reconnect_device(name)
        except KeyError as exc:
            QMessageBox.warning(self, "Reconnect failed", str(exc))
            _log.warning("device_panel_reconnect_failed", device=name, error=str(exc))

    @Slot(QPoint)
    def _on_tree_context_menu(self, pos: QPoint) -> None:
        """Right-click context menu mirrors the toolbar actions.

        We don't mutate selection on right-click — Qt already promotes
        the right-clicked row to the current item via the standard
        item-view machinery, so the toolbar's selection-driven enable
        states are already correct by the time the menu opens.
        """
        if self._store is None:
            return
        item = self._tree.itemAt(pos)
        # Right-click on empty space or a stream row only offers the
        # Add action; right-click on a device row offers all four.
        is_device_row = item is not None and item.parent() is None
        menu = QMenu(self._tree)
        menu.addAction(self._action_add)
        if is_device_row:
            menu.addSeparator()
            menu.addAction(self._action_edit)
            menu.addAction(self._action_remove)
            menu.addAction(self._action_reconnect)
        viewport = self._tree.viewport()
        if viewport is None:
            return
        menu.exec(viewport.mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Test-only accessors
    # ------------------------------------------------------------------
    def _device_count_for_test(self) -> int:
        return len(self._device_items)

    def _stream_count_for_test(self, device_name: str) -> int:
        return sum(1 for d, _ in self._stream_items if d == device_name)

    def _stats_text_for_test(self, device_name: str) -> str:
        item = self._device_items.get(device_name)
        if item is None:
            return ""
        return item.text(_COL_STATS)

    def _diagnostics_text_for_test(self, device_name: str) -> str:
        item = self._device_items.get(device_name)
        if item is None:
            return ""
        return item.text(_COL_DIAG)

    def _tooltip_text_for_test(self, device_name: str) -> str:
        item = self._device_items.get(device_name)
        if item is None:
            return ""
        return item.toolTip(_COL_NAME)

    def _state_label_for_test(self, device_name: str) -> str:
        item = self._device_items.get(device_name)
        if item is None:
            return ""
        return item.text(_COL_STATE)

    def _stats_timer_active_for_test(self) -> bool:
        return self._stats_timer.isActive()

    def _echos_text_for_test(self, device_name: str) -> str:
        item = self._device_items.get(device_name)
        if item is None:
            return ""
        return item.text(_COL_ECHOS)

    def _echos_tooltip_for_test(self, device_name: str) -> str:
        item = self._device_items.get(device_name)
        if item is None:
            return ""
        return item.toolTip(_COL_ECHOS)
