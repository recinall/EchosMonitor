"""Map tab — device positions + live acquisition state (M4-B, rule 16).

Tile-stack decision (ROADMAP open question 4, decision log 2026-06-12):
**pyqtgraph scatter in a local east/north metre frame, NO web tiles, NO
QtWebEngine.** The product's fleet is a handful of Echos nodes deployed
metres-to-kilometres apart for array work; what the user needs is the
*relative geometry* (who is where, how far apart — M5 consumes exactly
this), not basemap context. A pyqtgraph scatter is offline-by-
construction (field laptops), adds zero dependencies, and reuses the
plotting stack every other tab already ships. Revisit web tiles only on
a real field need, as an isolated optional widget per CLAUDE.md.

Positions arrive from the ONE shared
:class:`~echosmonitor.core.positions.PositionResolver` (rule 16) via
queued signals; this widget never fetches anything itself (rule 1). The
plot frame is metres east/north of the positioned-device centroid
(:func:`~echosmonitor.core.positions.local_east_north`), aspect-locked
1:1 so the on-screen shape IS the array shape; absolute lat/lon/elev
and the position source live in each marker's hover tip.

Marker colour = the rule-13 acquisition state (Idle grey, Monitoring
green, Recording red — same hexes as the Devices dock badges), with an
amber "trouble" tint when a non-idle device's SeedLink connection is
not currently CONNECTED. Clicking a marker selects the device in the
Devices dock (``deviceSelected``). Devices without a resolvable
position are listed under the map with the failure kind — honest state,
not an error dialog (decision log 2026-06-12).
"""

from __future__ import annotations

import itertools

import pyqtgraph as pg
import structlog
from PySide6.QtCore import QPointF, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.models import AcquisitionState, ConnState
from echosmonitor.core.positions import (
    ResolvedPosition,
    haversine_m,
    local_east_north,
)

_log = structlog.get_logger(__name__)

# Acquisition-state marker colours — deliberately the same hexes as the
# Devices dock badges (device_panel._ACQ_REC_COLOR / _ACQ_IDLE_COLOR and
# the CONNECTED row colour) so the two views agree at a glance.
_COLOR_IDLE = "#808080"
_COLOR_MONITORING = "#3aa371"
_COLOR_RECORDING = "#d04040"
# Non-idle device whose SeedLink socket is not CONNECTED right now —
# the dock's WAITING_RETRY amber.
_COLOR_TROUBLE = "#c98f2a"

_MARKER_SIZE = 14

# Distance readout formatting threshold.
_KM_THRESHOLD_M = 1000.0


def _format_distance(meters: float) -> str:
    if meters < _KM_THRESHOLD_M:
        return f"{meters:.1f} m"
    return f"{meters / 1000.0:.3f} km"


