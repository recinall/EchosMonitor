"""Tabbed live-view facade — an "All" overview plus one tab per device.

``LiveTabs`` is the single widget MainWindow routes all live data through.
It owns a ``QTabWidget`` with:

* **Tab 0 "All"** — a global :class:`LiveStack` honouring
  ``cfg.ui.max_visible_plots``. Preserves the pre-M7 overview behaviour
  exactly (this is the same ``LiveStack`` instance MainWindow keeps on
  ``self._live_stack``).
* **Tabs 1..N (per device)** — a :class:`LiveStack` scoped to one device
  with a per-device cap of :data:`_DEVICE_TAB_MAX_VISIBLE` (8), wrapped in
  a :class:`_DeviceTab` container that also carries a per-stream chips
  toolbar. Each tab is labelled with the device name and a small state
  dot (●) coloured by :class:`ConnState`.

Per-DEVICE vs per-STREAM distinction (intentional, not an inconsistency):

* The **Live** panel (this widget) is **per-DEVICE** — each tab shows
  *all* of one device's streams stacked together, for at-a-glance
  multi-channel monitoring.
* The **Spectrogram** dock
  (:class:`~echosmonitor.gui.widgets.spectrogram_dock.SpectrogramDock`)
  is **per-STREAM** — one full-size waterfall per tab, for detailed
  single-channel frequency inspection.

These two tab granularities serve different workflows and are kept
deliberately distinct.

Performance (M7 Stage B3): only the *visible* tab renders at full rate.
On ``currentChanged`` the now-visible tab's ``LiveStack`` is set
render-active and every other tab is set render-inactive. Hidden tabs
keep rolling their buffers (cheap, O(buffer)) but skip the costly
``setData`` / ``setImage`` calls, so switching tabs is instant and shows
recent data immediately. This is a GUI render-rate control ONLY: the
engine's ring buffers and storage writer queues are never touched
(CLAUDE.md rule 8).

Threading: all public methods MUST be called from the GUI thread; the
engine signals MainWindow routes here are already queued at the engine
boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.models import ConnState
from echosmonitor.gui.widgets.live_stack import _STATE_COLORS, LiveStack
from echosmonitor.gui.widgets.trace_plot import TracePlot

if TYPE_CHECKING:
    from echosmonitor.config import RootConfig

_log = structlog.get_logger(__name__)

# Per-device tabs cap each device at this many simultaneously visible
# stream plots, independent of the global ``cfg.ui.max_visible_plots``
# cap that still governs the "All" overview tab.
_DEVICE_TAB_MAX_VISIBLE = 8

# Index of the always-present "All" overview tab.
_ALL_TAB_INDEX = 0
_ALL_TAB_LABEL = "All"

# Diameter (device-independent px) of the per-tab connection-state dot.
_STATE_DOT_PX = 10

# QSettings keys. ``LiveActiveTab`` stores the active tab BY DEVICE NAME
# (the empty string means the "All" tab) because tab *indices* shift as
# devices come and go. ``LiveStreamVisible/<device>/<nslc>`` stores each
# chip's checked state.
_ACTIVE_TAB_KEY = "LiveActiveTab"
_STREAM_VISIBLE_GROUP = "LiveStreamVisible"

_CHIP_STYLE = (
    "QToolButton#LiveStreamChip { font-size: 10px; padding: 1px 6px; }"
    " QToolButton#LiveStreamChip:checked { font-weight: bold; }"
)
_OFFLINE_SUFFIX = " (offline)"


def _state_dot_icon(state_int: int) -> QIcon:
    """Render a small filled circle coloured by ``state_int``.

    A disconnected / stopped device reads as grey; connected as green,
    etc. Mirrors :data:`live_stack._STATE_COLORS` so the dot matches the
    in-stack badge.
    """
    color = _STATE_COLORS.get(state_int, "#888888")
    pix = QPixmap(_STATE_DOT_PX, _STATE_DOT_PX)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(0, 0, _STATE_DOT_PX, _STATE_DOT_PX)
    painter.end()
    return QIcon(pix)


class _DeviceTab(QWidget):
    """One device tab: a chips toolbar above a device-scoped LiveStack.

    The chips toolbar is a row of small checkable :class:`QToolButton`
    chips — one per NSLC of this device — letting the user hide / show
    individual streams within this tab only. Chip toggles route to the
    contained :class:`LiveStack` via
    :meth:`LiveStack.set_stream_user_visible`; they never affect any
    other tab.
    """

    def __init__(
        self,
        device_name: str,
        stack: LiveStack,
        settings_provider: Callable[[], QSettings],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._device_name = device_name
        self._stack = stack
        self._settings_provider = settings_provider
        self._chips: dict[str, QToolButton] = {}

        self._toolbar = QWidget(self)
        self._toolbar.setObjectName("LiveStreamChips")
        self._toolbar_layout = QHBoxLayout(self._toolbar)
        self._toolbar_layout.setContentsMargins(6, 2, 6, 2)
        self._toolbar_layout.setSpacing(4)
        self._toolbar_layout.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._stack, stretch=1)

    @property
    def stack(self) -> LiveStack:
        return self._stack

    def add_stream_chip(self, nslc: str) -> None:
        """Add a chip for ``nslc`` if absent; restore its persisted state."""
        if nslc in self._chips:
            return
        chip = QToolButton(self._toolbar)
        chip.setObjectName("LiveStreamChip")
        chip.setStyleSheet(_CHIP_STYLE)
        chip.setText(nslc)
        chip.setCheckable(True)
        chip.setToolTip(f"Show/hide {nslc} in this device tab only.")

        visible = self._persisted_visible(nslc)
        chip.setChecked(visible)
        # Apply the persisted choice to the stack before wiring the signal
        # so restoring a hidden chip does not require a user click.
        if not visible:
            self._stack.set_stream_user_visible(self._device_name, nslc, False)
        chip.toggled.connect(lambda checked, n=nslc: self._on_chip_toggled(n, checked))

        # Insert before the trailing stretch so chips pack left.
        self._toolbar_layout.insertWidget(self._toolbar_layout.count() - 1, chip)
        self._chips[nslc] = chip

    def _on_chip_toggled(self, nslc: str, checked: bool) -> None:
        self._stack.set_stream_user_visible(self._device_name, nslc, checked)
        settings = self._settings_provider()
        settings.setValue(self._chip_key(nslc), checked)
        _log.info(
            "live_stream_chip_toggled",
            device=self._device_name,
            nslc=nslc,
            visible=checked,
        )

    def _persisted_visible(self, nslc: str) -> bool:
        stored = self._settings_provider().value(self._chip_key(nslc))
        if isinstance(stored, bool):
            return stored
        if isinstance(stored, str):  # INI format round-trips as string
            return stored.lower() in ("1", "true", "yes")
        return True  # default: visible

    def _chip_key(self, nslc: str) -> str:
        return f"{_STREAM_VISIBLE_GROUP}/{self._device_name}/{nslc}"

    # Test-only accessors
    def _chip_for_test(self, nslc: str) -> QToolButton | None:
        return self._chips.get(nslc)


class LiveTabs(QTabWidget):
    """Facade tab-bar hosting the "All" overview + per-device live stacks.

    See the module docstring for the per-device / per-stream rationale
    and the tab-pause performance contract.

    Args:
        window_seconds: Visible window length passed to every LiveStack.
        cfg: Root configuration (drives the "All" tab's global cap and the
            device → has-chain mapping).
        settings_provider: Zero-arg callable returning the ``QSettings``
            to persist chip + active-tab state into. MainWindow passes
            ``self._settings`` so the test ``isolated_settings`` fixture
            (which monkeypatches ``MainWindow._settings``) transparently
            redirects this widget's persistence too.
        parent: Owning widget.
    """

    def __init__(
        self,
        window_seconds: float,
        cfg: RootConfig,
        settings_provider: Callable[[], QSettings],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._window_seconds = float(window_seconds)
        self._cfg = cfg
        self._settings_provider = settings_provider
        self.setUsesScrollButtons(True)
        self.setObjectName("LiveTabs")

        # Tab 0 "All" — the global overview. This is the LiveStack
        # MainWindow keeps as self._live_stack.
        self._all_stack = LiveStack(
            window_seconds=self._window_seconds,
            cfg=cfg,
            parent=self,
        )
        self._all_stack.setObjectName("AllLiveStack")
        self.addTab(self._all_stack, _ALL_TAB_LABEL)

        # Per-device tabs, keyed by device name.
        self._device_tabs: dict[str, _DeviceTab] = {}
        self._device_states: dict[str, int] = {}
        # Streams known per device (so chips can be (re)built lazily).
        self._device_streams: dict[str, list[str]] = {}

        # Persisted active-tab target (by device name). Device tabs are
        # created lazily on first stream, so at restore time the target
        # tab may not exist yet; we remember it and switch to it the
        # moment its tab is created. Empty string / None = the All tab.
        self._pending_active_device: str | None = None

        # Initial render state: only the All tab is active.
        self._all_stack.set_render_active(True)
        self.currentChanged.connect(self._on_current_changed)

    # ------------------------------------------------------------------
    # Routing facade — MainWindow handlers call these
    # ------------------------------------------------------------------
    def add_stream(self, device: str, nslc: str, fs: float) -> None:
        """Register a stream on the All tab AND the device tab.

        Creates the device tab on first sight of the device. Idempotent
        per ``(device, nslc)`` — LiveStack.add_stream returns the existing
        plot and the chip dedupes.
        """
        self._all_stack.add_stream(device, nslc, fs)
        tab = self._ensure_device_tab(device)
        tab.stack.add_stream(device, nslc, fs)
        tab.add_stream_chip(nslc)
        streams = self._device_streams.setdefault(device, [])
        if nslc not in streams:
            streams.append(nslc)

    def push_raw(self, device: str, nslc: str, samples: object) -> None:
        """Fan a raw packet out to the All-tab and device-tab plots.

        ``samples`` is a ``np.ndarray[float32]`` type-erased through the
        engine's ``Signal(object)``; ``TracePlot.push_raw`` re-narrows it.
        """
        self._fan_plots(device, nslc, lambda p: p.push_raw(samples))  # type: ignore[arg-type]

    def push_processed(self, device: str, nslc: str, samples: object) -> None:
        """Fan a processed packet out to the All-tab and device-tab plots."""
        self._fan_plots(device, nslc, lambda p: p.push_processed(samples))  # type: ignore[arg-type]

    def update_meta(self, device: str, nslc: str, fs: float, starttime_iso: str) -> None:
        for stack in self._stacks_for(device):
            plot = stack.plot_for(device, nslc)
            if plot is not None:
                plot.update_meta(nslc, fs, starttime_iso)
            spec = stack.spec_view_for(device, nslc)
            if spec is not None:
                spec.update_meta(fs=fs)

    def update_processed_meta(self, device: str, nslc: str, fs_out: float) -> None:
        for stack in self._stacks_for(device):
            plot = stack.plot_for(device, nslc)
            if plot is not None:
                plot.update_processed_meta(fs_out)
            stack.update_processed_meta(device, nslc, fs_out)

    def on_spectrogram_column(
        self,
        device: str,
        nslc: str,
        column: object,
        freqs: object,
        t_end: object,
    ) -> None:
        for stack in self._stacks_for(device):
            stack.on_spectrogram_column(device, nslc, column, freqs, t_end)

    def set_device_state(self, device: str, state_int: int) -> None:
        """Update both stacks' badges and the device-tab dot + label.

        Creates the device tab if a state arrives before any stream
        (during CONNECTING). A DISCONNECTED / STOPPED device keeps its tab
        but the dot greys out and the label gains an ``(offline)`` suffix.
        """
        self._all_stack.set_device_state(device, state_int)
        tab = self._ensure_device_tab(device)
        tab.stack.set_device_state(device, state_int)
        self._device_states[device] = state_int
        self._refresh_device_tab_label(device)

    def set_drop_count(self, device: str, nslc: str, count: int) -> None:
        for stack in self._stacks_for(device):
            stack.set_drop_count(device, nslc, count)

    def prune_to(self, device_names: set[str]) -> None:
        """Remove device tabs for devices no longer in the config.

        Called by MainWindow on ``configChanged`` / ``devicesChanged``.
        The "All" tab (index 0) is always retained. A disconnected device
        is NOT pruned here — only a device removed from config is.
        """
        for device in list(self._device_tabs):
            if device in device_names:
                continue
            tab = self._device_tabs.pop(device)
            self._device_streams.pop(device, None)
            self._device_states.pop(device, None)
            index = self.indexOf(tab)
            if index >= 0:
                self.removeTab(index)
            tab.deleteLater()
            _log.info("live_device_tab_pruned", device=device)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def save_active_tab(self) -> None:
        """Persist the active tab BY DEVICE NAME (empty = the All tab)."""
        settings = self._settings_provider()
        settings.setValue(_ACTIVE_TAB_KEY, self._active_device_name())

    def restore_active_tab(self) -> None:
        """Reopen the persisted active tab (by device name), else All.

        If the persisted device's tab does not exist yet (device tabs are
        created lazily on first stream), the target is remembered and
        applied the moment its tab is created. Falls back to the All tab.
        """
        stored = self._settings_provider().value(_ACTIVE_TAB_KEY)
        name = stored if isinstance(stored, str) else ""
        if name and name in self._device_tabs:
            index = self.indexOf(self._device_tabs[name])
            if index >= 0:
                self.setCurrentIndex(index)
                return
        if name:
            # Tab not built yet — switch to it on creation.
            self._pending_active_device = name
        self.setCurrentIndex(_ALL_TAB_INDEX)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _ensure_device_tab(self, device: str) -> _DeviceTab:
        tab = self._device_tabs.get(device)
        if tab is not None:
            return tab
        stack = LiveStack(
            window_seconds=self._window_seconds,
            cfg=self._cfg,
            parent=self,
            max_visible=_DEVICE_TAB_MAX_VISIBLE,
        )
        stack.setObjectName(f"DeviceLiveStack_{device}")
        # New device tab is not the visible one — start render-paused so a
        # background device does not redraw at full rate.
        stack.set_render_active(False)
        tab = _DeviceTab(device, stack, self._settings_provider, parent=self)
        tab.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._device_tabs[device] = tab
        index = self.addTab(tab, _state_dot_icon(int(ConnState.DISCONNECTED)), device)
        _log.info("live_device_tab_created", device=device)
        # Honour a pending restore target now that its tab exists.
        if self._pending_active_device == device:
            self._pending_active_device = None
            self.setCurrentIndex(index)
        return tab

    def _stacks_for(self, device: str) -> list[LiveStack]:
        stacks = [self._all_stack]
        tab = self._device_tabs.get(device)
        if tab is not None:
            stacks.append(tab.stack)
        return stacks

    def _fan_plots(self, device: str, nslc: str, action: Callable[[TracePlot], None]) -> None:
        for stack in self._stacks_for(device):
            plot = stack.plot_for(device, nslc)
            if plot is not None:
                action(plot)

    def _refresh_device_tab_label(self, device: str) -> None:
        tab = self._device_tabs.get(device)
        if tab is None:
            return
        index = self.indexOf(tab)
        if index < 0:
            return
        state = self._device_states.get(device, int(ConnState.DISCONNECTED))
        offline = state in (int(ConnState.DISCONNECTED), int(ConnState.STOPPED))
        label = f"{device}{_OFFLINE_SUFFIX}" if offline else device
        self.setTabText(index, label)
        self.setTabIcon(index, _state_dot_icon(state))

    def _active_device_name(self) -> str:
        widget = self.currentWidget()
        for device, tab in self._device_tabs.items():
            if tab is widget:
                return device
        return ""  # the All tab

    def _on_current_changed(self, index: int) -> None:
        """Render only the now-visible tab's stack at full rate."""
        current = self.widget(index)
        all_active = current is self._all_stack
        self._all_stack.set_render_active(all_active)
        for tab in self._device_tabs.values():
            tab.stack.set_render_active(tab is current)

    def focus_device(self, device: str) -> bool:
        """Switch to ``device``'s tab so its live plots are visible.

        Returns ``True`` if a matching device tab existed and was
        selected. If the device has no tab yet (no stream seen), the
        current tab is left unchanged and ``False`` is returned — the
        caller (a detection double-click) still gets the detail pane.
        Used by the M8 detection table's row double-click.
        """
        tab = self._device_tabs.get(device)
        if tab is None:
            return False
        index = self.indexOf(tab)
        if index < 0:
            return False
        self.setCurrentIndex(index)
        return True

    # ------------------------------------------------------------------
    # Detection markers (M8 C1) — fan out to a stream's trace in every
    # stack that hosts it (the "All" overview + the device tab).
    # ------------------------------------------------------------------
    def add_detection_marker(
        self,
        device: str,
        nslc: str,
        det_id: int,
        t_on: float,
        t_off: float | None,
        score: float,
    ) -> None:
        self._fan_plots(
            device, nslc, lambda p: p.add_detection_marker(det_id, t_on, t_off, score)
        )

    def update_detection_marker(self, device: str, nslc: str, det_id: int, t_off: float) -> None:
        self._fan_plots(device, nslc, lambda p: p.update_detection_marker(det_id, t_off))

    def set_markers_visible(self, visible: bool) -> None:
        self._all_stack.set_markers_visible(visible)
        for tab in self._device_tabs.values():
            tab.stack.set_markers_visible(visible)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    @property
    def all_stack(self) -> LiveStack:
        """The "All" overview LiveStack (MainWindow's ``self._live_stack``)."""
        return self._all_stack

    # Test-only accessors
    def _device_tab_for_test(self, device: str) -> _DeviceTab | None:
        return self._device_tabs.get(device)

    def _has_device_tab_for_test(self, device: str) -> bool:
        return device in self._device_tabs
