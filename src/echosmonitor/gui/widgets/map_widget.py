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

f0 overlay (M5-B): when an array HVSR measurement supplies per-device
fundamental frequencies (``set_f0_overlay``), markers WITH an f0 are
recoloured on a blue→red ramp over the overlay's log-frequency range —
the spatial-variation view of site response. Devices without an f0
keep their state colour (honest: not measured ≠ measured-low). The
hover tip gains the f0 line and the toolbar gains a "Clear f0" button;
the overlay persists across measurement stop (the result stays valid)
until cleared or superseded by the next array run.

Satellite basemap (M6.5-D, revising the M4-B "no tiles" decision on a
real field need): a checkable "Satellite" toolbar button fetches Esri
World Imagery XYZ tiles via :class:`~echosmonitor.core.map_tiles.
TileFetcher` on a widget-owned worker thread (lazy-started on first
toggle — the M6 wizard lesson) and draws them as ``ImageItem``s with
``zValue=-10`` UNDER the scatter/f0 overlay, placed in the same local
east/north frame (tile lat/lon corners through ``local_east_north``;
the equirectangular-vs-Mercator mismatch is sub-metre at array scale).
Still NO QtWebEngine and no slippy-map stack: one bounded batch per
array extent, disk-cached for offline field use, attribution rendered
whenever imagery is shown. ``Fit view`` keeps fitting the DEVICES
(tiles are added with ``ignoreBounds=True``).
"""

from __future__ import annotations

import contextlib
import math

import numpy as np
import pyqtgraph as pg
import structlog
from PySide6.QtCore import QMetaObject, QPointF, QRectF, Qt, QThread, QTimer, Signal, Slot
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

from echosmonitor.core.map_tiles import (
    ATTRIBUTION,
    TileFetcher,
    TileRequest,
    TileResult,
    tile_bounds,
    tiles_for_extent,
    zoom_for_span,
)
from echosmonitor.core.models import AcquisitionState, ConnState
from echosmonitor.core.positions import (
    ResolvedPosition,
    east_north_to_latlon,
    local_east_north,
    station_geometry,
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

# Basemap layering + sizing. Tiles sit below everything the map draws
# (scatter spots default to zValue 0, labels likewise).
_TILE_Z_VALUE = -10.0
# Minimum span "Fit view" may produce. A single positioned device is a
# lone point at the frame origin: pyqtgraph's autoRange collapses the
# view to a degenerate (~0 m) range — the pixel-sized marker still
# shows, but anything data-sized (grid, basemap tiles) becomes
# invisible. Found on the first real Satellite use (M6.5-E checklist).
_FIT_MIN_SPAN_M = 50.0
# Margin factor applied around the array extent when choosing the
# INITIAL basemap coverage (the rescue extent for a single node), and
# the floor for a degenerate (single-station) extent so one node still
# gets a recognisable patch of imagery on first show.
_BASEMAP_MARGIN = 2.0
_BASEMAP_MIN_SPAN_M = 200.0
# Bounded join for the tile worker thread at shutdown (rule 7).
_TILE_THREAD_JOIN_MS = 2000

# Viewport-following basemap (M6.5-F). Pan/zoom recompute the tiles for
# the VISIBLE region — the basemap is no longer a single static patch.
# A pan/zoom gesture fires many sigRangeChanged signals; coalesce them
# behind a single-shot debounce so one gesture = one fetch batch.
_BASEMAP_REFRESH_DEBOUNCE_MS = 200
# Fetch slightly beyond the visible edges so a small pan reveals
# already-loaded imagery (fraction of the viewport span per side).
_BASEMAP_VIEWPORT_MARGIN = 0.15
# LRU cap on drawn ImageItems across all zoom levels (rule 5/8: the
# seam is bounded). ~36 visible at one zoom + history for smooth
# pan/zoom and zoom-transition overlap. Least-recently-touched evicted.
_MAX_TILE_ITEMS = 96
# zValue spread per zoom level so a finer tile draws ABOVE a coarser one
# covering the same ground — no blank flash during a zoom transition,
# and still far below the scatter (z 0). z=19 → _TILE_Z_VALUE + 0.019.
_Z_PER_ZOOM = 0.001

# Tile worker/thread pairs whose bounded join timed out at shutdown.
# Retained for the process lifetime: dropping the last reference to a
# RUNNING QThread is a hard Qt abort (the M6-0 lesson; same pattern as
# the discovery dialog and both HVSR engines). Count is logged.
_ABANDONED: list[tuple[TileFetcher, QThread]] = []

# f0 overlay ramp endpoints (low f0 → blue, high f0 → red). A manual
# two-colour lerp over the log-frequency range: deterministic, no
# colormap-API dependency, and the two ends match nothing else on the
# map (state colours are grey/green/red/amber at full saturation).
_F0_LOW_RGB = (40, 96, 192)
_F0_HIGH_RGB = (208, 64, 64)


def _format_distance(meters: float) -> str:
    if meters < _KM_THRESHOLD_M:
        return f"{meters:.1f} m"
    return f"{meters / 1000.0:.3f} km"


def _f0_ramp_color(f0: float, f_lo: float, f_hi: float) -> str:
    """Hex colour for ``f0`` on the blue→red log-frequency ramp."""
    if f_hi <= f_lo:  # single-value overlay: midpoint
        t = 0.5
    else:
        t = (math.log(f0) - math.log(f_lo)) / (math.log(f_hi) - math.log(f_lo))
        t = min(1.0, max(0.0, t))
    rgb = tuple(
        round(lo + (hi - lo) * t) for lo, hi in zip(_F0_LOW_RGB, _F0_HIGH_RGB, strict=True)
    )
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


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
    # Owner→worker request signal for the tile fetcher (queued; the
    # worker lives on the widget-owned tile thread).
    _tileRequested = Signal(object)  # noqa: N815

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Full configured device set (order preserved for the table);
        # positions/states are per-device sub-maps of it.
        self._devices: tuple[str, ...] = ()
        self._positions: dict[str, ResolvedPosition] = {}
        self._failures: dict[str, str] = {}  # device -> PositionFailureKind
        self._acq_states: dict[str, AcquisitionState] = {}
        self._conn_states: dict[str, ConnState] = {}
        self._f0_overlay: dict[str, float] = {}  # device -> f0 Hz (M5-B)
        self._labels: list[pg.TextItem] = []

        # ----- M6.5-D/F satellite basemap state -----------------------
        # Worker + thread are lazily created on the first Satellite
        # toggle (an unused map owns no running thread — the M6 wizard
        # lesson). ``_frame_origin`` is the centroid the CURRENT tiles
        # were placed against; if it moves, every drawn rect is stale
        # and the tiles are cleared before the next viewport fetch.
        self._tile_fetcher: TileFetcher | None = None
        self._tile_thread: QThread | None = None
        # Sticky terminal flag: once the widget is shut down, no path
        # (a stray debounce tick, a programmatic refresh) may re-spawn
        # the worker thread — the same canon as PositionResolver.
        self._basemap_shutdown = False
        # ``_tile_items`` is an LRU keyed by (zoom, x, y); insertion
        # order = recency, oldest evicted past ``_MAX_TILE_ITEMS``.
        self._tile_items: dict[tuple[int, int, int], pg.ImageItem] = {}
        self._tile_generation = 0
        self._frame_origin: tuple[float, float] | None = None
        # The (lat0, lon0) the currently-drawn tiles were placed against
        # — compared on rebuild so an origin move clears stale rects.
        self._tiles_origin: tuple[float, float] | None = None
        # Memo of the last viewport fetch (zoom, frozenset of wanted
        # tiles): a refresh that wants exactly what is already drawn is
        # a no-op (marker recolours, f0 overlay, connection flaps, a
        # sub-tile pan). Cleared on batch failure so a retry can run.
        self._basemap_memo: tuple[object, ...] | None = None
        # East/north extent (e0, n0, e1, n1) of the array bounding box;
        # the rescue target so requested imagery is never invisible on
        # the degenerate single-device view.
        self._basemap_extent: tuple[float, float, float, float] | None = None

        root = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self._refresh_button = QPushButton("Refresh positions", self)
        self._refresh_button.clicked.connect(self.refreshRequested.emit)
        toolbar.addWidget(self._refresh_button)
        self._fit_button = QPushButton("Fit view", self)
        self._fit_button.clicked.connect(self._fit_view)
        toolbar.addWidget(self._fit_button)
        self._clear_f0_button = QPushButton("Clear f₀", self)
        self._clear_f0_button.setToolTip("Remove the array-HVSR f₀ colouring from the markers.")
        self._clear_f0_button.clicked.connect(self.clear_f0_overlay)
        self._clear_f0_button.setVisible(False)
        toolbar.addWidget(self._clear_f0_button)
        self._satellite_button = QPushButton("Satellite", self)
        self._satellite_button.setCheckable(True)
        self._satellite_button.setToolTip(
            "Esri World Imagery basemap under the markers (fetched once "
            "per array extent, cached on disk for offline use)."
        )
        self._satellite_button.toggled.connect(self._on_satellite_toggled)
        toolbar.addWidget(self._satellite_button)
        toolbar.addStretch(1)
        self._status_label = QLabel("", self)
        toolbar.addWidget(self._status_label)
        root.addLayout(toolbar)

        # Attribution is part of the imagery's usage terms: visible
        # whenever the basemap is on (also carries the failure note when
        # a batch can't be fetched and the cache is cold).
        self._attribution_label = QLabel(ATTRIBUTION, self)
        attribution_font = self._attribution_label.font()
        attribution_font.setPointSizeF(max(6.0, attribution_font.pointSizeF() - 2.0))
        self._attribution_label.setFont(attribution_font)
        self._attribution_label.setVisible(False)

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

        # Viewport-following basemap (M6.5-F): pan/zoom → debounced
        # refetch of the visible region's tiles. The timer is a GUI-
        # thread child of the widget; ``sigRangeChanged`` only schedules
        # work while the Satellite layer is on.
        self._basemap_refresh_timer = QTimer(self)
        self._basemap_refresh_timer.setSingleShot(True)
        self._basemap_refresh_timer.setInterval(_BASEMAP_REFRESH_DEBOUNCE_MS)
        self._basemap_refresh_timer.timeout.connect(self._refresh_basemap_for_viewport)
        plot_item.getViewBox().sigRangeChanged.connect(self._schedule_basemap_refresh)

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
        root.addWidget(self._attribution_label)

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
        for mapping in (
            self._positions,
            self._failures,
            self._acq_states,
            self._conn_states,
            self._f0_overlay,
        ):
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

    def set_f0_overlay(self, values: dict[str, float]) -> None:
        """Replace the array-HVSR f₀ overlay (device → f₀ Hz; M5-B).

        Only positive, finite values for configured devices are kept —
        a device with no honest f₀ keeps its state colour rather than
        being painted onto the ramp.
        """
        keep = set(self._devices)
        self._f0_overlay = {
            name: float(f0)
            for name, f0 in values.items()
            if name in keep and math.isfinite(f0) and f0 > 0.0
        }
        self._rebuild()

    @Slot()
    def clear_f0_overlay(self) -> None:
        if not self._f0_overlay:
            return
        self._f0_overlay = {}
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
        f0 = self._f0_overlay.get(device)
        if f0 is not None:
            return _f0_ramp_color(f0, min(self._f0_overlay.values()), max(self._f0_overlay.values()))
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
        new_origin: tuple[float, float] | None = None
        if positioned:
            lat0 = sum(p.latitude for _, p in positioned) / len(positioned)
            lon0 = sum(p.longitude for _, p in positioned) / len(positioned)
            new_origin = (lat0, lon0)
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

        self._frame_origin = new_origin
        self._update_basemap(positioned)
        self._update_distances(positioned)
        self._update_unpositioned()
        status = f"{len(positioned)} of {len(self._devices)} devices positioned"
        if self._f0_overlay:
            f_lo = min(self._f0_overlay.values())
            f_hi = max(self._f0_overlay.values())
            status += f"   ·   f₀ overlay {f_lo:.2f} to {f_hi:.2f} Hz (blue→red)"
        self._clear_f0_button.setVisible(bool(self._f0_overlay))
        self._status_label.setText(status)

    def _update_distances(self, positioned: list[tuple[str, ResolvedPosition]]) -> None:
        # Same geometry shape M5 consumes (core.positions.station_geometry)
        # so the table and the future array report can never disagree.
        geometry = station_geometry(dict(positioned))
        pairs = [(a, b, meters) for (a, b), meters in geometry.distances_m.items()]
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
        view_box = self._plot.getPlotItem().getViewBox()
        view_box.autoRange()
        # Floor each axis independently: a single-device array
        # autoRanges to a degenerate point and everything data-sized
        # vanishes. Per-axis (not a square snap) so a wide-but-flat
        # array can never be zoomed AWAY from by its own Fit button;
        # the 1:1 aspect lock reconciles the final shape.
        (x0, x1), (y0, y1) = view_box.viewRange()
        if (x1 - x0) < _FIT_MIN_SPAN_M or (y1 - y0) < _FIT_MIN_SPAN_M:
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            half_x = max(x1 - x0, _FIT_MIN_SPAN_M) / 2.0
            half_y = max(y1 - y0, _FIT_MIN_SPAN_M) / 2.0
            view_box.setRange(
                xRange=(cx - half_x, cx + half_x),
                yRange=(cy - half_y, cy + half_y),
                padding=0.0,
            )

    # ------------------------------------------------------------------
    # M6.5-D — satellite basemap
    # ------------------------------------------------------------------
    @Slot(bool)
    def _on_satellite_toggled(self, checked: bool) -> None:
        if not checked:
            self._basemap_refresh_timer.stop()
            self._tile_generation += 1  # supersede anything in flight
            if self._tile_fetcher is not None:
                self._tile_fetcher.supersede(self._tile_generation)
            self._clear_tiles()
            self._basemap_memo = None
            self._basemap_extent = None
            self._tiles_origin = None
            self._attribution_label.setVisible(False)
            return
        self._attribution_label.setText(ATTRIBUTION)
        self._attribution_label.setVisible(True)
        self._update_basemap(self._positioned())

    @Slot()
    def _schedule_basemap_refresh(self, *_args: object) -> None:
        """Debounced trigger for pan/zoom (``sigRangeChanged``).

        A single pan/zoom gesture fires many range-changed signals;
        coalesce them into one fetch batch (rule 5). No-op while the
        Satellite layer is off.
        """
        if self._satellite_button.isChecked() and not self._basemap_shutdown:
            self._basemap_refresh_timer.start()

    def _ensure_tile_worker(self) -> TileFetcher:
        """Lazily boot the tile worker thread (first Satellite toggle).

        Tests that toggle the Satellite button MUST stub this method
        (see ``_stub_tile_worker`` in the widget tests): a real thread
        booted here is only ever joined by :meth:`shutdown_basemap`,
        which production wires from ``MainWindow.closeEvent`` — a test
        dropping a toggled widget without that call would drop a
        RUNNING QThread (hard Qt abort, the M6-0 lesson).
        """
        if self._tile_fetcher is None:
            fetcher = TileFetcher()
            thread = QThread()
            thread.setObjectName("map-tiles")
            fetcher.moveToThread(thread)
            self._tileRequested.connect(fetcher.fetch, Qt.ConnectionType.QueuedConnection)
            fetcher.tileReady.connect(self._on_tile_ready, Qt.ConnectionType.QueuedConnection)
            fetcher.batchFailed.connect(
                self._on_tile_batch_failed, Qt.ConnectionType.QueuedConnection
            )
            thread.start()
            self._tile_fetcher = fetcher
            self._tile_thread = thread
        return self._tile_fetcher

    def _update_basemap(self, positioned: list[tuple[str, ResolvedPosition]]) -> None:
        """React to a positions / frame-origin change while the basemap is on.

        Sets the rescue extent (the array bounding box), clears the
        drawn tiles when the frame origin actually moved (their rects
        were placed against the OLD origin), recentres a degenerate
        single-device view, and kicks an immediate viewport fetch.
        Pan/zoom go through the debounced ``_schedule_basemap_refresh``
        instead — this is the positions-changed entry only. No-op when
        the basemap is off.
        """
        if not self._satellite_button.isChecked():
            return
        if not positioned or self._frame_origin is None:
            self._clear_tiles()
            self._basemap_memo = None
            self._basemap_extent = None
            self._tiles_origin = None
            return
        lat0, lon0 = self._frame_origin
        east_north = [
            local_east_north(p.latitude, p.longitude, lat0, lon0) for _, p in positioned
        ]
        span_e = max(e for e, _ in east_north) - min(e for e, _ in east_north)
        span_n = max(n for _, n in east_north) - min(n for _, n in east_north)
        span_m = max(span_e, span_n, _BASEMAP_MIN_SPAN_M) * _BASEMAP_MARGIN
        centre_e = (max(e for e, _ in east_north) + min(e for e, _ in east_north)) / 2.0
        centre_n = (max(n for _, n in east_north) + min(n for _, n in east_north)) / 2.0
        half_span = span_m / 2.0
        self._basemap_extent = (
            centre_e - half_span,
            centre_n - half_span,
            centre_e + half_span,
            centre_n + half_span,
        )
        if self._tiles_origin != (lat0, lon0):
            # Frame origin moved: every drawn tile rect (computed at the
            # old origin) is now misplaced. Drop them; the disk cache
            # makes the immediate refetch cheap.
            self._clear_tiles()
            self._basemap_memo = None
            self._tiles_origin = (lat0, lon0)
        self._ensure_view_shows_basemap(recenter_if_outside=True)
        self._refresh_basemap_for_viewport()
        # The rescue's setRange emits sigRangeChanged, which armed the
        # debounce; the immediate refresh above already covered this
        # viewport, so cancel the echo (it would re-request the same,
        # still-in-flight tiles under a fresh generation). A genuine
        # later pan/zoom re-arms it.
        self._basemap_refresh_timer.stop()

    def _refresh_basemap_for_viewport(self) -> None:
        """Fetch the satellite tiles covering the CURRENT viewport (M6.5-F).

        The basemap follows pan/zoom: the visible east/north rectangle
        is inverse-projected to a lat/lon box, a zoom is chosen from the
        viewport span, and the covering tiles are fetched — only the
        ones not already drawn. Tiles that scroll out of view are left
        alone (the LRU cap reclaims them) so a back-and-forth pan reuses
        them straight from memory.
        """
        if (
            self._basemap_shutdown
            or not self._satellite_button.isChecked()
            or self._frame_origin is None
        ):
            return
        view_box = self._plot.getPlotItem().getViewBox()
        (e0, e1), (n0, n1) = view_box.viewRange()
        if (e1 - e0) <= 2.0 or (n1 - n0) <= 2.0:
            # Degenerate view (the single-device collapse); the rescue
            # owns this case — fetching here would request garbage.
            return
        lat0, lon0 = self._frame_origin
        margin_e = (e1 - e0) * _BASEMAP_VIEWPORT_MARGIN
        margin_n = (n1 - n0) * _BASEMAP_VIEWPORT_MARGIN
        lat_a, lon_a = east_north_to_latlon(e0 - margin_e, n0 - margin_n, lat0, lon0)
        lat_b, lon_b = east_north_to_latlon(e1 + margin_e, n1 + margin_n, lat0, lon0)
        span_m = max(e1 - e0, n1 - n0)
        zoom = zoom_for_span(span_m, lat0)
        wanted = tiles_for_extent(
            min(lat_a, lat_b),
            max(lat_a, lat_b),
            min(lon_a, lon_b),
            max(lon_a, lon_b),
            zoom,
        )
        memo = (zoom, frozenset(wanted))
        missing = [(x, y) for (x, y) in wanted if (zoom, x, y) not in self._tile_items]
        if memo == self._basemap_memo and not missing:
            return  # viewport unchanged and everything wanted is drawn
        self._basemap_memo = memo
        # Touch already-drawn wanted tiles to the LRU tail so an
        # incoming batch's eviction can't drop what is on screen.
        for x, y in wanted:
            key = (zoom, x, y)
            if key in self._tile_items:
                self._tile_items[key] = self._tile_items.pop(key)
        if not missing:
            return
        # A fresh request always restores the credit line: a stale
        # "imagery unavailable" note must never caption tiles that DO
        # arrive (Esri usage terms).
        self._attribution_label.setText(ATTRIBUTION)
        self._tile_generation += 1
        fetcher = self._ensure_tile_worker()
        fetcher.supersede(self._tile_generation)
        _log.info(
            "map_basemap_requested",
            generation=self._tile_generation,
            zoom=zoom,
            n_tiles=len(missing),
            span_m=round(span_m, 1),
        )
        self._tileRequested.emit(
            TileRequest(
                generation=self._tile_generation,
                zoom=zoom,
                tiles=tuple(missing),
            )
        )

    def _ensure_view_shows_basemap(self, *, recenter_if_outside: bool) -> None:
        """Rescue the viewport so requested imagery is never invisible.

        Always fixes a DEGENERATE view: a single positioned device
        autoRanges to a ~0 m span (the never-ranged ``[0, 1]`` default
        is degenerate too), so the data-sized tiles vanish behind the
        pixel-sized marker — the original "la tendina map NON
        visualizza la mappa satellitare". When ``recenter_if_outside``
        (positions/origin changed, or first toggle), also snaps a view
        that does not intersect the array onto it. Pan/zoom callers
        pass ``False``: once the user has deliberately panned to the
        surroundings, an arriving tile must NOT yank the view back to
        the array (M6.5-F).
        """
        if self._basemap_extent is None:
            return
        view_box = self._plot.getPlotItem().getViewBox()
        (x0, x1), (y0, y1) = view_box.viewRange()
        east_0, north_0, east_1, north_1 = self._basemap_extent
        degenerate = (x1 - x0) <= 2.0 or (y1 - y0) <= 2.0
        outside = recenter_if_outside and (
            x1 < east_0 or x0 > east_1 or y1 < north_0 or y0 > north_1
        )
        if degenerate or outside:
            view_box.setRange(
                xRange=(east_0, east_1),
                yRange=(north_0, north_1),
                padding=0.05,
            )

    @Slot(object)
    def _on_tile_ready(self, payload: object) -> None:
        if not isinstance(payload, TileResult):  # rule 4 guard
            return
        if (
            payload.generation != self._tile_generation
            or not self._satellite_button.isChecked()
            or self._frame_origin is None
        ):
            return
        lat0, lon0 = self._frame_origin
        lat_n, lon_w, lat_s, lon_e = tile_bounds(payload.zoom, payload.x, payload.y)
        east_w, north_n = local_east_north(lat_n, lon_w, lat0, lon0)
        east_e, north_s = local_east_north(lat_s, lon_e, lat0, lon0)
        # Row 0 of the decoded image is the tile's NORTH edge; pyqtgraph
        # in row-major mode draws row 0 at the rect's BOTTOM (y grows
        # upward) — flip so north stays up.
        image = np.ascontiguousarray(payload.image[::-1])
        item = pg.ImageItem(image, axisOrder="row-major")
        item.setRect(QRectF(east_w, north_s, east_e - east_w, north_n - north_s))
        # Finer zoom draws above coarser over the same ground, so a
        # zoom transition never flashes blank; still far below the
        # scatter (z 0).
        item.setZValue(_TILE_Z_VALUE + payload.zoom * _Z_PER_ZOOM)
        # ignoreBounds: "Fit view" keeps fitting the ARRAY, not the
        # (much larger) imagery patch.
        self._plot.getPlotItem().addItem(item, ignoreBounds=True)
        key = (payload.zoom, payload.x, payload.y)
        previous = self._tile_items.pop(key, None)
        if previous is not None:
            self._plot.getPlotItem().removeItem(previous)
        self._tile_items[key] = item  # newest at the LRU tail
        self._evict_tiles_over_cap()
        # The request-time rescue can be undone between request and
        # arrival: pyqtgraph's auto-range (enabled until the first
        # explicit setRange) collapses a single-marker view at PAINT
        # time, after `_update_basemap` checked it. Re-fix a DEGENERATE
        # view now that imagery is on screen — but never recentre a
        # view the user has panned to the surroundings (M6.5-F).
        self._ensure_view_shows_basemap(recenter_if_outside=False)

    def _evict_tiles_over_cap(self) -> None:
        """Drop the least-recently-touched tiles past the LRU cap.

        Bounds the drawn-ImageItem count (rule 5/8) as pan/zoom
        accumulate tiles; recently-viewed tiles survive a back-and-forth
        pan, and the disk cache makes any re-fetch cheap.
        """
        plot_item = self._plot.getPlotItem()
        while len(self._tile_items) > _MAX_TILE_ITEMS:
            old_key = next(iter(self._tile_items))  # LRU front = oldest
            plot_item.removeItem(self._tile_items.pop(old_key))

    @Slot(int, str)
    def _on_tile_batch_failed(self, generation: int, reason: str) -> None:
        if generation != self._tile_generation or not self._satellite_button.isChecked():
            return
        # Honest offline state on the attribution line — the map keeps
        # working without imagery (field laptops). Clearing the memo
        # lets the next refresh retry instead of believing the failed
        # batch is still on screen.
        self._basemap_memo = None
        self._attribution_label.setText(f"Satellite imagery unavailable: {reason}")

    def _clear_tiles(self) -> None:
        plot_item = self._plot.getPlotItem()
        for item in self._tile_items.values():
            plot_item.removeItem(item)
        self._tile_items.clear()

    def shutdown_basemap(self) -> None:
        """Stop the tile worker thread (bounded join; rule 7).

        Called from MainWindow.closeEvent. Idempotent; a widget whose
        Satellite button was never toggled owns no running thread.
        """
        self._basemap_shutdown = True
        self._basemap_refresh_timer.stop()
        if self._tile_thread is None:
            return
        fetcher = self._tile_fetcher
        if fetcher is not None:
            fetcher.stop()
            self._tile_generation += 1
            fetcher.supersede(self._tile_generation)
            with contextlib.suppress(RuntimeError, TypeError):
                self._tileRequested.disconnect(fetcher.fetch)
            # Best-effort queued close of the httpx client on ITS
            # thread. quit() below may interrupt the dispatcher before
            # this dispatches (POSTMORTEMS 2026-05-10 — quit is NOT a
            # queue barrier); the fetch loop also closes the client
            # itself when it observes the stop flag, and at worst the
            # OS reclaims the sockets at process exit.
            QMetaObject.invokeMethod(
                fetcher, "shutdown", Qt.ConnectionType.QueuedConnection
            )
        self._tile_thread.quit()
        joined = self._tile_thread.wait(_TILE_THREAD_JOIN_MS)
        # Sever the worker→owner direction in BOTH branches (skill §3:
        # disconnect at the join). For an abandoned pair this is what
        # keeps a still-running fetch from posting events into a widget
        # that is being destroyed as the app exits.
        if fetcher is not None:
            # Only the two directions the widget actually connects;
            # disconnecting a never-connected signal is a libpyside
            # RuntimeWarning, not an exception, so suppress won't hide
            # the noise.
            for signal in (fetcher.tileReady, fetcher.batchFailed):
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect()
        if not joined:
            # A tile fetch stuck inside its HTTP timeout can outlive the
            # bounded join; retain the pair instead of dropping a
            # running QThread (hard Qt abort — M6-0 lesson).
            if fetcher is not None:
                _ABANDONED.append((fetcher, self._tile_thread))
            _log.warning("map_tile_thread_join_timeout", abandoned=len(_ABANDONED))
        self._tile_fetcher = None
        self._tile_thread = None

    def closeEvent(self, event: object) -> None:  # noqa: N802 — Qt override
        """Stop the debounce timer + join the tile thread on widget close.

        MainWindow joins the basemap explicitly in its own closeEvent
        (Qt does not propagate closeEvent to docked children), so in the
        app this is belt-and-suspenders; for a standalone widget (tests,
        future embeds) it is the only cleanup — without it a debounce
        tick could spawn a tile thread mid-teardown. ``shutdown_basemap``
        is idempotent.
        """
        self.shutdown_basemap()
        super().closeEvent(event)  # type: ignore[arg-type]

    def _spot_tip(self, x: float, y: float, data: object) -> str:
        if not isinstance(data, str):
            return ""
        position = self._positions.get(data)
        if position is None:
            return data
        tip = (
            f"{data}\n"
            f"lat {position.latitude:.6f}, lon {position.longitude:.6f}\n"
            f"elev {position.elevation_m:.1f} m\n"
            f"source: {position.source}"
        )
        f0 = self._f0_overlay.get(data)
        if f0 is not None:
            tip += f"\nf₀ = {f0:.2f} Hz (array HVSR)"
        return tip

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
