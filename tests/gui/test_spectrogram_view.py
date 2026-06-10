"""Tests for :class:`SpectrogramView`.

Focus on the contract that matters to consumers: column ingestion does
not crash on edge cases (very first column, NaNs, zeros, smaller than
expected shape), color-mode swap is safe, and the buffer ages out as
columns roll in.
"""

from __future__ import annotations

import numpy as np
import pytest

from echosmonitor.gui.widgets.spectrogram_view import (
    ColorMode,
    SpectrogramView,
    colorize,
    levels_for,
)

_FS = 100.0
_N_BINS = 65  # nperseg=128 → 65 bins via rfft


def _make_view(qtbot) -> SpectrogramView:
    view = SpectrogramView(window_seconds=10.0, fs=_FS, label="N.S.L.HHZ")
    qtbot.addWidget(view)
    return view


def _power_column() -> np.ndarray:
    rng = np.random.default_rng(seed=42)
    return rng.exponential(scale=1.0, size=_N_BINS).astype(np.float32)


def _freqs() -> np.ndarray:
    # Start above 0 so 1/f spectra are finite.
    return np.linspace(0.1, _FS / 2.0, _N_BINS, dtype=np.float32)


def _mapped_indices(view: SpectrogramView) -> np.ndarray:
    """Map the displayed buffer through the ImageItem's levels into the
    0..255 colour-map index space — i.e. what the user actually sees."""
    spec = view._spec
    assert spec is not None
    lo, hi = view._image.getLevels()
    norm = np.clip((spec - lo) / (hi - lo), 0.0, 1.0)
    return (norm * 255).astype(np.uint8)


def test_colorize_linear_returns_input() -> None:
    col = _power_column()
    out = colorize(col, ColorMode.LINEAR)
    np.testing.assert_array_equal(out, col)


def test_colorize_db_yields_finite_values() -> None:
    col = np.array([0.0, 1e-30, 1.0, 100.0, 1e6], dtype=np.float32)
    out = colorize(col, ColorMode.DB)
    assert np.all(np.isfinite(out))
    # 1.0 power → 0 dB; 100 power → 20 dB; 1e6 power → 60 dB.
    np.testing.assert_allclose(out[2], 0.0, atol=1e-3)
    np.testing.assert_allclose(out[3], 20.0, atol=1e-3)
    np.testing.assert_allclose(out[4], 60.0, atol=1e-3)


def test_colorize_zscore_is_per_column_and_normalised() -> None:
    """Z-score normalises each column over its own frequency axis: a
    non-constant power column yields a zero-mean, ~unit-std display
    column — the structure-revealing transform, not a degenerate one."""
    col = _power_column()
    out = colorize(col, ColorMode.Z_SCORE)
    assert np.all(np.isfinite(out))
    np.testing.assert_allclose(out.mean(), 0.0, atol=1e-4)
    # Unit std by construction (computed on log power, but still ~1).
    assert 0.5 < out.std() < 2.0


def test_colorize_zscore_safe_on_constant_column() -> None:
    """A constant (zero-variance) column must not divide by zero."""
    out = colorize(np.full(_N_BINS, 5.0, dtype=np.float32), ColorMode.Z_SCORE)
    assert np.all(np.isfinite(out))
    np.testing.assert_array_equal(out, np.zeros(_N_BINS, dtype=np.float32))


def test_levels_for_each_mode_is_distinct() -> None:
    assert levels_for(ColorMode.DB) != levels_for(ColorMode.Z_SCORE)
    assert levels_for(ColorMode.LINEAR) != levels_for(ColorMode.Z_SCORE)


def test_invalid_arguments_raise(qtbot) -> None:
    with pytest.raises(ValueError):
        SpectrogramView(window_seconds=0.0)
    with pytest.raises(ValueError):
        SpectrogramView(fs=0.0)


def test_add_column_initialises_buffer(qtbot) -> None:
    view = _make_view(qtbot)
    col = _power_column()
    freqs = np.linspace(0.0, _FS / 2.0, _N_BINS, dtype=np.float32)
    view.add_column(col, freqs)
    assert view._spec is not None
    assert view._spec.shape == (_N_BINS, view._max_columns)


def test_color_mode_swap_clears_buffer(qtbot) -> None:
    view = _make_view(qtbot)
    col = _power_column()
    freqs = np.linspace(0.0, _FS / 2.0, _N_BINS, dtype=np.float32)
    for _ in range(50):
        view.add_column(col, freqs)
    assert view._column_count > 0
    view.set_color_mode(ColorMode.DB)
    # Mode swap clears the buffer because we can't faithfully back-
    # transform raw values from display values; the user sees a fresh
    # canvas under the new color scale.
    assert view._column_count == 0


def test_update_meta_resets_state(qtbot) -> None:
    view = _make_view(qtbot)
    col = _power_column()
    freqs = np.linspace(0.0, _FS / 2.0, _N_BINS, dtype=np.float32)
    view.add_column(col, freqs)
    view.update_meta(fs=50.0)
    assert view._fs == 50.0
    assert view._column_count == 0


def test_clear_drops_buffer(qtbot) -> None:
    view = _make_view(qtbot)
    col = _power_column()
    freqs = np.linspace(0.0, _FS / 2.0, _N_BINS, dtype=np.float32)
    for _ in range(10):
        view.add_column(col, freqs)
    view.clear()
    assert view._column_count == 0


def test_mismatched_shape_is_dropped(qtbot) -> None:
    view = _make_view(qtbot)
    col = _power_column()
    freqs = np.linspace(0.0, _FS / 2.0, _N_BINS, dtype=np.float32)
    # Shape mismatch — must not raise nor allocate.
    view.add_column(col[:10], freqs)
    assert view._spec is None


