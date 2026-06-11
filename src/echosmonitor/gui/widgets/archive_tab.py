"""Central "Archive" tab — session browser + static window view (M3-A).

Sessions are the archive unit (rule 14), so the browser is session-centric:
a searchable/date-filterable session list (crash-dirty sessions visibly
flagged), a per-session device/station tree with coverage strips, and the
static 3C + spectrogram view over a chosen interval. Selecting a CLOSED
session works with no live engine context at all — its data lives under
``<base>/<project>/`` where the live readers cannot reach it (the M2-B
NOTE in ROADMAP); the browser carries each session's root + ``archive.db``
explicitly (:class:`~echosmonitor.core.models.SessionEntry`).

Every read happens off the GUI thread (rule 1): session discovery and the
per-session tree/coverage on the
:class:`~echosmonitor.core.archive_browser_loader.ArchiveBrowserLoader`,
the waveform/spectrogram load on :class:`~echosmonitor.core.
archive_window_loader.ArchiveWindowLoader` — this widget only emits
requests and renders results with cheap ``setData`` calls. The coverage
strip under the interval editors is sliced client-side from the already-
loaded session coverage (pure arithmetic, no DB).

Archive access is **read-only** (CLAUDE.md rule 8). The browser never
invents placeholder dates: an un-archived stream shows an honest empty
state, and the default interval is always a recent slice **within the
real per-session coverage**.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
import structlog
from obspy import UTCDateTime
from PySide6.QtCore import QDate, QDateTime, QRectF, Qt, QTimeZone, Signal, SignalInstance, Slot
from PySide6.QtGui import QBrush, QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.archive_browser_loader import (
    SessionDetailResult,
    SessionListResult,
    StationCoverage,
)
from echosmonitor.core.archive_window_loader import (
    ArchiveWindowResult,
)
from echosmonitor.core.models import SessionEntry
from echosmonitor.gui.widgets.pane_header import (
    PANE_TITLE_OBJECT_NAME,
    PANE_TITLE_STYLE,
)
from echosmonitor.gui.widgets.spectrogram_view import (
    ColorMode,
    colorize,
    levels_for,
)

if TYPE_CHECKING:
    from echosmonitor.core.archive_browser_loader import ArchiveBrowserLoader

_log = structlog.get_logger(__name__)

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

_NO_STREAM_TITLE = "Archive — no station selected"
_NO_DATA_TEXT = "No archived data for this stream."
_NO_SESSION_TEXT = "Select a session to browse its archive."

# Session-list status colours: crash-dirty rows must be unmistakable
# (rule 14 / M2-C), the open (recording) session clearly live.
_DIRTY_BRUSH = QBrush(QColor("#e0a93a"))
_OPEN_BRUSH = QBrush(QColor("#7ee081"))

# Qt.ItemDataRole.UserRole payloads on tree rows.
_ENTRY_ROLE = Qt.ItemDataRole.UserRole
_STATION_ROLE = Qt.ItemDataRole.UserRole

# Default load window: the last 10 minutes of available data (clamped to the
# real per-session coverage). A sensible recent slice, never a placeholder.
_DEFAULT_WINDOW_S = 600.0


def _qdt_from_epoch(epoch: float) -> QDateTime:
    """Build a UTC ``QDateTime`` from a POSIX epoch (whole seconds shown)."""
    return QDateTime.fromSecsSinceEpoch(int(epoch), QTimeZone.utc())


def _epoch_from_qdt(qdt: QDateTime) -> float:
    """Read a ``QDateTimeEdit`` value as a UTC wall-clock epoch.

    Mirrors :class:`HvsrWidget`'s archive-field interpretation exactly (the
    displayed naive wall-clock is treated as UTC) so an interval handed off to
    HVSR round-trips to the same instant.
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
    """The central Archive tab (session browser + static view + tools)."""

    # device, group({"Z","N","E": nslc}), t_start_epoch, t_end_epoch
    loadRequested = Signal(str, object, float, float)  # noqa: N815
    hvsrRequested = Signal(str, object, float, float)  # noqa: N815
    unitChangeRequested = Signal(str)  # unit code  # noqa: N815

    def __init__(
        self,
        browser: ArchiveBrowserLoader,
        base_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # The browser loader is owned by the main window (it joins the
        # thread on shutdown); this widget only emits requests into it
        # and renders the results (rule 1).
        self._browser = browser
        self._base_root = str(base_root)
        self._entries: list[SessionEntry] = []
        self._detail: SessionDetailResult | None = None
        self._selected_station: StationCoverage | None = None
        self._list_token = 0
        self._detail_token = 0

        # The currently-loaded window: the selection it was loaded for, the
        # displayed (x, y) per component in the current unit, and the window
        # bounds. Cursors read amplitude from ``_display`` (rule 11: a cheap
        # nearest-index GUI-thread read, no compute).
        self._loaded_device = ""
        self._loaded_group: dict[str, str] = {}
        self._win_t_start = 0.0
        self._win_t_end = 0.0
        # The request currently in flight, committed into the _loaded_*
        # fields only when its result actually renders (show_result) — a
        # failed/empty request must not rebind what exports and hand-offs
        # say about the window still on screen.
        self._pending_window: tuple[str, dict[str, str], float, float] | None = None
        self._display: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        # Unit label PER COMPONENT (M3-B): a gappy component is skipped by
        # deconvolution (an FFT response removal would smear its NaNs) and
        # stays in counts while its siblings switch — one global label
        # would lie about whichever side it doesn't match.
        self._unit_labels: dict[str, str] = {}
        self._cursor_pos: dict[str, float] = {"A": 0.0, "B": 0.0}
        self._cursor_lines: dict[str, list[pg.InfiniteLine]] = {"A": [], "B": []}
        self._suppress_cursor = False
        self._suppress_unit = False

        self._build_ui()
        self._wire()
        # Launch discovery: closed sessions are browsable from the first
        # show, no acquisition required (rule 13 keeps launch idle).
        self.refresh_sessions()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._title = QLabel(_NO_STREAM_TITLE, self)
        self._title.setObjectName(PANE_TITLE_OBJECT_NAME)
        self._title.setStyleSheet(PANE_TITLE_STYLE)

        # --- session browser (left panel) ---------------------------------
        self._search_edit = QLineEdit(self)
        self._search_edit.setPlaceholderText("Filter by project name…")
        self._search_edit.setClearButtonEnabled(True)
        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_button.setToolTip("Re-scan the archive root for sessions.")

        self._date_check = QCheckBox("Date:", self)
        self._date_from = QDateEdit(self)
        self._date_from.setCalendarPopup(True)
        self._date_from.setDisplayFormat("yyyy-MM-dd")
        self._date_to = QDateEdit(self)
        self._date_to.setCalendarPopup(True)
        self._date_to.setDisplayFormat("yyyy-MM-dd")
        today = QDate.currentDate()
        self._date_from.setDate(today.addDays(-30))
        self._date_to.setDate(today)
        self._date_from.setEnabled(False)
        self._date_to.setEnabled(False)

        self._session_tree = QTreeWidget(self)
        self._session_tree.setHeaderLabels(["Project", "Started", "Status"])
        self._session_tree.setRootIsDecorated(False)
        self._session_tree.setUniformRowHeights(True)
        self._session_tree.setToolTip(
            "Recorded sessions found under the archive root (newest first)."
        )

        self._station_tree = QTreeWidget(self)
        self._station_tree.setHeaderLabels(["Device / station", "Coverage"])
        self._station_tree.setUniformRowHeights(False)
        self._station_tree.setToolTip(
            "3-component stations recorded in the selected session;"
            " green = data within the session span, dark = gap."
        )
        self._browser_status = QLabel(_NO_SESSION_TEXT, self)
        self._browser_status.setStyleSheet("color: #9aa4af; font-style: italic;")
        self._browser_status.setWordWrap(True)

        left = QWidget(self)
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(3)
        search_row = QHBoxLayout()
        search_row.addWidget(self._search_edit, stretch=1)
        search_row.addWidget(self._refresh_button)
        left_box.addLayout(search_row)
        date_row = QHBoxLayout()
        date_row.addWidget(self._date_check)
        date_row.addWidget(self._date_from, stretch=1)
        date_row.addWidget(QLabel("→"))
        date_row.addWidget(self._date_to, stretch=1)
        left_box.addLayout(date_row)
        left_box.addWidget(self._session_tree, stretch=2)
        left_box.addWidget(self._station_tree, stretch=3)
        left_box.addWidget(self._browser_status)
        # The browser column must shrink with the tab (the HVSR layout trap):
        # no hard minimums beyond what the trees need to stay usable.
        for w in (self._search_edit, self._date_from, self._date_to):
            w.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        left.setMinimumWidth(120)

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
        wide: QWidget
        for wide in (
            self._start_edit,
            self._end_edit,
            self._group_label,
        ):
            wide.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        # --- form ---------------------------------------------------------
        form = QFormLayout()
        form.setContentsMargins(6, 2, 6, 2)
        form.setVerticalSpacing(3)
        form.addRow("Station:", self._group_label)

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

        right = QWidget(self)
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(2)
        right_box.addLayout(form)
        right_box.addWidget(self._extent_label)
        right_box.addWidget(self._coverage)
        right_box.addWidget(self._view_container, stretch=1)

        self._browser_split = QSplitter(Qt.Orientation.Horizontal, self)
        self._browser_split.addWidget(left)
        self._browser_split.addWidget(right)
        self._browser_split.setChildrenCollapsible(False)
        self._browser_split.setStretchFactor(0, 0)
        self._browser_split.setStretchFactor(1, 1)
        self._browser_split.setSizes([260, 900])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        layout.addWidget(self._title)
        layout.addWidget(self._browser_split, stretch=1)

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
        self._export_button = QPushButton("Export PNG…", self)
        self._export_button.setEnabled(False)
        self._export_button.setToolTip("Save the rendered traces + spectrogram as a PNG image.")
        self._hvsr_button = QPushButton("Run HVSR on this window", self)
        self._hvsr_button.setEnabled(False)
        bar.addWidget(QLabel("Units:"))
        bar.addWidget(self._unit_combo)
        bar.addWidget(self._stacked_radio)
        bar.addWidget(self._overlaid_radio)
        bar.addWidget(QLabel("Cursor on:"))
        bar.addWidget(self._readout_combo)
        bar.addWidget(self._reset_button)
        bar.addStretch(1)
        bar.addWidget(self._export_button)
        bar.addWidget(self._hvsr_button)
        # The toolbar's long-text buttons + combos would otherwise pin this tab
        # page's minimum width and inflate the central QTabWidget minimum (the
        # layout trap HVSR already paid for). Let them shrink horizontally so
        # the page minimum tracks the (already-bounded, 40 px) plots.
        for w in (
            self._unit_combo,
            self._readout_combo,
            self._reset_button,
            self._export_button,
            self._hvsr_button,
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
        # Zoom/pan ergonomics (M3-B): browsing an archive window is a
        # time-axis activity — the mouse drives X only, and Y auto-fits
        # the data VISIBLE in the current X range (the "seismologist
        # zoom": zooming into a quiet stretch rescales amplitudes to it).
        # An accidental Y zoom losing the trace was the ergonomic trap.
        for plot in self._stacked_plots.values():
            plot.setMouseEnabled(x=True, y=False)
            plot.getViewBox().setAutoVisible(y=True)
            plot.enableAutoRange(axis="y")

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
        # NOT statically X-linked to the stacked plots: exactly one of the
        # two trace views is visible at a time, and pyqtgraph maps linked
        # ranges through each view's pixel geometry — a HIDDEN view's
        # degenerate geometry distorts the range it pushes back (measured
        # ~3 % drift per load). The time zoom is carried across the
        # Stacked/Overlaid switch by _on_layout_toggled instead.
        self._overlay_plot.setMouseEnabled(x=True, y=False)
        self._overlay_plot.getViewBox().setAutoVisible(y=True)
        self._overlay_plot.enableAutoRange(axis="y")

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
        # The spectrogram shares the traces' time axis: zoom/pan in either
        # stays in sync (M3-B ergonomics). Its Y is the frequency extent —
        # fixed per load in _render_spectrogram, never mouse-driven.
        self._spec_plot.setXLink(self._stacked_plots["Z"])
        self._spec_plot.setMouseEnabled(x=True, y=False)

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
        self._browser.sessionsListed.connect(
            self._on_sessions_listed, Qt.ConnectionType.QueuedConnection
        )
        self._browser.detailLoaded.connect(
            self._on_detail_loaded, Qt.ConnectionType.QueuedConnection
        )
        self._browser.listFailed.connect(
            self._on_list_failed, Qt.ConnectionType.QueuedConnection
        )
        self._browser.detailFailed.connect(
            self._on_detail_failed, Qt.ConnectionType.QueuedConnection
        )
        self._refresh_button.clicked.connect(lambda: self.refresh_sessions())
        self._search_edit.textChanged.connect(lambda _t: self._populate_sessions())
        self._date_check.toggled.connect(self._on_date_filter_toggled)
        self._date_from.dateChanged.connect(lambda _d: self._populate_sessions())
        self._date_to.dateChanged.connect(lambda _d: self._populate_sessions())
        self._session_tree.currentItemChanged.connect(self._on_session_selected)
        self._station_tree.currentItemChanged.connect(self._on_station_selected)
        # Coverage re-slices are pure in-memory arithmetic now — track edits.
        self._start_edit.dateTimeChanged.connect(lambda _d: self._update_coverage())
        self._end_edit.dateTimeChanged.connect(lambda _d: self._update_coverage())
        self._load_button.clicked.connect(self._on_load_clicked)
        self._unit_combo.currentIndexChanged.connect(self._on_unit_combo_changed)
        self._stacked_radio.toggled.connect(self._on_layout_toggled)
        self._readout_combo.currentIndexChanged.connect(lambda _i: self._refresh_readout())
        self._reset_button.clicked.connect(self._reset_view)
        self._export_button.clicked.connect(self._on_export_clicked)
        self._hvsr_button.clicked.connect(lambda: self._emit_handoff(self.hvsrRequested))

    # ------------------------------------------------------------------
    # Session list (discovery + filter)
    # ------------------------------------------------------------------
    @Slot()
    def refresh_sessions(self, _payload: object = None) -> None:
        """Re-scan the archive root for sessions (off the GUI thread).

        Public: the main window connects the engine's ``sessionChanged``
        here (queued) so a started/ended recording session appears
        without a manual refresh; the optional payload is ignored.
        """
        self._list_token = self._browser.request_sessions(self._base_root)

    @Slot(object)
    def _on_sessions_listed(self, payload: object) -> None:
        if not isinstance(payload, SessionListResult):
            return
        if payload.token != self._list_token:
            return  # stale (latest-wins) — drop
        self._entries = list(payload.entries)
        self._populate_sessions()

    @Slot(int, str)
    def _on_list_failed(self, token: int, message: str) -> None:
        if token != self._list_token:
            return
        self._browser_status.setText(f"Session scan failed: {message}")

    def _on_date_filter_toggled(self, checked: bool) -> None:
        self._date_from.setEnabled(checked)
        self._date_to.setEnabled(checked)
        self._populate_sessions()

    def _session_matches(self, entry: SessionEntry) -> bool:
        """Client-side name + date filter over the discovered list."""
        needle = self._search_edit.text().strip().lower()
        name = entry.record.project_name or "(monitoring)"
        if needle and needle not in name.lower():
            return False
        if self._date_check.isChecked():
            # ISO-8601 prefix compare: lexicographic == chronological.
            day = entry.record.started_at[:10]
            if not (
                self._date_from.date().toString("yyyy-MM-dd")
                <= day
                <= self._date_to.date().toString("yyyy-MM-dd")
            ):
                return False
        return True

    def _populate_sessions(self) -> None:
        """Render the filtered session list, preserving the selection."""
        prior = self.selected_session_entry()
        prior_key = (prior.db_path, prior.record.id) if prior is not None else None
        self._session_tree.blockSignals(True)
        self._session_tree.clear()
        restored = None
        shown = 0
        for entry in self._entries:
            if not self._session_matches(entry):
                continue
            item = self._session_item(entry)
            self._session_tree.addTopLevelItem(item)
            shown += 1
            if prior_key is not None and (entry.db_path, entry.record.id) == prior_key:
                restored = item
        if restored is not None:
            self._session_tree.setCurrentItem(restored)
        self._session_tree.blockSignals(False)
        if restored is None and prior_key is not None:
            # The selected session vanished from the filtered list —
            # clear the dependent panes honestly.
            self._clear_session_detail()
        elif restored is not None:
            # The selection survived a refresh, but its ROW may have
            # changed (e.g. Stop just closed the open session: ended_at
            # appears, so the span/coverage shown are stale). Re-request
            # the detail only on a real change — an unrelated refresh
            # must not clobber the user's station/interval selection.
            fresh = restored.data(0, _ENTRY_ROLE)
            shown_record = self._detail.entry.record if self._detail is not None else None
            if (
                isinstance(fresh, SessionEntry)
                and shown_record is not None
                and fresh.record != shown_record
            ):
                self._browser_status.setText("Refreshing session…")
                self._detail_token = self._browser.request_detail(fresh)
        if not self._entries:
            self._browser_status.setText(
                "No sessions found. Start a Recording session to create one."
            )
        elif shown == 0:
            self._browser_status.setText("No sessions match the current filter.")
        elif self._detail is None:
            self._browser_status.setText(_NO_SESSION_TEXT)

    @staticmethod
    def _session_item(entry: SessionEntry) -> QTreeWidgetItem:
        record = entry.record
        name = record.project_name or "(monitoring)"
        started = record.started_at[:19].replace("T", " ")
        if record.ended_at is None:
            status, brush = "● open", _OPEN_BRUSH
        elif record.closed_dirty:
            status, brush = "⚠ dirty", _DIRTY_BRUSH
        else:
            status, brush = "closed", None
        item = QTreeWidgetItem([name, started, status])
        if brush is not None:
            item.setForeground(2, brush)
        if record.closed_dirty:
            item.setToolTip(
                2,
                "Closed administratively after a crash — the recorded end "
                "time is the recovery time, not the real end of recording.",
            )
        item.setToolTip(0, f"{name}\ndevices: {', '.join(record.devices) or '—'}")
        item.setData(0, _ENTRY_ROLE, entry)
        return item

    def selected_session_entry(self) -> SessionEntry | None:
        """The selected session's entry (root + DB) — the main window
        resolves load requests against exactly this context (rule 14)."""
        item = self._session_tree.currentItem()
        if item is None:
            return None
        entry = item.data(0, _ENTRY_ROLE)
        return entry if isinstance(entry, SessionEntry) else None

    # ------------------------------------------------------------------
    # Per-session detail (device/station tree + coverage)
    # ------------------------------------------------------------------
    def _on_session_selected(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        del _previous
        if current is None:
            self._clear_session_detail()
            return
        entry = current.data(0, _ENTRY_ROLE)
        if not isinstance(entry, SessionEntry):
            return
        self._browser_status.setText("Loading session…")
        self._detail_token = self._browser.request_detail(entry)

    @Slot(object)
    def _on_detail_loaded(self, payload: object) -> None:
        if not isinstance(payload, SessionDetailResult):
            return
        if payload.token != self._detail_token:
            return  # stale (latest-wins) — drop
        self._detail = payload
        self._populate_stations()

    @Slot(int, str)
    def _on_detail_failed(self, token: int, message: str) -> None:
        if token != self._detail_token:
            return
        self._clear_session_detail()
        self._browser_status.setText(f"Session read failed: {message}")

    def _populate_stations(self) -> None:
        """Render the selected session's device/station tree + strips."""
        detail = self._detail
        self._station_tree.blockSignals(True)
        self._station_tree.clear()
        self._selected_station = None
        if detail is None:
            self._station_tree.blockSignals(False)
            self._reset_selection_panes()
            return
        span_start, span_end = detail.span
        by_device: dict[str, QTreeWidgetItem] = {}
        first_station: QTreeWidgetItem | None = None
        for station in detail.stations:
            parent = by_device.get(station.device)
            if parent is None:
                parent = QTreeWidgetItem([station.device, ""])
                parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                by_device[station.device] = parent
                self._station_tree.addTopLevelItem(parent)
                parent.setExpanded(True)
            child = QTreeWidgetItem([station.station, ""])
            child.setData(0, _STATION_ROLE, station)
            parent.addChild(child)
            strip = CoverageStrip(self._station_tree)
            strip.set_coverage(span_start, span_end, list(station.intervals))
            self._station_tree.setItemWidget(child, 1, strip)
            if first_station is None:
                first_station = child
        self._station_tree.blockSignals(False)
        if first_station is not None:
            self._station_tree.setCurrentItem(first_station)
        else:
            self._reset_selection_panes()
            self._browser_status.setText(
                "No 3-component stations recorded in this session."
            )
            return
        project = detail.entry.record.project_name or "(monitoring)"
        self._browser_status.setText(
            f"{project}: {len(detail.stations)} station(s), "
            f"{len(by_device)} device(s)."
        )

    def _clear_session_detail(self) -> None:
        # Invalidate any in-flight detail load (loader tokens start at 1):
        # a late detailLoaded for a session that vanished from the list
        # must not resurrect a ghost station tree (the ghost-row class —
        # qt-concurrency-auditor F2; Load on a ghost would fall back to
        # the live engine root and read the wrong archive).
        self._detail_token = -1
        self._detail = None
        self._selected_station = None
        self._station_tree.blockSignals(True)
        self._station_tree.clear()
        self._station_tree.blockSignals(False)
        self._reset_selection_panes()
        self._browser_status.setText(_NO_SESSION_TEXT)

    def _reset_selection_panes(self) -> None:
        self._group_label.setText("—")
        self._extent_label.setText(_NO_DATA_TEXT)
        self._coverage.set_coverage(0.0, 0.0, [])
        self._load_button.setEnabled(False)
        self._title.setText(_NO_STREAM_TITLE)

    # ------------------------------------------------------------------
    # Station selection → default interval + coverage
    # ------------------------------------------------------------------
    def _on_station_selected(
        self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None
    ) -> None:
        del _previous
        station = current.data(0, _STATION_ROLE) if current is not None else None
        if not isinstance(station, StationCoverage):
            self._selected_station = None
            self._reset_selection_panes()
            return
        self._selected_station = station
        self._group_label.setText(
            "  ".join(f"{c}={station.group[c].split('.')[-1]}" for c in ("Z", "N", "E"))
        )
        if not station.intervals:
            # Honest empty state — this session recorded nothing for the
            # stream (the project may still hold data from OTHER sessions;
            # those are browsable via their own rows).
            self._extent_label.setText(self._empty_extent_message(station.device))
            self._coverage.set_coverage(0.0, 0.0, [])
            self._load_button.setEnabled(False)
            self._title.setText(_NO_STREAM_TITLE)
            return
        cov_start = station.intervals[0][0]
        cov_end = station.intervals[-1][1]
        self._extent_label.setText(
            f"Archived (this session): {UTCDateTime(cov_start)} → {UTCDateTime(cov_end)}"
        )
        # Default to the last _DEFAULT_WINDOW_S within the real coverage.
        start_epoch = max(cov_start, cov_end - _DEFAULT_WINDOW_S)
        self._start_edit.blockSignals(True)
        self._end_edit.blockSignals(True)
        self._start_edit.setDateTime(_qdt_from_epoch(start_epoch))
        self._end_edit.setDateTime(_qdt_from_epoch(cov_end))
        self._start_edit.blockSignals(False)
        self._end_edit.blockSignals(False)
        self._load_button.setEnabled(True)
        self._update_coverage()
        detail = self._detail
        project = (
            (detail.entry.record.project_name or "(monitoring)")
            if detail is not None
            else "?"
        )
        self._title.setText(f"Archive — {project} / {station.device} / {station.station}")

    def selected_group(self) -> dict[str, str] | None:
        station = self._selected_station
        return dict(station.group) if station is not None else None

    def selected_device(self) -> str | None:
        station = self._selected_station
        return station.device if station is not None else None

    def _empty_extent_message(self, device: object) -> str:
        """Actionable empty-state text. Since M2-A, archives exist only
        when the user runs a Recording session (rule 13) — the old
        ``archive.enabled`` special case is gone with the config-driven
        writers it described."""
        if isinstance(device, str):
            return (
                f"No archived waveforms for '{device}' in this session. "
                f"Start a Recording session to archive data here."
            )
        return _NO_DATA_TEXT

    def _update_coverage(self) -> None:
        """Re-slice the selected station's session coverage to the interval.

        Pure GUI-thread arithmetic over the already-loaded coverage
        (rule 1: the DB was read once, on the browser worker) — clip each
        covered interval to the edited window.
        """
        station = self._selected_station
        if station is None:
            return
        t_start = _epoch_from_qdt(self._start_edit.dateTime())
        t_end = _epoch_from_qdt(self._end_edit.dateTime())
        if t_end <= t_start:
            self._coverage.set_coverage(t_start, t_end, [])
            return
        clipped = [
            (max(s, t_start), min(e, t_end))
            for s, e in station.intervals
            if min(e, t_end) > max(s, t_start)
        ]
        self._coverage.set_coverage(t_start, t_end, clipped)

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------
    def _on_load_clicked(self) -> None:
        group = self.selected_group()
        device = self.selected_device()
        if group is None or device is None:
            return
        t_start = _epoch_from_qdt(self._start_edit.dateTime())
        t_end = _epoch_from_qdt(self._end_edit.dateTime())
        if t_end <= t_start:
            self._status_label.setText("The end time must be after the start time.")
            return
        self._update_coverage()
        # Snapshot the selection the load was requested for; show_result
        # commits it into the _loaded_* fields when (and only when) the
        # window renders — the HVSR hand-off, unit decon and PNG export
        # all describe exactly the window on screen.
        self._pending_window = (device, dict(group), t_start, t_end)
        self._status_label.setText("Loading window…")
        self.loadRequested.emit(device, dict(group), t_start, t_end)

    # ------------------------------------------------------------------
    # Rendering the loaded window (called by the host on the GUI thread)
    # ------------------------------------------------------------------
    def show_result(self, result: ArchiveWindowResult) -> None:
        """Render a loaded window: 3C traces (counts) + spectrogram + cursors."""
        # Commit the request this result belongs to (snapshotted by
        # _on_load_clicked): the loaded-window metadata must describe what
        # is ON SCREEN, never a later request that failed or came back
        # empty — exports and hand-offs read it (review finding).
        if self._pending_window is not None:
            (
                self._loaded_device,
                self._loaded_group,
                self._win_t_start,
                self._win_t_end,
            ) = self._pending_window
            self._pending_window = None
        self._display = {}
        self._reset_unit_combo_to_counts()
        self._unit_labels = {}
        for comp in _COMPONENTS:
            # Relabel EVERY row per load: an absent component must not keep
            # the previous window's unit / "counts — gaps" label over an
            # empty plot (review finding).
            self._stacked_plots[comp].setLabel("left", f"{comp} (counts)")
            tr = next((t for t in result.traces if t.comp == comp), None)
            if tr is None:
                self._stacked_curves[comp].clear()
                self._overlay_curves[comp].clear()
                continue
            x = np.asarray(tr.x, dtype=np.float64)
            y = np.asarray(tr.y, dtype=np.float64)
            self._stacked_curves[comp].setData(x, y, connect="finite")
            self._overlay_curves[comp].setData(x, y, connect="finite")
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
            # Bound pan/zoom to the loaded window ± one window-width
            # (M3-B ergonomics): there is nothing to see further out, and
            # an over-pan to epoch-nowhere used to require a full reset.
            span = self._win_t_end - self._win_t_start
            for plot in (*self._stacked_plots.values(), self._overlay_plot, self._spec_plot):
                plot.setLimits(
                    xMin=self._win_t_start - span,
                    xMax=self._win_t_end + span,
                )
        self._place_cursors()
        self._hvsr_button.setEnabled(True)
        self._export_button.setEnabled(True)
        self._refresh_overlay_label()
        self._status_label.setText(
            f"Loaded {len(result.traces)} component(s) over "
            f"{UTCDateTime(self._win_t_start)} → {UTCDateTime(self._win_t_end)}."
        )

    def show_empty(self) -> None:
        self._display = {}
        self._pending_window = None  # the request found nothing — discard
        # Reset the unit state with the curves: empty axes must not keep
        # claiming "Velocity" / "counts — gaps" / "mixed units" from the
        # previous window (review major — the unit-honesty objective).
        self._unit_labels = {}
        self._reset_unit_combo_to_counts()
        for comp in _COMPONENTS:
            self._stacked_curves[comp].clear()
            self._overlay_curves[comp].clear()
            self._stacked_plots[comp].setLabel("left", f"{comp} (counts)")
        self._refresh_overlay_label()
        self._spec_image.clear()
        for which in ("A", "B"):
            for line in self._cursor_lines[which]:
                line.setVisible(False)
        self._readout_label.setText("")
        self._hvsr_button.setEnabled(False)
        self._export_button.setEnabled(False)
        self._unit_combo.setEnabled(False)
        self._status_label.setText("No archived data for this interval.")

    def show_failed(self, message: str) -> None:
        # The failed REQUEST never rendered: keep showing (and describing)
        # the previous window — discard the pending metadata so exports
        # and hand-offs keep naming what is actually on screen.
        self._pending_window = None
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
        # Per-component unit: a gappy sibling left in counts must not make
        # this component's readout claim the wrong unit (M3-B).
        unit = self._unit_labels.get(comp, "counts")

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
    # PNG export (M3-B)
    # ------------------------------------------------------------------
    def export_png(self, path: Path) -> bool:
        """Render the current traces + spectrogram view to ``path`` as PNG.

        A widget grab of the view splitter (exactly what is on screen —
        zoom, units, cursors included). One-shot user action on the GUI
        thread, same as the HVSR report/CSV exports; the write is a
        screenshot-sized pixmap, not the archive data path.
        """
        pixmap = self._trace_host.grab()
        ok = bool(pixmap.save(str(path), "PNG"))
        if ok:
            self._status_label.setText(f"View exported to {path}.")
            _log.info(
                "archive_view_png_exported",
                path=str(path),
                device=self._loaded_device,
                width=pixmap.width(),
                height=pixmap.height(),
            )
        else:
            _log.warning("archive_view_png_export_failed", path=str(path))
        return ok

    def _on_export_clicked(self) -> None:
        if not self._display:
            return
        from echosmonitor.storage.sds import sanitize_device_name

        stamp = UTCDateTime(self._win_t_start).strftime("%Y%m%dT%H%M%S")
        default = f"archive_{sanitize_device_name(self._loaded_device)}_{stamp}.png"
        path, _selected = QFileDialog.getSaveFileName(
            self, "Export archive view", default, "PNG image (*.png)"
        )
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        if not self.export_png(Path(path)):
            QMessageBox.warning(
                self, "Export PNG", f"Could not write the image to:\n{path}"
            )

    # ------------------------------------------------------------------
    # Stacked / overlaid toggle
    # ------------------------------------------------------------------
    def _on_layout_toggled(self, _checked: bool) -> None:
        stacked = self._stacked_radio.isChecked()
        # Carry the time zoom across the switch (M3-B ergonomics) by
        # copying the range once, and re-target the spectrogram's X-link
        # to the view that is about to be visible — a static link through
        # the hidden one would distort ranges (see _build_view note).
        src = self._overlay_plot if stacked else self._stacked_plots["Z"]
        dst = self._stacked_plots["Z"] if stacked else self._overlay_plot
        lo, hi = src.viewRange()[0]
        dst.setXRange(lo, hi, padding=0.0)
        self._spec_plot.setXLink(dst)
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
        self._unit_labels = {}
        for comp, (x, y) in self._display.items():
            self._stacked_curves[comp].setData(x, y, connect="finite")
            self._overlay_curves[comp].setData(x, y, connect="finite")
            self._stacked_plots[comp].setLabel("left", f"{comp} (counts)")
        self._refresh_overlay_label()
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
        self._unit_labels[comp] = _UNIT_LABELS.get(code, unit_label)
        self._refresh_overlay_label()
        self._refresh_readout()

    def mark_components_left_in_counts(self, comps: list[str]) -> None:
        """Flag components a unit switch left in counts (gaps — rule honesty).

        Deconvolution skips components carrying NaN gaps (an FFT response
        removal would smear them across the window); the display must say
        so rather than silently mixing units. Called by the host after a
        PARTIAL unit dispatch.
        """
        for comp in comps:
            if comp in self._display:
                self._stacked_plots[comp].setLabel("left", f"{comp} (counts — gaps)")
        if comps:
            self._status_label.setText(
                f"{', '.join(sorted(comps))} left in counts: gaps prevent deconvolution."
            )
        self._refresh_overlay_label()

    def _refresh_overlay_label(self) -> None:
        """The overlaid plot has ONE y axis — label it with the common unit,
        or call out the mix (it used to stay 'counts' forever). An empty
        display resets to counts (a frozen stale label was the trap)."""
        labels = {self._unit_labels.get(comp, "counts") for comp in self._display}
        if len(labels) == 1:
            self._overlay_plot.setLabel("left", next(iter(labels)))
        elif len(labels) > 1:
            self._overlay_plot.setLabel("left", "mixed units — see stacked view")
        else:
            self._overlay_plot.setLabel("left", "counts")

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
    def session_rows_for_test(self) -> list[tuple[str, str, str]]:
        """Visible session rows as ``(project, started, status)`` triples."""
        rows: list[tuple[str, str, str]] = []
        for i in range(self._session_tree.topLevelItemCount()):
            item = self._session_tree.topLevelItem(i)
            if item is not None:
                rows.append((item.text(0), item.text(1), item.text(2)))
        return rows

    def select_session_for_test(self, row: int) -> None:
        item = self._session_tree.topLevelItem(row)
        if item is not None:
            self._session_tree.setCurrentItem(item)

    def _station_item_for_test(self, device: str, station: str) -> QTreeWidgetItem | None:
        for i in range(self._station_tree.topLevelItemCount()):
            parent = self._station_tree.topLevelItem(i)
            if parent is None or parent.text(0) != device:
                continue
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child is not None and child.text(0) == station:
                    return child
        return None

    def select_station_for_test(self, device: str, station: str) -> bool:
        """Select a station row in the session tree; True when found."""
        child = self._station_item_for_test(device, station)
        if child is None:
            return False
        self._station_tree.setCurrentItem(child)
        return True

    def station_strip_for_test(self, device: str, station: str) -> CoverageStrip | None:
        child = self._station_item_for_test(device, station)
        if child is None:
            return None
        widget = self._station_tree.itemWidget(child, 1)
        return widget if isinstance(widget, CoverageStrip) else None

    def browser_status_for_test(self) -> str:
        return self._browser_status.text()

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

    def overlay_unit_label_for_test(self) -> str:
        return str(self._overlay_plot.getAxis("left").labelText)

    def export_enabled_for_test(self) -> bool:
        return self._export_button.isEnabled()

    def set_cursor_epoch_for_test(self, which: str, epoch: float) -> None:
        """Drive a cursor to a known epoch (offscreen drag is undeliverable)."""
        self._cursor_lines[which][0].setValue(epoch)

    def cursor_pos_for_test(self) -> dict[str, float]:
        return dict(self._cursor_pos)


def _wrap(layout: QHBoxLayout) -> QWidget:
    w = QWidget()
    w.setLayout(layout)
    return w
