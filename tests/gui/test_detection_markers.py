"""Tests for detection markers on trace plots + spectrograms (M8 C1/C2).

Per rule 10 these assert the marker items actually exist on the plot,
actually toggle visibility, and are actually pruned when scrolled out —
not merely that a method was called.
"""

from __future__ import annotations

import inspect

import numpy as np
import pyqtgraph as pg
from obspy import UTCDateTime

from echosmonitor.gui.widgets import marker_style
from echosmonitor.gui.widgets.live_tabs import LiveTabs
from echosmonitor.gui.widgets.marker_style import STA_LTA_COLOR
from echosmonitor.gui.widgets.spectrogram_dock import SpectrogramDock
from echosmonitor.gui.widgets.spectrogram_view import SpectrogramView
from echosmonitor.gui.widgets.trace_plot import TracePlot

# Use a fixed wall-clock anchor so marker posix coordinates are inside
# the trace's visible window deterministically.
_T0 = float(UTCDateTime("2026-06-01T00:00:00"))


def _seed_trace(plot: TracePlot, fs: float = 100.0, seconds: float = 5.0) -> None:
    """Push one chunk and pin the wall-clock anchor near _T0."""
    plot.update_meta("IU.ANMO.00.BHZ", fs, "2026-06-01T00:00:00")
    plot.push_raw(np.zeros(int(fs * seconds), dtype=np.float32))


def test_trace_marker_added_and_closed(qtbot) -> None:
    plot = TracePlot(window_seconds=60.0, fs=100.0, label="IU.ANMO.00.BHZ")
    qtbot.addWidget(plot)
    _seed_trace(plot)
    t_on = plot._latest_raw_t - 2.0  # 2 s before the latest sample → in window

    plot.add_detection_marker(7, t_on, None, 8.4)
    markers = plot._markers_for_test()
    assert 7 in markers
    assert markers[7].region is None  # open → onset line only
    assert markers[7].line.value() == t_on

    plot.update_detection_marker(7, t_on + 1.0)
    assert markers[7].region is not None  # closed → region added
    lo, hi = markers[7].region.getRegion()
    assert lo == t_on and hi == t_on + 1.0


def test_trace_marker_toggle_hides(qtbot) -> None:
    plot = TracePlot(window_seconds=60.0, fs=100.0, label="x")
    qtbot.addWidget(plot)
    _seed_trace(plot)
    plot.add_detection_marker(1, plot._latest_raw_t - 1.0, plot._latest_raw_t, 5.0)
    marker = plot._markers_for_test()[1]
    assert marker.line.isVisible()

    plot.set_markers_visible(False)
    assert not marker.line.isVisible()
    assert marker.region is not None and not marker.region.isVisible()

    plot.set_markers_visible(True)
    assert marker.line.isVisible()


def test_trace_marker_pruned_when_scrolled_out(qtbot) -> None:
    fs = 100.0
    plot = TracePlot(window_seconds=10.0, fs=fs, label="x")
    qtbot.addWidget(plot)
    _seed_trace(plot, fs=fs, seconds=2.0)
    # Marker near the current right edge.
    plot.add_detection_marker(3, plot._latest_raw_t - 1.0, plot._latest_raw_t - 0.5, 4.0)
    assert 3 in plot._markers_for_test()

    # Advance the trace well past the 10 s window so the marker scrolls out.
    for _ in range(15):
        plot.push_raw(np.zeros(int(fs), dtype=np.float32))  # +1 s each
    assert 3 not in plot._markers_for_test()


def test_spectrogram_marker_only_on_wall_clock_view(qtbot) -> None:
    # Inline (column-index) view: a posix marker has no meaning → no-op.
    inline = SpectrogramView(window_seconds=60.0, fs=100.0, label="x")
    qtbot.addWidget(inline)
    inline.add_detection_marker(1, _T0)
    assert inline._det_markers_for_test() == {}

    # Dock (wall-clock) view: the marker is placed.
    dock = SpectrogramView(window_seconds=60.0, fs=100.0, label="x", time_axis=True)
    qtbot.addWidget(dock)
    dock.add_detection_marker(1, _T0)
    assert 1 in dock._det_markers_for_test()
    assert dock._det_markers_for_test()[1].value() == _T0

    dock.set_markers_visible(False)
    assert not dock._det_markers_for_test()[1].isVisible()


def test_markers_render_sta_lta_amber_on_both_twins(qtbot) -> None:
    """M0 regression (rule 12): the per-phase colour chain is gone — every
    detection marker renders the single STA/LTA amber on BOTH twins (trace
    plot and wall-clock spectrogram), onset line and shaded region alike."""
    expected = pg.mkColor(STA_LTA_COLOR)

    plot = TracePlot(window_seconds=60.0, fs=100.0, label="x")
    qtbot.addWidget(plot)
    _seed_trace(plot)
    t_on = plot._latest_raw_t - 2.0
    plot.add_detection_marker(5, t_on, t_on + 1.0, 6.0)
    marker = plot._markers_for_test()[5]
    assert marker.line.pen.color().name() == STA_LTA_COLOR
    assert marker.region is not None
    region_color = marker.region.brush.color()
    assert (region_color.red(), region_color.green(), region_color.blue()) == (
        expected.red(),
        expected.green(),
        expected.blue(),
    )
    assert 0 < region_color.alpha() < 255  # translucent overlay, not opaque

    dock = SpectrogramView(window_seconds=60.0, fs=100.0, label="x", time_axis=True)
    qtbot.addWidget(dock)
    dock.add_detection_marker(5, _T0)
    line = dock._det_markers_for_test()[5]
    assert line.pen.color().name() == STA_LTA_COLOR


def test_marker_chain_carries_no_phase_param() -> None:
    """M0 regression (rule 12, "keep deleted"): the ``phase`` parameter was
    removed from the whole marker fan-out chain, and the per-phase colour
    map (``marker_color``) no longer exists — amber is the only colour."""
    for func in (
        TracePlot.add_detection_marker,
        SpectrogramView.add_detection_marker,
        LiveTabs.add_detection_marker,
        SpectrogramDock.add_detection_marker,
    ):
        assert "phase" not in inspect.signature(func).parameters, func.__qualname__
    assert not hasattr(marker_style, "marker_color")


def test_spectrogram_marker_pruned_when_scrolled_out(qtbot) -> None:
    dock = SpectrogramView(window_seconds=60.0, fs=100.0, label="x", time_axis=True)
    qtbot.addWidget(dock)
    freqs = np.linspace(0.0, 50.0, 65)
    base = _T0
    # First column at base; marker just before it.
    dock.add_column(np.ones(65, dtype=np.float64), freqs, t_end=base)
    dock.add_detection_marker(9, base - 1.0)
    assert 9 in dock._det_markers_for_test()

    # Advance past the view's full column span (600 columns @ 1 s) so the
    # marker falls off the left edge and is pruned.
    for i in range(1, 660):
        dock.add_column(np.ones(65, dtype=np.float64), freqs, t_end=base + float(i))
    assert 9 not in dock._det_markers_for_test()
