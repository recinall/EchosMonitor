"""Behaviour tests for TracePlot display-only peak decimation (rule 11).

These assert observable invariants (CLAUDE.md rule 10), not mechanism shape:

* the rendered point count handed to the curve is bounded by the
  pixel/``max_display_rate_hz`` budget, NOT by ``window_seconds * fs``;
* a single-sample spike survives min/max decimation (no transient loss);
* an fs change rebuilds the full-rate buffer to the new size.

The full-rate buffer itself is never decimated — only what gets rendered.
"""

from __future__ import annotations

import numpy as np

from echosmonitor.gui.widgets.trace_plot import TracePlot, _minmax_decimate


def test_high_fs_render_is_bounded_not_window_times_fs(qtbot) -> None:
    """40k full-rate samples must render as a few-thousand-point curve."""
    fs = 4000.0
    window = 10.0
    plot = TracePlot(window_seconds=window, fs=fs, label="IV.HIFS..HHZ", max_display_rate_hz=250)
    qtbot.addWidget(plot)

    n = int(window * fs)  # 40_000
    samples = np.random.default_rng(0).standard_normal(n).astype(np.float32)
    plot.push(samples)

    # Full-rate buffer is untouched (rule 11: DSP/detection/storage rate).
    assert plot._buffer_size_for_test() == n

    _x, y = plot._curve_for_test().getData()
    assert y is not None
    # Rendered points bounded by the budget, not the 40k full-rate count.
    budget = plot._display_point_budget_for_test()
    assert y.shape[0] <= budget + 2  # +2 slack for interleave rounding
    assert y.shape[0] < n
    # Sanity: the budget itself is the rule-11 bound, not window*fs.
    assert budget <= int(window * 250) + 1
    assert budget < n


