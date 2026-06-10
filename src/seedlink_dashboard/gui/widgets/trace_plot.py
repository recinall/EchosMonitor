"""Single- or stacked-channel scrolling trace plot.

`mode="single"` (default) renders one PlotItem per `TracePlot`. `mode="stacked"`
renders two stacked PlotItems sharing an X axis — the top shows the raw
trace, the bottom shows the post-DSP filtered trace. Both X axes are
`pyqtgraph.DateAxisItem` (wall-clock) and linked via `setXLink()` so panning
or zooming one stays synchronised with the other.

In both modes the buffer is preallocated as `window_seconds * fs` float32
samples and `push_raw` / `push_processed` roll the tail. The widget tracks
`_latest_endtime_posix` so the X-axis labels reflect real wall-clock time
rather than seconds-since-startup.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QStackedLayout,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from seedlink_dashboard.gui.widgets.marker_style import STA_LTA_COLOR
from seedlink_dashboard.gui.widgets.pane_header import (
    PANE_HEADER_MARGINS,
    PANE_TITLE_OBJECT_NAME,
    PANE_TITLE_STYLE,
    format_pane_title,
)

_FS_CHANGE_EPSILON_REL = 0.01
# Display decimation (rule 11): the rendered point count is bounded by the
# pixel/``max_display_rate_hz`` budget, never by ``window_seconds * fs``.
# At most this many rendered points per visible pixel — min/max decimation
# emits two points (a min and a max) per bin, so ~2 keeps one bin per pixel.
_DISPLAY_POINTS_PER_PIXEL = 2
# Only decimate once the full-rate sample count exceeds this multiple of the
# point budget; below it the buffer is already small enough to draw verbatim
# (avoids min/max overhead and aliasing artefacts on already-coarse data).
_DECIMATE_TRIGGER_FACTOR = 2.0
# Fallback plot width (px) used to size the budget before the widget has been
# laid out (viewbox width reports 0 at construction time).
_FALLBACK_PLOT_WIDTH_PX = 1500
# pyqtgraph internal layout tightening (M7 C2). Outer margins around the
# plot grid and the gap between stacked plots, in pixels — small but
# non-zero so axis tick labels are never clipped.
_PLOT_LAYOUT_MARGINS: tuple[int, int, int, int] = (2, 2, 2, 2)
_PLOT_LAYOUT_SPACING = 4
_DROP_BADGE_BASE_STYLE = "QLabel#TracePlotDropBadge { font-size: 10px; padding: 1px 4px; }"
_DROP_BADGE_RED_STYLE = (
    _DROP_BADGE_BASE_STYLE + " QLabel#TracePlotDropBadge { color: white; background: #c0392b; }"
)
_DROP_BADGE_DIM_STYLE = (
    _DROP_BADGE_BASE_STYLE + " QLabel#TracePlotDropBadge { color: #888; background: transparent; }"
)

# Detection-marker styling (M8 C1): amber, distinct from the grey trace.
# A vertical line marks the onset (t_on); a semi-transparent region spans
# t_on..t_off once the trigger closes.
_MARKER_LINE_PEN = pg.mkPen(STA_LTA_COLOR, width=1)
_MARKER_REGION_BRUSH = pg.mkBrush(224, 160, 48, 50)


def _minmax_decimate(
    x: np.ndarray, y: np.ndarray, max_points: int
) -> tuple[np.ndarray, np.ndarray]:
    """Min/max (peak) decimate ``(x, y)`` to roughly ``max_points`` points.

    Display-only helper (rule 11). Splits the signal into contiguous bins and
    emits, per bin, the ``(x, y)`` of both the minimum and the maximum sample
    in source order. This preserves transients and spikes that naive stride
    decimation would drop: a single-sample peak always survives as its bin's
    max. The returned arrays have at most ``~max_points`` elements (two per
    bin); when ``len(y) <= max_points`` the inputs are returned unchanged.

    Args:
        x: Monotonic X coordinates (wall-clock POSIX seconds), float64.
        y: Sample values aligned with ``x``, float32.
        max_points: Target rendered-point budget. Must be >= 2.

    Returns:
        ``(x_dec, y_dec)`` ready to hand to ``PlotDataItem.setData``.
    """
    n = y.shape[0]
    if max_points < 2:
        max_points = 2
    if n <= max_points:
        return x, y
    # Two points per bin (min + max), so the bin count is half the budget.
    n_bins = max(1, max_points // 2)
    # Whole bins only; trailing samples that do not fill a bin are folded into
    # the last bin so no data at the right edge (newest samples) is dropped.
    bin_size = n // n_bins
    if bin_size < 1:  # defensive — unreachable while n > max_points >= n_bins
        bin_size = 1
        n_bins = n
    usable = bin_size * n_bins
    head = y[:usable].reshape(n_bins, bin_size)
    argmin = head.argmin(axis=1)
    argmax = head.argmax(axis=1)
    base = np.arange(n_bins) * bin_size
    idx_min = base + argmin
    idx_max = base + argmax
    # Fold any trailing remainder into the final bin's candidate extrema so a
    # spike in the last partial bin is not lost.
    if usable < n:
        tail = y[usable:]
        tail_min_i = usable + int(tail.argmin())
        tail_max_i = usable + int(tail.argmax())
        idx_min[-1] = idx_min[-1] if y[idx_min[-1]] <= y[tail_min_i] else tail_min_i
        idx_max[-1] = idx_max[-1] if y[idx_max[-1]] >= y[tail_max_i] else tail_max_i
    # Interleave in source (time) order per bin so the polyline stays monotone
    # in X and the min/max pair plots as a vertical stroke at the bin.
    lo = np.minimum(idx_min, idx_max)
    hi = np.maximum(idx_min, idx_max)
    idx = np.empty(2 * n_bins, dtype=np.intp)
    idx[0::2] = lo
    idx[1::2] = hi
    return x[idx], y[idx]


class _DetMarker:
    """Holds the pyqtgraph items for one detection marker on a trace."""

    __slots__ = ("line", "region", "t_off", "t_on")

    def __init__(
        self,
        t_on: float,
        t_off: float | None,
        line: pg.InfiniteLine,
        region: pg.LinearRegionItem | None,
    ) -> None:
        self.t_on = t_on
        self.t_off = t_off
        self.line = line
        self.region = region


class TracePlot(QWidget):
    """A pyqtgraph-backed scrolling trace.

    Args:
        window_seconds: Visible window length in seconds.
        fs: Sample rate (Hz) used to size the preallocated buffer; the
            buffer is rebuilt if a later `update_meta` reports a different
            rate.
        label: Display label (typically the NSLC string).
        mode: ``"single"`` for one plot, ``"stacked"`` for raw-on-top /
            filtered-on-bottom with linked X axes.
        fs_processed: Initial sample rate of the filtered (post-DSP) stream.
            Used in stacked mode to size the lower buffer; updated on the
            first `push_processed` call when the chain's actual `fs_out`
            becomes available.
        max_display_rate_hz: Display-only peak-decimation cap (rule 11). The
            rendered point count is bounded by ``min(plot_px *
            _DISPLAY_POINTS_PER_PIXEL, window_seconds * max_display_rate_hz)``;
            the full-rate buffer (and everything the engine sends to DSP /
            detection / storage) is never decimated.
    """

    def __init__(
        self,
        window_seconds: float,
        fs: float,
        label: str,
        parent: QWidget | None = None,
        *,
        mode: Literal["single", "stacked"] = "single",
        fs_processed: float | None = None,
        max_display_rate_hz: int = 250,
    ) -> None:
        super().__init__(parent)
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds}")
        if fs <= 0:
            raise ValueError(f"fs must be > 0, got {fs}")
        if max_display_rate_hz <= 0:
            raise ValueError(f"max_display_rate_hz must be > 0, got {max_display_rate_hz}")

        self._max_display_rate_hz = int(max_display_rate_hz)
        self._window_seconds = float(window_seconds)
        self._fs_raw = float(fs)
        self._fs_processed = float(fs_processed if fs_processed is not None else fs)
        self._label = label
        self._mode: Literal["single", "stacked"] = mode
        self._has_data_raw = False
        self._has_data_processed = False
        # Initial wall-clock anchor — replaced on the first streamMeta or push.
        self._latest_raw_t = 0.0
        self._latest_processed_t = 0.0
        self._x_raw: np.ndarray = np.empty(0, dtype=np.float64)
        self._y_raw: np.ndarray = np.empty(0, dtype=np.float32)
        self._x_proc: np.ndarray = np.empty(0, dtype=np.float64)
        self._y_proc: np.ndarray = np.empty(0, dtype=np.float32)
        self._drop_count_recent = 0
        # M8 detection markers, keyed by detection id. Visible by default;
        # toggled per-plot (header button) or globally (View menu).
        self._markers: dict[int, _DetMarker] = {}
        self._markers_visible = True
        # Tab-pause (M7 Stage B3): when render-inactive, push_raw /
        # push_processed still roll the buffer (cheap, O(buffer)) so the
        # tab shows recent data on re-activation, but skip the costly
        # ``setData`` call. Flag flips via :meth:`set_render_active`.
        self._render_active = True
        # Test/measurement hook (M7 Stage B4): incremented only when a
        # real ``setData`` actually fires (i.e. render-active). The
        # setData-count proxy stands in for CPU% under the headless
        # offscreen platform where real CPU is not measurable.
        self._set_data_calls = 0

        # ----- header (label + drop badge + autofit) -----
        self._title_label = QLabel(self._format_title())
        self._title_label.setObjectName(PANE_TITLE_OBJECT_NAME)
        self._title_label.setStyleSheet(PANE_TITLE_STYLE)
        self._drop_badge = QLabel("")
        self._drop_badge.setObjectName("TracePlotDropBadge")
        self._set_drop_badge_count(0)

        self._autofit = QToolButton()
        self._autofit.setText("Auto Y")
        self._autofit.setCheckable(True)
        self._autofit.setChecked(True)
        self._autofit.toggled.connect(self._on_autofit_toggled)

        self._markers_btn = QToolButton()
        self._markers_btn.setText("⚑")
        self._markers_btn.setCheckable(True)
        self._markers_btn.setChecked(True)
        self._markers_btn.setToolTip("Show/hide detection markers on this trace")
        self._markers_btn.toggled.connect(self.set_markers_visible)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(*PANE_HEADER_MARGINS)
        header_layout.addWidget(self._title_label, stretch=1)
        header_layout.addWidget(self._drop_badge)
        header_layout.addWidget(self._markers_btn)
        header_layout.addWidget(self._autofit)

        # ----- pyqtgraph layout (one or two stacked PlotItems) -----
        # Tighten the layout's outer margins + inter-plot spacing (M7 C2)
        # so the traces use the available pixels rather than pyqtgraph's
        # default padding. Subtle: tick labels stay fully visible.
        self._graphics = pg.GraphicsLayoutWidget()
        self._graphics.ci.layout.setContentsMargins(*_PLOT_LAYOUT_MARGINS)
        self._graphics.ci.layout.setSpacing(_PLOT_LAYOUT_SPACING)
        self._raw_plot = self._graphics.addPlot(
            row=0,
            col=0,
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")},
        )
        self._raw_plot.setMouseEnabled(x=False, y=True)
        self._raw_plot.showGrid(x=True, y=True, alpha=0.3)
        self._raw_plot.setMenuEnabled(False)
        self._raw_plot.enableAutoRange(axis="y", enable=True)
        self._raw_curve = self._raw_plot.plot(pen=pg.mkPen("#888", width=1))

        self._processed_plot: pg.PlotItem | None = None
        self._processed_curve: pg.PlotDataItem | None = None
        if self._mode == "stacked":
            self._processed_plot = self._graphics.addPlot(
                row=1,
                col=0,
                axisItems={"bottom": pg.DateAxisItem(orientation="bottom")},
            )
            self._processed_plot.setMouseEnabled(x=False, y=True)
            self._processed_plot.showGrid(x=True, y=True, alpha=0.3)
            self._processed_plot.setMenuEnabled(False)
            self._processed_plot.enableAutoRange(axis="y", enable=True)
            self._processed_plot.setXLink(self._raw_plot)
            self._processed_curve = self._processed_plot.plot(pen=pg.mkPen("#3aa", width=1))

        self._no_data_overlay = QLabel("no data")
        self._no_data_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._no_data_overlay.setObjectName("TracePlotNoData")
        self._no_data_overlay.setStyleSheet(
            "QLabel#TracePlotNoData { color: #888; font-style: italic; }"
        )

        stack_host = QWidget(self)
        self._stack = QStackedLayout(stack_host)
        self._stack.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._graphics)
        self._stack.addWidget(self._no_data_overlay)
        self._stack.setCurrentWidget(self._no_data_overlay)

        # ----- root layout -----
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(header)
        root.addWidget(stack_host, stretch=1)

        self._build_raw_buffer()
        if self._mode == "stacked":
            self._build_processed_buffer()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @Slot(object)
    def push(self, samples: np.ndarray) -> None:
        """Back-compat alias for `push_raw`."""
        self.push_raw(samples)

    @Slot(object)
    def push_raw(self, samples: np.ndarray) -> None:
        """Append `samples` to the raw window. Cross-thread safe via Qt slots."""
        if samples.ndim != 1:
            return
        n = int(samples.shape[0])
        if n == 0:
            return
        if not self._has_data_raw:
            self._has_data_raw = True
            self._stack.setCurrentWidget(self._graphics)

        cap = self._y_raw.shape[0]
        if n >= cap:
            self._y_raw[:] = samples[-cap:].astype(np.float32, copy=False)
        else:
            self._y_raw = np.roll(self._y_raw, -n)
            self._y_raw[-n:] = samples.astype(np.float32, copy=False)
        self._latest_raw_t = self._latest_raw_t + n / self._fs_raw
        self._refresh_x_raw()
        self._prune_markers()
        if self._render_active:
            self._render_curve(self._raw_curve, self._raw_plot, self._x_raw, self._y_raw)

    @Slot(object)
    def push_processed(self, samples: np.ndarray) -> None:
        """Append `samples` to the filtered window (stacked mode only)."""
        if self._mode != "stacked" or self._processed_curve is None:
            return
        if samples.ndim != 1:
            return
        n = int(samples.shape[0])
        if n == 0:
            return
        if not self._has_data_processed:
            self._has_data_processed = True
        cap = self._y_proc.shape[0]
        if n >= cap:
            self._y_proc[:] = samples[-cap:].astype(np.float32, copy=False)
        else:
            self._y_proc = np.roll(self._y_proc, -n)
            self._y_proc[-n:] = samples.astype(np.float32, copy=False)
        # Slave the processed time base to the raw one. The lower plot is
        # X-linked to the raw plot, whose axis is anchored to wall-clock
        # (``update_meta`` from the packet ``starttime``). The processed
        # samples carry no absolute timestamp through the DSP path, so
        # accumulating from 0 would leave them at the 1970 epoch — ~56 years
        # left of the live view, i.e. drawn entirely off-screen (the "filtered
        # plot is empty" bug). Both panes show the same ``window_seconds`` up
        # to "now", so aligning the right edges to ``_latest_raw_t`` is correct.
        self._latest_processed_t = self._latest_raw_t
        self._refresh_x_processed()
        if self._render_active and self._processed_plot is not None:
            self._render_curve(
                self._processed_curve, self._processed_plot, self._x_proc, self._y_proc
            )

    @Slot(str, float, str)
    def update_meta(self, _nslc: str, fs: float, starttime_iso: str) -> None:
        """Slot signature matches `StreamingEngine.streamMeta(str, float, str)`."""
        if fs <= 0:
            return
        denom = max(self._fs_raw, fs)
        if denom > 0 and abs(self._fs_raw - fs) / denom > _FS_CHANGE_EPSILON_REL:
            self._fs_raw = fs
            self._build_raw_buffer()
        try:
            from obspy.core.utcdatetime import UTCDateTime

            self._latest_raw_t = float(UTCDateTime(starttime_iso))
        except (ValueError, TypeError):
            pass
        self._title_label.setText(self._format_title())

    def update_processed_meta(self, fs_processed: float) -> None:
        """Update the lower (filtered) plot's sample rate. Called from
        `LiveStack` when the engine reports an effective `fs_out`."""
        if fs_processed <= 0 or self._mode != "stacked":
            return
        denom = max(self._fs_processed, fs_processed)
        if denom > 0 and abs(self._fs_processed - fs_processed) / denom > _FS_CHANGE_EPSILON_REL:
            self._fs_processed = fs_processed
            self._build_processed_buffer()
            self._title_label.setText(self._format_title())

    def set_drop_count(self, count: int) -> None:
        """Update the small drop-status badge in the header."""
        self._set_drop_badge_count(count)

    # ------------------------------------------------------------------
    # Detection markers (M8 C1)
    # ------------------------------------------------------------------
    def add_detection_marker(
        self,
        det_id: int,
        t_on: float,
        t_off: float | None,
        score: float,
    ) -> None:
        """Add (or replace) a detection marker at wall-clock ``t_on``.

        ``t_on`` / ``t_off`` are POSIX seconds on the same axis as the
        trace (the M6 wall-clock X axis). An open detection (``t_off is
        None``) is a single onset line; a closed one also gets a shaded
        amber region spanning the trigger. Markers scroll with the data
        and are pruned once they leave the visible window
        (:meth:`_prune_markers`).
        """
        self._remove_marker(det_id)
        line = pg.InfiniteLine(
            pos=t_on,
            angle=90,
            pen=_MARKER_LINE_PEN,
            label=f"{score:.1f}",
            labelOpts={"color": STA_LTA_COLOR, "position": 0.9, "movable": False},
        )
        self._raw_plot.addItem(line)
        region: pg.LinearRegionItem | None = None
        if t_off is not None:
            region = pg.LinearRegionItem(
                values=(t_on, t_off), brush=_MARKER_REGION_BRUSH, movable=False
            )
            region.setZValue(-10)
            self._raw_plot.addItem(region)
        marker = _DetMarker(t_on, t_off, line, region)
        self._set_marker_visible(marker, self._markers_visible)
        self._markers[det_id] = marker

    def update_detection_marker(self, det_id: int, t_off: float) -> None:
        """Close an open marker: extend the onset line into a shaded region."""
        marker = self._markers.get(det_id)
        if marker is None:
            return
        marker.t_off = t_off
        if marker.region is None:
            marker.region = pg.LinearRegionItem(
                values=(marker.t_on, t_off), brush=_MARKER_REGION_BRUSH, movable=False
            )
            marker.region.setZValue(-10)
            self._raw_plot.addItem(marker.region)
            marker.region.setVisible(self._markers_visible)
        else:
            marker.region.setRegion((marker.t_on, t_off))

    def set_markers_visible(self, visible: bool) -> None:
        """Show/hide every detection marker on this trace (per-plot toggle
        and the View-menu global toggle both land here)."""
        self._markers_visible = visible
        self._markers_btn.blockSignals(True)
        self._markers_btn.setChecked(visible)
        self._markers_btn.blockSignals(False)
        for marker in self._markers.values():
            self._set_marker_visible(marker, visible)

    @staticmethod
    def _set_marker_visible(marker: _DetMarker, visible: bool) -> None:
        marker.line.setVisible(visible)
        if marker.region is not None:
            marker.region.setVisible(visible)

    def _remove_marker(self, det_id: int) -> None:
        marker = self._markers.pop(det_id, None)
        if marker is None:
            return
        self._raw_plot.removeItem(marker.line)
        if marker.region is not None:
            self._raw_plot.removeItem(marker.region)

    def _prune_markers(self) -> None:
        """Drop markers that have scrolled out of the visible window."""
        if not self._markers:
            return
        left = self._latest_raw_t - self._window_seconds
        stale = [
            det_id
            for det_id, m in self._markers.items()
            if (m.t_off if m.t_off is not None else m.t_on) < left
        ]
        for det_id in stale:
            self._remove_marker(det_id)

    def set_render_active(self, active: bool) -> None:
        """Enable/disable the costly ``setData`` redraws (M7 Stage B3).

        When ``active`` is ``False`` the rolling buffers still advance on
        every ``push_raw`` / ``push_processed`` (so the most recent window
        is always current) but the curve is not redrawn. On the
        ``False → True`` transition a single ``setData`` flushes the
        current buffer so a tab that becomes visible shows recent data
        immediately rather than waiting for the next packet.

        This is a GUI render-rate control only: it never touches the
        engine ring buffers or storage (CLAUDE.md rule 8).
        """
        if active == self._render_active:
            return
        self._render_active = active
        if active:
            # Flush whatever is currently buffered so the now-visible tab
            # is immediately up to date.
            if self._has_data_raw:
                self._render_curve(self._raw_curve, self._raw_plot, self._x_raw, self._y_raw)
            if (
                self._mode == "stacked"
                and self._has_data_processed
                and self._processed_curve is not None
                and self._processed_plot is not None
            ):
                self._render_curve(
                    self._processed_curve, self._processed_plot, self._x_proc, self._y_proc
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _display_point_budget(self, plot: pg.PlotItem) -> int:
        """Maximum points to render for ``plot`` (rule 11).

        ``min(plot_pixel_width * _DISPLAY_POINTS_PER_PIXEL,
        window_seconds * max_display_rate_hz)``. The pixel width is read from
        the plot's viewbox; if it is zero (widget not yet laid out) a
        conservative fallback width keeps the budget bounded from the first
        push. Always >= 2 so the smallest possible curve still has endpoints.
        """
        view_box = plot.getViewBox()
        width_px = int(view_box.width()) if view_box is not None else 0
        if width_px <= 0:
            width_px = _FALLBACK_PLOT_WIDTH_PX
        pixel_budget = width_px * _DISPLAY_POINTS_PER_PIXEL
        rate_budget = int(self._window_seconds * self._max_display_rate_hz)
        return max(2, min(pixel_budget, rate_budget))

    def _render_curve(
        self, curve: pg.PlotDataItem, plot: pg.PlotItem, x: np.ndarray, y: np.ndarray
    ) -> None:
        """Hand a (possibly peak-decimated) copy of ``(x, y)`` to ``curve``.

        Display-only (rule 11): the caller's full-rate buffers are untouched.
        Decimation kicks in only once the buffer exceeds
        ``_DECIMATE_TRIGGER_FACTOR`` times the point budget. Increments the
        ``setData`` counter exactly once per real draw, as before.
        """
        budget = self._display_point_budget(plot)
        if y.shape[0] > budget * _DECIMATE_TRIGGER_FACTOR:
            x, y = _minmax_decimate(x, y, budget)
        curve.setData(x, y)
        self._set_data_calls += 1

    def _build_raw_buffer(self) -> None:
        n = max(2, int(self._window_seconds * self._fs_raw))
        self._y_raw = np.zeros(n, dtype=np.float32)
        self._refresh_x_raw()
        if self._has_data_raw:
            self._render_curve(self._raw_curve, self._raw_plot, self._x_raw, self._y_raw)

    def _build_processed_buffer(self) -> None:
        n = max(2, int(self._window_seconds * self._fs_processed))
        self._y_proc = np.zeros(n, dtype=np.float32)
        self._refresh_x_processed()
        if (
            self._has_data_processed
            and self._processed_curve is not None
            and self._processed_plot is not None
        ):
            self._render_curve(
                self._processed_curve, self._processed_plot, self._x_proc, self._y_proc
            )

    def _refresh_x_raw(self) -> None:
        n = self._y_raw.shape[0]
        # Anchor the rightmost sample to `_latest_raw_t`. Negative offsets fall
        # back to seconds-since-zero if the wall-clock anchor is unset.
        self._x_raw = self._latest_raw_t - (n - 1 - np.arange(n, dtype=np.float64)) / self._fs_raw

    def _refresh_x_processed(self) -> None:
        n = self._y_proc.shape[0]
        self._x_proc = (
            self._latest_processed_t - (n - 1 - np.arange(n, dtype=np.float64)) / self._fs_processed
        )

    def _format_title(self) -> str:
        if self._mode == "stacked":
            return format_pane_title(self._label, self._fs_raw, self._fs_processed)
        return format_pane_title(self._label, self._fs_raw)

    def _set_drop_badge_count(self, count: int) -> None:
        self._drop_count_recent = count
        if count > 0:
            self._drop_badge.setText(f"drops: {count}")
            self._drop_badge.setStyleSheet(_DROP_BADGE_RED_STYLE)
        else:
            self._drop_badge.setText("")
            self._drop_badge.setStyleSheet(_DROP_BADGE_DIM_STYLE)

    @Slot(bool)
    def _on_autofit_toggled(self, checked: bool) -> None:
        self._raw_plot.enableAutoRange(axis="y", enable=checked)
        if self._processed_plot is not None:
            self._processed_plot.enableAutoRange(axis="y", enable=checked)

    # ------------------------------------------------------------------
    # Test-only accessors
    # ------------------------------------------------------------------
    def _curve_for_test(self) -> pg.PlotDataItem:
        return self._raw_curve

    def _processed_curve_for_test(self) -> pg.PlotDataItem | None:
        return self._processed_curve

    def _buffer_size_for_test(self) -> int:
        return int(self._y_raw.shape[0])

    def _processed_buffer_size_for_test(self) -> int:
        return int(self._y_proc.shape[0])

    def _set_data_call_count_for_test(self) -> int:
        """Total real ``setData`` calls (only when render-active)."""
        return self._set_data_calls

    def _is_render_active_for_test(self) -> bool:
        return self._render_active

    def _max_display_rate_hz_for_test(self) -> int:
        return self._max_display_rate_hz

    def _display_point_budget_for_test(self) -> int:
        return self._display_point_budget(self._raw_plot)

    def _raw_plot_item(self) -> pg.PlotItem:
        return self._raw_plot

    def _markers_for_test(self) -> dict[int, _DetMarker]:
        return self._markers

    def _processed_plot_item(self) -> pg.PlotItem | None:
        return self._processed_plot