def test_2d_input_is_dropped(qtbot) -> None:
    view = _make_view(qtbot)
    view.add_column(
        np.zeros((10, 10), dtype=np.float32),
        np.zeros((10, 10), dtype=np.float32),
    )
    assert view._spec is None


def test_on_column_slot_routes_to_add_column(qtbot) -> None:
    view = _make_view(qtbot)
    col = _power_column()
    freqs = np.linspace(0.0, _FS / 2.0, _N_BINS, dtype=np.float32)
    view.on_column("dev", "N.S.L.HHZ", col, freqs, None)
    assert view._spec is not None
    assert view._column_count == 1


def test_image_has_variance(qtbot) -> None:
    """The displayed image must carry information, not collapse to one
    colour. This is the test the original shape-only suite lacked: it
    fails on the old per-bin-over-time z-score (which drove a steady
    structured spectrum to a uniform mid-colormap block — the "green
    rectangle") and passes on the per-column z-score.

    Input: a steady, spectrally-structured column (1/f² red spectrum)
    repeated until the buffer is full — i.e. stationary in time but
    structured across frequency, the continuous-background case.
    """
    view = _make_view(qtbot)  # default mode is z-score
    freqs = _freqs()
    shape = ((1.0 / freqs) ** 2).astype(np.float32)
    for _ in range(view._max_columns + 10):
        view.add_column(shape.copy(), freqs)

    spec = view._spec
    assert spec is not None
    # Real variance in the stored display buffer...
    assert spec.std() > 0.1, "displayed buffer collapsed to ~constant"
    # ...and many distinct colours actually reach the screen.
    idx = _mapped_indices(view)
    assert len(np.unique(idx)) > 8, "image mapped to a near-single colour"


def test_color_modes_produce_different_levels(qtbot) -> None:
    """Each colour mode sets finite, non-degenerate (lo < hi) levels on
    the ImageItem, and the three modes differ from one another."""
    freqs = _freqs()
    rng = np.random.default_rng(7)
    # Realistic power magnitudes (counts²-ish) so dB and linear ranges
    # are clearly distinct from the fixed z-score range.
    cols = [rng.exponential(scale=1e4, size=_N_BINS).astype(np.float32) for _ in range(40)]

    seen: set[tuple[float, float]] = set()
    for mode in ColorMode:
        view = _make_view(qtbot)
        view.set_color_mode(mode)
        for col in cols:
            view.add_column(col, freqs)
        lo, hi = view._image.getLevels()
        assert np.isfinite(lo) and np.isfinite(hi), f"{mode}: non-finite levels"
        assert lo < hi, f"{mode}: degenerate levels {lo}..{hi}"
        seen.add((round(lo, 6), round(hi, 6)))
    assert len(seen) == 3, f"modes did not produce distinct levels: {seen}"


def test_zscore_handles_zero_variance_column(qtbot) -> None:
    """A warm-up / dead column of constant values must not introduce
    nan/inf into the displayed buffer (old code's std-floor division)."""
    view = _make_view(qtbot)  # z-score
    freqs = _freqs()
    view.add_column(np.full(_N_BINS, 7.0, dtype=np.float32), freqs)
    assert view._spec is not None
    assert np.all(np.isfinite(view._spec))


def test_time_axis_view_places_columns_on_wall_clock(qtbot) -> None:
    """A time-axis view maps the latest column to its t_end on the X
    axis (UTC epoch seconds), so the dock shows wall-clock time."""
    view = SpectrogramView(window_seconds=10.0, fs=_FS, label="t", time_axis=True)
    qtbot.addWidget(view)
    freqs = _freqs()
    col = _power_column()
    base = 1_700_000_000.0  # fixed epoch -> deterministic
    view.add_column(col, freqs, t_end=base)
    view.add_column(col, freqs, t_end=base + 1.0)
    # Right edge of the visible X range tracks the latest column's t_end.
    (x_lo, x_hi) = view._plot.viewRange()[0]
    assert x_hi == pytest.approx(base + 1.0, abs=1e-6)
    assert x_lo < x_hi


def test_time_axis_image_rect_anchored_to_epoch_not_column_index(qtbot) -> None:
    """The waterfall image must sit in epoch-seconds space, pinned to the
    column ``t_end`` values fed to it — not in column-index space.

    Twin of the trace-plot wall-clock fix (CLAUDE.md rule 10): the trace
    plot and the spectrogram each carry their own X axis, and the bug was
    that the spectrogram's axis fell back to a collapsed sub-second slice
    when the engine fed ``None`` end times. Here we feed real epochs and
    assert the image's left edge lands at ``latest_t_end - span`` (far
    from column index 0) and the span is the full window, never collapsed
    to the inter-column microsecond jitter that produced the
    "20.000 … 21.799" ticks.
    """
    view = SpectrogramView(window_seconds=10.0, fs=_FS, label="t", time_axis=True)
    qtbot.addWidget(view)
    freqs = _freqs()
    col = _power_column()
    base = 1_700_000_000.0
    view.add_column(col, freqs, t_end=base)
    view.add_column(col, freqs, t_end=base + 1.0)  # column step = 1.0 s

    # Image placement in plot (view) coordinates — reflects setRect().
    rect = view._image.mapRectToView(view._image.boundingRect())
    span = view._max_columns * 1.0  # max_columns columns at 1 s/col
    # Left edge is wall-clock epoch (~1.7e9), NOT column index 0.
    assert rect.left() == pytest.approx(base + 1.0 - span, abs=1e-3)
    assert rect.left() > 1_000_000_000.0
    # Width spans the full rolling window, not a collapsed sub-second slice.
    assert rect.width() == pytest.approx(span, abs=1e-3)
