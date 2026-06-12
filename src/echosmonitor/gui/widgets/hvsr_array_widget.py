"""HVSR Array tab — synchronous per-station HVSR over N devices (M5-B).

A sibling of the single-station HVSR tab, kept as its own widget so the
existing flows stay untouched. It drives a
:class:`~echosmonitor.core.hvsr_array.HvsrArrayEngine` over a
user-checked device selection and renders:

* the **H/V overlay** — one mean curve per device (one colour each),
  with each device's per-window curves faintly behind it on demand (the
  comparison view; curves are NEVER averaged across stations — skill
  ``hvsr-array``);
* the **per-device results table** — f0 +/- sigma, T0, A0, windows
  valid/total, SESAME reliability/clarity, the per-device same-response
  honesty verdict and any compute error. A0 comparison across stations
  is response-sensitive and annotated as such; f0 comparison is not.
* a **status line** with live per-device window counts, and an explicit
  "no position" note for selected devices absent from the geometry
  snapshot (rule 16 — said, not guessed).

One shared settings row drives all stations (skill ``hvsr-array``).
The geometry snapshot comes from the host-injected provider (the ONE
shared ``PositionResolver`` — rule 16) at measurement start.

Threading: every method here runs on the GUI thread; the widget only
consumes the frozen :class:`~echosmonitor.core.hvsr_array.
ArrayHvsrResult` the engine emits and never imports hvsrpy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.hvsr import HvsrResult, HvsrSettings
from echosmonitor.core.hvsr_array import ArrayHvsrResult
from echosmonitor.gui.widgets.hvsr_widget import three_component_groups
from echosmonitor.gui.widgets.log_axis import DecimatedLogAxisItem
from echosmonitor.gui.widgets.pane_header import (
    PANE_TITLE_OBJECT_NAME,
    PANE_TITLE_STYLE,
)

if TYPE_CHECKING:
    from echosmonitor.core.hvsr_array import HvsrArrayEngine
    from echosmonitor.core.positions import StationGeometry
    from echosmonitor.core.streaming_engine import StreamingEngine
    from echosmonitor.storage.hvsr_report import ArrayReportContext

# Host-injected geometry snapshot source: the ONE shared PositionResolver's
# ``geometry(devices)`` (rule 16). Injected so the widget holds no resolver.
GeometryProvider = Callable[[Iterable[str]], "StationGeometry"]

# (groups, t_start, t_end, settings, geometry) -> measurement_id ("" if no
# device has a gap-free 3C window in the range). Wired by main_window so the
# widget stays free of ArchiveReader / storage construction (M5-D).
ArrayArchiveHandler = Callable[
    [dict[str, dict[str, str]], UTCDateTime, UTCDateTime, HvsrSettings, "StationGeometry"],
    str,
]

_NO_MEASUREMENT_TITLE = "HVSR array — no measurement"

# One colour per device, assigned by start-order index (stable across the
# whole measurement). Distinguishable on the dark plot background.
_DEVICE_COLORS = (
    "#3aa3ff",
    "#f5b942",
    "#7ac74f",
    "#e0526b",
    "#b07aff",
    "#4fd0c0",
    "#ff8a3a",
    "#d0d04a",
)
_UNRELIABLE_BRUSH = pg.mkBrush(224, 82, 107, 35)

_TABLE_HEADERS = (
    "Device",
    "f₀ (Hz)",
    "T₀ (s)",
    "A₀",
    "Windows",
    "SESAME rel.",
    "SESAME clar.",
    "Response",
    "Status",
)
_A0_COLUMN = 3
_A0_NOTE = (
    "A₀ comparison ACROSS stations is response-sensitive (H/V cancels the "
    "instrument response per station only); f₀ comparison is not."
)

# Spin bounds (mirror the single-station tab).
_WL_MIN, _WL_MAX = 5.0, 600.0
_B_MIN, _B_MAX = 5.0, 200.0
_FREQ_MIN, _FREQ_MAX = 0.05, 100.0


def device_color(index: int) -> str:
    return _DEVICE_COLORS[index % len(_DEVICE_COLORS)]


class HvsrArrayWidget(QWidget):
    """The multi-device HVSR analysis tab."""

    def __init__(
        self,
        engine: StreamingEngine,
        array_engine: HvsrArrayEngine,
        geometry_provider: GeometryProvider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._array = array_engine
        self._geometry_provider = geometry_provider
        self._measurement_id: str | None = None
        self._live_running = False  # True only while a LIVE measurement runs
        self._result: ArrayHvsrResult | None = None
        self._counts: dict[str, tuple[int, int]] = {}
        # The start-time selection (device -> Z/N/E), for the report context.
        self._active_groups: dict[str, dict[str, str]] = {}
        # Host callback for archive runs (M5-D); None disables the button.
        self._archive_handler: ArrayArchiveHandler | None = None
        self._build_ui()
        self._wire()
        self._refresh_devices()
        self._update_start_enabled()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._title = QLabel(_NO_MEASUREMENT_TITLE, self)
        self._title.setObjectName(PANE_TITLE_OBJECT_NAME)
        self._title.setStyleSheet(PANE_TITLE_STYLE)

        # --- device multi-select ------------------------------------------
        self._device_list = QListWidget(self)
        self._device_list.setToolTip(
            "Tick the 3-component stations to include in the array. One "
            "entry per device; all stations share the settings below."
        )
        self._device_list.setMaximumHeight(110)

        # --- shared settings row ------------------------------------------
        self._wl_spin = self._make_spin(_WL_MIN, _WL_MAX, 60.0, " s", 0)
        self._b_spin = self._make_spin(_B_MIN, _B_MAX, 40.0, "", 0)
        self._fmin_spin = self._make_spin(_FREQ_MIN, _FREQ_MAX, 0.2, " Hz", 2)
        self._fmax_spin = self._make_spin(_FREQ_MIN, _FREQ_MAX, 20.0, " Hz", 2)
        self._rejection_combo = QComboBox(self)
        self._rejection_combo.addItem("frequency-domain (Cox 2020)", "frequency_domain")
        self._rejection_combo.addItem("none", "none")
        self._horizontal_combo = QComboBox(self)
        for value, label in (
            ("geometric_mean", "geometric mean"),
            ("squared_average", "squared average"),
            ("total_horizontal_energy", "total horizontal energy"),
            ("maximum_horizontal_value", "maximum horizontal"),
        ):
            self._horizontal_combo.addItem(label, value)

        self._show_windows_button = QPushButton("Show windows", self)
        self._show_windows_button.setCheckable(True)
        self._show_windows_button.setToolTip(
            "Draw each device's per-window H/V curves faintly behind its mean."
        )
        self._save_pdf_button = QPushButton("Save report…", self)
        self._save_pdf_button.setToolTip(
            "Save a multi-station PDF report (comparison page + one section per station)."
        )
        self._export_button = QPushButton("Export JSON…", self)
        self._export_button.setToolTip("Export the full array result as JSON.")
        self._save_pdf_button.setEnabled(False)
        self._export_button.setEnabled(False)
        self._start_button = QPushButton("Start array measurement", self)

        params = QHBoxLayout()
        params.addWidget(QLabel("Window:"))
        params.addWidget(self._wl_spin)
        params.addWidget(QLabel("K-O b:"))
        params.addWidget(self._b_spin)
        params.addWidget(QLabel("f:"))
        params.addWidget(self._fmin_spin)
        params.addWidget(QLabel("to"))
        params.addWidget(self._fmax_spin)
        params.addWidget(QLabel("reject:"))
        params.addWidget(self._rejection_combo)
        params.addWidget(QLabel("horizontal:"))
        params.addWidget(self._horizontal_combo)
        params.addStretch(1)
        params.addWidget(self._show_windows_button)
        params.addWidget(self._save_pdf_button)
        params.addWidget(self._export_button)
        params.addWidget(self._start_button)

        # --- archive row (M5-D) -------------------------------------------
        self._archive_start = QDateTimeEdit(self)
        self._archive_start.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._archive_start.setCalendarPopup(True)
        self._archive_end = QDateTimeEdit(self)
        self._archive_end.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._archive_end.setCalendarPopup(True)
        end_dt = self._archive_end.dateTime()
        self._archive_end.setDateTime(end_dt)
        self._archive_start.setDateTime(end_dt.addSecs(-3600))  # default: last hour
        self._archive_button = QPushButton("Run on archive", self)
        self._archive_button.setToolTip(
            "Run the array analysis over an archived time range for the "
            "checked stations (select a session in the Archive tab first to "
            "read a CLOSED session's data; the result is archive-sourced)."
        )

        # --- plot -----------------------------------------------------------
        self._hv_plot = pg.PlotWidget(
            self, axisItems={"bottom": DecimatedLogAxisItem(orientation="bottom")}
        )
        self._hv_plot.setBackground("#101418")
        self._hv_plot.setLogMode(x=True, y=False)
        self._hv_plot.showGrid(x=True, y=True, alpha=0.25)
        self._hv_plot.setLabel("left", "H/V amplitude")
        self._hv_plot.setLabel("bottom", "Frequency", units="Hz")
        self._hv_plot.setMenuEnabled(False)
        self._hv_legend = self._hv_plot.addLegend(offset=(-10, 10))

        # --- per-device results table ----------------------------------------
        self._table = QTableWidget(0, len(_TABLE_HEADERS), self)
        self._table.setHorizontalHeaderLabels(list(_TABLE_HEADERS))
        a0_header = self._table.horizontalHeaderItem(_A0_COLUMN)
        if a0_header is not None:
            a0_header.setToolTip(_A0_NOTE)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)

        body = QSplitter(Qt.Orientation.Vertical, self)
        body.addWidget(self._hv_plot)
        body.addWidget(self._table)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 1)
        # Keep the page's minimum modest (the tabbed layout trap — see the
        # single-station widget): every child must be able to shrink.
        self._hv_plot.setMinimumSize(40, 40)
        self._table.setMinimumHeight(40)
        self._device_list.setMinimumHeight(40)

        # --- status -----------------------------------------------------------
        self._status_label = QLabel("Idle.", self)
        self._position_label = QLabel("", self)
        self._position_label.setWordWrap(True)
        self._position_label.setStyleSheet("color: #c9b36b;")
        self._a0_note_label = QLabel(_A0_NOTE, self)
        self._a0_note_label.setStyleSheet("color: #9aa4af; font-style: italic;")
        for lbl in (self._position_label, self._a0_note_label):
            lbl.setMaximumHeight(34)
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(1)
        layout.addWidget(self._title)
        layout.addWidget(self._device_list)
        layout.addLayout(_wrapped(params))
        archive_row = QHBoxLayout()
        archive_row.addWidget(QLabel("Archive: from"))
        archive_row.addWidget(self._archive_start)
        archive_row.addWidget(QLabel("to"))
        archive_row.addWidget(self._archive_end)
        archive_row.addWidget(self._archive_button)
        archive_row.addStretch(1)
        layout.addLayout(_wrapped(archive_row))
        layout.addWidget(body, stretch=1)
        layout.addWidget(self._a0_note_label)
        layout.addWidget(self._position_label)
        layout.addWidget(self._status_label)

    def _make_spin(
        self, lo: float, hi: float, val: float, suffix: str, decimals: int
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setRange(lo, hi)
        spin.setDecimals(decimals)
        spin.setValue(val)
        if suffix:
            spin.setSuffix(suffix)
        return spin

    def _wire(self) -> None:
        self._engine.newStreamSeen.connect(self._on_new_stream)
        self._engine.devicesChanged.connect(self._refresh_devices)
        self._device_list.itemChanged.connect(self._on_selection_changed)
        self._start_button.clicked.connect(self._on_start_clicked)
        self._show_windows_button.toggled.connect(self._on_show_windows_toggled)
        self._save_pdf_button.clicked.connect(self._on_save_report)
        self._export_button.clicked.connect(self._on_export_json)
        self._archive_button.clicked.connect(self._on_archive_clicked)

        self._array.arrayUpdated.connect(self._on_array_updated)
        self._array.arrayWindowCounts.connect(self._on_window_counts)
        self._array.arrayBackpressure.connect(self._on_backpressure)
        self._array.arrayMeasurementStopped.connect(self._on_stopped)

    # ------------------------------------------------------------------
    # Device selection
    # ------------------------------------------------------------------
    @Slot(str, str)
    def _on_new_stream(self, device: str, nslc: str) -> None:
        del device, nslc
        self._refresh_devices()

    @Slot()
    def _refresh_devices(self) -> None:
        """Rebuild the checkable (device, station) rows, keeping checks."""
        checked = {
            item.data(Qt.ItemDataRole.UserRole)
            for item in self._items()
            if item.checkState() == Qt.CheckState.Checked
        }
        groups = three_component_groups(self._engine)
        self._device_list.blockSignals(True)
        self._device_list.clear()
        for device in sorted(groups):
            for station in sorted(groups[device]):
                item = QListWidgetItem(f"{device}  ·  {station}")
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                key = (device, station)
                item.setData(Qt.ItemDataRole.UserRole, key)
                item.setData(Qt.ItemDataRole.UserRole + 1, dict(groups[device][station]))
                item.setCheckState(
                    Qt.CheckState.Checked if key in checked else Qt.CheckState.Unchecked
                )
                self._device_list.addItem(item)
        self._device_list.blockSignals(False)
        self._update_start_enabled()

    def _items(self) -> list[QListWidgetItem]:
        items = (self._device_list.item(i) for i in range(self._device_list.count()))
        return [item for item in items if item is not None]

    def checked_groups(self) -> dict[str, dict[str, str]] | None:
        """The checked selection as device → Z/N/E NSLCs, or ``None``.

        ``None`` when nothing is checked or when two stations of the SAME
        device are checked — the array layer is keyed by device (positions
        resolve per device, rule 16), so a duplicate is refused loudly.
        """
        groups: dict[str, dict[str, str]] = {}
        for item in self._items():
            if item.checkState() != Qt.CheckState.Checked:
                continue
            key = item.data(Qt.ItemDataRole.UserRole)
            group = item.data(Qt.ItemDataRole.UserRole + 1)
            if not isinstance(key, tuple) or not isinstance(group, dict):
                continue
            device = str(key[0])
            if device in groups:
                self._status_label.setText(
                    f"Two stations of {device!r} are checked — the array is "
                    "keyed by device; untick one."
                )
                return None
            groups[device] = dict(group)
        return groups or None

    @Slot(object)
    def _on_selection_changed(self, _item: object) -> None:
        self._update_start_enabled()

    def _update_start_enabled(self) -> None:
        if self._live_running:
            self._start_button.setEnabled(True)  # always allow Stop
            return
        any_checked = any(
            item.checkState() == Qt.CheckState.Checked for item in self._items()
        )
        self._start_button.setEnabled(any_checked)
        self._start_button.setToolTip(
            "" if any_checked else "Tick at least one 3-component station."
        )

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------
    def current_settings(self) -> HvsrSettings:
        return HvsrSettings(
            window_length_s=float(self._wl_spin.value()),
            konno_ohmachi_b=float(self._b_spin.value()),
            freqmin_hz=float(self._fmin_spin.value()),
            freqmax_hz=float(self._fmax_spin.value()),
            horizontal_method=str(self._horizontal_combo.currentData()),
            rejection_method=str(self._rejection_combo.currentData()),
        )

    def is_running(self) -> bool:
        """Whether a LIVE measurement is active (archive runs are one-shot)."""
        return self._live_running

    def status_text(self) -> str:
        return self._status_label.text()

    def _on_start_clicked(self) -> None:
        if self._live_running:
            self._array.stop_measurement(self._measurement_id)
            return
        groups = self.checked_groups()
        if groups is None:
            return
        geometry = self._geometry_provider(tuple(groups))
        self._clear_views()
        self._active_groups = {device: dict(group) for device, group in groups.items()}
        self._measurement_id = self._array.start_measurement(
            groups, self.current_settings(), geometry
        )
        self._live_running = True
        self._counts = dict.fromkeys(groups, (0, 0))
        self._set_controls_enabled(False)
        self._start_button.setText("Stop array measurement")
        self._title.setToolTip("")  # do not inherit a prior run's throttle note
        self._title.setText("HVSR array — " + ", ".join(sorted(groups)) + " (live)")
        self._status_label.setText("Measuring… accumulating windows on each device.")
        self._update_position_note(tuple(groups), geometry)
        self._rebuild_table()

    @Slot(str)
    def _on_stopped(self, measurement_id: str) -> None:
        if measurement_id != self._measurement_id:
            return
        self._measurement_id = None
        self._live_running = False
        self._set_controls_enabled(True)
        self._start_button.setText("Start array measurement")
        self._update_start_enabled()
        self._status_label.setText("Stopped.")
        # The last result stays on screen but is no longer live — say so.
        self._title.setText(self._title.text().replace(" (live)", " (stopped)"))
        self._title.setToolTip("")

    def _set_controls_enabled(self, enabled: bool) -> None:
        for w in (
            self._device_list,
            self._wl_spin,
            self._b_spin,
            self._fmin_spin,
            self._fmax_spin,
            self._rejection_combo,
            self._horizontal_combo,
        ):
            w.setEnabled(enabled)

    def _clear_views(self) -> None:
        self._hv_plot.clear()
        if self._hv_legend is not None:
            self._hv_legend.clear()
        self._table.setRowCount(0)
        self._result = None
        self._counts = {}
        self._position_label.setText("")
        self._save_pdf_button.setEnabled(False)
        self._export_button.setEnabled(False)

    # ------------------------------------------------------------------
    # Archive mode (M5-D)
    # ------------------------------------------------------------------
    def set_archive_request_handler(self, handler: ArrayArchiveHandler) -> None:
        """Install the host callback that runs the array over an archive."""
        self._archive_handler = handler

    def _on_archive_clicked(self) -> None:
        if self._archive_handler is None:
            return
        groups = self.checked_groups()
        if groups is None:
            return
        t_start = UTCDateTime(self._archive_start.dateTime().toString("yyyy-MM-ddTHH:mm:ss"))
        t_end = UTCDateTime(self._archive_end.dateTime().toString("yyyy-MM-ddTHH:mm:ss"))
        if t_end <= t_start:
            QMessageBox.warning(
                self, "HVSR array archive", "The end time must be after the start time."
            )
            return
        geometry = self._geometry_provider(tuple(groups))
        # Starting a new run supersedes any prior measurement (the handler's
        # engine call stops it). NOTE that stop fires arrayMeasurementStopped
        # via a same-thread direct connection, so _on_stopped runs
        # REENTRANTLY inside the handler call below (matching the old id) —
        # every field it touches is deliberately (re)set after the handler
        # returns. The archive run is one-shot, not live.
        self._live_running = False
        self._start_button.setText("Start array measurement")
        self._set_controls_enabled(True)
        self._clear_views()
        self._active_groups = {device: dict(group) for device, group in groups.items()}
        measurement_id = self._archive_handler(
            groups, t_start, t_end, self.current_settings(), geometry
        )
        if not measurement_id:
            self._measurement_id = None
            self._status_label.setText(
                "No archived data with full 3C coverage on any checked station "
                "in that range."
            )
            return
        self._measurement_id = measurement_id
        # Seed the honest sliced-window totals: the engine's start-time
        # counts emit fires before this handler returns the id, so it is
        # pulled here instead of received (reviewer F3).
        summary = self._array.active_measurement()
        if summary is not None and summary.measurement_id == measurement_id:
            self._counts = dict(summary.window_counts)
        else:
            self._counts = dict.fromkeys(groups, (0, 0))
        self._title.setToolTip("")
        self._title.setText(
            "HVSR array — " + ", ".join(sorted(groups)) + f" (archive {t_start} to {t_end})"
        )
        self._status_label.setText("Computing array HVSR over the archived range…")
        self._update_position_note(tuple(groups), geometry)
        self._rebuild_table()
        self._update_start_enabled()

    # ------------------------------------------------------------------
    # Engine signal handlers
    # ------------------------------------------------------------------
    @Slot(object)
    def _on_array_updated(self, result: object) -> None:
        if not isinstance(result, ArrayHvsrResult):
            return
        if result.measurement_id != self._measurement_id:
            return  # stale cycle from a stopped run
        self._result = result
        exportable = any(r.n_windows_valid > 0 for r in result.results.values())
        self._save_pdf_button.setEnabled(exportable)
        self._export_button.setEnabled(exportable)
        self._draw(result)
        self._rebuild_table()
        self._update_position_note(result.devices, result.geometry)

    @Slot(str, object)
    def _on_window_counts(self, measurement_id: str, counts: object) -> None:
        if measurement_id != self._measurement_id or not isinstance(counts, dict):
            return
        self._counts = {str(d): (int(v), int(t)) for d, (v, t) in counts.items()}
        parts = [f"{d} {v}/{t}" for d, (v, t) in sorted(self._counts.items())]
        self._status_label.setText("Windows (valid/total):  " + "   ".join(parts))
        self._rebuild_table()

    @Slot(str, int)
    def _on_backpressure(self, measurement_id: str, skipped: int) -> None:
        if measurement_id != self._measurement_id:
            return
        self._title.setToolTip(f"recompute throttled — {skipped} skipped (busy)")

    def _on_show_windows_toggled(self, _checked: bool) -> None:
        if self._result is not None:
            self._draw(self._result)

    # ------------------------------------------------------------------
    # Report / export (M5-C)
    # ------------------------------------------------------------------
    def _array_report_context(self) -> ArrayReportContext:
        from echosmonitor.storage.hvsr_report import ArrayReportContext

        result = self._result
        assert result is not None
        spans = [
            (r.t_start, r.t_end) for r in result.results.values() if r.n_windows_valid > 0
        ]
        if spans:
            t_lo = min(s for s, _ in spans)
            t_hi = max(e for _, e in spans)
            period = f"{t_lo} to {t_hi}"
        else:  # unreachable behind the exportable gate; honest fallback
            period = "no valid windows"
        return ArrayReportContext(
            group_by_device={d: dict(g) for d, g in self._active_groups.items()},
            period_label=period,
            generated_at=str(UTCDateTime()),
        )

    def _on_save_report(self) -> None:
        result = self._result
        if result is None or not any(r.n_windows_valid > 0 for r in result.results.values()):
            QMessageBox.warning(self, "HVSR array report", "No valid array result yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save HVSR array report", "hvsr_array_report.pdf", "PDF (*.pdf)"
        )
        if not path:
            return
        from pathlib import Path

        from echosmonitor.storage.hvsr_report import HvsrExportError, write_hvsr_array_pdf

        try:
            write_hvsr_array_pdf(result, Path(path), self._array_report_context())
        except HvsrExportError as exc:
            QMessageBox.warning(self, "HVSR array report", str(exc))

    def _on_export_json(self) -> None:
        result = self._result
        if result is None or not any(r.n_windows_valid > 0 for r in result.results.values()):
            QMessageBox.warning(self, "HVSR array export", "No valid array result yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export HVSR array data", "hvsr_array_data.json", "JSON (*.json)"
        )
        if not path:
            return
        from pathlib import Path

        from echosmonitor.storage.hvsr_report import HvsrExportError, export_hvsr_array_json

        try:
            export_hvsr_array_json(result, Path(path), self._array_report_context())
        except HvsrExportError as exc:
            QMessageBox.warning(self, "HVSR array export", str(exc))

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def _draw(self, result: ArrayHvsrResult) -> None:
        self._hv_plot.clear()
        if self._hv_legend is not None:
            self._hv_legend.clear()
        # One shared settings → one SESAME unreliable low-frequency strip.
        f_unreliable = result.settings.min_reliable_frequency_hz()
        if f_unreliable > result.settings.freqmin_hz:
            region = pg.LinearRegionItem(
                values=(
                    np.log10(max(result.settings.freqmin_hz, 1e-6)),
                    np.log10(f_unreliable),
                ),
                movable=False,
                brush=_UNRELIABLE_BRUSH,
            )
            region.setZValue(-20)
            self._hv_plot.addItem(region)

        show_windows = self._show_windows_button.isChecked()
        for index, device in enumerate(result.devices):
            device_result = result.results.get(device)
            if device_result is None:
                continue
            color = device_color(index)
            freq = device_result.frequency
            mask = freq > 0
            f = freq[mask]
            if f.size == 0:
                continue
            if show_windows and device_result.window_curves.shape[0] > 0:
                faint_color = pg.mkColor(color)
                faint_color.setAlpha(45)
                # ONE NaN-separated item per device, not one per window: a
                # long live run accumulates hundreds of windows per device,
                # and per-window PlotDataItems would stall the GUI thread on
                # every recompute (rule 1; auditor F2).
                curves = device_result.window_curves[:, mask]
                n_win, n_f = curves.shape
                xs = np.empty((n_win, n_f + 1))
                ys = np.empty((n_win, n_f + 1))
                xs[:, :n_f] = f
                ys[:, :n_f] = curves
                xs[:, n_f] = np.nan
                ys[:, n_f] = np.nan
                self._hv_plot.plot(
                    xs.ravel(),
                    ys.ravel(),
                    pen=pg.mkPen(faint_color, width=1),
                    connect="finite",
                )
            self._hv_plot.plot(
                f,
                device_result.mean_curve[mask],
                pen=pg.mkPen(color, width=2),
                name=device,
            )

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------
    def _rebuild_table(self) -> None:
        result = self._result
        devices: tuple[str, ...]
        if result is not None:
            devices = result.devices
        elif self._counts:
            devices = tuple(sorted(self._counts))
        else:
            self._table.setRowCount(0)
            return
        self._table.setRowCount(len(devices))
        for row, device in enumerate(devices):
            device_result = result.results.get(device) if result is not None else None
            error = result.errors.get(device, "") if result is not None else ""
            color = device_color(result.devices.index(device)) if result is not None else None
            self._fill_row(row, device, device_result, error, color)
        self._table.resizeColumnsToContents()

    def _fill_row(
        self,
        row: int,
        device: str,
        r: HvsrResult | None,
        error: str,
        color: str | None,
    ) -> None:
        valid, total = self._counts.get(device, (0, 0))
        if r is not None:
            valid, total = r.n_windows_valid, r.n_windows_total
            f0 = f"{r.f0_hz:.3f} ± {r.f0_sigma:.3f}" if np.isfinite(r.f0_hz) else "—"
            t0 = f"{1.0 / r.f0_hz:.3f}" if np.isfinite(r.f0_hz) and r.f0_hz > 0 else "—"
            a0 = f"{r.a0:.2f}" if np.isfinite(r.a0) else "—"
            rel = f"{'✓' if r.reliability_passed else '✗'} {sum(c.passed for c in r.reliability)}/3"
            cla = f"{'✓' if r.clarity_passed else '✗'} {sum(c.passed for c in r.clarity)}/6"
            if not r.same_response:
                resp = "✗ differ"
            elif "verified" in r.same_response_detail.lower():
                resp = "✓ verified"
            else:
                resp = "assumed"
            status = "ok"
        else:
            f0 = t0 = a0 = rel = cla = resp = "—"
            status = error if error else "accumulating…"
        cells = (device, f0, t0, a0, f"{valid}/{total}", rel, cla, resp, status)
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if col == 0 and color is not None:
                item.setForeground(pg.mkColor(color))
            if col == len(cells) - 1 and error:
                item.setForeground(pg.mkColor("#e0526b"))
                item.setToolTip(error)
            if col == _A0_COLUMN:
                item.setToolTip(_A0_NOTE)
            if col == 7 and r is not None:
                item.setToolTip(r.same_response_detail)
            if col == 5 and r is not None:
                item.setToolTip(
                    "\n".join(
                        f"{'✓' if c.passed else '✗'} {c.name} — {c.detail}"
                        for c in r.reliability
                    )
                )
            if col == 6 and r is not None:
                item.setToolTip(
                    "\n".join(
                        f"{'✓' if c.passed else '✗'} {c.name} — {c.detail}"
                        for c in r.clarity
                    )
                )
            self._table.setItem(row, col, item)

    def _update_position_note(
        self, devices: tuple[str, ...], geometry: StationGeometry
    ) -> None:
        missing = [d for d in devices if d not in geometry.positions]
        if missing:
            self._position_label.setText(
                "No position: " + ", ".join(sorted(missing)) + " — excluded from "
                "the map f₀ overlay and the report geometry (rule 16)."
            )
        else:
            self._position_label.setText("")


def _wrapped(layout: QHBoxLayout) -> QHBoxLayout:
    """Let the wide params row shrink (the tabbed-page minimum-size trap)."""
    wrapper = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    wrapper.setLayout(layout)
    wrapper.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    out = QHBoxLayout()
    out.setContentsMargins(0, 0, 0, 0)
    out.addWidget(wrapper)
    return out
