"""Rolling-waterfall spectrogram view backed by ``pyqtgraph.ImageItem``.

Live consumer of :attr:`StreamingEngine.spectrogramColumnReady`. One
view per stream in the live stack; the Spectrogram dock instantiates a
larger one per active tab. Both share the same column-to-display
transform via :func:`colorize` so a future change to the colour-map
behaviour lands in one place.

Color modes (chosen via :class:`ColorMode`):

* ``Z_SCORE`` (default) — **per-column** z-score over the frequency
  axis, computed on log power. Each column is normalised against its
  own spectral mean / std, so a sustained spectral feature (e.g. the
  microseism band) reads as a consistent per-column deviation — a
  visible horizontal band — while the level is self-normalising and
  needs no calibration. Display range fixed at ``(-3, 3)``.
* ``DB`` — ``10 * log10(power)``. The display range is auto-scaled
  from a robust percentile of the live buffer so absolute power on
  any instrument / units shows structure rather than clamping.
* ``LINEAR`` — raw power values, range auto-scaled from a robust
  percentile; debugging / sanity checking.

History (POSTMORTEMS 2026-05-31 "Degenerate spectrogram passed
shape-only tests"): the original z-score normalised **per frequency
bin over time** (a Welford accumulator), which drove every bin of a
stationary spectrum to ≈0 — the temporal mean — so the whole image
collapsed to the mid-colormap (uniform green) and sustained bands
vanished. LINEAR additionally used a fixed ``(0, 1)`` range against
power values spanning many orders of magnitude, clamping every pixel
to the top of the colormap. Both are domain/level mismatches; see the
module docstring above for the corrected transforms.

Threading: all public methods MUST be called from the GUI thread.
``add_column`` is the slot most callers wire to the engine; route it
via :class:`Qt.ConnectionType.QueuedConnection`.
"""

from __future__ import annotations

import math
import time
from enum import StrEnum

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from seedlink_dashboard.gui.widgets.marker_style import marker_color
from seedlink_dashboard.gui.widgets.pane_header import (
    PANE_HEADER_MARGINS,
    PANE_TITLE_OBJECT_NAME,
    PANE_TITLE_STYLE,
    format_pane_title,
)

# Default rolling-window width. 600 columns x 1 s/col ≈ 10 minutes of
# history, matching what users typically want to see at a glance for
# diagnosing local events.
_DEFAULT_MAX_COLUMNS = 600
# Floor for the log() inside the dB / z-score transforms. Power values
# below this clamp; protects against -inf when log10(0) creeps in.
_DB_FLOOR = 1e-30
# Fixed display range for z-score — self-normalising, so a constant
# range is correct by construction (a per-column z-score has unit std).
_ZSCORE_RANGE = (-3.0, 3.0)
# Placeholder ranges used only before any data has arrived; dB / linear
# re-derive their range from the live buffer on every column (see
# :meth:`SpectrogramView._compute_levels`). Kept distinct so callers
# that query :func:`levels_for` without data still get a sane,
# mode-specific stand-in.
_DB_DEFAULT_RANGE = (0.0, 120.0)
_LINEAR_DEFAULT_RANGE = (0.0, 1.0)
# Robust percentile bounds for the auto-scaled dB / linear ranges. The
# upper bound stops short of 100 so a single telemetry spike cannot
# wash the rest of the image out to one colour.
_DB_PCTL = (2.0, 98.0)
_LINEAR_PCTL = (5.0, 99.0)
# Std floor (in dB) for the per-column z-score so a constant
# (zero-variance) column yields 0 rather than inf/nan.
_ZSCORE_STD_FLOOR = 1e-9
# Fallback column step when we cannot yet infer it from timestamps.
_DEFAULT_COLUMN_DT = 1.0
# pyqtgraph internal layout tightening (M7 C2). Small outer margins so the
# waterfall uses available pixels without clipping the axis tick labels.
_PLOT_LAYOUT_MARGINS: tuple[int, int, int, int] = (2, 2, 2, 2)


class ColorMode(StrEnum):
    Z_SCORE = "z-score"
    DB = "dB"
    LINEAR = "linear"


def _to_db(column: np.ndarray) -> np.ndarray:
    """Linear power → dB, floored so ``log10`` never sees zero."""
    db: np.ndarray = (10.0 * np.log10(np.maximum(column, _DB_FLOOR))).astype(np.float32)
    return db