def test_minmax_decimation_preserves_a_spike(qtbot) -> None:
    """A single-sample transient must survive into the rendered curve.

    Naive stride decimation would drop it; min/max keeps it as a bin max.
    """
    fs = 4000.0
    window = 10.0
    plot = TracePlot(window_seconds=window, fs=fs, label="IV.SPIKE..HHZ", max_display_rate_hz=250)
    qtbot.addWidget(plot)

    n = int(window * fs)
    samples = np.zeros(n, dtype=np.float32)
    spike_amp = 1234.0
    samples[n // 2] = spike_amp  # lone, between flat zeros
    plot.push(samples)

    _x, y = plot._curve_for_test().getData()
    assert y is not None
    # The spike amplitude survives decimation (max bin retains it).
    assert float(y.max()) == spike_amp


def test_minmax_decimate_pure_helper_keeps_extrema() -> None:
    """The pure helper retains both the global min and max of each region."""
    n = 10_000
    y = np.zeros(n, dtype=np.float32)
    y[100] = 50.0  # spike in the first half
    y[9000] = -70.0  # trough in the second half
    x = np.arange(n, dtype=np.float64)
    x_dec, y_dec = _minmax_decimate(x, y, max_points=400)

    assert y_dec.shape[0] <= 402
    assert float(y_dec.max()) == 50.0
    assert float(y_dec.min()) == -70.0
    # X stays monotonic non-decreasing (valid polyline for the plot).
    assert np.all(np.diff(x_dec) >= 0)


def test_minmax_decimate_passthrough_when_small() -> None:
    """Below the budget the helper returns the inputs unchanged."""
    y = np.arange(100, dtype=np.float32)
    x = np.arange(100, dtype=np.float64)
    x_dec, y_dec = _minmax_decimate(x, y, max_points=2000)
    assert x_dec is x
    assert y_dec is y


def test_low_fs_is_not_decimated(qtbot) -> None:
    """At 100 Hz / 10 s (1000 pts) the curve renders verbatim."""
    plot = TracePlot(window_seconds=10.0, fs=100.0, label="IV.LOFS..HHZ", max_display_rate_hz=250)
    qtbot.addWidget(plot)
    samples = (np.arange(1000, dtype=np.float32) - 500.0) * 0.25
    plot.push(samples)
    _x, y = plot._curve_for_test().getData()
    assert y is not None
    assert y.shape[0] == 1000
    assert float(y[-1]) == float(samples[-1])


def test_fs_change_rebuilds_full_rate_buffer(qtbot) -> None:
    """update_meta with a new fs resizes the full-rate buffer (not display)."""
    plot = TracePlot(window_seconds=10.0, fs=100.0, label="IV.META..HHZ")
    qtbot.addWidget(plot)
    assert plot._buffer_size_for_test() == 1000

    plot.update_meta("IV.META..HHZ", 4000.0, "1970-01-01T00:00:00.000000Z")
    # Full-rate buffer follows fs: 10 s * 4000 Hz, never decimated.
    assert plot._buffer_size_for_test() == 40_000

    n = 40_000
    samples = np.random.default_rng(1).standard_normal(n).astype(np.float32)
    plot.push(samples)
    assert plot._buffer_size_for_test() == n
    _x, y = plot._curve_for_test().getData()
    assert y is not None
    assert y.shape[0] < n  # rendered count stays bounded after the fs change


def test_stacked_processed_curve_is_also_bounded(qtbot) -> None:
    """The lower (processed) curve gets the same display-rate bound."""
    fs = 4000.0
    window = 10.0
    plot = TracePlot(
        window_seconds=window,
        fs=fs,
        label="IV.STK..HHZ",
        mode="stacked",
        fs_processed=fs,
        max_display_rate_hz=250,
    )
    qtbot.addWidget(plot)
    n = int(window * fs)
    samples = np.random.default_rng(2).standard_normal(n).astype(np.float32)
    plot.push_raw(samples)
    plot.push_processed(samples)

    pc = plot._processed_curve_for_test()
    assert pc is not None
    _x, y = pc.getData()
    assert y is not None
    assert y.shape[0] < n
    assert plot._processed_buffer_size_for_test() == n


def test_stacked_processed_curve_aligns_with_raw_wall_clock(qtbot) -> None:
    """The filtered (lower) plot must render within the raw plot's visible
    X window (regression for the "filtered plot is empty" bug).

    The lower plot is X-linked to the raw plot, whose axis is anchored to
    wall-clock via ``update_meta``. If the processed time base were left at
    the 1970 epoch it would render ~56 years to the left of the live view —
    off-screen. The processed curve's X range must overlap the raw curve's.
    """
    fs = 100.0
    plot = TracePlot(window_seconds=10.0, fs=fs, label="IU.ANMO.00.BHZ", mode="stacked")
    qtbot.addWidget(plot)
    # Anchor the raw axis to a 2026 wall-clock start, as the engine's
    # streamMeta does on the first packet.
    plot.update_meta("IU.ANMO.00.BHZ", fs, "2026-06-01T00:00:00")

    rng = np.random.default_rng(3)
    plot.push_raw(rng.standard_normal(500).astype(np.float32))
    plot.push_processed(rng.standard_normal(500).astype(np.float32))

    raw_x, _ = plot._curve_for_test().getData()
    proc_curve = plot._processed_curve_for_test()
    assert proc_curve is not None
    proc_x, _ = proc_curve.getData()

    # Both anchored near the same 2026 wall-clock; the processed window must
    # overlap the raw window (not sit decades away at the 1970 epoch).
    assert proc_x.max() > 1.0e9, "processed X is at the 1970 epoch — off-screen (empty plot bug)"
    overlaps = not (proc_x.max() < raw_x.min() or proc_x.min() > raw_x.max())
    assert overlaps, (
        f"processed X [{proc_x.min():.0f},{proc_x.max():.0f}] does not overlap "
        f"raw X [{raw_x.min():.0f},{raw_x.max():.0f}]"
    )
