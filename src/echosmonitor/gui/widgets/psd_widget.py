"""Power Spectral Density widget — on-demand Welch PSD overlay.

Layout (top-down):

* Toolbar: stream picker (device + NSLC combo), window length combo,
  refresh button + auto-refresh checkbox, NLNM/NHNM toggle.
* pyqtgraph PlotWidget: log-X frequency, dB Y. The currently-selected
  stream is one solid curve; NLNM / NHNM are dim grey reference
  curves underneath. A small "+ overlay" button adds another stream's
  PSD as a separate curve so the user can compare quiet vs noisy
  stations side by side.

The widget never computes PSD on the GUI thread; every request goes
out via ``StreamingEngine.psdRequested`` and the result lands on
``StreamingEngine.psdReady``. The widget drops results whose
``(device, nslc, seconds)`` no longer matches the live selection
(latest-result-wins).

Threading: every method on this widget MUST be invoked on the GUI
thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.dsp.nlnm import interpolate_to as _nlnm_interp
from echosmonitor.gui.widgets.log_axis import DecimatedLogAxisItem
from echosmonitor.gui.widgets.pane_header import (
    PANE_TITLE_OBJECT_NAME,
    PANE_TITLE_STYLE,
)

if TYPE_CHECKING:
    from echosmonitor.core.streaming_engine import StreamingEngine

# Shown in the PSD pane title when no stream is selected yet.
_PSD_NO_STREAM_TITLE = "PSD — no stream"


# Window length presets. (label, seconds).
_WINDOW_PRESETS: tuple[tuple[str, float], ...] = (
    ("30 s", 30.0),
    ("60 s", 60.0),
    ("5 min", 300.0),
    ("15 min", 900.0),
    ("1 h", 3600.0),
)
_DEFAULT_WINDOW_INDEX = 1  # 60 s

# Auto-refresh interval is ``max(_AUTO_REFRESH_MIN_S, window/4)``. With
# 60 s windows that's a 15 s refresh; with 1 h windows a 15 min one.
_AUTO_REFRESH_MIN_S = 5.0

# Curve colours — primary, then overlays cycled.
_PRIMARY_PEN = pg.mkPen("#3aa3ff", width=2)
_OVERLAY_PENS: tuple[pg.QtGui.QPen, ...] = (
    pg.mkPen("#f5b942", width=2),
    pg.mkPen("#7ac74f", width=2),
    pg.mkPen("#c067e0", width=2),
    pg.mkPen("#e0526b", width=2),
)
_NLNM_PEN = pg.mkPen("#888888", width=1, style=Qt.PenStyle.DashLine)
_NHNM_PEN = pg.mkPen("#888888", width=1, style=Qt.PenStyle.DashLine)


@dataclass
class _StreamKey:
    """Composite identity for a stream in the toolbar combo. Carrying
    the user-visible label keeps the legend stable when the same NSLC
    is announced by two different devices."""

    device: str
    nslc: str

    @property
    def display(self) -> str:
        return f"{self.device} / {self.nslc}"


def _enumerate_streams(engine: StreamingEngine) -> list[_StreamKey]:
    """Return the union of currently-buffered streams. Reads
    ``engine._buffers`` directly because the engine has no public
    "enumerate live streams" API yet; the dict is keyed by
    ``device_stream_key`` so we parse it once per refresh."""
    seen: list[_StreamKey] = []
    # ``device_stream_key`` is ``f"{device}/{nslc}"`` — model invariant
    # documented on ``DEVICE_KEY_SEP``. Using rsplit handles NSLCs that
    # happen to contain the separator in the location code (legal but
    # rare); using split with maxsplit=1 is correct because the
    # separator immediately follows the device name.
    from echosmonitor.core.models import DEVICE_KEY_SEP

    for composite in engine._buffers:
        if DEVICE_KEY_SEP not in composite:
            continue
        device, nslc = composite.split(DEVICE_KEY_SEP, maxsplit=1)
        seen.append(_StreamKey(device=device, nslc=nslc))
    seen.sort(key=lambda s: (s.device, s.nslc))
    return seen


class PsdWidget(QWidget):
    """PSD viewer for one or more streams."""

    def __init__(
        self,
        engine: StreamingEngine,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._streams_in_combo: list[_StreamKey] = []
        self._primary: _StreamKey | None = None
        self._overlays: list[_StreamKey] = []
        # One pyqtgraph PlotDataItem per stream + two for the NLNM /
        # NHNM. Stored keyed by stream so updates land on the right
        # curve when results arrive out of order.
        self._curves: dict[tuple[str, str], pg.PlotDataItem] = {}
        self._nlnm_curve: pg.PlotDataItem | None = None
        self._nhnm_curve: pg.PlotDataItem | None = None

        # ---- toolbar ----
        # Pane title (M7 C2): unified with the Live / Spectrogram pane
        # headers via the shared style so the three panes read as one
        # family. Tracks the primary stream selection.
        self._title_label = QLabel(_PSD_NO_STREAM_TITLE, self)
        self._title_label.setObjectName(PANE_TITLE_OBJECT_NAME)
        self._title_label.setStyleSheet(PANE_TITLE_STYLE)

        self._stream_combo = QComboBox(self)
        self._stream_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self._stream_combo.currentIndexChanged.connect(self._on_stream_changed)

        self._window_combo = QComboBox(self)
        for label, _ in _WINDOW_PRESETS:
            self._window_combo.addItem(label)
        self._window_combo.setCurrentIndex(_DEFAULT_WINDOW_INDEX)
        self._window_combo.currentIndexChanged.connect(self._on_window_changed)

        self._refresh_button = QPushButton("Refresh", self)
        self._refresh_button.clicked.connect(self._fire_request_all)

        self._auto_refresh = QCheckBox("auto", self)
        self._auto_refresh.setChecked(True)
        self._auto_refresh.toggled.connect(self._on_auto_refresh_toggled)

        self._nlnm_toggle = QCheckBox("NLNM/NHNM", self)
        # Default OFF — the trace PSD is in dB rel. counts²/Hz (the
        # engine's ring buffer holds raw counts, never response-
        # corrected) while NLNM/NHNM are in dB rel. (m/s²)²/Hz
        # (acceleration). Overlaying them with the toggle ON is only
        # physically meaningful on response-corrected channels — a
        # capability that arrives with the instrument-response work in
        # M8. We keep the toggle present so a user with an externally
        # converted feed can opt in; the tooltip spells out the
        # constraint.
        self._nlnm_toggle.setChecked(False)
        self._nlnm_toggle.setToolTip(
            "Overlay Peterson 1993 New Low / High Noise Models. "
            "Only physically meaningful when the trace PSD is in "
            "dB rel. (m/s²)²/Hz (acceleration, response-corrected). "
            "Counts²/Hz PSDs cannot be compared to these curves "
            "directly — leave OFF unless your channel is already "
            "in acceleration units."
        )
        self._nlnm_toggle.toggled.connect(self._on_nlnm_toggled)

        self._add_overlay_button = QPushButton("+ overlay", self)
        self._add_overlay_button.setToolTip(
            "Add another stream's PSD as an extra curve, for side-by-side comparison."
        )
        self._add_overlay_button.clicked.connect(self._on_add_overlay)

        toolbar = QWidget(self)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(4, 2, 4, 2)
        toolbar_layout.addWidget(self._title_label)
        toolbar_layout.addWidget(QLabel("Stream:"))
        toolbar_layout.addWidget(self._stream_combo, stretch=1)
        toolbar_layout.addWidget(QLabel("Window:"))
        toolbar_layout.addWidget(self._window_combo)
        toolbar_layout.addWidget(self._refresh_button)
        toolbar_layout.addWidget(self._auto_refresh)
        toolbar_layout.addWidget(self._nlnm_toggle)
        toolbar_layout.addWidget(self._add_overlay_button)

        # ---- plot ----
        # Decimated log axis so the 5-10 Hz / 50-100 Hz minor-tick labels do
        # not collide (shared with the HVSR plots).
        self._plot_widget = pg.PlotWidget(
            self, axisItems={"bottom": DecimatedLogAxisItem(orientation="bottom")}
        )
        self._plot_widget.setBackground("#101418")
        self._plot_widget.setLogMode(x=True, y=False)
        self._plot_widget.showGrid(x=True, y=True, alpha=0.25)
        self._plot_widget.setLabel("left", "PSD", units="dB rel. counts²/Hz")
        self._plot_widget.setLabel("bottom", "Frequency", units="Hz")
        self._plot_widget.setMenuEnabled(False)
        self._plot_widget.addLegend(offset=(-10, 10))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        layout.addWidget(toolbar)
        layout.addWidget(self._plot_widget, stretch=1)

        # ---- engine wiring ----
        self._engine.newStreamSeen.connect(self._on_new_stream)
        self._engine.devicesChanged.connect(self._refresh_stream_combo)
        self._engine.psdReady.connect(self._on_psd_ready)

        # ---- auto-refresh timer ----
        self._auto_timer = QTimer(self)
        self._auto_timer.timeout.connect(self._fire_request_all)
        self._update_auto_timer_interval()
        self._auto_timer.start()

        # Populate the combo from whatever streams the engine already
        # knows about (post-restore-from-snapshot path).
        self._refresh_stream_combo()

    # ------------------------------------------------------------------
    # Public methods (mainly for tests)
    # ------------------------------------------------------------------
    def selected_stream(self) -> _StreamKey | None:
        return self._primary

    def overlays(self) -> tuple[_StreamKey, ...]:
        return tuple(self._overlays)

    def window_seconds(self) -> float:
        idx = self._window_combo.currentIndex()
        if 0 <= idx < len(_WINDOW_PRESETS):
            return _WINDOW_PRESETS[idx][1]
        return _WINDOW_PRESETS[_DEFAULT_WINDOW_INDEX][1]

    def request_refresh(self) -> None:
        self._fire_request_all()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    @Slot(str, str)
    def _on_new_stream(self, device: str, nslc: str) -> None:
        del device, nslc  # combo refresh re-enumerates engine state
        self._refresh_stream_combo()

    @Slot(str, str, float, object, object)
    def _on_psd_ready(
        self,
        device: str,
        nslc: str,
        seconds: float,
        freqs: object,
        db: object,
    ) -> None:
        # Drop stale results — the user might have moved on.
        if not self._is_active_stream(device, nslc):
            return
        if seconds != self.window_seconds():
            return
        if not isinstance(freqs, np.ndarray) or not isinstance(db, np.ndarray):
            return
        if freqs.size == 0:
            return
        self._update_curve_for(device, nslc, freqs, db)
        self._update_nlnm_overlay(freqs)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _refresh_stream_combo(self) -> None:
        keys = _enumerate_streams(self._engine)
        # Remember the current selection so a refresh that adds a new
        # stream does not yank the user's combo to a different choice.
        prior = self._primary
        self._streams_in_combo = keys
        self._stream_combo.blockSignals(True)
        self._stream_combo.clear()
        for key in keys:
            self._stream_combo.addItem(key.display, userData=key)
        if prior is not None:
            for i, key in enumerate(keys):
                if key.device == prior.device and key.nslc == prior.nslc:
                    self._stream_combo.setCurrentIndex(i)
                    break
        elif keys:
            self._stream_combo.setCurrentIndex(0)
        self._stream_combo.blockSignals(False)
        # If selection changed (e.g. first stream just arrived), pick up
        # the change and fire a refresh.
        new_primary = self._stream_combo.currentData()
        if isinstance(new_primary, _StreamKey):
            self._primary = new_primary
            self._update_title()
            self._fire_request_all()

    def _on_stream_changed(self, index: int) -> None:
        if not 0 <= index < len(self._streams_in_combo):
            return
        self._primary = self._streams_in_combo[index]
        self._update_title()
        # Drop any cached curves whose stream is no longer relevant.
        self._prune_stale_curves()
        self._fire_request_all()

    def _update_title(self) -> None:
        """Sync the pane title to the primary stream selection (M7 C2)."""
        if self._primary is None:
            self._title_label.setText(_PSD_NO_STREAM_TITLE)
        else:
            self._title_label.setText(self._primary.display)

    def _on_window_changed(self, _index: int) -> None:
        self._update_auto_timer_interval()
        self._fire_request_all()

    def _on_auto_refresh_toggled(self, checked: bool) -> None:
        if checked:
            self._auto_timer.start()
        else:
            self._auto_timer.stop()

    def _on_nlnm_toggled(self, checked: bool) -> None:
        if not checked:
            # Hide existing curves; we keep them around so a later
            # toggle-on doesn't have to wait for a new request.
            if self._nlnm_curve is not None:
                self._nlnm_curve.setVisible(False)
            if self._nhnm_curve is not None:
                self._nhnm_curve.setVisible(False)
            return
        if self._nlnm_curve is not None:
            self._nlnm_curve.setVisible(True)
        if self._nhnm_curve is not None:
            self._nhnm_curve.setVisible(True)

    def _on_add_overlay(self) -> None:
        # Take the currently-selected stream as the overlay. The user
        # then changes the combo to compare against another stream.
        if self._primary is None:
            return
        if any(
            o.device == self._primary.device and o.nslc == self._primary.nslc
            for o in self._overlays
        ):
            return
        # We allow up to len(_OVERLAY_PENS) overlays — beyond that the
        # legend becomes unreadable. Silently drop the request.
        if len(self._overlays) >= len(_OVERLAY_PENS):
            return
        self._overlays.append(self._primary)
        self._fire_request_all()

    def _fire_request_all(self) -> None:
        seconds = self.window_seconds()
        if self._primary is not None:
            self._engine.psdRequested.emit(self._primary.device, self._primary.nslc, seconds)
        for overlay in self._overlays:
            self._engine.psdRequested.emit(overlay.device, overlay.nslc, seconds)

    def _is_active_stream(self, device: str, nslc: str) -> bool:
        if self._primary is not None and (
            self._primary.device == device and self._primary.nslc == nslc
        ):
            return True
        return any(o.device == device and o.nslc == nslc for o in self._overlays)

    def _update_curve_for(self, device: str, nslc: str, freqs: np.ndarray, db: np.ndarray) -> None:
        key = (device, nslc)
        curve = self._curves.get(key)
        if curve is None:
            pen = (
                _PRIMARY_PEN
                if self._primary is not None
                and self._primary.device == device
                and self._primary.nslc == nslc
                else _OVERLAY_PENS[len([k for k in self._curves if k != key]) % len(_OVERLAY_PENS)]
            )
            label = f"{device}/{nslc}"
            curve = self._plot_widget.plot(name=label, pen=pen)
            self._curves[key] = curve
        # Skip the DC bin in log-X mode — log10(0) is undefined and
        # pyqtgraph warns / drops the point anyway.
        mask = freqs > 0
        curve.setData(freqs[mask], db[mask])

    def _update_nlnm_overlay(self, freqs: np.ndarray) -> None:
        if not self._nlnm_toggle.isChecked():
            return
        mask = freqs > 0
        positive_freqs = freqs[mask]
        if positive_freqs.size == 0:
            return
        nlnm, nhnm = _nlnm_interp(positive_freqs.astype(np.float64))
        if self._nlnm_curve is None:
            self._nlnm_curve = self._plot_widget.plot(
                positive_freqs, nlnm, name="NLNM", pen=_NLNM_PEN
            )
        else:
            self._nlnm_curve.setData(positive_freqs, nlnm)
            self._nlnm_curve.setVisible(True)
        if self._nhnm_curve is None:
            self._nhnm_curve = self._plot_widget.plot(
                positive_freqs, nhnm, name="NHNM", pen=_NHNM_PEN
            )
        else:
            self._nhnm_curve.setData(positive_freqs, nhnm)
            self._nhnm_curve.setVisible(True)

    def _prune_stale_curves(self) -> None:
        active = set()
        if self._primary is not None:
            active.add((self._primary.device, self._primary.nslc))
        for o in self._overlays:
            active.add((o.device, o.nslc))
        for key in list(self._curves):
            if key not in active:
                curve = self._curves.pop(key)
                self._plot_widget.removeItem(curve)

    def _update_auto_timer_interval(self) -> None:
        seconds = self.window_seconds()
        interval_s = max(_AUTO_REFRESH_MIN_S, seconds / 4.0)
        self._auto_timer.setInterval(int(interval_s * 1000))