def colorize(column: np.ndarray, mode: ColorMode) -> np.ndarray:
    """Convert a linear power column to the display value for ``mode``.

    Args:
        column: 1-D float32 power column from :class:`RollingSpectrogram`.
        mode: Display mode.

    Returns:
        1-D float32 display column. Same length as ``column``.
    """
    if mode is ColorMode.LINEAR:
        return column.astype(np.float32, copy=False)
    db = _to_db(column)
    if mode is ColorMode.DB:
        return db
    # Z-score over the frequency axis of THIS column, on log power.
    # Log first so seismic's many-orders-of-magnitude dynamic range
    # does not let the low-frequency bins dominate the column mean/std.
    mean = float(db.mean())
    std = float(db.std())
    if std < _ZSCORE_STD_FLOOR:
        # Constant column (warm-up / dead channel): no information to
        # normalise. Return zeros — finite, lands at the colormap mid.
        return np.zeros_like(db)
    return ((db - mean) / std).astype(np.float32)


def levels_for(mode: ColorMode) -> tuple[float, float]:
    """Return the placeholder ``(lo, hi)`` range for ``mode``.

    Z-score is fixed; dB / linear return a mode-specific default that
    :class:`SpectrogramView` overrides from live data on the first
    column. The three are kept distinct so a pre-data query still
    yields the right shape.
    """
    if mode is ColorMode.DB:
        return _DB_DEFAULT_RANGE
    if mode is ColorMode.LINEAR:
        return _LINEAR_DEFAULT_RANGE
    return _ZSCORE_RANGE


