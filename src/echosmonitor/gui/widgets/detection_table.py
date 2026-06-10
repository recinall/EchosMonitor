"""Live, sortable, filterable table of STA/LTA detections (M8 stage B).

The Detections dock hosts this widget. It is a model/view table (not a
``QTableWidget``) so thousands of rows update incrementally and sort
cheaply:

* :class:`DetectionTableModel` is the single source of truth — it holds
  every detection (live + historical). Filtering NEVER mutates it.
* :class:`DetectionFilterProxy` is a view-side ``QSortFilterProxyModel``;
  the toolbar drives its device / NSLC / min-score / time-window
  predicates (CLAUDE.md hard rule: filter via the proxy, never the
  source model).

Open detections (``t_off is None``) render a ticking "open Ns" duration
driven by a 1 Hz timer; when the engine reports the close
(:meth:`on_detection_updated`) the duration freezes to the final span.

``now`` is injected (``now_provider``) so the ticking / freezing and the
time-window filter are testable without wall-clock flakiness.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import IntEnum

from obspy import UTCDateTime
from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QStackedLayout,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.models import Detection

# Refresh cadence for open-duration ticks + time-window re-filtering.
_TICK_MS = 1000
# A custom role returning a sortable scalar per column (posix time, float
# score, float duration) so the proxy sorts numerically/chronologically
# regardless of the display string.
_SORT_ROLE = Qt.ItemDataRole.UserRole + 1
# Role returning the row's :class:`Detection` (for selection / copy).
_DETECTION_ROLE = Qt.ItemDataRole.UserRole + 2

# Time-window filter options: label → seconds (None = no bound / "all").
_WINDOW_OPTIONS: tuple[tuple[str, float | None], ...] = (
    ("Last 1h", 3600.0),
    ("Last 6h", 6 * 3600.0),
    ("Last 24h", 24 * 3600.0),
    ("All", None),
)


class DetectionColumn(IntEnum):
    TIME = 0
    DEVICE = 1
    NSLC = 2
    KIND = 3
    SCORE = 4
    DURATION = 5
    DELTA = 6


_HEADERS = ("Time (UTC)", "Device", "NSLC", "Kind", "Score", "Duration", "Δ prev")


class _Row:
    """One table row: a detection plus presentation-derived extras.

    ``historical`` flags detections loaded from the DB at startup (vs.
    live this-session ones); ``delta`` is the gap to the previous
    detection on the same ``(device, nslc)`` stream, computed at insert.
    """

    __slots__ = ("delta", "detection", "historical")

    def __init__(self, detection: Detection, historical: bool, delta: float | None) -> None:
        self.detection = detection
        self.historical = historical
        self.delta = delta


def _fmt_duration(detection: Detection, now: UTCDateTime) -> str:
    if detection.t_off is None:
        elapsed = max(0.0, float(now - detection.t_on))
        return f"open {elapsed:.0f}s"
    return f"{float(detection.t_off - detection.t_on):.1f}s"


def _duration_sort_key(detection: Detection, now: UTCDateTime) -> float:
    if detection.t_off is None:
        return max(0.0, float(now - detection.t_on))
    return float(detection.t_off - detection.t_on)


class DetectionTableModel(QAbstractTableModel):
    """Holds all detections; never filtered in place (rule: proxy filters)."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        now_provider: Callable[[], UTCDateTime] | None = None,
    ) -> None:
        super().__init__(parent)
        self._rows: list[_Row] = []
        self._by_id: dict[int, int] = {}
        self._now: Callable[[], UTCDateTime] = now_provider or (lambda: UTCDateTime())

    # ----- Qt model API ------------------------------------------------
    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008, N802
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        det = row.detection
        col = index.column()
        if role == _DETECTION_ROLE:
            return det
        if role == _SORT_ROLE:
            return self._sort_value(row, col)
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(row, col)
        if role == Qt.ItemDataRole.ForegroundRole and row.historical:
            # Dimmed text marks historical (pre-session) detections.
            from PySide6.QtGui import QColor

            return QColor("#888")
        return None

    # ----- mutation ----------------------------------------------------
    def add_detection(self, detection: Detection, *, historical: bool = False) -> None:
        """Append a detection. Recomputes its Δ-from-previous on the same
        stream. Sorting is the proxy/view's job, so insertion order is
        irrelevant to what the user sees."""
        delta = self._delta_for(detection)
        at = len(self._rows)
        self.beginInsertRows(QModelIndex(), at, at)
        self._rows.append(_Row(detection, historical, delta))
        if detection.id is not None:
            self._by_id[detection.id] = at
        self.endInsertRows()

    def update_detection(self, detection: Detection) -> None:
        """Apply a close (t_off + final score) to the existing open row.

        No-op if the id is unknown (e.g. the open onset predated this
        session and was never inserted)."""
        if detection.id is None:
            return
        at = self._by_id.get(detection.id)
        if at is None:
            return
        self._rows[at].detection = detection
        top = self.index(at, DetectionColumn.SCORE)
        bottom = self.index(at, DetectionColumn.DURATION)
        self.dataChanged.emit(top, bottom)

    def tick(self) -> None:
        """Repaint the Duration column for still-open rows (1 Hz)."""
        if not self._rows:
            return
        open_rows = [i for i, r in enumerate(self._rows) if r.detection.t_off is None]
        for at in open_rows:
            idx = self.index(at, DetectionColumn.DURATION)
            self.dataChanged.emit(idx, idx)

    # ----- helpers -----------------------------------------------------
    def detection_for_source_row(self, row: int) -> Detection:
        return self._rows[row].detection

    def known_devices(self) -> list[str]:
        return sorted({r.detection.device for r in self._rows})

    def known_nslcs(self) -> list[str]:
        return sorted({r.detection.nslc for r in self._rows})

    def _delta_for(self, detection: Detection) -> float | None:
        prev: UTCDateTime | None = None
        for r in self._rows:
            d = r.detection
            if (
                d.device == detection.device
                and d.nslc == detection.nslc
                and d.t_on < detection.t_on
                and (prev is None or d.t_on > prev)
            ):
                prev = d.t_on
        return None if prev is None else float(detection.t_on - prev)

    def _display(self, row: _Row, col: int) -> str:
        det = row.detection
        if col == DetectionColumn.TIME:
            return str(det.t_on.strftime("%Y-%m-%d %H:%M:%S"))
        if col == DetectionColumn.DEVICE:
            return det.device
        if col == DetectionColumn.NSLC:
            return det.nslc
        if col == DetectionColumn.KIND:
            return det.kind
        if col == DetectionColumn.SCORE:
            return f"{det.score:.2f}"
        if col == DetectionColumn.DURATION:
            return _fmt_duration(det, self._now())
        if col == DetectionColumn.DELTA:
            return "" if row.delta is None else f"+{row.delta:.1f}s"
        return ""

    def _sort_value(self, row: _Row, col: int) -> object:
        det = row.detection
        if col == DetectionColumn.TIME:
            return float(det.t_on)
        if col == DetectionColumn.SCORE:
            return float(det.score)
        if col == DetectionColumn.DURATION:
            return _duration_sort_key(det, self._now())
        if col == DetectionColumn.DELTA:
            return -1.0 if row.delta is None else row.delta
        return self._display(row, col)


