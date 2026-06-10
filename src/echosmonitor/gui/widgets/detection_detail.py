"""Central "why did this fire?" detail pane for a selected detection (M8 B2).

Turns the post-M7 empty central widget into the detection workspace.
When a detection row is selected, the main window fetches that stream's
ring-buffer window via ``engine.read_recent`` and hands it here; the pane
renders:

* the relevant trace segment (top plot, wall-clock X axis),
* the recomputed STA/LTA ratio curve (bottom plot) — recomputed from the
  detection's stored ``meta`` (sta_s / lta_s) so the user sees the exact
  curve that crossed threshold,
* the on/off thresholds as horizontal lines on the ratio plot,
* the trigger window ``[t_on, t_off]`` shaded on both plots.

If the detection has scrolled out of the live ring buffer, the main
window dispatches an off-thread archive read (see
``core/archive_detail_loader.py``) and hands the prepared Z/N/E component
arrays here for a STATIC 3-component view (Stacked / Overlaid toggle, no
playback). The ``show_detection`` scrolled-out branch remains as a
defensive fallback with an honest brief message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.dsp.stages import sta_lta_ratio

if TYPE_CHECKING:
    from obspy import UTCDateTime

    from echosmonitor.core.archive_detail_loader import ComponentTrace
    from echosmonitor.core.models import Detection

_TRACE_PEN = pg.mkPen("#888", width=1)
_RATIO_PEN = pg.mkPen("#e0a030", width=1)  # amber, matches detection markers
_ON_PEN = pg.mkPen("#e0a030", width=1, style=Qt.PenStyle.DashLine)
_OFF_PEN = pg.mkPen("#8a6", width=1, style=Qt.PenStyle.DashLine)
_REGION_BRUSH = pg.mkBrush(224, 160, 48, 60)  # semi-transparent amber
# 3C archive component pens (distinct, readable on a dark background). The
# SAME pen object is reused for a component's stacked and overlaid curve so
# the overlay legend colours match the stacked rows.
_Z_PEN = pg.mkPen("#e6e6e6", width=1)  # near-white
_N_PEN = pg.mkPen("#5cc8ff", width=1)  # cyan-blue
_E_PEN = pg.mkPen("#ff9a52", width=1)  # orange
_ARC_COMP_PENS: dict[str, pg.mkPen] = {"Z": _Z_PEN, "N": _N_PEN, "E": _E_PEN}
_ARC_COMPONENTS: tuple[str, ...] = ("Z", "N", "E")
_DEFAULT_RATIO_LABEL = "STA/LTA"
# Top-trace Y-axis label when showing raw counts (the source of truth).
_COUNTS_LABEL = "counts"
# Unit selector items: (display, output-code). Order is fixed; Counts is
# always index 0 and always enabled. The three physical items are enabled
# only when a matching instrument response exists for the channel.
_UNIT_ITEMS: tuple[tuple[str, str], ...] = (
    ("Counts", "COUNTS"),
    ("Velocity (m/s)", "VEL"),
    ("Acceleration (m/s²)", "ACC"),
    ("Displacement (m)", "DISP"),
)
# Tooltip shown on the selector when the channel has no usable response.
NO_RESPONSE_TOOLTIP = (
    "No response metadata for this channel — set response_metadata in the device config."
)
_SCROLLED_OUT_MSG = "This detection has scrolled out of the live buffer."
_LOADING_MSG = "Loading waveform from the archive…"
_NO_ARCHIVE_MSG = "No archived waveform data exists for this detection's time window."
_EMPTY_MSG = "Select a detection to see the trace segment and the STA/LTA ratio that fired it."


class DetectionDetailPane(QWidget):
    """Trace + STA/LTA ratio + thresholds for one selected detection.

    M11 B adds a unit selector on the title row that re-renders the TOP
    trace in physical units (velocity / acceleration / displacement) by
    deconvolving the instrument response off the GUI thread. Counts remain
    the source of truth; the bottom ratio plot is untouched by the unit
    choice (it always operates on counts).
    """

    # ``unitChangeRequested(unit_code)`` — emitted when the user picks a
    # unit from the selector (``"COUNTS"``, ``"VEL"``, ``"ACC"``,
    # ``"DISP"``). Suppressed while the pane resets the selector itself.
    unitChangeRequested = Signal(str)  # noqa: N815

    # ``componentLayoutChanged(layout)`` — emitted when the user toggles the
    # 3C archive view between ``"stacked"`` and ``"overlaid"``. Suppressed
    # while the pane sets the radios programmatically.
    componentLayoutChanged = Signal(str)  # noqa: N815

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._title = QLabel("")
        self._title.setObjectName("DetectionDetailTitle")
        self._title.setStyleSheet(
            "QLabel#DetectionDetailTitle { font-weight: bold; padding: 4px 8px; }"
        )
        # BUG 2 fix: a long, single-line detection title (wordWrap stays
        # False for a tidy one-liner) must NOT dictate the pane's minimum
        # width. Without this, the title's full-text width (~580px) would
        # propagate up through the central QStackedWidget (whose minimum is
        # the MAX over all pages, so it persists even when the placeholder
        # is shown) and pin the whole middle-row layout, squeezing the side
        # docks to their minimum and freezing the splitters. An ``Ignored``
        # horizontal policy lets the label shrink below its text width; we
        # elide the text to whatever width it is actually given (see
        # ``_apply_elided_title`` / ``resizeEvent``) and keep the full text
        # in a tooltip so nothing is lost.
        self._title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._full_title = ""

        # Unit selector (M11 B). Guarded by ``_suppress_unit_signal`` so a
        # programmatic reset to Counts (on a new detection) never emits a
        # spurious request.
        self._suppress_unit_signal = False
        self._unit_combo = QComboBox()
        self._unit_combo.setObjectName("DetectionDetailUnitCombo")
        for label, code in _UNIT_ITEMS:
            self._unit_combo.addItem(label, userData=code)
        self._unit_combo.currentIndexChanged.connect(self._on_unit_index_changed)

        # Stacked / Overlaid toggle for the 3C archive view (M12). Hidden in
        # live / message / loading states; shown only by ``show_archive_3c``.
        # Guarded by ``_suppress_layout_signal`` (mirrors the unit pattern) so
        # a programmatic ``set_component_layout`` never re-emits.
        self._suppress_layout_signal = False
        self._arc_layout = "stacked"
        self._layout_toggle = QWidget()
        toggle_layout = QHBoxLayout(self._layout_toggle)
        toggle_layout.setContentsMargins(0, 0, 0, 0)
        toggle_layout.setSpacing(4)
        self._stacked_radio = QRadioButton("Stacked")
        self._overlaid_radio = QRadioButton("Overlaid")
        self._stacked_radio.setChecked(True)
        self._layout_group = QButtonGroup(self)
        self._layout_group.addButton(self._stacked_radio)
        self._layout_group.addButton(self._overlaid_radio)
        toggle_layout.addWidget(self._stacked_radio)
        toggle_layout.addWidget(self._overlaid_radio)
        self._stacked_radio.toggled.connect(self._on_layout_toggled)
        self._layout_toggle.setVisible(False)

        title_row = QWidget(self)
        title_layout = QHBoxLayout(title_row)
        title_layout.setContentsMargins(0, 0, 8, 0)
        title_layout.addWidget(self._title, stretch=1)
        title_layout.addWidget(self._layout_toggle)
        title_layout.addWidget(self._unit_combo)

        # Stored counts context for re-render when the unit changes. Set on
        # every successful counts render (``show_detection``); the worker is
        # fed from the MainWindow-side context, but the pane keeps its own
        # copy so ``revert_to_counts`` / ``show_physical_trace`` can reuse
        # the SAME wall-clock X axis (same window) for the physical trace.
        self._counts_x: np.ndarray = np.empty(0, dtype=np.float64)
        self._counts_y: np.ndarray = np.empty(0, dtype=np.float64)
        self._fs: float = 0.0
        self._ctx_device: str = ""
        self._ctx_nslc: str = ""
        self._ctx_start_epoch: float = 0.0

        self._graphics = pg.GraphicsLayoutWidget()
        self._trace_plot = self._graphics.addPlot(
            row=0, col=0, axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self._trace_plot.setMouseEnabled(x=True, y=True)
        self._trace_plot.showGrid(x=True, y=True, alpha=0.3)
        self._trace_plot.setMenuEnabled(False)
        self._trace_plot.setLabel("left", _COUNTS_LABEL)
        self._trace_curve = self._trace_plot.plot(pen=_TRACE_PEN)

        self._ratio_plot = self._graphics.addPlot(
            row=1, col=0, axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self._ratio_plot.setMouseEnabled(x=True, y=True)
        self._ratio_plot.showGrid(x=True, y=True, alpha=0.3)
        self._ratio_plot.setMenuEnabled(False)
        self._ratio_plot.setXLink(self._trace_plot)
        self._ratio_plot.setLabel("left", "STA/LTA")
        self._ratio_curve = self._ratio_plot.plot(pen=_RATIO_PEN)

        # Reusable overlay items (added/removed per render to keep them tidy).
        self._on_line = pg.InfiniteLine(angle=0, pen=_ON_PEN, movable=False)
        self._off_line = pg.InfiniteLine(angle=0, pen=_OFF_PEN, movable=False)
        self._trace_region = pg.LinearRegionItem(brush=_REGION_BRUSH, movable=False)
        self._ratio_region = pg.LinearRegionItem(brush=_REGION_BRUSH, movable=False)
        for item in (self._trace_region, self._ratio_region):
            item.setZValue(-10)

        self._message = QLabel(_EMPTY_MSG)
        self._message.setObjectName("DetectionDetailMessage")
        self._message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setStyleSheet(
            "QLabel#DetectionDetailMessage { color: #888; font-style: italic; padding: 24px; }"
        )

        # 3C archive view (M12). A QWidget hosting a nested QStackedLayout
        # with two GraphicsLayoutWidget children built ONCE here; curves are
        # created once and fed via setData on show, toggled by switching the
        # current child — never rebuilt.
        self._build_archive_graphics()

        stack_host = QWidget(self)
        self._stack = QStackedLayout(stack_host)
        self._stack.addWidget(self._graphics)
        self._stack.addWidget(self._message)
        self._stack.addWidget(self._archive_graphics)
        self._stack.setCurrentWidget(self._message)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(title_row)
        root.addWidget(stack_host, stretch=1)

        self._overlays_attached = False

        # 3C archive state: the components shown most recently and the
        # trigger component letter (so a physical-unit swap can reuse the
        # stored per-component X grid).
        self._arc_traces: dict[str, ComponentTrace] = {}
        self._arc_trigger_comp = ""

    # ------------------------------------------------------------------
    def _build_archive_graphics(self) -> None:
        """Build the static 3C archive page (stacked + overlaid GLWs).

        All plots and curves are created exactly once here; ``show_archive_3c``
        only feeds them via ``setData`` and toggles which GLW is current.
        """
        self._archive_graphics = QWidget(self)
        arc_host_layout = QVBoxLayout(self._archive_graphics)
        arc_host_layout.setContentsMargins(0, 0, 0, 0)
        self._arc_stack = QStackedLayout()
        arc_host_layout.addLayout(self._arc_stack)

        # ---- Stacked GLW: Z / N / E component rows + a ratio row. ----
        self._arc_stacked_glw = pg.GraphicsLayoutWidget()
        self._arc_plots: dict[str, pg.PlotItem] = {}
        self._arc_curves: dict[str, pg.PlotDataItem] = {}
        self._arc_regions: dict[str, pg.LinearRegionItem] = {}
        for row, comp in enumerate(_ARC_COMPONENTS):
            plot = self._arc_stacked_glw.addPlot(
                row=row, col=0, axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
            )
            plot.setMouseEnabled(x=True, y=True)
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setMenuEnabled(False)
            plot.setLabel("left", f"{comp} (counts)")
            self._arc_plots[comp] = plot
            self._arc_curves[comp] = plot.plot(pen=_ARC_COMP_PENS[comp])
            region = pg.LinearRegionItem(brush=_REGION_BRUSH, movable=False)
            region.setZValue(-10)
            plot.addItem(region)
            self._arc_regions[comp] = region
        # X-link N / E to Z.
        self._arc_plots["N"].setXLink(self._arc_plots["Z"])
        self._arc_plots["E"].setXLink(self._arc_plots["Z"])

        # Ratio row (the "why it fired" curve for the trigger component).
        self._arc_ratio_plot = self._arc_stacked_glw.addPlot(
            row=len(_ARC_COMPONENTS),
            col=0,
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")},
        )
        self._arc_ratio_plot.setMouseEnabled(x=True, y=True)
        self._arc_ratio_plot.showGrid(x=True, y=True, alpha=0.3)
        self._arc_ratio_plot.setMenuEnabled(False)
        self._arc_ratio_plot.setXLink(self._arc_plots["Z"])
        self._arc_ratio_plot.setLabel("left", _DEFAULT_RATIO_LABEL)
        self._arc_ratio_curve = self._arc_ratio_plot.plot(pen=_RATIO_PEN)
        self._arc_on_line = pg.InfiniteLine(angle=0, pen=_ON_PEN, movable=False)
        self._arc_off_line = pg.InfiniteLine(angle=0, pen=_OFF_PEN, movable=False)
        for line in (self._arc_on_line, self._arc_off_line):
            line.setVisible(False)
            self._arc_ratio_plot.addItem(line)
        self._arc_ratio_region = pg.LinearRegionItem(brush=_REGION_BRUSH, movable=False)
        self._arc_ratio_region.setZValue(-10)
        self._arc_ratio_plot.addItem(self._arc_ratio_region)

        # ---- Overlaid GLW: one plot, three coloured curves + legend. ----
        self._arc_overlay_glw = pg.GraphicsLayoutWidget()
        self._arc_overlay_plot = self._arc_overlay_glw.addPlot(
            row=0, col=0, axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self._arc_overlay_plot.setMouseEnabled(x=True, y=True)
        self._arc_overlay_plot.showGrid(x=True, y=True, alpha=0.3)
        self._arc_overlay_plot.setMenuEnabled(False)
        self._arc_overlay_plot.setLabel("left", "counts")
        self._arc_overlay_plot.addLegend()
        self._arc_overlay_curves: dict[str, pg.PlotDataItem] = {}
        for comp in _ARC_COMPONENTS:
            self._arc_overlay_curves[comp] = self._arc_overlay_plot.plot(
                pen=_ARC_COMP_PENS[comp], name=comp
            )
        self._arc_overlay_region = pg.LinearRegionItem(brush=_REGION_BRUSH, movable=False)
        self._arc_overlay_region.setZValue(-10)
        self._arc_overlay_plot.addItem(self._arc_overlay_region)

        self._arc_stack.addWidget(self._arc_stacked_glw)
        self._arc_stack.addWidget(self._arc_overlay_glw)
        self._arc_stack.setCurrentWidget(self._arc_stacked_glw)

    # ------------------------------------------------------------------
    def clear(self) -> None:
        """Return to the empty hint (no detection selected)."""
        self._set_title("")
        self._message.setText(_EMPTY_MSG)
        self._stack.setCurrentWidget(self._message)
        self._reset_combo_to_counts()
        self._unit_combo.setEnabled(False)
        self._layout_toggle.setVisible(False)

    def _set_title(self, text: str) -> None:
        """Set the title, storing the full text and showing it elided.

        The label has an ``Ignored`` horizontal size policy so it never
        pins the pane's minimum width (BUG 2). We keep the full text in
        ``_full_title`` and render an end-elided copy that fits the label's
        current width; the full text stays available as a tooltip.
        """
        self._full_title = text
        self._title.setToolTip(text)
        self._apply_elided_title()

    def _apply_elided_title(self) -> None:
        """Render ``_full_title`` end-elided to the label's current width."""
        width = self._title.width()
        if width <= 0 or not self._full_title:
            self._title.setText(self._full_title)
            return
        metrics = QFontMetrics(self._title.font())
        self._title.setText(
            metrics.elidedText(self._full_title, Qt.TextElideMode.ElideRight, width)
        )

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        # Re-elide the title to the new width so it tracks pane resizes.
        self._apply_elided_title()

    def show_detection(
        self,
        detection: Detection,
        samples: np.ndarray,
        fs: float,
        latest_t: UTCDateTime | None,
    ) -> None:
        """Render the detection's trace window + STA/LTA ratio.

        ``samples`` is the ring-buffer tail (length ``n``) whose LAST
        sample is at ``latest_t``; ``fs`` is its sample rate. If the
        detection's onset predates the start of that window (scrolled
        out), the honest archive-replay message is shown instead.
        """
        self._set_title(self._format_title(detection))
        self._layout_toggle.setVisible(False)
        n = int(samples.shape[0]) if samples is not None else 0
        if n == 0 or fs <= 0 or latest_t is None:
            self._message.setText(_SCROLLED_OUT_MSG)
            self._stack.setCurrentWidget(self._message)
            self._reset_combo_to_counts()
            self._unit_combo.setEnabled(False)
            return

        latest = float(latest_t)
        # x[i] = latest - (n-1-i)/fs  (rightmost sample anchored to latest_t)
        x = latest - (n - 1 - np.arange(n, dtype=np.float64)) / fs
        t_on = float(detection.t_on)
        if t_on < x[0]:
            # Onset is older than the oldest buffered sample → scrolled out.
            self._message.setText(_SCROLLED_OUT_MSG)
            self._stack.setCurrentWidget(self._message)
            self._reset_combo_to_counts()
            self._unit_combo.setEnabled(False)
            return

        y = samples.astype(np.float64, copy=False)
        self._trace_curve.setData(x, y)
        self._trace_plot.setLabel("left", _COUNTS_LABEL)

        # Store the counts render context so a unit change can re-render the
        # TOP trace against the SAME wall-clock X axis (counts stay the
        # source of truth, rule 8). The window start epoch is x[0].
        self._counts_x = x
        self._counts_y = y
        self._fs = float(fs)
        self._ctx_device = detection.device
        self._ctx_nslc = detection.nslc
        self._ctx_start_epoch = float(x[0])

        # Reset the selector to Counts (suppressed so no spurious request)
        # and re-enable it; per-physical-item enablement is set separately
        # via ``set_response_available`` once the caller resolves it.
        self._reset_combo_to_counts()
        self._unit_combo.setEnabled(True)

        self._render_ratio(detection, x, y, fs)
        self._apply_overlays(detection, x)
        self._stack.setCurrentWidget(self._graphics)

    # ------------------------------------------------------------------
    # Unit selector (M11 B)
    # ------------------------------------------------------------------
    def _on_unit_index_changed(self, index: int) -> None:
        if self._suppress_unit_signal:
            return
        code = self._unit_combo.itemData(index)
        if isinstance(code, str):
            self.unitChangeRequested.emit(code)

    def _on_layout_toggled(self, checked: bool) -> None:
        """React to the user switching the Stacked/Overlaid radios.

        Connected to the Stacked radio's ``toggled(checked)``. Because the
        two radios share an exclusive ``QButtonGroup``, ``checked`` carries
        the full state (``True`` → stacked selected, ``False`` → overlaid),
        so this fires exactly once per user change. Programmatic sets are
        suppressed via ``_suppress_layout_signal``.
        """
        layout = "stacked" if checked else "overlaid"
        self.set_component_layout(layout)
        if not self._suppress_layout_signal:
            self.componentLayoutChanged.emit(layout)

    def _reset_combo_to_counts(self) -> None:
        """Force the selector to Counts (index 0) without emitting."""
        self._suppress_unit_signal = True
        try:
            self._unit_combo.setCurrentIndex(0)
        finally:
            self._suppress_unit_signal = False

    def set_response_available(self, available: bool, tooltip: str) -> None:
        """Enable/disable the three physical units and set the tooltip.

        ``Counts`` (index 0) is always enabled. When ``available`` is
        ``False`` the three physical items are disabled and ``tooltip``
        (typically :data:`NO_RESPONSE_TOOLTIP`) is shown on the combo.
        """
        model = self._unit_combo.model()
        for i in range(1, self._unit_combo.count()):
            item = model.item(i)  # type: ignore[attr-defined]
            if item is not None:
                item.setEnabled(available)
        self._unit_combo.setToolTip("" if available else tooltip)

    def show_physical_trace(self, unit_label: str, samples: np.ndarray) -> None:
        """Swap the TOP trace to physical units (same X window as counts).

        The bottom ratio plot is untouched — it stays on
        counts. Clears any "computing…" busy state.
        """
        y = np.asarray(samples, dtype=np.float64)
        self._trace_curve.setData(self._counts_x, y)
        self._trace_plot.setLabel("left", unit_label)
        self.set_computing(False)

    def is_showing_plots(self) -> bool:
        """Whether the LIVE single-trace graphics page is current."""
        return self._stack.currentWidget() is self._graphics

    def is_showing_archive(self) -> bool:
        """Whether the static 3C archive page is current."""
        return self._stack.currentWidget() is self._archive_graphics

    def rendered_counts_context(self) -> tuple[float, float] | None:
        """The current window's ``(fs, start_epoch)`` if a real window is up.

        Returns the trigger context whenever a real window with ``_counts_x``
        is rendered — either the LIVE single-trace page or the static 3C
        archive page (whose context is the trigger component). ``None`` when
        the message page is showing (loading / scrolled-out / no-data /
        empty). Used by the main window to feed the deconvolution worker and
        to resolve response availability against the window's start time.
        """
        if not (self.is_showing_plots() or self.is_showing_archive()):
            return None
        return self._fs, self._ctx_start_epoch

    def counts_samples(self) -> np.ndarray:
        """The float64 counts currently rendered on the top trace."""
        return self._counts_y

    def revert_to_counts(self) -> None:
        """Restore the TOP trace to counts and reset the selector.

        Used when the user picks Counts, or when a deconvolution fails.
        """
        self._trace_curve.setData(self._counts_x, self._counts_y)
        self._trace_plot.setLabel("left", _COUNTS_LABEL)
        self._reset_combo_to_counts()
        self.set_computing(False)

    def set_computing(self, computing: bool) -> None:
        """Brief busy indicator while a deconvolution is in flight.

        Disables the selector and titles the top plot "computing…" so the
        user sees the request was accepted; cleared on the result.
        """
        self._unit_combo.setEnabled(not computing)
        self._trace_plot.setTitle("computing…" if computing else None)

    # ------------------------------------------------------------------
    # 3C archive view (M12)
    # ------------------------------------------------------------------
    def set_loading(self, detection: Detection) -> None:
        """Show a transient loading message while the archive read runs.

        Sets the title from the detection, switches to the message page with
        :data:`_LOADING_MSG`, disables the unit selector, and hides the
        Stacked/Overlaid toggle.
        """
        self._set_title(self._format_title(detection))
        self._message.setText(_LOADING_MSG)
        self._stack.setCurrentWidget(self._message)
        self._reset_combo_to_counts()
        self._unit_combo.setEnabled(False)
        self._layout_toggle.setVisible(False)

    def show_no_archive_data(self, detection: Detection) -> None:
        """Show the no-archived-data message for this detection's window."""
        self._set_title(self._format_title(detection))
        self._message.setText(_NO_ARCHIVE_MSG)
        self._stack.setCurrentWidget(self._message)
        self._reset_combo_to_counts()
        self._unit_combo.setEnabled(False)
        self._layout_toggle.setVisible(False)

    def show_archive_3c(
        self,
        detection: Detection,
        traces: list[ComponentTrace],
        trigger_comp: str,
        view_start_epoch: float | None = None,
        view_end_epoch: float | None = None,
    ) -> None:
        """Render a static 3-component archive view of a scrolled-out event.

        Feeds the prepared per-component arrays into the stacked and overlaid
        curves (both built once), shades the trigger window, renders the
        trigger component's "why it fired" ratio, sets the trigger counts
        context so the unit selector works, and shows the Stacked/Overlaid
        toggle (re-applying the remembered ``self._arc_layout``).

        Args:
            detection: The selected detection (for the title / device).
            traces: Present components only (Z first), each a
                ``ComponentTrace`` with wall-clock ``x`` and NaN-gap ``y``.
            trigger_comp: The component letter of the detection's own NSLC.
            view_start_epoch: Left edge (POSIX epoch) of the on-screen X
                window. When given with ``view_end_epoch`` the X axis is
                ranged to ``[view_start, view_end]`` rather than the full data
                span: the archive read deliberately pulls extra pre-roll so a
                recomputed recursive STA/LTA's LTA has converged by the onset
                (H3 warm-up fix), and that pre-roll is rendered OFF-SCREEN so
                the ratio peak lines up with the trigger window. ``None``
                (both) falls back to the full data span.
            view_end_epoch: Right edge (POSIX epoch) of the on-screen X window.
        """
        self._set_title(self._format_title(detection))
        self._arc_traces = {t.comp: t for t in traces}
        self._arc_trigger_comp = trigger_comp

        empty = np.empty(0, dtype=np.float64)
        for comp in _ARC_COMPONENTS:
            trace = self._arc_traces.get(comp)
            if trace is not None:
                # NaN preserved — pyqtgraph breaks the line at NaN (honest gaps).
                self._arc_curves[comp].setData(trace.x, trace.y)
                self._arc_overlay_curves[comp].setData(trace.x, trace.y)
                self._arc_plots[comp].setLabel("left", f"{comp} (counts)")
            else:
                self._arc_curves[comp].setData(empty, empty)
                self._arc_overlay_curves[comp].setData(empty, empty)
                self._arc_plots[comp].setLabel("left", f"{comp} (no data)")

        trigger = self._arc_traces.get(trigger_comp)
        if trigger is None and self._arc_traces:
            # Fall back to the first present component for the ratio/context.
            trigger = next(iter(self._arc_traces.values()))
            trigger_comp = trigger.comp
            self._arc_trigger_comp = trigger_comp

        self._render_arc_ratio(detection, trigger)
        self._shade_arc_regions(detection, trigger)

        # Fixed window: anchor the shared X axis. The stacked component/ratio
        # plots are x-linked to the Z plot, so ranging Z propagates to them;
        # the overlay plot is independent. Without an explicit range the
        # source view stays at its default [0, 1] X range (the x-linked
        # trigger-window region does not autorange the source), so the
        # wall-clock curves would render off-screen.
        #
        # Prefer the caller's VIEW window over the full data span: the read
        # may extend further left than the inspect window (STA/LTA warm-up
        # pre-roll, H3) and that pre-roll must stay off-screen so the
        # recomputed ratio's peak lines up with the trigger window instead of
        # drifting to the right edge. Fall back to the data span when no view
        # window is supplied (direct pane callers / tests).
        if view_start_epoch is not None and view_end_epoch is not None:
            self._arc_plots["Z"].setXRange(
                float(view_start_epoch), float(view_end_epoch), padding=0.02
            )
            self._arc_overlay_plot.setXRange(
                float(view_start_epoch), float(view_end_epoch), padding=0.02
            )
        elif trigger is not None and trigger.x.size:
            x0, x1 = float(trigger.x[0]), float(trigger.x[-1])
            self._arc_plots["Z"].setXRange(x0, x1, padding=0.02)
            self._arc_overlay_plot.setXRange(x0, x1, padding=0.02)

        if trigger is not None:
            # Trigger context so the unit selector works (same attrs the live
            # path sets); is_showing_archive() — not is_showing_plots() —
            # keeps these gated correctly.
            self._counts_x = trigger.x
            self._counts_y = trigger.y
            self._fs = float(trigger.fs)
            self._ctx_device = detection.device
            self._ctx_nslc = trigger.nslc
            self._ctx_start_epoch = float(trigger.x[0]) if trigger.x.size else 0.0

        self._reset_combo_to_counts()
        self._unit_combo.setEnabled(True)
        self._layout_toggle.setVisible(True)
        self.set_component_layout(self._arc_layout)
        self._stack.setCurrentWidget(self._archive_graphics)

    def set_component_layout(self, layout: str) -> None:
        """Switch the 3C view between ``"stacked"`` and ``"overlaid"``.

        Idempotent; the data already lives in the curve items, so this only
        swaps the current child GLW and remembers the choice (per session).
        Guards the radios against re-emitting while it syncs them.
        """
        self._arc_layout = "overlaid" if layout == "overlaid" else "stacked"
        if self._arc_layout == "overlaid":
            self._arc_stack.setCurrentWidget(self._arc_overlay_glw)
        else:
            self._arc_stack.setCurrentWidget(self._arc_stacked_glw)
        self._suppress_layout_signal = True
        try:
            if self._arc_layout == "overlaid":
                self._overlaid_radio.setChecked(True)
            else:
                self._stacked_radio.setChecked(True)
        finally:
            self._suppress_layout_signal = False

    def show_physical_component(self, comp: str, unit_label: str, samples: np.ndarray) -> None:
        """Swap one component's Y to physical units (reusing its X grid).

        Updates BOTH the stacked and overlaid curve for ``comp`` against the
        stored component X grid and relabels that component plot. The busy
        state is NOT cleared here — the caller clears it once the whole
        (≤3-component) batch has landed.
        """
        trace = self._arc_traces.get(comp)
        if trace is None:
            return
        y = np.asarray(samples, dtype=np.float64)
        self._arc_curves[comp].setData(trace.x, y)
        self._arc_overlay_curves[comp].setData(trace.x, y)
        self._arc_plots[comp].setLabel("left", f"{comp} ({unit_label})")
        # NB: the busy state is cleared by the caller once the WHOLE batch of
        # (≤3) components has landed — not here, or it would re-enable the
        # selector after only the first component arrives.

    def revert_archive_to_counts(self) -> None:
        """Restore every archive component to its original counts.

        Re-feeds each present component's stored counts onto both the
        stacked and overlaid curve, restores the counts labels, and resets
        the unit selector. Used when the user picks Counts (or a
        deconvolution fails) while the 3C archive view is up.
        """
        for comp, trace in self._arc_traces.items():
            self._arc_curves[comp].setData(trace.x, trace.y)
            self._arc_overlay_curves[comp].setData(trace.x, trace.y)
            self._arc_plots[comp].setLabel("left", f"{comp} ({_COUNTS_LABEL})")
        self._reset_combo_to_counts()
        self.set_computing(False)

    def _render_arc_ratio(self, detection: Detection, trigger_trace: ComponentTrace | None) -> None:
        """Render the trigger component's "why it fired" curve (archive).

        Mirrors the live ``_render_ratio`` logic but writes to the dedicated
        ``_arc_ratio_*`` items so the live items the existing tests assert on
        are untouched. ``np.nan_to_num`` is applied ONLY to the STA/LTA input
        — the component TRACE plots keep their NaN gaps.
        """
        for line in (self._arc_on_line, self._arc_off_line):
            line.setVisible(False)
        if trigger_trace is None:
            self._arc_ratio_curve.setData(np.empty(0), np.empty(0))
            return

        meta = detection.meta if isinstance(detection.meta, dict) else {}
        x = trigger_trace.x
        sta_s = meta.get("sta_s")
        lta_s = meta.get("lta_s")
        if not (isinstance(sta_s, (int, float)) and isinstance(lta_s, (int, float))):
            # No STA/LTA params recorded — clear rather than guess.
            self._arc_ratio_curve.setData(np.empty(0), np.empty(0))
            return
        y_clean = np.nan_to_num(trigger_trace.y, nan=0.0)
        ratio = sta_lta_ratio(y_clean, float(sta_s), float(lta_s), trigger_trace.fs)
        self._arc_ratio_plot.setLabel("left", _DEFAULT_RATIO_LABEL)
        if ratio.size != x.shape[0]:
            self._arc_ratio_curve.setData(np.empty(0), np.empty(0))
            return
        self._arc_ratio_curve.setData(x, ratio)
        on_thr = meta.get("on_thr")
        off_thr = meta.get("off_thr")
        if isinstance(on_thr, (int, float)):
            self._arc_on_line.setValue(float(on_thr))
            self._arc_on_line.setVisible(True)
        if isinstance(off_thr, (int, float)):
            self._arc_off_line.setValue(float(off_thr))
            self._arc_off_line.setVisible(True)

    def _shade_arc_regions(
        self, detection: Detection, trigger_trace: ComponentTrace | None
    ) -> None:
        """Shade ``[t_on, t_off or last-x]`` on all stacked plots + overlay."""
        t_on = float(detection.t_on)
        if detection.t_off is not None:
            t_off = float(detection.t_off)
        elif trigger_trace is not None and trigger_trace.x.size:
            t_off = float(trigger_trace.x[-1])
        else:
            t_off = t_on
        span = (t_on, t_off)
        for region in self._arc_regions.values():
            region.setRegion(span)
        self._arc_ratio_region.setRegion(span)
        self._arc_overlay_region.setRegion(span)

    # ------------------------------------------------------------------
    def _render_ratio(
        self,
        detection: Detection,
        x: np.ndarray,
        y: np.ndarray,
        fs: float,
    ) -> None:
        self._ratio_plot.setLabel("left", _DEFAULT_RATIO_LABEL)
        sta_s = detection.meta.get("sta_s")
        lta_s = detection.meta.get("lta_s")
        if not isinstance(sta_s, (int, float)) or not isinstance(lta_s, (int, float)):
            # No STA/LTA params recorded (e.g. a future detector kind) —
            # clear the ratio curve rather than guess.
            self._ratio_curve.setData(np.empty(0), np.empty(0))
            return
        # Recompute via the pure dsp helper (keeps the seismology in dsp/).
        ratio = sta_lta_ratio(y, float(sta_s), float(lta_s), fs)
        if ratio.size != x.shape[0]:
            self._ratio_curve.setData(np.empty(0), np.empty(0))
            return
        self._ratio_curve.setData(x, ratio)

    def _apply_overlays(self, detection: Detection, x: np.ndarray) -> None:
        if not self._overlays_attached:
            self._ratio_plot.addItem(self._on_line)
            self._ratio_plot.addItem(self._off_line)
            self._trace_plot.addItem(self._trace_region)
            self._ratio_plot.addItem(self._ratio_region)
            self._overlays_attached = True

        on_thr = detection.meta.get("on_thr")
        off_thr = detection.meta.get("off_thr")
        self._on_line.setVisible(isinstance(on_thr, (int, float)))
        self._off_line.setVisible(isinstance(off_thr, (int, float)))
        if isinstance(on_thr, (int, float)):
            self._on_line.setValue(float(on_thr))
        if isinstance(off_thr, (int, float)):
            self._off_line.setValue(float(off_thr))

        t_on = float(detection.t_on)
        # An open detection has no end yet — shade to the right edge.
        t_off = float(detection.t_off) if detection.t_off is not None else float(x[-1])
        self._trace_region.setRegion((t_on, t_off))
        self._ratio_region.setRegion((t_on, t_off))

    @staticmethod
    def _format_title(detection: Detection) -> str:
        off = "open" if detection.t_off is None else str(detection.t_off)
        return (
            f"{detection.nslc}  ·  {detection.device}  ·  {detection.kind}  ·  "
            f"score {detection.score:.2f}  ·  t_on {detection.t_on}  →  {off}"
        )

    # ----- test-only accessors -----
    def _is_showing_plots_for_test(self) -> bool:
        return self._stack.currentWidget() is self._graphics

    def _trace_curve_for_test(self) -> pg.PlotDataItem:
        return self._trace_curve

    def _ratio_curve_for_test(self) -> pg.PlotDataItem:
        return self._ratio_curve

    def _ratio_label_for_test(self) -> str:
        return str(self._ratio_plot.getAxis("left").labelText)

    def _message_text_for_test(self) -> str:
        return self._message.text()

    def _title_tooltip_for_test(self) -> str:
        return str(self._title.toolTip())

    def current_unit_for_test(self) -> str:
        code = self._unit_combo.itemData(self._unit_combo.currentIndex())
        return str(code) if isinstance(code, str) else ""

    def top_axis_label_for_test(self) -> str:
        return str(self._trace_plot.getAxis("left").labelText)

    def unit_item_enabled_for_test(self, idx: int) -> bool:
        item = self._unit_combo.model().item(idx)  # type: ignore[attr-defined]
        return bool(item is not None and item.isEnabled())

    def unit_combo_tooltip_for_test(self) -> str:
        return str(self._unit_combo.toolTip())

    def counts_context_for_test(self) -> tuple[np.ndarray, np.ndarray, float, str, str, float]:
        return (
            self._counts_x,
            self._counts_y,
            self._fs,
            self._ctx_device,
            self._ctx_nslc,
            self._ctx_start_epoch,
        )

    # ----- 3C archive view test accessors (M12) -----
    def _is_showing_archive_for_test(self) -> bool:
        return self._stack.currentWidget() is self._archive_graphics

    def _archive_curve_for_test(self, comp: str) -> pg.PlotDataItem:
        return self._arc_curves[comp]

    def _overlay_curve_for_test(self, comp: str) -> pg.PlotDataItem:
        return self._arc_overlay_curves[comp]

    def _arc_ratio_curve_for_test(self) -> pg.PlotDataItem:
        return self._arc_ratio_curve

    def _component_layout_for_test(self) -> str:
        return self._arc_layout

    def _layout_toggle_visible_for_test(self) -> bool:
        # ``isHidden`` reflects the explicit setVisible state regardless of
        # whether an offscreen test ever showed the parent pane.
        return not self._layout_toggle.isHidden()

    def _arc_x_range_for_test(self) -> tuple[float, float]:
        (x0, x1), _y = self._arc_plots["Z"].viewRange()
        return float(x0), float(x1)