class MapWidget(QWidget):
    """Central-tab map of the device fleet (state in, signals out).

    All inputs are slots fed by MainWindow (resolver results, engine
    state changes, the config device set); the only outputs are
    ``deviceSelected`` (marker click → Devices dock) and
    ``refreshRequested`` (toolbar button → ``PositionResolver.refresh``).
    The widget holds no reference to the resolver or the engine.
    """

    deviceSelected = Signal(str)  # noqa: N815
    refreshRequested = Signal()  # noqa: N815

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Full configured device set (order preserved for the table);
        # positions/states are per-device sub-maps of it.
        self._devices: tuple[str, ...] = ()
        self._positions: dict[str, ResolvedPosition] = {}
        self._failures: dict[str, str] = {}  # device -> PositionFailureKind
        self._acq_states: dict[str, AcquisitionState] = {}
        self._conn_states: dict[str, ConnState] = {}
        self._labels: list[pg.TextItem] = []

        root = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self._refresh_button = QPushButton("Refresh positions", self)
        self._refresh_button.clicked.connect(self.refreshRequested.emit)
        toolbar.addWidget(self._refresh_button)
        self._fit_button = QPushButton("Fit view", self)
        self._fit_button.clicked.connect(self._fit_view)
        toolbar.addWidget(self._fit_button)
        toolbar.addStretch(1)
        self._status_label = QLabel("", self)
        toolbar.addWidget(self._status_label)
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self._plot = pg.PlotWidget()
        self._plot.setBackground("default")
        plot_item = self._plot.getPlotItem()
        plot_item.setLabel("bottom", "East", units="m")
        plot_item.setLabel("left", "North", units="m")
        plot_item.showGrid(x=True, y=True, alpha=0.3)
        # 1:1 metres — the on-screen shape is the array shape.
        plot_item.getViewBox().setAspectLocked(True, 1.0)
        self._scatter = pg.ScatterPlotItem(
            size=_MARKER_SIZE,
            pen=pg.mkPen("#202020"),
            hoverable=True,
            tip=self._spot_tip,
        )
        self._scatter.sigClicked.connect(self._on_spot_clicked)
        plot_item.addItem(self._scatter)
        splitter.addWidget(self._plot)

        side = QWidget(self)
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(QLabel("Inter-device distances", side))
        self._distance_table = QTableWidget(0, 3, side)
        self._distance_table.setHorizontalHeaderLabels(["Device A", "Device B", "Distance"])
        self._distance_table.verticalHeader().setVisible(False)
        self._distance_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._distance_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        side_layout.addWidget(self._distance_table, 1)
        self._unpositioned_label = QLabel("", side)
        self._unpositioned_label.setWordWrap(True)
        side_layout.addWidget(self._unpositioned_label)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    # ------------------------------------------------------------------
    # Inputs (wired by MainWindow)
    # ------------------------------------------------------------------
    def set_devices(self, names: tuple[str, ...]) -> None:
        """Full replacement of the configured device set (config truth).

        Removed devices drop every per-device record — the resolver
        prunes its own cache on configure; this is the widget-side
        mirror so a removed device's marker cannot linger.
        """
        self._devices = names
        keep = set(names)
        for mapping in (self._positions, self._failures, self._acq_states, self._conn_states):
            for name in [n for n in mapping if n not in keep]:
                del mapping[name]
        self._rebuild()

    def set_positions(self, snapshot: dict[str, ResolvedPosition]) -> None:
        """Initial fill from ``PositionResolver.positions()``."""
        self._positions = {n: p for n, p in snapshot.items() if n in set(self._devices)}
        self._rebuild()

    @Slot(object)
    def on_position(self, payload: object) -> None:
        """One resolver result (rule 4 isinstance guard)."""
        if not isinstance(payload, ResolvedPosition):
            return
        if payload.device not in set(self._devices):
            return  # stale delivery for a removed device — never resurrect
        self._positions[payload.device] = payload
        self._failures.pop(payload.device, None)
        self._rebuild()

    @Slot(str, str, str)
    def on_position_failed(self, device: str, kind: str, _message: str) -> None:
        if device not in set(self._devices):
            return
        self._failures[device] = kind
        # A failed refresh keeps the last known position (resolver
        # semantics) — only a never-positioned device joins the list.
        self._rebuild()

    @Slot(str, int)
    def on_acquisition_state(self, device: str, state: int) -> None:
        try:
            acq = AcquisitionState(state)
        except ValueError:
            return
        if device not in set(self._devices) or self._acq_states.get(device) is acq:
            return  # unchanged-state early-out: no churn on repeats
        self._acq_states[device] = acq
        self._rebuild()

    @Slot(str, int)
    def on_device_state(self, device: str, state: int) -> None:
        try:
            conn = ConnState(state)
        except ValueError:
            return
        if device not in set(self._devices) or self._conn_states.get(device) is conn:
            return  # flapping-retry repeats must not churn the plot
        self._conn_states[device] = conn
        self._rebuild()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _marker_color(self, device: str) -> str:
        acq = self._acq_states.get(device, AcquisitionState.IDLE)
        if acq is AcquisitionState.IDLE:
            return _COLOR_IDLE
        conn = self._conn_states.get(device, ConnState.DISCONNECTED)
        if conn is not ConnState.CONNECTED:
            return _COLOR_TROUBLE
        if acq is AcquisitionState.RECORDING:
            return _COLOR_RECORDING
        return _COLOR_MONITORING

    def _positioned(self) -> list[tuple[str, ResolvedPosition]]:
        return [(name, self._positions[name]) for name in self._devices if name in self._positions]

    def _rebuild(self) -> None:
        positioned = self._positioned()
        plot_item = self._plot.getPlotItem()
        for label in self._labels:
            plot_item.removeItem(label)
        self._labels.clear()

        spots: list[dict[str, object]] = []
        if positioned:
            lat0 = sum(p.latitude for _, p in positioned) / len(positioned)
            lon0 = sum(p.longitude for _, p in positioned) / len(positioned)
            for name, position in positioned:
                east, north = local_east_north(position.latitude, position.longitude, lat0, lon0)
                spots.append(
                    {
                        "pos": (east, north),
                        "brush": pg.mkBrush(self._marker_color(name)),
                        "data": name,
                    }
                )
                label = pg.TextItem(name, anchor=(0.5, 1.4))
                label.setPos(east, north)
                plot_item.addItem(label)
                self._labels.append(label)
        self._scatter.setData(spots=spots)

        self._update_distances(positioned)
        self._update_unpositioned()
        self._status_label.setText(
            f"{len(positioned)} of {len(self._devices)} devices positioned"
        )

    def _update_distances(self, positioned: list[tuple[str, ResolvedPosition]]) -> None:
        pairs = [
            (a, b, haversine_m(pa.latitude, pa.longitude, pb.latitude, pb.longitude))
            for (a, pa), (b, pb) in itertools.combinations(positioned, 2)
        ]
        pairs.sort(key=lambda row: row[2])
        self._distance_table.setRowCount(len(pairs))
        for row, (name_a, name_b, meters) in enumerate(pairs):
            self._distance_table.setItem(row, 0, QTableWidgetItem(name_a))
            self._distance_table.setItem(row, 1, QTableWidgetItem(name_b))
            self._distance_table.setItem(row, 2, QTableWidgetItem(_format_distance(meters)))

    def _update_unpositioned(self) -> None:
        missing = [
            f"{name} ({self._failures.get(name, 'pending')})"
            for name in self._devices
            if name not in self._positions
        ]
        if missing:
            self._unpositioned_label.setText("No position: " + ", ".join(missing))
        else:
            self._unpositioned_label.setText("")

    def _fit_view(self) -> None:
        self._plot.getPlotItem().getViewBox().autoRange()

    def _spot_tip(self, x: float, y: float, data: object) -> str:
        if not isinstance(data, str):
            return ""
        position = self._positions.get(data)
        if position is None:
            return data
        return (
            f"{data}\n"
            f"lat {position.latitude:.6f}, lon {position.longitude:.6f}\n"
            f"elev {position.elevation_m:.1f} m\n"
            f"source: {position.source}"
        )

    def _on_spot_clicked(self, _scatter: object, points: object, _event: object = None) -> None:
        try:
            first = points[0]  # type: ignore[index]
        except (TypeError, IndexError):
            return
        name = first.data()
        if isinstance(name, str):
            _log.info("map_device_clicked", device=name)
            self.deviceSelected.emit(name)

    # ------------------------------------------------------------------
    # Test seams
    # ------------------------------------------------------------------
    def _spot_count_for_test(self) -> int:
        return len(self._scatter.points())

    def _spot_color_for_test(self, device: str) -> str | None:
        for point in self._scatter.points():
            if point.data() == device:
                return str(point.brush().color().name())
        return None

    def _spot_pos_for_test(self, device: str) -> tuple[float, float] | None:
        for point in self._scatter.points():
            if point.data() == device:
                pos: QPointF = point.pos()
                return (pos.x(), pos.y())
        return None

    def _distance_rows_for_test(self) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for row in range(self._distance_table.rowCount()):
            cells = [self._distance_table.item(row, col) for col in range(3)]
            rows.append(tuple(cell.text() if cell else "" for cell in cells))  # type: ignore[arg-type]
        return rows

    def _unpositioned_text_for_test(self) -> str:
        return self._unpositioned_label.text()

    def _status_text_for_test(self) -> str:
        return self._status_label.text()


__all__ = ["MapWidget"]