def _epoch_from(t_end: object) -> float | None:
    """Coerce a column timestamp to epoch seconds.

    Accepts an ObsPy ``UTCDateTime`` (``float()`` yields its POSIX
    timestamp), a raw ``float`` / ``int``, or ``None``. Anything else
    returns ``None`` so a malformed timestamp never raises in a slot.
    """
    if t_end is None:
        return None
    try:
        return float(t_end)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class SpectrogramView(QWidget):
    """Rolling waterfall view for one stream.

    Args:
        window_seconds: How many seconds of history the widget shows.
            The widget translates this into a column count using the
            first arriving column's implied step (estimated from the
            view's preallocated ``column_dt`` hint) or simply uses
            :data:`_DEFAULT_MAX_COLUMNS` if not specified.
        fs: Initial sample rate for the title/y-axis label. The widget
            updates these on :meth:`update_meta`.
        fmax: Optional max frequency to clamp the Y range. Defaults to
            ``fs / 2``.
        label: Title label text — typically the NSLC string.
        time_axis: When ``True`` the bottom axis renders wall-clock UTC
            (a :class:`pyqtgraph.DateAxisItem`) and columns are placed
            on the X axis by their ``t_end``. When ``False`` the axis is
            a plain column index — used by the inline preview / live
            panes where a calendar axis adds no value. The Spectrogram
            dock sets this ``True``.
    """

    def __init__(
        self,
        *,
        window_seconds: float = 600.0,
        fs: float = 100.0,
        fmax: float | None = None,
        label: str = "",
        time_axis: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        # Validate BEFORE constructing the QWidget. A ValueError raised
        # after ``super().__init__`` would leave an unparented C++ widget
        # alive that pytest-qt never sees; at process shutdown the
        # widget races the QApplication teardown and aborts the
        # interpreter — observed as faulthandler "Aborted" in 30-iter
        # smoke runs of the M6 stage 1 tests.
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds}")
        if fs <= 0:
            raise ValueError(f"fs must be > 0, got {fs}")

        super().__init__(parent)

        self._window_seconds = float(window_seconds)
        self._fs = float(fs)
        self._fmax = float(fmax if fmax is not None else fs / 2.0)
        self._label = label
        self._time_axis = bool(time_axis)
        self._mode: ColorMode = ColorMode.Z_SCORE
        self._levels: tuple[float, float] = levels_for(self._mode)
        # Number of columns kept in the rolling image. We don't know
        # the column rate until the first column arrives; once we do,
        # we resize the buffer accordingly.
        self._max_columns = _DEFAULT_MAX_COLUMNS
        self._spec: np.ndarray | None = None
        self._freqs: np.ndarray | None = None
        self._f_min = 0.0
        self._f_max = self._fmax
        self._column_count = 0  # how many columns have actually been written
        # Wall-clock placement state for the time axis.
        self._last_epoch: float | None = None
        self._column_dt = _DEFAULT_COLUMN_DT
        # M8 C2 detection markers (wall-clock views only — a thin vertical
        # line per detection onset, no shading so the spectrogram stays
        # readable). Keyed by detection id.
        self._det_markers: dict[int, pg.InfiniteLine] = {}
        self._markers_visible = True
        # Tab-pause (M7 Stage B3): when inactive, ``add_column`` still
        # rolls the image array but skips the costly ``setImage`` redraw;
        # the array is flushed once on re-activation. GUI render-rate only
        # — never affects engine buffers or storage (CLAUDE.md rule 8).
        self._render_active = True

        # ---- header ----
        self._title_label = QLabel(self._format_title())
        self._title_label.setObjectName(PANE_TITLE_OBJECT_NAME)
        self._title_label.setStyleSheet(PANE_TITLE_STYLE)
        self._mode_combo = QComboBox(self)
        for m in ColorMode:
            self._mode_combo.addItem(m.value, userData=m)
        self._mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self._log_y = QToolButton(self)
        self._log_y.setText("log f")
        self._log_y.setCheckable(True)
        self._log_y.setChecked(False)
        self._log_y.toggled.connect(self._on_log_y_toggled)

        header = QWidget(self)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(*PANE_HEADER_MARGINS)
        header_layout.addWidget(self._title_label, stretch=1)
        header_layout.addWidget(QLabel("color:"))
        header_layout.addWidget(self._mode_combo)
        header_layout.addWidget(self._log_y)

        # ---- plot ----
        self._graphics = pg.GraphicsLayoutWidget()
        self._graphics.ci.layout.setContentsMargins(*_PLOT_LAYOUT_MARGINS)
        if self._time_axis:
            # utcOffset=0 → DateAxisItem renders the epoch X coordinates
            # as UTC rather than the host's local timezone.
            axis_items = {"bottom": pg.DateAxisItem(orientation="bottom", utcOffset=0)}
            self._plot = self._graphics.addPlot(row=0, col=0, axisItems=axis_items)
        else:
            self._plot = self._graphics.addPlot(row=0, col=0)
        self._plot.setMouseEnabled(x=False, y=True)
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        self._plot.setMenuEnabled(False)
        self._plot.setLabel("left", "Frequency", units="Hz")
        self._plot.setLabel(
            "bottom", "Time (UTC)" if self._time_axis else "Columns (older → newer)"
        )

        self._image = pg.ImageItem(axisOrder="row-major")
        self._image.setLookupTable(self._build_lut())
        self._image.setLevels(self._levels)
        self._plot.addItem(self._image)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self._graphics, stretch=1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    @Slot(str, str, object, object, object)
    def on_column(
        self,
        device_name: str,
        nslc: str,
        column: object,
        freqs: object,
        t_end: object,
    ) -> None:
        """Slot binding for the engine's ``spectrogramColumnReady`` signal.

        Subclasses that want stricter dispatch (e.g. only render for one
        stream) should connect their own slot instead and call
        :meth:`add_column` directly.
        """
        del device_name, nslc  # not used by the single-stream view
        if not isinstance(column, np.ndarray) or not isinstance(freqs, np.ndarray):
            return
        self.add_column(column, freqs, t_end=_epoch_from(t_end))

    def add_column(
        self,
        column: np.ndarray,
        freqs: np.ndarray,
        *,
        t_end: float | None = None,
    ) -> None:
        """Append a fresh STFT column to the right edge of the image.

        Args:
            column: 1-D linear power column.
            freqs: Matching frequency bin centres (Hz).
            t_end: Epoch seconds of the column's last sample. Used only
                when this view has a wall-clock time axis; ``None`` (no
                source timestamp) falls back to the receive wall clock
                so the rolling axis still advances.
        """
        if column.ndim != 1 or freqs.ndim != 1:
            return
        if column.shape[0] != freqs.shape[0]:
            return
        if self._spec is None or self._freqs is None or self._spec.shape[0] != column.shape[0]:
            self._reallocate(column.shape[0], freqs)

        assert self._spec is not None
        display = colorize(column, self._mode)
        self._spec = np.roll(self._spec, -1, axis=1)
        self._spec[:, -1] = display
        self._column_count = min(self._column_count + 1, self._max_columns)

        lo, hi = self._compute_levels()
        # Keep the not-yet-written warm-up region pinned to the display
        # floor so a fresh view reads as empty/dark rather than a solid
        # mid-colormap block (the historic "uniform green" symptom).
        unwritten = self._max_columns - self._column_count
        if unwritten > 0:
            self._spec[:, :unwritten] = lo
        self._levels = (lo, hi)
        if self._render_active:
            self._image.setLevels(self._levels)
            self._image.setImage(self._spec, autoLevels=False)
        if self._time_axis:
            self._advance_time_axis(t_end)
            self._prune_markers()

    # ------------------------------------------------------------------
    # Detection markers (M8 C2) — wall-clock (time_axis) views only.
    # ------------------------------------------------------------------
    def add_detection_marker(self, det_id: int, t_on: float, phase: str | None = None) -> None:
        """Place a thin vertical onset line at wall-clock ``t_on``.

        No-op on a column-index (inline) view: a POSIX coordinate has no
        meaning on its axis. The dock view (``time_axis=True``) shares the
        trace's wall-clock axis, so the marker aligns with the trace
        marker for the same detection (M6 shared axis). AI picks colour
        by ``phase`` via :func:`marker_color` (rule 10 twin of the trace
        plot); STA/LTA (``phase is None``) keeps the amber default."""
        if not self._time_axis:
            return
        self._remove_marker(det_id)
        line = pg.InfiniteLine(pos=t_on, angle=90, pen=pg.mkPen(marker_color(phase), width=1))
        self._plot.addItem(line)
        line.setVisible(self._markers_visible)
        self._det_markers[det_id] = line

    def set_markers_visible(self, visible: bool) -> None:
        self._markers_visible = visible
        for line in self._det_markers.values():
            line.setVisible(visible)

    def _remove_marker(self, det_id: int) -> None:
        line = self._det_markers.pop(det_id, None)
        if line is not None:
            self._plot.removeItem(line)

    def _prune_markers(self) -> None:
        if not self._det_markers or self._last_epoch is None:
            return
        left = self._last_epoch - self._max_columns * self._column_dt
        stale = [det_id for det_id, line in self._det_markers.items() if line.value() < left]
        for det_id in stale:
            self._remove_marker(det_id)

    def set_color_mode(self, mode: ColorMode) -> None:
        """Programmatic equivalent of the toolbar combo."""
        if mode is self._mode:
            return
        self._mode = mode
        # Seed the levels with the mode's placeholder; dB / linear refine
        # this from live data on the next column, z-score keeps it fixed.
        self._levels = levels_for(mode)
        self._image.setLevels(self._levels)
        # We can't faithfully back-transform raw power from the stored
        # display values, so we clear the waterfall on a mode change to
        # signal "fresh canvas" rather than re-render stale data.
        self._clear_buffer()

    def update_meta(self, *, fs: float, fmax: float | None = None) -> None:
        """Update labels when the upstream sample rate changes (e.g. a
        decimating chain just installed). Drops the buffer so the new
        fs's frequency axis takes over immediately."""
        self._fs = float(fs)
        self._fmax = float(fmax if fmax is not None else fs / 2.0)
        self._title_label.setText(self._format_title())
        self._clear_buffer()

    def clear(self) -> None:
        """Drop every buffered column. Used on stream reinstall and
        when the user toggles the view's visibility off and back on."""
        self._clear_buffer()
        for det_id in list(self._det_markers):
            self._remove_marker(det_id)

    def _det_markers_for_test(self) -> dict[int, pg.InfiniteLine]:
        return self._det_markers

    def set_render_active(self, active: bool) -> None:
        """Enable/disable the costly ``setImage`` redraws (M7 Stage B3).

        When ``active`` is ``False`` the rolling image array keeps
        advancing on every :meth:`add_column` but the displayed image is
        not refreshed. On the ``False → True`` transition the current
        array is pushed once so a newly-visible tab shows recent data
        immediately. GUI render-rate only — never touches engine buffers
        or storage (CLAUDE.md rule 8).
        """
        if active == self._render_active:
            return
        self._render_active = active
        if active and self._spec is not None:
            self._image.setLevels(self._levels)
            self._image.setImage(self._spec, autoLevels=False)

    def color_mode(self) -> ColorMode:
        return self._mode

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _reallocate(self, n_freq: int, freqs: np.ndarray) -> None:
        self._freqs = freqs.astype(np.float32, copy=False)
        # Initialise to the display floor so the buffer renders dark
        # ("empty") until real columns roll in, rather than a solid
        # mid-colormap block.
        self._spec = np.full((n_freq, self._max_columns), self._levels[0], dtype=np.float32)
        self._column_count = 0
        self._last_epoch = None
        # Map image rows ↔ frequency Hz on the Y axis. ImageItem's
        # default coordinate system is pixel-space; setRect() pins the
        # image to (x_left, y_bottom, width, height) in plot units.
        self._f_min = float(freqs[0]) if freqs.size else 0.0
        self._f_max = float(freqs[-1]) if freqs.size else self._fmax
        self._image.setRect(
            QRectF(0.0, self._f_min, float(self._max_columns), self._f_max - self._f_min)
        )
        self._plot.setYRange(self._f_min, min(self._f_max, self._fmax))
        if not self._time_axis:
            self._plot.setXRange(0.0, float(self._max_columns))

    def _clear_buffer(self) -> None:
        if self._spec is not None:
            self._spec.fill(self._levels[0])
            self._image.setLevels(self._levels)
            self._image.setImage(self._spec, autoLevels=False)
        self._column_count = 0
        self._last_epoch = None

    def _compute_levels(self) -> tuple[float, float]:
        """Display ``(lo, hi)`` for the current mode and buffer state.

        Z-score is self-normalising so its range is fixed. dB / linear
        derive a robust percentile range from the columns written so
        far, so a single spike cannot wash the image out to one colour
        and the range adapts to the stream's actual units.
        """
        if self._mode is ColorMode.Z_SCORE:
            return _ZSCORE_RANGE
        if self._spec is None or self._column_count == 0:
            return self._levels
        written = self._spec[:, self._max_columns - self._column_count :]
        plo, phi = _DB_PCTL if self._mode is ColorMode.DB else _LINEAR_PCTL
        lo, hi = (float(v) for v in np.percentile(written, (plo, phi)))
        if not (np.isfinite(lo) and np.isfinite(hi)) or hi - lo < 1e-9:
            # Degenerate (all-equal) buffer: widen around the value so
            # ImageItem always gets lo < hi.
            centre = lo if np.isfinite(lo) else 0.0
            return centre - 0.5, centre + 0.5
        return lo, hi

    def _advance_time_axis(self, t_end: float | None) -> None:
        """Place the rolling image on the wall-clock X axis.

        Uses the supplied source timestamp when present, else the
        receive wall clock (the DSP-processed path does not thread the
        source end time through to the GUI). The column step is inferred
        from successive timestamps so the axis matches the true cadence.
        On the timestamp-less fallback the inferred cadence is only
        approximate (it tracks receive time, which jitters with event-loop
        latency); the dock path always supplies a real ``t_end``.
        """
        epoch = t_end if t_end is not None else time.time()
        if self._last_epoch is not None:
            dt = epoch - self._last_epoch
            if dt > 0:
                self._column_dt = dt
        self._last_epoch = epoch
        span = self._max_columns * self._column_dt
        x_left = epoch - span
        self._image.setRect(QRectF(x_left, self._f_min, span, self._f_max - self._f_min))
        # padding=0 so the right edge sits exactly at the newest column's
        # time ("now") rather than pyqtgraph's default auto-padding.
        self._plot.setXRange(x_left, epoch, padding=0)

    def _on_mode_changed(self, index: int) -> None:
        mode = self._mode_combo.itemData(index)
        if isinstance(mode, ColorMode):
            self.set_color_mode(mode)

    def _on_log_y_toggled(self, checked: bool) -> None:
        self._plot.setLogMode(x=False, y=checked)

    def _build_lut(self) -> np.ndarray:
        """Return a 256-entry RGBA lookup table.

        We sidestep pyqtgraph's optional matplotlib dependency by
        constructing the colormap manually — Viridis-like ramp from
        dark blue through teal / yellow.
        """
        n = 256
        idx = np.linspace(0.0, 1.0, n, dtype=np.float32)
        # Approximate viridis: blend three control points.
        r = (0.267 + idx * (0.993 - 0.267)).clip(0.0, 1.0)
        g = (0.005 + idx * (0.906 - 0.005)).clip(0.0, 1.0)
        b = (0.329 + idx * (0.144 - 0.329)).clip(0.0, 1.0)
        # Slight S-curve through the green band for the mid-range.
        g = (g + 0.3 * np.sin(math.pi * idx)).clip(0.0, 1.0)
        a = np.ones_like(idx)
        lut = np.stack(
            [
                (r * 255).astype(np.uint8),
                (g * 255).astype(np.uint8),
                (b * 255).astype(np.uint8),
                (a * 255).astype(np.uint8),
            ],
            axis=1,
        )
        return lut

    def _format_title(self) -> str:
        # Extra header bits like Hz/bin x s/col arrive on the first
        # add_column so we leave them blank until then.
        return format_pane_title(self._label, self._fs)
