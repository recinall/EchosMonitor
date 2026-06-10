"""HVSR analysis tab — live H/V spectral ratio measurement (Stage B).

The widget beside the PSD tab. It drives an :class:`~seedlink_dashboard.
core.hvsr_engine.HvsrEngine` measurement and renders, as ambient-noise
windows accumulate and the estimate refines:

* the **H/V curve** — the mean (bold) with its lognormal times/divide-sigma
  band, the individual per-window curves faint behind it (the J-SESAME
  look), toggleable between PRE-rejection (all windows) and POST-rejection
  (valid only); the f0 peak marked with its dispersion strip; and the SESAME
  unreliable low-frequency band shaded;
* the **3-channel PSD** (Welch) of Z / N / E — the diagnostic panel;
* a **status line** — N valid / N total, f0 +/- dispersion, and the SESAME
  reliability (3) + clarity (6) verdict;
* a **window list** with each window's auto verdict and a manual
  include/exclude toggle (the override survives every recompute).

The 3-channel PSD is Konno-Ohmachi smoothed (a diagnostic, not the H/V
science) and populates early — a cheap raw PSD shows from the first window,
then the first full result's smoothed PSD takes over.

Threading: every method here runs on the GUI thread. The widget never
computes HVSR itself — it consumes the frozen
:class:`~seedlink_dashboard.core.hvsr.HvsrResult` the engine emits (the
engine runs hvsrpy off-thread) and never imports hvsrpy.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QDateTime, Qt, QTimeZone, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from seedlink_dashboard.core.hvsr import HvsrResult, HvsrSettings
from seedlink_dashboard.gui.widgets.log_axis import DecimatedLogAxisItem
from seedlink_dashboard.gui.widgets.pane_header import (
    PANE_TITLE_OBJECT_NAME,
    PANE_TITLE_STYLE,
)

if TYPE_CHECKING:
    from seedlink_dashboard.core.hvsr_engine import HvsrEngine
    from seedlink_dashboard.core.streaming_engine import StreamingEngine

# (device, group{Z,N,E}, t_start, t_end, settings) -> measurement_id ("" if
# no gap-free 3C window in the range). Wired by main_window so the widget
# stays free of ArchiveReader / storage construction.
ArchiveHandler = Callable[[str, dict[str, str], UTCDateTime, UTCDateTime, HvsrSettings], str]

# Vertical orientation letter; everything else in a station group is a
# horizontal. SEED uses Z/N/E or Z/1/2 — we map the two horizontals to N/E.
_VERTICAL = "Z"

# Pens / brushes.
_MEAN_PEN = pg.mkPen("#3aa3ff", width=3)
_PRE_MEAN_PEN = pg.mkPen("#f5b942", width=2, style=Qt.PenStyle.DashLine)
_WINDOW_PEN = pg.mkPen(color=(120, 140, 160, 60), width=1)
_F0_PEN = pg.mkPen("#e0526b", width=2, style=Qt.PenStyle.DashLine)
_SIGMA_BRUSH = pg.mkBrush(58, 163, 255, 40)
_F0_STRIP_BRUSH = pg.mkBrush(120, 120, 120, 50)
_UNRELIABLE_BRUSH = pg.mkBrush(224, 82, 107, 35)
_PSD_PENS = {
    "Z": pg.mkPen("#3aa3ff", width=2),
    "N": pg.mkPen("#7ac74f", width=2),
    "E": pg.mkPen("#f5b942", width=2),
}
_NO_STREAM_TITLE = "HVSR — no measurement"

# Default control bounds.
_WL_MIN, _WL_MAX = 5.0, 600.0
_B_MIN, _B_MAX = 5.0, 200.0
_FREQ_MIN, _FREQ_MAX = 0.05, 100.0


def _orientation(nslc: str) -> str:
    parts = nslc.split(".")
    if len(parts) != 4 or len(parts[3]) < 3:
        return ""
    return parts[3][2]


def _station_key(nslc: str) -> str:
    parts = nslc.split(".")
    if len(parts) != 4:
        return nslc
    net, sta, loc, cha = parts
    base = cha[:2] if len(cha) >= 3 else cha
    return f"{net}.{sta}.{loc}.{base}"


def three_component_groups(engine: StreamingEngine) -> dict[str, dict[str, dict[str, str]]]:
    """Map ``device -> station_key -> {Z,N,E: nslc}`` for 3C-capable stations.

    A station is 3C-capable when it has a vertical (``Z``) plus two
    horizontals (``N``/``E`` or ``1``/``2``). The two horizontals map to
    ``N`` (first) and ``E`` (second) so they feed hvsrpy's ``ns``/``ew``.
    """
    from seedlink_dashboard.core.models import DEVICE_KEY_SEP

    by_device: dict[str, dict[str, dict[str, str]]] = {}
    raw: dict[tuple[str, str], dict[str, str]] = {}
    for composite in engine._buffers:
        if DEVICE_KEY_SEP not in composite:
            continue
        device, nslc = composite.split(DEVICE_KEY_SEP, maxsplit=1)
        orient = _orientation(nslc)
        if not orient:
            continue
        raw.setdefault((device, _station_key(nslc)), {})[orient] = nslc
    for (device, station), orients in raw.items():
        vertical = orients.get(_VERTICAL)
        horizontals = sorted(n for o, n in orients.items() if o != _VERTICAL)
        if vertical is None or len(horizontals) < 2:
            continue
        group = {"Z": vertical, "N": horizontals[0], "E": horizontals[1]}
        by_device.setdefault(device, {})[station] = group
    return by_device


class HvsrWidget(QWidget):
    """The HVSR analysis tab."""

    def __init__(
        self,
        engine: StreamingEngine,
        hvsr_engine: HvsrEngine,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._hvsr = hvsr_engine
        self._groups: dict[str, dict[str, dict[str, str]]] = {}
        self._measurement_id: str | None = None
        self._live_running = False  # True only while a LIVE measurement runs
        self._result: HvsrResult | None = None
        self._show_post = True  # H/V pre/post-rejection toggle (post default)
        self._row_to_window: list[int] = []  # list-row -> window_id
        self._early_psd_shown = False  # an early raw PSD is on screen (pre first result)
        self._active_group: dict[str, str] = {}  # the Z/N/E NSLCs of the current run
        # Set by the host (main_window) to run a measurement over an archived
        # range: (device, group, t_start, t_end, settings) -> measurement_id
        # ("" if the range holds no gap-free 3C window).
        self._archive_handler: ArchiveHandler | None = None

        self._build_ui()
        self._wire()
        self._refresh_devices()
        self._update_start_enabled()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._title = QLabel(_NO_STREAM_TITLE, self)
        self._title.setObjectName(PANE_TITLE_OBJECT_NAME)
        self._title.setStyleSheet(PANE_TITLE_STYLE)

        # --- controls row -------------------------------------------------
        self._device_combo = QComboBox(self)
        self._station_combo = QComboBox(self)
        self._group_label = QLabel("—", self)

        self._wl_spin = self._make_spin(_WL_MIN, _WL_MAX, 60.0, " s", 0)
        self._b_spin = self._make_spin(_B_MIN, _B_MAX, 40.0, "", 0)
        self._fmin_spin = self._make_spin(_FREQ_MIN, _FREQ_MAX, 0.2, " Hz", 2)
        self._fmax_spin = self._make_spin(_FREQ_MIN, _FREQ_MAX, 20.0, " Hz", 2)
        self._rejection_combo = QComboBox(self)
        self._rejection_combo.addItem("frequency-domain (Cox 2020)", "frequency_domain")
        self._rejection_combo.addItem("none", "none")

        # Advanced analysis params (FEATURE 6) — all feed core/hvsr.py and the
        # report's PROCESSING SETTINGS block.
        self._horizontal_combo = QComboBox(self)
        for value, label in (
            ("geometric_mean", "geometric mean"),
            ("squared_average", "squared average"),
            ("total_horizontal_energy", "total horizontal energy"),
            ("maximum_horizontal_value", "maximum horizontal"),
        ):
            self._horizontal_combo.addItem(label, value)
        self._horizontal_combo.setToolTip("How the two horizontals (N/E) combine into one.")
        self._detrend_combo = QComboBox(self)
        self._detrend_combo.addItem("linear", "linear")
        self._detrend_combo.addItem("constant (mean)", "constant")
        self._detrend_combo.setToolTip("Per-window detrend before the FFT.")
        self._rejection_n_spin = self._make_spin(1.0, 5.0, 2.0, "", 1)
        self._rejection_n_spin.setToolTip("Cox-2020 std multiplier (larger = fewer rejections).")
        self._resample_spin = self._make_spin(64.0, 4096.0, 512.0, "", 0)
        self._resample_spin.setToolTip("Number of log-spaced Konno-Ohmachi frequencies.")
        # PSD smoothing (FEATURE 5).
        self._psd_smooth_check = QCheckBox("smooth PSD", self)
        self._psd_smooth_check.setChecked(True)
        self._psd_smooth_check.setToolTip(
            "Konno-Ohmachi smooth the 3-channel PSD display + report."
        )
        self._psd_b_spin = self._make_spin(_B_MIN, _B_MAX, 40.0, "", 0)
        self._psd_b_spin.setToolTip("PSD Konno-Ohmachi bandwidth b (diagnostic; own value).")

        self._start_button = QPushButton("Start measurement", self)
        self._prepost_button = QPushButton("Showing: post-rejection", self)
        self._prepost_button.setToolTip(
            "Toggle the H/V curve between all windows (pre-rejection) and "
            "valid windows only (post-rejection)."
        )
        self._save_pdf_button = QPushButton("Save report…", self)
        self._save_pdf_button.setToolTip("Save a PDF report of the current HVSR result.")
        self._export_button = QPushButton("Export raw…", self)
        self._export_button.setToolTip("Export the H/V curve + per-window data as JSON or CSV.")
        self._save_pdf_button.setEnabled(False)
        self._export_button.setEnabled(False)

        self._minfreq_label = QLabel("", self)
        self._minfreq_label.setStyleSheet("color: #9aa4af; font-style: italic;")
        self._same_response_label = QLabel("", self)
        self._same_response_label.setWordWrap(True)
        self._same_response_label.setStyleSheet("color: #c9b36b;")
        # A word-wrapped label reports a tall ``heightForWidth`` minimum when
        # squeezed, which can transiently inflate this tab page's minimum and
        # pin the central area larger than the 1600x1000 default (the layout
        # trap). Cap both info labels and let them shrink horizontally so they
        # never balloon the minimum height.
        for lbl in (self._minfreq_label, self._same_response_label):
            lbl.setMaximumHeight(34)
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        form = QFormLayout()
        form.setContentsMargins(6, 1, 6, 1)
        form.setVerticalSpacing(2)
        dev_row = QHBoxLayout()
        dev_row.addWidget(self._device_combo, stretch=1)
        dev_row.addWidget(QLabel("Station:"))
        dev_row.addWidget(self._station_combo, stretch=1)
        dev_row.addWidget(self._group_label, stretch=2)
        form.addRow("Device:", _wrap(dev_row))

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
        params.addStretch(1)
        params.addWidget(self._prepost_button)
        params.addWidget(self._save_pdf_button)
        params.addWidget(self._export_button)
        params.addWidget(self._start_button)
        form.addRow("Params:", _wrap(params))

        # Advanced row (kept on its own line so the params row does not
        # overflow). The Ignored-policy wrapper lets it shrink.
        adv = QHBoxLayout()
        adv.addWidget(QLabel("horizontal:"))
        adv.addWidget(self._horizontal_combo)
        adv.addWidget(QLabel("detrend:"))
        adv.addWidget(self._detrend_combo)
        adv.addWidget(QLabel("reject n:"))
        adv.addWidget(self._rejection_n_spin)
        adv.addWidget(QLabel("freqs:"))
        adv.addWidget(self._resample_spin)
        adv.addWidget(self._psd_smooth_check)
        adv.addWidget(QLabel("PSD b:"))
        adv.addWidget(self._psd_b_spin)
        adv.addStretch(1)
        form.addRow("More:", _wrap(adv))

        # --- archive row (Stage C) ---------------------------------------
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
            "Run HVSR over an archived time range for the selected 3C station "
            "(uses the same settings; the result is marked as archive-sourced)."
        )
        archive = QHBoxLayout()
        archive.addWidget(QLabel("from"))
        archive.addWidget(self._archive_start)
        archive.addWidget(QLabel("to"))
        archive.addWidget(self._archive_end)
        archive.addWidget(self._archive_button)
        archive.addStretch(1)
        form.addRow("Archive:", _wrap(archive))

        # --- plots --------------------------------------------------------
        # Decimated log axes so the dense 5-10 / 50-100 Hz minor-tick labels
        # never collide (shared with the standalone PSD tab).
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

        self._psd_plot = pg.PlotWidget(
            self, axisItems={"bottom": DecimatedLogAxisItem(orientation="bottom")}
        )
        self._psd_plot.setBackground("#101418")
        self._psd_plot.setLogMode(x=True, y=False)
        self._psd_plot.showGrid(x=True, y=True, alpha=0.25)
        self._psd_plot.setLabel("left", "PSD", units="dB rel. counts²/Hz")
        self._psd_plot.setLabel("bottom", "Frequency", units="Hz")
        self._psd_plot.setMenuEnabled(False)
        self._psd_legend = self._psd_plot.addLegend(offset=(-10, 10))

        plots = QSplitter(Qt.Orientation.Vertical, self)
        plots.addWidget(self._hv_plot)
        plots.addWidget(self._psd_plot)
        plots.setStretchFactor(0, 3)
        plots.setStretchFactor(1, 2)

        # --- window list (override) --------------------------------------
        self._window_list = QListWidget(self)
        self._window_list.setToolTip(
            "Each accumulated window with its auto verdict. Tick/untick to "
            "include/exclude — your override survives every recompute."
        )

        body = QSplitter(Qt.Orientation.Horizontal, self)
        body.addWidget(plots)
        body.addWidget(self._window_list)
        body.setStretchFactor(0, 4)
        body.setStretchFactor(1, 1)

        # Keep every plot / list able to shrink small so this widget's
        # minimumSizeHint stays modest. It is tabbed behind PSD, and a tab
        # page's minimum pins the whole tab group's minimum (the layout trap
        # already paid for once) — a compact minimum guarantees HVSR never
        # forces the central area (and thus the 1600x1000 default) larger.
        for plot in (self._hv_plot, self._psd_plot):
            plot.setMinimumSize(40, 40)
        self._window_list.setMinimumHeight(40)

        # --- status -------------------------------------------------------
        self._status_label = QLabel("Idle.", self)
        self._sesame_label = QLabel("", self)
        status = QHBoxLayout()
        status.addWidget(self._status_label, stretch=2)
        status.addWidget(self._sesame_label, stretch=3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(1)
        layout.addWidget(self._title)
        layout.addLayout(form)
        layout.addWidget(self._same_response_label)
        layout.addWidget(self._minfreq_label)
        layout.addWidget(body, stretch=1)
        layout.addLayout(status)

        self._update_minfreq_label()

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
        self._device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._station_combo.currentIndexChanged.connect(self._on_station_changed)
        self._wl_spin.valueChanged.connect(self._update_minfreq_label)
        self._start_button.clicked.connect(self._on_start_clicked)
        self._archive_button.clicked.connect(self._on_archive_clicked)
        self._prepost_button.clicked.connect(self._on_prepost_toggled)
        self._save_pdf_button.clicked.connect(self._on_save_report)
        self._export_button.clicked.connect(self._on_export_raw)
        self._window_list.itemChanged.connect(self._on_window_item_changed)

        self._hvsr.hvsrUpdated.connect(self._on_hvsr_updated)
        self._hvsr.hvsrPsdReady.connect(self._on_early_psd)
        self._hvsr.hvsrWindowCount.connect(self._on_window_count)
        self._hvsr.hvsrStateChanged.connect(self._on_state_changed)
        self._hvsr.hvsrBackpressure.connect(self._on_backpressure)
        self._hvsr.hvsrMeasurementStopped.connect(self._on_stopped)
        self._psd_smooth_check.toggled.connect(self._on_psd_smooth_toggled)

    # ------------------------------------------------------------------
    # Public accessors (tests)
    # ------------------------------------------------------------------
    def selected_group(self) -> dict[str, str] | None:
        device = self._device_combo.currentData()
        station = self._station_combo.currentData()
        if not isinstance(device, str) or not isinstance(station, str):
            return None
        return self._groups.get(device, {}).get(station)

    def start_enabled(self) -> bool:
        return self._start_button.isEnabled()

    def is_running(self) -> bool:
        """Whether a LIVE measurement is active (archive runs are one-shot)."""
        return self._live_running

    def current_settings(self) -> HvsrSettings:
        return HvsrSettings(
            window_length_s=float(self._wl_spin.value()),
            konno_ohmachi_b=float(self._b_spin.value()),
            freqmin_hz=float(self._fmin_spin.value()),
            freqmax_hz=float(self._fmax_spin.value()),
            horizontal_method=str(self._horizontal_combo.currentData()),
            rejection_method=str(self._rejection_combo.currentData()),
            rejection_n=float(self._rejection_n_spin.value()),
            detrend=str(self._detrend_combo.currentData()),
            resample_n=int(self._resample_spin.value()),
            psd_smoothing=self._psd_smooth_check.isChecked(),
            psd_konno_ohmachi_b=float(self._psd_b_spin.value()),
        )

    def status_text(self) -> str:
        return self._status_label.text()

    def sesame_text(self) -> str:
        return self._sesame_label.text()

    # ------------------------------------------------------------------
    # Device / group selection
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
        self._update_start_enabled()

    def _on_device_changed(self, _index: int) -> None:
        self._refresh_stations()

    def _on_station_changed(self, _index: int) -> None:
        self._update_group_label()
        self._update_start_enabled()

    def _update_group_label(self) -> None:
        group = self.selected_group()
        if group is None:
            self._group_label.setText("no 3-component station")
        else:
            self._group_label.setText(
                "  ".join(f"{c}={group[c].split('.')[-1]}" for c in ("Z", "N", "E"))
            )

    def _update_start_enabled(self) -> None:
        if self._measurement_id is not None:
            self._start_button.setEnabled(True)  # always allow Stop
            return
        ok = self.selected_group() is not None
        self._start_button.setEnabled(ok)
        if not ok:
            self._start_button.setToolTip(
                "Select a device + station with 3 components (Z + 2 horizontals)."
            )
        else:
            self._start_button.setToolTip("")

    def _update_minfreq_label(self) -> None:
        wl = float(self._wl_spin.value())
        f_min = 10.0 / wl if wl > 0 else float("inf")
        self._minfreq_label.setText(
            f"Lowest reliably-resolvable frequency for a {wl:g} s window: "
            f"f₀ > {f_min:.2g} Hz (SESAME). Lengthen the window to resolve lower f₀."
        )

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------
    def _on_start_clicked(self) -> None:
        if self._live_running:
            self._hvsr.stop_measurement(self._measurement_id)
            return
        group = self.selected_group()
        device = self._device_combo.currentData()
        if group is None or not isinstance(device, str):
            return
        self._clear_plots()
        self._active_group = dict(group)
        self._measurement_id = self._hvsr.start_measurement(device, group, self.current_settings())
        self._live_running = True
        self._start_button.setText("Stop measurement")
        self._set_controls_enabled(False)
        self._window_list.setEnabled(True)
        self._title.setText(f"HVSR — {device} / {self._station_combo.currentData()} (live)")
        self._status_label.setText("Measuring… accumulating windows.")

    def set_archive_request_handler(self, handler: ArchiveHandler) -> None:
        """Install the host callback that runs HVSR over an archived range."""
        self._archive_handler = handler

    def prefill_archive(
        self,
        device: str,
        group: dict[str, str],
        t_start_epoch: float,
        t_end_epoch: float,
    ) -> None:
        """Pre-select the device/station + archive interval (Archive tab hand-off).

        Selects the device/station combos so the chosen 3C station matches
        ``group`` and fills the archive start/end fields with the handed-off
        interval. Does **not** auto-run — the user reviews the settings and
        clicks "Run on archive". The interval round-trips exactly: the fields
        are set as UTC wall-clock, the same convention ``_on_archive_clicked``
        reads back.
        """
        idx = self._device_combo.findData(device)
        if idx >= 0:
            self._device_combo.setCurrentIndex(idx)  # cascades _refresh_stations
        z_nslc = group.get("Z")
        if z_nslc is not None:
            sidx = self._station_combo.findData(_station_key(z_nslc))
            if sidx >= 0:
                self._station_combo.setCurrentIndex(sidx)
        for edit, epoch in (
            (self._archive_start, t_start_epoch),
            (self._archive_end, t_end_epoch),
        ):
            u = QDateTime.fromSecsSinceEpoch(int(epoch), QTimeZone.utc())
            edit.setDateTime(QDateTime(u.date(), u.time()))

    def _on_archive_clicked(self) -> None:
        if self._archive_handler is None:
            return
        group = self.selected_group()
        device = self._device_combo.currentData()
        if group is None or not isinstance(device, str):
            return
        t_start = UTCDateTime(self._archive_start.dateTime().toString("yyyy-MM-ddTHH:mm:ss"))
        t_end = UTCDateTime(self._archive_end.dateTime().toString("yyyy-MM-ddTHH:mm:ss"))
        if t_end <= t_start:
            QMessageBox.warning(self, "HVSR archive", "The end time must be after the start time.")
            return
        # Starting a new run supersedes any prior measurement.
        self._live_running = False
        self._start_button.setText("Start measurement")
        self._set_controls_enabled(True)
        self._clear_plots()
        self._active_group = dict(group)
        measurement_id = self._archive_handler(
            device, group, t_start, t_end, self.current_settings()
        )
        if not measurement_id:
            self._measurement_id = None
            self._status_label.setText("No archived data with full 3C coverage in that range.")
            return
        self._measurement_id = measurement_id
        self._window_list.setEnabled(True)
        station = self._station_combo.currentData()
        self._title.setText(f"HVSR — {device} / {station} (archive {t_start} to {t_end})")
        self._status_label.setText("Computing HVSR over the archived range…")

    @Slot(str)
    def _on_stopped(self, measurement_id: str) -> None:
        if measurement_id != self._measurement_id:
            return
        self._measurement_id = None
        self._live_running = False
        self._start_button.setText("Start measurement")
        self._set_controls_enabled(True)
        # The list rows stay visible (the last result), but the overrides are
        # inert once the measurement is gone — disable so the UI is honest.
        self._window_list.setEnabled(False)
        self._update_start_enabled()
        self._status_label.setText("Stopped.")

    def _set_controls_enabled(self, enabled: bool) -> None:
        for w in (
            self._device_combo,
            self._station_combo,
            self._wl_spin,
            self._b_spin,
            self._fmin_spin,
            self._fmax_spin,
            self._rejection_combo,
        ):
            w.setEnabled(enabled)

    # ------------------------------------------------------------------
    # Report / export (Stage D)
    # ------------------------------------------------------------------
    def _report_context(self) -> object:
        from seedlink_dashboard.storage.hvsr_report import ReportContext

        result = self._result
        assert result is not None
        # Just the time span — the report adds the provenance suffix once
        # (the period_label must NOT itself repeat it).
        period = f"{result.t_start} to {result.t_end}"
        group = self._active_group or dict.fromkeys(("Z", "N", "E"), result.station_key)
        return ReportContext(
            nslc_by_component=dict(group),
            period_label=period,
            generated_at=str(UTCDateTime()),
        )

    def _on_save_report(self) -> None:
        if self._result is None or self._result.n_windows_valid == 0:
            QMessageBox.warning(self, "HVSR report", "No valid HVSR result to report yet.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save HVSR report", "hvsr_report.pdf", "PDF (*.pdf)"
        )
        if not path:
            return
        from pathlib import Path

        from seedlink_dashboard.storage.hvsr_report import HvsrExportError, write_hvsr_pdf

        try:
            write_hvsr_pdf(self._result, Path(path), self._report_context())  # type: ignore[arg-type]
        except HvsrExportError as exc:
            QMessageBox.warning(self, "HVSR report", str(exc))

    def _on_export_raw(self) -> None:
        if self._result is None or self._result.n_windows_valid == 0:
            QMessageBox.warning(self, "HVSR export", "No valid HVSR result to export yet.")
            return
        path, selected = QFileDialog.getSaveFileName(
            self, "Export HVSR data", "hvsr_data.json", "JSON (*.json);;CSV (*.csv)"
        )
        if not path:
            return
        from pathlib import Path

        from seedlink_dashboard.storage.hvsr_report import (
            HvsrExportError,
            export_hvsr_csv,
            export_hvsr_json,
        )

        ctx = self._report_context()
        is_csv = path.lower().endswith(".csv") or "csv" in selected.lower()
        try:
            if is_csv:
                export_hvsr_csv(self._result, Path(path), ctx)  # type: ignore[arg-type]
            else:
                export_hvsr_json(self._result, Path(path), ctx)  # type: ignore[arg-type]
        except HvsrExportError as exc:
            QMessageBox.warning(self, "HVSR export", str(exc))

    def _on_prepost_toggled(self) -> None:
        self._show_post = not self._show_post
        self._prepost_button.setText(
            "Showing: post-rejection" if self._show_post else "Showing: pre-rejection"
        )
        if self._result is not None:
            self._draw_hv(self._result)

    # ------------------------------------------------------------------
    # Engine signal handlers
    # ------------------------------------------------------------------
    @Slot(int, int)
    def _on_window_count(self, n_valid: int, n_total: int) -> None:
        # Show live accumulation while running. Before the first result this
        # is the only status; after it, surface windows that have accumulated
        # since the last compute (recompute may lag/skip under load — rule 11)
        # so the count never silently freezes at the last computed result.
        if self._measurement_id is None:
            return
        if self._result is None or n_total > self._result.n_windows_total:
            self._status_label.setText(f"Measuring… {n_total} windows ({n_valid} valid).")

    @Slot(str, int)
    def _on_backpressure(self, measurement_id: str, skipped: int) -> None:
        if measurement_id != self._measurement_id:
            return
        self._title.setToolTip(f"recompute throttled — {skipped} skipped (busy)")

    @Slot(str, str)
    def _on_state_changed(self, measurement_id: str, state: str) -> None:
        if measurement_id != self._measurement_id:
            return
        if state == "computing":
            self._title.setToolTip("updating…")

    @Slot(object)
    def _on_hvsr_updated(self, result: object) -> None:
        if not isinstance(result, HvsrResult):
            return
        self._result = result
        self._early_psd_shown = False  # the full result's PSD now owns the panel
        exportable = result.n_windows_valid > 0
        self._save_pdf_button.setEnabled(exportable)
        self._export_button.setEnabled(exportable)
        self._same_response_label.setText(result.same_response_detail)
        self._draw_hv(result)
        self._draw_psd({"Z": result.psd_z, "N": result.psd_n, "E": result.psd_e})
        self._rebuild_window_list(result)
        self._update_status(result)

    @Slot(object)
    def _on_early_psd(self, psds: object) -> None:
        """Draw the early raw PSD (before the first full HVSR compute).

        FIX 3: the 3-channel PSD appears as soon as one window exists, rather
        than waiting for the (slow, JIT-bearing) first HVSR compute. Once a
        full result lands, that result's (smoothed) PSD owns the panel and the
        early raw updates are ignored.
        """
        if self._result is not None or not isinstance(psds, dict):
            return
        self._draw_psd(psds)
        self._early_psd_shown = True

    def _on_psd_smooth_toggled(self, _checked: bool) -> None:
        # Re-run with the new smoothing only matters on the next compute; for
        # an existing live measurement the engine picks it up via current
        # settings on restart. Nothing to redraw immediately here.
        pass

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------
    def _clear_plots(self) -> None:
        for plot, legend in (
            (self._hv_plot, self._hv_legend),
            (self._psd_plot, self._psd_legend),
        ):
            plot.clear()
            if legend is not None:
                legend.clear()
        self._window_list.clear()
        self._row_to_window = []
        self._result = None
        self._early_psd_shown = False
        self._save_pdf_button.setEnabled(False)
        self._export_button.setEnabled(False)

    def _draw_hv(self, result: HvsrResult) -> None:
        self._hv_plot.clear()
        # ``clear()`` removes the curves but NOT the legend rows; clear it too
        # or entries pile up on every recompute.
        if self._hv_legend is not None:
            self._hv_legend.clear()
        freq = result.frequency
        mask = freq > 0
        f = freq[mask]
        if f.size == 0:
            return
        # Unreliable low-frequency SESAME strip: f < 10 / window length.
        f_unreliable = result.settings.min_reliable_frequency_hz()
        if f_unreliable > f[0]:
            region = pg.LinearRegionItem(
                values=(np.log10(max(f[0], 1e-6)), np.log10(f_unreliable)),
                movable=False,
                brush=_UNRELIABLE_BRUSH,
            )
            region.setZValue(-20)
            self._hv_plot.addItem(region)

        # Per-window curves faint behind the mean.
        for i in range(result.window_curves.shape[0]):
            self._hv_plot.plot(f, result.window_curves[i][mask], pen=_WINDOW_PEN)

        if self._show_post:
            mean = result.mean_curve
            sigma = result.lognormal_sigma
            label = "mean (post-rejection)"
        else:
            # Pre-rejection: aggregate ALL window curves (display-side).
            log_all = np.log(np.maximum(result.window_curves, 1e-12))
            mean = np.exp(np.mean(log_all, axis=0))
            sigma = np.std(log_all, axis=0)
            label = "mean (pre-rejection)"

        if np.any(np.isfinite(mean)):
            upper = mean * np.exp(sigma)
            lower = mean * np.exp(-sigma)
            band_top = self._hv_plot.plot(f, upper[mask], pen=None)
            band_bot = self._hv_plot.plot(f, lower[mask], pen=None)
            fill = pg.FillBetweenItem(band_top, band_bot, brush=_SIGMA_BRUSH)
            self._hv_plot.addItem(fill)
            pen = _MEAN_PEN if self._show_post else _PRE_MEAN_PEN
            self._hv_plot.plot(f, mean[mask], pen=pen, name=label)

            # f0 marker + dispersion strip.
            if np.isfinite(result.f0_hz) and result.f0_hz > 0:
                lo = max(result.f0_hz - result.f0_sigma, f[0] if f.size else result.f0_hz)
                hi = result.f0_hz + result.f0_sigma
                strip = pg.LinearRegionItem(
                    values=(np.log10(max(lo, 1e-6)), np.log10(max(hi, lo + 1e-6))),
                    movable=False,
                    brush=_F0_STRIP_BRUSH,
                )
                strip.setZValue(-10)
                self._hv_plot.addItem(strip)
                line = pg.InfiniteLine(
                    pos=np.log10(result.f0_hz),
                    angle=90,
                    pen=_F0_PEN,
                    label=f"f₀={result.f0_hz:.2f} Hz",
                    labelOpts={"position": 0.95},
                )
                self._hv_plot.addItem(line)

    def _draw_psd(self, psds: dict[str, tuple[np.ndarray, np.ndarray]]) -> None:
        self._psd_plot.clear()
        if self._psd_legend is not None:
            self._psd_legend.clear()
        for comp in ("Z", "N", "E"):
            freqs, db = psds.get(comp, (np.empty(0), np.empty(0)))
            if freqs.size == 0:
                continue
            mask = freqs > 0
            self._psd_plot.plot(freqs[mask], db[mask], pen=_PSD_PENS[comp], name=comp)

    def _rebuild_window_list(self, result: HvsrResult) -> None:
        # Rebuild only when the window set changed; otherwise update in place.
        if list(result.window_ids) != self._row_to_window:
            self._window_list.blockSignals(True)
            self._window_list.clear()
            self._row_to_window = list(result.window_ids)
            for _wid in result.window_ids:
                item = QListWidgetItem()
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                self._window_list.addItem(item)
            self._window_list.blockSignals(False)
        self._window_list.blockSignals(True)
        for row, wid in enumerate(self._row_to_window):
            item = self._window_list.item(row)
            if item is None:
                continue
            accepted = bool(result.effective_mask[row])
            auto = bool(result.auto_accept_mask[row])
            overridden = bool(result.manual_override_mask[row])
            verdict = "accepted" if accepted else "rejected"
            note = " (override)" if overridden else (" (auto)" if not auto else "")
            item.setText(f"#{wid}  {verdict}{note}")
            item.setCheckState(Qt.CheckState.Checked if accepted else Qt.CheckState.Unchecked)
            item.setForeground(pg.mkColor("#7ac74f") if accepted else pg.mkColor("#e0526b"))
        self._window_list.blockSignals(False)

    def _update_status(self, result: HvsrResult) -> None:
        f0 = result.f0_hz
        period = (1.0 / f0) if np.isfinite(f0) and f0 > 0 else float("nan")
        self._status_label.setText(
            f"N {result.n_windows_valid}/{result.n_windows_total} valid   "
            f"f₀ = {f0:.3f} ± {result.f0_sigma:.3f} Hz   T₀ = {period:.3f} s"
        )
        rel_ok = sum(c.passed for c in result.reliability)
        cla_ok = sum(c.passed for c in result.clarity)
        rel_mark = "✓" if result.reliability_passed else "✗"
        cla_mark = "✓" if result.clarity_passed else "✗"
        self._sesame_label.setText(
            f"SESAME  reliability {rel_mark} {rel_ok}/3   clarity {cla_mark} {cla_ok}/6"
        )
        tip_lines = ["Reliability:"]
        tip_lines += [
            f"  {'✓' if c.passed else '✗'} {c.name} — {c.detail}" for c in result.reliability
        ]
        tip_lines.append("Clarity:")
        tip_lines += [f"  {'✓' if c.passed else '✗'} {c.name} — {c.detail}" for c in result.clarity]
        self._sesame_label.setToolTip("\n".join(tip_lines))

    # ------------------------------------------------------------------
    # Manual override
    # ------------------------------------------------------------------
    def _on_window_item_changed(self, item: QListWidgetItem) -> None:
        row = self._window_list.row(item)
        if not (0 <= row < len(self._row_to_window)) or self._measurement_id is None:
            return
        wid = self._row_to_window[row]
        accepted = item.checkState() == Qt.CheckState.Checked
        # Only act on a real user change (the displayed state lags the result).
        if (
            self._result is not None
            and row < self._result.effective_mask.shape[0]
            and bool(self._result.effective_mask[row]) == accepted
        ):
            return
        self._hvsr.set_window_override(self._measurement_id, wid, accepted)


def _wrap(layout: QHBoxLayout) -> QWidget:
    """Wrap an HBox in a widget so it can sit in a QFormLayout field.

    The wrapper uses an ``Ignored`` horizontal size policy so the controls
    row's wide preferred width does NOT propagate as a hard MINIMUM to the
    dock / central area. This widget is tabbed behind PSD, and a tabbed
    page's ``minimumSizeHint`` pins the whole tab group's minimum (the
    layout trap fixed once already for the detail pane) — Ignored lets the
    row shrink (the controls stay reachable at the default 1600px width)
    rather than forcing the central minimum out.
    """
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    return w