class DetectionFilterProxy(QSortFilterProxyModel):
    """View-side filtering for the detection table. The source model is
    never mutated to filter (CLAUDE.md hard rule)."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        now_provider: Callable[[], UTCDateTime] | None = None,
    ) -> None:
        super().__init__(parent)
        self._device: str | None = None
        self._nslc: str | None = None
        self._min_score: float = 0.0
        self._window_s: float | None = None
        self._now: Callable[[], UTCDateTime] = now_provider or (lambda: UTCDateTime())
        self.setSortRole(_SORT_ROLE)

    def set_device(self, device: str | None) -> None:
        self._device = device
        self.invalidate()

    def set_nslc(self, nslc: str | None) -> None:
        self._nslc = nslc
        self.invalidate()

    def set_min_score(self, score: float) -> None:
        self._min_score = score
        self.invalidate()

    def set_window(self, seconds: float | None) -> None:
        self._window_s = seconds
        self.invalidate()

    def refresh_window(self) -> None:
        """Re-evaluate the (moving) time-window predicate."""
        if self._window_s is not None:
            self.invalidate()

    def filterAcceptsRow(  # noqa: N802
        self,
        source_row: int,
        source_parent: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        model = self.sourceModel()
        if not isinstance(model, DetectionTableModel):
            return True
        det = model.detection_for_source_row(source_row)
        if self._device is not None and det.device != self._device:
            return False
        if self._nslc is not None and det.nslc != self._nslc:
            return False
        if det.score < self._min_score:
            return False
        return not (
            self._window_s is not None and float(det.t_on) < float(self._now()) - self._window_s
        )


class DetectionTable(QWidget):
    """Detections dock contents: filter toolbar + model/view table.

    Signals:
        focusDetectionRequested(object): a row was double-clicked — the
            main window focuses the device's Live tab + the detail pane.
        detectionSelected(object): the selected ``Detection`` (or ``None``
            on clear) — drives the central "why did this fire?" pane.
        inspectInUnitRequested(object, str): a context-menu request to
            inspect a ``Detection`` in physical units (the second arg is a
            unit code: ``"VEL"``, ``"ACC"``, or ``"DISP"``). The main
            window selects the detection then drives the deconvolution.
    """

    focusDetectionRequested = Signal(object)  # Detection  # noqa: N815
    detectionSelected = Signal(object)  # Detection | None  # noqa: N815
    inspectInUnitRequested = Signal(object, str)  # (Detection, unit_code)  # noqa: N815

    _ALL = "All"

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        now_provider: Callable[[], UTCDateTime] | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = DetectionTableModel(self, now_provider=now_provider)
        self._proxy = DetectionFilterProxy(self, now_provider=now_provider)
        self._proxy.setSourceModel(self._model)

        # ----- filter toolbar -----
        self._device_combo = QComboBox()
        self._device_combo.addItem(self._ALL)
        self._device_combo.currentTextChanged.connect(self._on_device_filter)
        self._nslc_combo = QComboBox()
        self._nslc_combo.addItem(self._ALL)
        self._nslc_combo.currentTextChanged.connect(self._on_nslc_filter)
        self._score_spin = QDoubleSpinBox()
        self._score_spin.setRange(0.0, 1000.0)
        self._score_spin.setSingleStep(0.5)
        self._score_spin.setPrefix("≥ ")
        self._score_spin.valueChanged.connect(self._proxy.set_min_score)
        self._window_combo = QComboBox()
        for label, _secs in _WINDOW_OPTIONS:
            self._window_combo.addItem(label)
        self._window_combo.setCurrentText("All")
        self._window_combo.currentIndexChanged.connect(self._on_window_filter)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 2, 4, 2)
        toolbar.addWidget(QLabel("Device:"))
        toolbar.addWidget(self._device_combo)
        toolbar.addWidget(QLabel("NSLC:"))
        toolbar.addWidget(self._nslc_combo)
        toolbar.addWidget(QLabel("Score:"))
        toolbar.addWidget(self._score_spin)
        toolbar.addWidget(QLabel("Window:"))
        toolbar.addWidget(self._window_combo)
        toolbar.addStretch(1)
        toolbar_host = QWidget(self)
        toolbar_host.setLayout(toolbar)

        # ----- table view -----
        self._view = QTableView(self)
        self._view.setModel(self._proxy)
        self._view.setSortingEnabled(True)
        self._view.sortByColumn(int(DetectionColumn.TIME), Qt.SortOrder.DescendingOrder)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setAlternatingRowColors(True)
        self._view.verticalHeader().setVisible(False)
        header = self._view.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(int(DetectionColumn.NSLC), QHeaderView.ResizeMode.Stretch)
        self._view.doubleClicked.connect(self._on_double_clicked)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)
        sel = self._view.selectionModel()
        if sel is not None:
            sel.selectionChanged.connect(self._on_selection_changed)

        # ----- empty-state overlay -----
        self._empty = QLabel(
            "No detections yet. STA/LTA is running on streams with a "
            "sta_lta stage in their DSP chain."
        )
        self._empty.setObjectName("DetectionTableEmpty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("QLabel#DetectionTableEmpty { color: #888; font-style: italic; }")
        stack_host = QWidget(self)
        self._stack = QStackedLayout(stack_host)
        self._stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._view)
        self._stack.addWidget(self._empty)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(toolbar_host)
        root.addWidget(stack_host, stretch=1)

        # 1 Hz tick: advance open durations + re-apply the moving window.
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

        self._refresh_empty_state()

    # ------------------------------------------------------------------
    # Engine-facing slots
    # ------------------------------------------------------------------
    def on_detection_recorded(self, detection: object) -> None:
        if not isinstance(detection, Detection):
            return
        self._model.add_detection(detection, historical=False)
        self._sync_filter_combos()
        self._refresh_empty_state()

    def on_detection_updated(self, detection: object) -> None:
        if isinstance(detection, Detection):
            self._model.update_detection(detection)

    def load_historical(self, detections: list[Detection]) -> None:
        """Pre-populate from the DB index at startup. Rows are flagged
        historical (dimmed). Filters/sort apply identically.

        ``recent_detections`` returns newest-first; we insert oldest-first
        so each row's Δ-from-previous (computed against rows already in
        the model) resolves against the genuinely earlier detection on the
        same stream rather than always coming up empty."""
        for det in reversed(detections):
            self._model.add_detection(det, historical=True)
        self._sync_filter_combos()
        self._refresh_empty_state()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _on_tick(self) -> None:
        self._model.tick()
        self._proxy.refresh_window()
        self._refresh_empty_state()

    def _refresh_empty_state(self) -> None:
        empty = self._proxy.rowCount() == 0
        self._stack.setCurrentWidget(self._empty if empty else self._view)

    def _sync_filter_combos(self) -> None:
        self._sync_combo(self._device_combo, self._model.known_devices())
        self._sync_combo(self._nslc_combo, self._model.known_nslcs())

    @staticmethod
    def _sync_combo(combo: QComboBox, values: list[str]) -> None:
        existing = {combo.itemText(i) for i in range(combo.count())}
        for v in values:
            if v not in existing:
                combo.addItem(v)

    def _on_device_filter(self, text: str) -> None:
        self._proxy.set_device(None if text == self._ALL else text)
        self._refresh_empty_state()

    def _on_nslc_filter(self, text: str) -> None:
        self._proxy.set_nslc(None if text == self._ALL else text)
        self._refresh_empty_state()

    def _on_window_filter(self, index: int) -> None:
        self._proxy.set_window(_WINDOW_OPTIONS[index][1])
        self._refresh_empty_state()

    def _selected_detection(self) -> Detection | None:
        indexes = self._view.selectionModel().selectedRows()
        if not indexes:
            return None
        det = indexes[0].data(_DETECTION_ROLE)
        return det if isinstance(det, Detection) else None

    def _on_selection_changed(self, *_args: object) -> None:
        self.detectionSelected.emit(self._selected_detection())

    def _on_double_clicked(self, index: QModelIndex) -> None:
        det = index.data(_DETECTION_ROLE)
        if isinstance(det, Detection):
            self.focusDetectionRequested.emit(det)

    def _on_context_menu(self, pos: QPoint) -> None:
        det = self._selected_detection()
        if det is None:
            return
        menu = QMenu(self)
        copy_act = QAction("Copy detection (NSLC + time + score)", menu)
        copy_act.triggered.connect(lambda: self._copy_text(self._format_plain(det)))
        snippet_act = QAction("Copy as ObsPy snippet", menu)
        snippet_act.triggered.connect(lambda: self._copy_text(self._format_obspy(det)))
        menu.addAction(copy_act)
        menu.addAction(snippet_act)
        # M11 B: inspect in physical units. The actions are always shown;
        # the no-response path surfaces the standard tooltip/message on the
        # detail pane (the table widget does not hold the ResponseProvider,
        # so gating here would couple it to config — kept simple).
        menu.addSeparator()
        inspect_menu = menu.addMenu("Inspect in physical units")
        for label, code in (
            ("Velocity (m/s)", "VEL"),
            ("Acceleration (m/s²)", "ACC"),
            ("Displacement (m)", "DISP"),
        ):
            act = QAction(label, inspect_menu)
            act.triggered.connect(
                lambda _checked=False, d=det, c=code: self.inspectInUnitRequested.emit(d, c)
            )
            inspect_menu.addAction(act)
        viewport = self._view.viewport()
        if viewport is not None:
            menu.exec(viewport.mapToGlobal(pos))

    @staticmethod
    def _copy_text(text: str) -> None:
        clip = QGuiApplication.clipboard()
        if clip is not None:
            clip.setText(text)

    @staticmethod
    def _format_plain(det: Detection) -> str:
        off = "open" if det.t_off is None else str(det.t_off)
        return f"{det.nslc}  t_on={det.t_on}  t_off={off}  score={det.score:.2f}  ({det.kind})"

    @staticmethod
    def _format_obspy(det: Detection) -> str:
        net, sta, loc, cha = det.nslc.split(".")
        pad = 5.0
        start = det.t_on - pad
        end = (det.t_off if det.t_off is not None else det.t_on) + pad
        return (
            "from obspy import read, UTCDateTime\n"
            f'st = read("ARCHIVE_PATH")  # e.g. SDS path for {det.nslc}\n'
            f'st = st.select(network="{net}", station="{sta}", '
            f'location="{loc}", channel="{cha}")\n'
            f'st.trim(UTCDateTime("{start}"), UTCDateTime("{end}"))'
        )

    # ----- test-only accessors -----
    def _model_for_test(self) -> DetectionTableModel:
        return self._model

    def _proxy_for_test(self) -> DetectionFilterProxy:
        return self._proxy

    def _view_for_test(self) -> QTableView:
        return self._view
