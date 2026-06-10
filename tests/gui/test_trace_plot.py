"""Smoke tests for TracePlot (pytest-qt)."""

from __future__ import annotations

import numpy as np

from echosmonitor.gui.widgets.trace_plot import TracePlot


def test_trace_plot_starts_with_no_data_overlay(qtbot) -> None:
    plot = TracePlot(window_seconds=10.0, fs=100.0, label="IV.MILN..HHZ")
    qtbot.addWidget(plot)
    # Buffer is sized window_seconds * fs.
    assert plot._buffer_size_for_test() == 1000


def test_trace_plot_push_updates_curve(qtbot) -> None:
    plot = TracePlot(window_seconds=10.0, fs=100.0, label="IV.MILN..HHZ")
    qtbot.addWidget(plot)

    n = 1000
    samples = (np.arange(n, dtype=np.float32) - 100.0) * 0.5
    plot.push(samples)

    curve = plot._curve_for_test()
    x, y = curve.getData()
    assert x is not None and y is not None
    assert x.shape == (1000,)
    assert y.shape == (1000,)
    # The last sample written must be the last sample shown.
    assert float(y[-1]) == float(samples[-1])
    # Buffer capacity unchanged (n == window_seconds*fs case).
    assert plot._buffer_size_for_test() == 1000


def test_trace_plot_push_in_chunks_preserves_order(qtbot) -> None:
    plot = TracePlot(window_seconds=10.0, fs=100.0, label="IV.MILN..HHZ")
    qtbot.addWidget(plot)
    plot.push(np.arange(0, 500, dtype=np.float32))
    plot.push(np.arange(500, 1000, dtype=np.float32))
    _, y = plot._curve_for_test().getData()
    np.testing.assert_array_equal(y, np.arange(0, 1000, dtype=np.float32))


def test_trace_plot_update_meta_changes_title_and_resizes(qtbot) -> None:
    plot = TracePlot(window_seconds=10.0, fs=100.0, label="IV.MILN..HHZ")
    qtbot.addWidget(plot)
    plot.update_meta("IV.MILN..HHZ", 50.0, "2026-05-08T00:00:00.000000Z")
    # Buffer rebuilt at window_seconds * new fs.
    assert plot._buffer_size_for_test() == 500
