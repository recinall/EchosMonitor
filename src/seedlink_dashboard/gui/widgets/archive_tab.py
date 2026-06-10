"""Central "Archive" tab — browse the recorded archive and view it statically.

This is the explore-driven entry point into the SDS archive: pick a device +
3-component station, see the real recorded extent and where data exists vs
gaps, choose an interval, and load it **statically** (no animated playback —
an explicit scope decision; the reverted replay attempt starved the live
worker by rendering on the GUI thread). The heavy read + spectrogram build run
off the GUI thread via :class:`~seedlink_dashboard.core.archive_window_loader.
ArchiveWindowLoader` (Stage B); this widget only emits a request and renders
the result with cheap ``setData`` calls.

Archive access is **read-only** (CLAUDE.md rule 8). The browser never invents
placeholder dates: an un-archived stream shows an honest empty state, and the
default interval is always a recent slice **within the real extent**.

Stage A (this skeleton): the browser — device/station selection, extent +
coverage display, a sensible default interval, and a "Load window" button that
emits :attr:`loadRequested`. Stage B fills in the static 3C view, the
spectrogram, the physical-unit selector, and the measurement cursors; Stage C
adds the HVSR / AI hand-off buttons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from obspy import UTCDateTime
from PySide6.QtCore import QDateTime, QRectF, Qt, QTimeZone, Signal, SignalInstance, Slot
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDateTimeEdit,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from seedlink_dashboard.core.archive_window_loader import (
    ArchiveWindowResult,
)
from seedlink_dashboard.gui.widgets.hvsr_widget import three_component_groups
from seedlink_dashboard.gui.widgets.pane_header import (
    PANE_TITLE_OBJECT_NAME,
    PANE_TITLE_STYLE,
)
from seedlink_dashboard.gui.widgets.spectrogram_view import (
    ColorMode,
    colorize,
    levels_for,
)

if TYPE_CHECKING:
    from seedlink_dashboard.core.streaming_engine import StreamingEngine
    from seedlink_dashboard.storage.dao import ArchiveDao

_COMPONENTS = ("Z", "N", "E")
_COMP_PENS = {
    "Z": pg.mkPen("#4fb0ff", width=1),
    "N": pg.mkPen("#7ee081", width=1),
    "E": pg.mkPen("#ffb454", width=1),
}
_CURSOR_PENS = {
    "A": pg.mkPen("#ff5d6c", width=2, style=Qt.PenStyle.DashLine),
    "B": pg.mkPen("#ffd166", width=2, style=Qt.PenStyle.DashLine),
}
_UNIT_ITEMS = (
    ("Counts", "COUNTS"),
    ("Velocity (m/s)", "VEL"),
    ("Acceleration (m/s²)", "ACC"),
    ("Displacement (m)", "DISP"),
)
_UNIT_LABELS = {
    "COUNTS": "counts",
    "VEL": "m/s",
    "ACC": "m/s²",
    "DISP": "m",
}

_NO_STREAM_TITLE = "Archive — no stream selected"
_NO_DATA_TEXT = "No archived data for this stream."

# Default load window: the last 10 minutes of available data (clamped to the
# real extent). A sensible recent slice, never an epoch placeholder.
_DEFAULT_WINDOW_S = 600.0


def _qdt_from_epoch(epoch: float) -> QDateTime:
    """Build a UTC ``QDateTime`` from a POSIX epoch (whole seconds shown)."""
    return QDateTime.fromSecsSinceEpoch(int(epoch), QTimeZone.utc())


def _epoch_from_qdt(qdt: QDateTime) -> float:
    """Read a ``QDateTimeEdit`` value as a UTC wall-clock epoch.

    Mirrors :class:`HvsrWidget`'s archive-field interpretation exactly (the
    displayed naive wall-clock is treated as UTC) so an interval handed off to
    HVSR/AI round-trips to the same instant.
    """
    return float(UTCDateTime(qdt.toString("yyyy-MM-ddTHH:mm:ss")).timestamp)


class CoverageStrip(QWidget):
    """A thin horizontal bar painting covered intervals vs gaps over a window.

    Covered spans render solid; uncovered spans (gaps) render dark. Purely a
    visual indicator — the data it holds is exposed for tests so assertions
    target the modelled coverage, not pixels (rule 10).
    """

    _COVERED = QColor("#3a7d44")
    _GAP = QColor("#2a2f36")
    _BORDER = QColor("#101418")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._t_start = 0.0
        self._t_end = 0.0
        self._intervals: list[tuple[float, float]] = []
        self.setMinimumHeight(18)
        self.setMaximumHeight(24)
        self.setToolTip("Recorded coverage over the selected range (green = data, dark = gap).")

    def set_coverage(
        self, t_start: float, t_end: float, intervals: list[tuple[float, float]]
    ) -> None:
        """Set the window and the covered (start, end) epoch intervals."""
        self._t_start = t_start
        self._t_end = t_end
        self._intervals = list(intervals)
        self.update()

    def coverage_for_test(self) -> tuple[float, float, list[tuple[float, float]]]:
        return self._t_start, self._t_end, list(self._intervals)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 (Qt override)
        del event
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, self._GAP)
        span = self._t_end - self._t_start
        if span > 0:
            w = rect.width()
            for seg_start, seg_end in self._intervals:
                x0 = int((seg_start - self._t_start) / span * w)
                x1 = int((seg_end - self._t_start) / span * w)
                painter.fillRect(x0, rect.top(), max(1, x1 - x0), rect.height(), self._COVERED)
        painter.setPen(self._BORDER)
        painter.drawRect(rect.adjusted(0, 0, -1, -1))


class ArchiveTab(QWidget):
    """The central Archive tab (browser + static view + measurement tools)."""

    # device, group({"Z","N","E": nslc}), t_start_epoch, t_end_epoch
    loadRequested = Signal(str, object, float, float)  # noqa: N815
    hvsrRequested = Signal(str, object, float, float)  # noqa: N815
    aiRequested = Signal(str, object, float, float)  # noqa: N815
    unitChangeRequested = Signal(str)  # unit code  # noqa: N815

    def __init__(
        self,
        engine: StreamingEngine,
        dao: ArchiveDao | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._dao = dao
        self._groups: dict[str, dict[str, dict[str, str]]] = {}

        # The currently-loaded window: the selection it was loaded for, the
        # displayed (x, y) per component in the current unit, and the window
        # bounds. Cursors read amplitude from ``_display`` (rule 11: a cheap
        # nearest-index GUI-thread read, no compute).
        self._loaded_device = ""
        self._loaded_group: dict[str, str] = {}
        self._win_t_start = 0.0
        self._win_t_end = 0.0
        self._display: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self._unit_label = "counts"
        self._cursor_pos: dict[str, float] = {"A": 0.0, "B": 0.0}
        self._cursor_lines: dict[str, list[pg.InfiniteLine]] = {"A": [], "B": []}
        self._suppress_cursor = False
        self._suppress_unit = False

        self._build_ui()
        self._wire()
        self._refresh_devices()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._title = QLabel(_NO_STREAM_TITLE, self)
        self._title.setObjectName(PANE_TITLE_OBJECT_NAME)
        self._title.setStyleSheet(PANE_TITLE_STYLE)

        self._device_combo = QComboBox(self)
        self._station_combo = QComboBox(self)
        self._group_label = QLabel("—", self)

        self._extent_label = QLabel(_NO_DATA_TEXT, self)
        self._extent_label.setStyleSheet("color: #9aa4af; font-style: italic;")
        # A long "Archived: <start> → <end>" string would otherwise pin this
        # tab page's minimum width and inflate the central QTabWidget minimum
        # (the layout trap HVSR already paid for). Let it shrink horizontally.
        self._extent_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._coverage = CoverageStrip(self)

        self._start_edit = QDateTimeEdit(self)
        self._start_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._start_edit.setCalendarPopup(True)
        # Display + interpret as UTC so a set epoch round-trips to the same
        # instant (the widget defaults to local-time spec otherwise).
        self._start_edit.setTimeZone(QTimeZone.utc())
        self._end_edit = QDateTimeEdit(self)
        self._end_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._end_edit.setCalendarPopup(True)
        self._end_edit.setTimeZone(QTimeZone.utc())
        # Honest non-placeholder defaults until a stream is chosen: "now".
        now_qdt = _qdt_from_epoch(UTCDateTime().timestamp)
        self._end_edit.setDateTime(now_qdt)
        self._start_edit.setDateTime(now_qdt.addSecs(int(-_DEFAULT_WINDOW_S)))

        self._load_button = QPushButton("Load window", self)
        self._load_button.setToolTip("Read the selected interval from the archive (static view).")

        # Let the wide controls shrink horizontally so this tab page's minimum
        # width never inflates the central QTabWidget minimum (the layout trap
        # HVSR already paid for — Ignored hsize policy on the offenders).
        for w in (
            self._start_edit,
            self._end_edit,
            self._group_label,
            self._station_combo,
            self._device_combo,
        ):
            w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        # --- form ---------------------------------------------------------
        form = QFormLayout()
        form.setContentsMargins(6, 2, 6, 2)
        form.setVerticalSpacing(3)
        dev_row = QHBoxLayout()
        dev_row.addWidget(self._device_combo, stretch=1)
        dev_row.addWidget(QLabel("Station:"))
        dev_row.addWidget(self._station_combo, stretch=1)
        dev_row.addWidget(self._group_label, stretch=2)
        form.addRow("Device:", _wrap(dev_row))

        interval = QHBoxLayout()
        interval.addWidget(QLabel("from"))
        # Stretch the Ignored-policy edits so they fill the available width when
        # space allows, yet still collapse when the tab is squeezed (keeping the
        # page minimum small).
        interval.addWidget(self._start_edit, stretch=3)
        interval.addWidget(QLabel("to"))
        interval.addWidget(self._end_edit, stretch=3)
        interval.addWidget(self._load_button)
        interval.addStretch(1)
        form.addRow("Interval:", _wrap(interval))

        # --- view area ---------------------------------------------------
        self._view_container = self._build_view()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        layout.addWidget(self._title)
        layout.addLayout(form)
        layout.addWidget(self._extent_label)
        layout.addWidget(self._coverage)
        layout.addWidget(self._view_container, stretch=1)

    def _build_view(self) -> QWidget:
        """Build the static 3C view, spectrogram, measurement cursors + readout.

        All plots/curves/cursors are created once here; loading a window only
        feeds them via ``setData`` / ``setImage`` and repositions the cursors.
        """
        container = QWidget(self)
        container.setMinimumHeight(40)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)

        # ---- toolbar -----------------------------------------------------
        bar = QHBoxLayout()
        self._unit_combo = QComboBox(self)
        for label, code in _UNIT_ITEMS:
            self._unit_combo.addItem(label, code)
        self._unit_combo.setEnabled(False)
        self._unit_combo.setToolTip("Physical units require instrument response metadata.")
        self._stacked_radio = QRadioButton("Stacked", self)
        self._overlaid_radio = QRadioButton("Overlaid", self)
        self._stacked_radio.setChecked(True)
        self._layout_group = QButtonGroup(self)
        self._layout_group.addButton(self._stacked_radio)
        self._layout_group.addButton(self._overlaid_radio)
        self._readout_combo = QComboBox(self)  # active component for cursor amplitude
        for comp in _COMPONENTS:
            self._readout_combo.addItem(comp, comp)
        self._reset_button = QPushButton("Reset view", self)
        self._hvsr_button = QPushButton("Run HVSR on this window", self)
        self._ai_button = QPushButton("Run AI agent on this window", self)
        for b in (self._hvsr_button, self._ai_button):
            b.setEnabled(False)
        bar.addWidget(QLabel("Units:"))
        bar.addWidget(self._unit_combo)
        bar.addWidget(self._stacked_radio)
        bar.addWidget(self._overlaid_radio)
        bar.addWidget(QLabel("Cursor on:"))
        bar.addWidget(self._readout_combo)
        bar.addWidget(self._reset_button)
        bar.addStretch(1)
        bar.addWidget(self._hvsr_button)
        bar.addWidget(self._ai_button)
        # The toolbar's long-text buttons + combos would otherwise pin this tab
        # page's minimum width and inflate the central QTabWidget minimum (the
        # layout trap HVSR already paid for). Let them shrink horizontally so
        # the page minimum tracks the (already-bounded, 40 px) plots.
        for w in (
            self._unit_combo,
            self._readout_combo,
            self._reset_button,
            self._hvsr_button,
            self._ai_button,
        ):
            w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        outer.addLayout(bar)

        # ---- trace plots (stacked GLW + overlaid GLW in a stack) --------
        self._trace_host = QSplitter(Qt.Orientation.Vertical, self)

        self._stacked_glw = pg.GraphicsLayoutWidget()
        self._stacked_plots: dict[str, pg.PlotItem] = {}
        self._stacked_curves: dict[str, pg.PlotDataItem] = {}
        for row, comp in enumerate(_COMPONENTS):
            plot = self._stacked_glw.addPlot(
                row=row, col=0, axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
            )
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setMenuEnabled(False)
            plot.setLabel("left", f"{comp} (counts)")
            plot.setMinimumSize(40, 40)
            self._stacked_plots[comp] = plot
            self._stacked_curves[comp] = plot.plot(pen=_COMP_PENS[comp])
        self._stacked_plots["N"].setXLink(self._stacked_plots["Z"])
        self._stacked_plots["E"].setXLink(self._stacked_plots["Z"])

        self._overlay_glw = pg.GraphicsLayoutWidget()
        self._overlay_plot = self._overlay_glw.addPlot(
            row=0, col=0, axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self._overlay_plot.showGrid(x=True, y=True, alpha=0.3)
        self._overlay_plot.setMenuEnabled(False)
        self._overlay_plot.setLabel("left", "counts")
        self._overlay_plot.setMinimumSize(40, 40)
        self._overlay_plot.addLegend()
        self._overlay_curves: dict[str, pg.PlotDataItem] = {}
        for comp in _COMPONENTS:
            self._overlay_curves[comp] = self._overlay_plot.plot(pen=_COMP_PENS[comp], name=comp)

        self._trace_stack_host = QWidget(self)
        self._trace_stack = QVBoxLayout(self._trace_stack_host)
        self._trace_stack.setContentsMargins(0, 0, 0, 0)
        self._trace_stack.addWidget(self._stacked_glw)
        self._trace_stack.addWidget(self._overlay_glw)
        self._overlay_glw.setVisible(False)

        # ---- spectrogram of the primary component -----------------------
        self._spec_glw = pg.GraphicsLayoutWidget()
        self._spec_plot = self._spec_glw.addPlot(
            row=0, col=0, axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self._spec_plot.setMenuEnabled(False)
        self._spec_plot.setLabel("left", "Frequency", units="Hz")
        self._spec_plot.setMinimumSize(40, 40)
        self._spec_image = pg.ImageItem(axisOrder="row-major")
        self._spec_plot.addItem(self._spec_image)

        self._trace_host.addWidget(self._trace_stack_host)
        self._trace_host.addWidget(self._spec_glw)
        self._trace_host.setStretchFactor(0, 3)
        self._trace_host.setStretchFactor(1, 2)

        # ---- draggable measurement cursors (on stacked-Z + overlay) -----
        for which in ("A", "B"):
            for plot in (self._stacked_plots["Z"], self._overlay_plot):
                line = pg.InfiniteLine(angle=90, movable=True, pen=_CURSOR_PENS[which])
                line.setVisible(False)
                plot.addItem(line)
                line.sigPositionChanged.connect(
                    lambda _l=None, w=which, ln=line: self._on_cursor_moved(w, ln)
                )
                self._cursor_lines[which].append(line)

        # ---- readout panel ----------------------------------------------
        self._readout_label = QLabel("", self)
        self._readout_label.setStyleSheet("font-family: monospace; color: #c9d4df;")
        self._readout_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self._status_label = QLabel("Select a 3-component station and load a window.", self)
        self._status_label.setStyleSheet("color: #9aa4af;")
        self._status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        outer.addWidget(self._trace_host, stretch=1)
        outer.addWidget(self._readout_label)
        outer.addWidget(self._status_label)
        return container

    def _wire(self) -> None:
        self._engine.newStreamSeen.connect(self._on_new_stream)
        self._engine.devicesChanged.connect(self._refresh_devices)
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._station_combo.currentIndexChanged.connect(self._on_station_changed)
        self._load_button.clicked.connect(self._on_load_clicked)
        self._unit_combo.currentIndexChanged.connect(self._on_unit_combo_changed)
        self._stacked_radio.toggled.connect(self._on_layout_toggled)
        self._readout_combo.currentIndexChanged.connect(lambda _i: self._refresh_readout())
        self._reset_button.clicked.connect(self._reset_view)
        self._hvsr_button.clicked.connect(lambda: self._emit_handoff(self.hvsrRequested))
        self._ai_button.clicked.connect(lambda: self._emit_handoff(self.aiRequested))

    # ------------------------------------------------------------------
    # Device / station selection (mirrors HvsrWidget)
    # ------------------------------------------------------------------
    @Slot(str, str)
    def _on_new_stream(self, device: str, nslc: str) -> None:
        del device, nslc
        self._refresh_devices()

    @Slot()
    def _refresh_devices(self) -> None:
        self._groups = three_component_groups(self._engine)
        prior = self._device_combo.currentData()
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        for device in sorted(self._groups):
            self._device_combo.addItem(device, device)
        if isinstance(prior, str):
            idx = self._device_combo.findData(prior)
            if idx >= 0:
                self._device_combo.setCurrentIndex(idx)
        self._device_combo.blockSignals(False)
        self._refresh_stations()

    def _refresh_stations(self) -> None:
        device = self._device_combo.currentData()
        prior = self._station_combo.currentData()
        self._station_combo.blockSignals(True)
        self._station_combo.clear()
        if isinstance(device, str):
            for station in sorted(self._groups.get(device, {})):
                self._station_combo.addItem(station, station)
        if isinstance(prior, str):
            idx = self._station_combo.findData(prior)
            if idx >= 0:
                self._station_combo.setCurrentIndex(idx)
        self._station_combo.blockSignals(False)
        self._update_group_label()
        self._refresh_extent()

    def _on_device_changed(self, _index: int) -> None:
        self._refresh_stations()

    def _on_station_changed(self, _index: int) -> None:
        self._update_group_label()
        self._refresh_extent()

    def selected_group(self) -> dict[str, str] | None:
        device = self._device_combo.currentData()
        station = self._station_combo.currentData()
        if not isinstance(device, str) or not isinstance(station, str):
            return None
        return self._groups.get(device, {}).get(station)

    def _update_group_label(self) -> None:
        group = self.selected_group()
        if group is None:
            self._group_label.setText("no 3-component station")
        else:
            self._group_label.setText(
                "  ".join(f"{c}={group[c].split('.')[-1]}" for c in ("Z", "N", "E"))
            )

    # ------------------------------------------------------------------
    # Extent / coverage / default interval
    # ------------------------------------------------------------------
    def _refresh_extent(self) -> None:
        """Query the archive extent for the primary (Z) component and set a
        sensible default interval + coverage strip — or an honest empty state.
        """
        group = self.selected_group()
        device = self._device_combo.currentData()
        extent = None
        if group is not None and isinstance(device, str) and self._dao is not None:
            extent = self._dao.archive_extent(device, group["Z"])
        if extent is None:
            # Distinguish "archiving is off for this device" (no waveforms are
            # being recorded — the common cause) from "archiving is on but
            # nothing is indexed yet". Knowing this resolves the frequent
            # confusion of an existing archive.db (full of detection/session
            # metadata) but an empty waveform archive.
            self._extent_label.setText(self._empty_extent_message(device))
            self._coverage.set_coverage(0.0, 0.0, [])
            self._load_button.setEnabled(False)
            self._title.setText(_NO_STREAM_TITLE)
            return
        t_min, t_max = extent
        self._extent_label.setText(f"Archived: {t_min} → {t_max}")
        # Default to the last _DEFAULT_WINDOW_S within the real extent.
        end_epoch = float(t_max.timestamp)
        start_epoch = max(float(t_min.timestamp), end_epoch - _DEFAULT_WINDOW_S)
        self._start_edit.blockSignals(True)
        self._end_edit.blockSignals(True)
        self._start_edit.setDateTime(_qdt_from_epoch(start_epoch))
        self._end_edit.setDateTime(_qdt_from_epoch(end_epoch))
        self._start_edit.blockSignals(False)
        self._end_edit.blockSignals(False)
        self._load_button.setEnabled(True)
        self._update_coverage()
        station = self._station_combo.currentData()
        self._title.setText(f"Archive — {device} / {station}")

    def _empty_extent_message(self, device: object) -> str:
        """Actionable empty-state text — names archiving-disabled when that is
        why the ``files`` index is empty (read via the engine's public
        ``devices()`` snapshot; degrades to the generic text if unavailable)."""
        if isinstance(device, str) and self._archive_disabled_for(device):
            return (
                f"No archived waveforms — archiving is disabled for '{device}'. "
                f"Enable archive in the device settings to record data here."
            )
        return _NO_DATA_TEXT

    def _archive_disabled_for(self, device: str) -> bool:
        getter = getattr(self._engine, "devices", None)
        if not callable(getter):
            return False
        try:
            for dev in getter():
                if getattr(dev, "name", None) == device:
                    return not bool(dev.archive.enabled)
        except Exception:
            return False
        return False

    def _update_coverage(self) -> None:
        group = self.selected_group()
        device = self._device_combo.currentData()
        if group is None or not isinstance(device, str) or self._dao is None:
            return
        t_start = _epoch_from_qdt(self._start_edit.dateTime())
        t_end = _epoch_from_qdt(self._end_edit.dateTime())
        if t_end <= t_start:
            self._coverage.set_coverage(t_start, t_end, [])
            return
        intervals = self._dao.archive_coverage(
            device, group["Z"], UTCDateTime(t_start), UTCDateTime(t_end)
        )
        self._coverage.set_coverage(
            t_start, t_end, [(float(s.timestamp), float(e.timestamp)) for s, e in intervals]
        )

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    def _on_load_clicked(self) -> None:
        group = self.selected_group()
        device = self._device_combo.currentData()
        if group is None or not isinstance(device, str):
            return
        t_start = _epoch_from_qdt(self._start_edit.dateTime())
        t_end = _epoch_from_qdt(self._end_edit.dateTime())
        if t_end <= t_start:
            self._status_label.setText("The end time must be after the start time.")
            return
        self._update_coverage()
        # Record the selection the load was requested for; the result + the
        # HVSR/AI hand-off and unit decon all operate on exactly this window.
        self._loaded_device = device
        self._loaded_group = dict(group)
        self._win_t_start = t_start
        self._win_t_end = t_end
        self._status_label.setText("Loading window…")
        self.loadRequested.emit(device, dict(group), t_start, t_end)

    # ------------------------------------------------------------------
    # Rendering the loaded window (called by the host on the GUI thread)
    # ------------------------------------------------------------------
    def show_result(self, result: ArchiveWindowResult) -> None:
        """Render a loaded window: 3C traces (counts) + spectrogram + cursors."""
        self._display = {}
        self._reset_unit_combo_to_counts()
        self._unit_label = "counts"
        for comp in _COMPONENTS:
            tr = next((t for t in result.traces if t.comp == comp), None)
            if tr is None:
                self._stacked_curves[comp].clear()
                self._overlay_curves[comp].clear()
                continue
            x = np.asarray(tr.x, dtype=np.float64)
            y = np.asarray(tr.y, dtype=np.float64)
            self._stacked_curves[comp].setData(x, y, connect="finite")
            self._overlay_curves[comp].setData(x, y, connect="finite")
            self._stacked_plots[comp].setLabel("left", f"{comp} (counts)")
            self._display[comp] = (x, y)
        self._render_spectrogram(result)
        # Fit the X range to the loaded window so curves are never stuck at
        # [0, 1] (the detail-pane regression). N/E are x-linked to Z.
        if self._win_t_end > self._win_t_start:
            self._stacked_plots["Z"].setXRange(self._win_t_start, self._win_t_end, padding=0.0)
            self._overlay_plot.setXRange(self._win_t_start, self._win_t_end, padding=0.0)
            for plot in self._stacked_plots.values():
                plot.enableAutoRange(axis="y")
            self._overlay_plot.enableAutoRange(axis="y")
        self._place_cursors()
        self._hvsr_button.setEnabled(True)
        self._ai_button.setEnabled(True)
        self._status_label.setText(
            f"Loaded {len(result.traces)} component(s) over "
            f"{UTCDateTime(self._win_t_start)} → {UTCDateTime(self._win_t_end)}."
        )

    def show_empty(self) -> None:
        self._display = {}
        for comp in _COMPONENTS:
            self._stacked_curves[comp].clear()
            self._overlay_curves[comp].clear()
        self._spec_image.clear()
        for which in ("A", "B"):
            for line in self._cursor_lines[which]:
                line.setVisible(False)
        self._readout_label.setText("")
        self._hvsr_button.setEnabled(False)
        self._ai_button.setEnabled(False)
        self._unit_combo.setEnabled(False)
        self._status_label.setText("No archived data for this interval.")

    def show_failed(self, message: str) -> None:
        self._status_label.setText(f"Archive read failed: {message}")

    def _render_spectrogram(self, result: ArchiveWindowResult) -> None:
        if result.spec_power is None or result.spec_freqs is None:
            self._spec_image.clear()
            return
        power = result.spec_power  # (n_freq, n_cols) raw linear power
        # Display-domain transform (a UI concern) on the finished image — cheap,
        # vectorised, done once on the GUI thread. Per-column z-score on log
        # power (same transform the live spectrogram uses).
        cols = [colorize(power[:, i], ColorMode.Z_SCORE) for i in range(power.shape[1])]
        image = np.stack(cols, axis=1) if cols else power
        self._spec_image.setImage(image, autoLevels=False)
        self._spec_image.setLevels(levels_for(ColorMode.Z_SCORE))
        freqs = np.asarray(result.spec_freqs, dtype=np.float64)
        f_min = float(freqs[0]) if freqs.size else 0.0
        f_max = float(freqs[-1]) if freqs.size else 1.0
        width = result.spec_t_end - result.spec_t_start
        self._spec_image.setRect(QRectF(result.spec_t_start, f_min, width, f_max - f_min))
        self._spec_plot.setXRange(result.spec_t_start, result.spec_t_end, padding=0.0)
        self._spec_plot.setYRange(f_min, f_max, padding=0.0)

    # ------------------------------------------------------------------
    # Measurement cursors
    # ------------------------------------------------------------------
    def _place_cursors(self) -> None:
        """Position the two cursors at 25% / 75% of the loaded window."""
        span = self._win_t_end - self._win_t_start
        if span <= 0:
            return
        self._suppress_cursor = True
        for which, frac in (("A", 0.25), ("B", 0.75)):
            pos = self._win_t_start + frac * span
            self._cursor_pos[which] = pos
            for line in self._cursor_lines[which]:
                line.setValue(pos)
                line.setVisible(True)
        self._suppress_cursor = False
        self._refresh_readout()

    def _on_cursor_moved(self, which: str, line: pg.InfiniteLine) -> None:
        if self._suppress_cursor:
            return
        value = float(line.value())
        self._cursor_pos[which] = value
        self._suppress_cursor = True
        for twin in self._cursor_lines[which]:
            if twin is not line and float(twin.value()) != value:
                twin.setValue(value)
        self._suppress_cursor = False
        self._refresh_readout()

    def _amplitude_at(self, comp: str, epoch: float) -> float | None:
        data = self._display.get(comp)
        if data is None:
            return None
        x, y = data
        if x.size == 0:
            return None
        idx = int(np.clip(np.searchsorted(x, epoch), 0, x.size - 1))
        val = float(y[idx])
        return val if np.isfinite(val) else None

    def _refresh_readout(self) -> None:
        if not self._display or self._win_t_end <= self._win_t_start:
            self._readout_label.setText("")
            return
        comp = str(self._readout_combo.currentData() or "Z")
        a, b = self._cursor_pos["A"], self._cursor_pos["B"]
        amp_a = self._amplitude_at(comp, a)
        amp_b = self._amplitude_at(comp, b)
        unit = self._unit_label

        def _amp(v: float | None) -> str:
            return "gap" if v is None else f"{v:.4g} {unit}"

        dt = abs(b - a)
        lines = [
            f"A {UTCDateTime(a)}  {comp}={_amp(amp_a)}",
            f"B {UTCDateTime(b)}  {comp}={_amp(amp_b)}",
        ]
        if dt > 0:
            damp = (
                f"{abs(amp_b - amp_a):.4g} {unit}"
                if amp_a is not None and amp_b is not None
                else "—"
            )
            lines.append(
                f"Δt={dt:.4g} s   Δamp={damp}   "
                f"f=1/Δt={1.0 / dt:.4g} Hz   T=Δt={dt:.4g} s   "
                f"(manual period estimate: pick two successive peaks)"
            )
        self._readout_label.setText("\n".join(lines))

    def _reset_view(self) -> None:
        if self._win_t_end <= self._win_t_start:
            return
        self._stacked_plots["Z"].setXRange(self._win_t_start, self._win_t_end, padding=0.0)
        self._overlay_plot.setXRange(self._win_t_start, self._win_t_end, padding=0.0)
        for plot in self._stacked_plots.values():
            plot.enableAutoRange(axis="y")
        self._overlay_plot.enableAutoRange(axis="y")

    # ------------------------------------------------------------------
    # Stacked / overlaid toggle
    # ------------------------------------------------------------------
    def _on_layout_toggled(self, _checked: bool) -> None:
        stacked = self._stacked_radio.isChecked()
        self._stacked_glw.setVisible(stacked)
        self._overlay_glw.setVisible(not stacked)

    # ------------------------------------------------------------------
    # Physical units (decon driven by the host's dedicated worker)
    # ------------------------------------------------------------------
    def _on_unit_combo_changed(self, _index: int) -> None:
        if self._suppress_unit:
            return
        code = str(self._unit_combo.currentData())
        self.unitChangeRequested.emit(code)

    def set_response_available(self, available: bool, tooltip: str) -> None:
        """Enable/disable the physical-unit items (Counts always available)."""
        self._unit_combo.setEnabled(True)
        model = self._unit_combo.model()
        for i in range(1, self._unit_combo.count()):
            item = model.item(i)  # type: ignore[attr-defined]
            if item is not None:
                item.setEnabled(available)
        self._unit_combo.setToolTip(tooltip)
        if not available:
            self._reset_unit_combo_to_counts()

    def revert_to_counts(self) -> None:
        """Re-render every component in counts (e.g. response unavailable)."""
        self._reset_unit_combo_to_counts()
        self._unit_label = "counts"
        for comp, (x, y) in self._display.items():
            self._stacked_curves[comp].setData(x, y, connect="finite")
            self._overlay_curves[comp].setData(x, y, connect="finite")
            self._stacked_plots[comp].setLabel("left", f"{comp} (counts)")
        self._refresh_readout()

    def show_physical_component(self, comp: str, unit_label: str, samples: np.ndarray) -> None:
        """Render one component in physical units (host decon result)."""
        data = self._display.get(comp)
        if data is None:
            return
        x, _old = data
        y = np.asarray(samples, dtype=np.float64)
        if y.shape[0] != x.shape[0]:
            return
        self._display[comp] = (x, y)
        self._stacked_curves[comp].setData(x, y, connect="finite")
        self._overlay_curves[comp].setData(x, y, connect="finite")
        self._stacked_plots[comp].setLabel("left", f"{comp} ({unit_label})")
        code = str(self._unit_combo.currentData())
        self._unit_label = _UNIT_LABELS.get(code, unit_label)
        self._refresh_readout()

    def _reset_unit_combo_to_counts(self) -> None:
        self._suppress_unit = True
        self._unit_combo.setCurrentIndex(0)
        self._suppress_unit = False

    def current_window(self) -> tuple[str, dict[str, str], float, float]:
        """The loaded (device, group, t_start, t_end) — for the host's decon /
        hand-off. Exactly the interval the user selected and measured."""
        return self._loaded_device, dict(self._loaded_group), self._win_t_start, self._win_t_end

    def _emit_handoff(self, signal: SignalInstance) -> None:
        if not self._loaded_device or not self._loaded_group:
            return
        signal.emit(
            self._loaded_device, dict(self._loaded_group), self._win_t_start, self._win_t_end
        )

    # ------------------------------------------------------------------
    # Test accessors
    # ------------------------------------------------------------------
    def extent_text_for_test(self) -> str:
        return self._extent_label.text()

    def status_text_for_test(self) -> str:
        return self._status_label.text()

    def interval_for_test(self) -> tuple[float, float]:
        return (
            _epoch_from_qdt(self._start_edit.dateTime()),
            _epoch_from_qdt(self._end_edit.dateTime()),
        )

    def load_enabled_for_test(self) -> bool:
        return self._load_button.isEnabled()

    def trace_curve_for_test(self, comp: str) -> pg.PlotDataItem:
        return self._stacked_curves[comp]

    def spectrogram_image_for_test(self) -> np.ndarray | None:
        return self._spec_image.image  # type: ignore[no-any-return]

    def trace_x_range_for_test(self) -> tuple[float, float]:
        lo, hi = self._stacked_plots["Z"].viewRange()[0]
        return float(lo), float(hi)

    def readout_text_for_test(self) -> str:
        return self._readout_label.text()

    def top_unit_label_for_test(self, comp: str = "Z") -> str:
        axis = self._stacked_plots[comp].getAxis("left")
        return str(axis.labelText)

    def set_cursor_epoch_for_test(self, which: str, epoch: float) -> None:
        """Drive a cursor to a known epoch (offscreen drag is undeliverable)."""
        self._cursor_lines[which][0].setValue(epoch)

    def cursor_pos_for_test(self) -> dict[str, float]:
        return dict(self._cursor_pos)


def _wrap(layout: QHBoxLayout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
