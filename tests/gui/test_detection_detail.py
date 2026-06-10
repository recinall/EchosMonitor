"""Tests for the central detection detail pane (M8 B2).

Asserts observable rendering outcomes (rule 10): a real trace + ratio
curve get plotted with data when the detection is in the buffer, and
the honest scrolled-out message appears when it is not.
"""

from __future__ import annotations

import numpy as np
from obspy import UTCDateTime

from echosmonitor.core.models import Detection
from echosmonitor.gui.widgets.detection_detail import DetectionDetailPane

_FS = 100.0


def _det(t_on: str, t_off: str | None) -> Detection:
    return Detection(
        device="dev",
        nslc="IU.ANMO.00.BHZ",
        kind="sta_lta",
        t_on=UTCDateTime(t_on),
        t_off=UTCDateTime(t_off) if t_off is not None else None,
        score=8.0,
        detected_at=UTCDateTime(t_on),
        meta={"sta_s": 1.0, "lta_s": 10.0, "on_thr": 3.5, "off_thr": 1.5},
    )


def _buffer_ending_at(latest: UTCDateTime, seconds: float) -> np.ndarray:
    n = int(seconds * _FS)
    rng = np.random.default_rng(1)
    return rng.standard_normal(n).astype(np.float32)


def test_long_title_does_not_inflate_minimum_width(qtbot) -> None:
    """BUG 2 regression: a long detection title must NOT pin the pane's
    minimum width.

    The detail pane is the central widget (inside a QStackedWidget whose
    minimum is the MAX over all pages). Before the fix, the title QLabel
    (wordWrap=False) propagated its full ~580px text width into the pane
    minimum once rendered, saturating the middle-row layout and freezing
    the dock splitters. The fix gives the title an ``Ignored`` horizontal
    size policy + end-elision. We assert the observable invariant: the
    pane minimum width stays bounded regardless of title length.
    """
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    pane.resize(900, 600)
    pane.show()
    qtbot.waitExposed(pane)

    # A pathologically long NSLC/title — far wider than any side dock.
    long_det = Detection(
        device="a-very-long-device-name-for-the-title-bar",
        nslc="XX.LONGSTATION.00.HHZ",
        kind="sta_lta",
        t_on=UTCDateTime("2026-06-01T00:00:30"),
        t_off=UTCDateTime("2026-06-01T00:09:33"),
        score=8.0,
        detected_at=UTCDateTime("2026-06-01T00:00:30"),
        meta={"sta_s": 1.0, "lta_s": 10.0},
    )
    samples = _buffer_ending_at(UTCDateTime("2026-06-01T00:10:00"), 60.0)
    pane.show_detection(long_det, samples, _FS, UTCDateTime("2026-06-01T00:10:00"))
    qtbot.wait(10)

    # The title text the pane would otherwise show is long...
    full_title = pane._format_title(long_det)
    assert len(full_title) > 80
    # ...but the pane's minimum width stays small (was ~729 before the fix).
    assert pane.minimumSizeHint().width() < 400
    # The full title is preserved in the tooltip; the visible text is elided.
    assert pane._title_tooltip_for_test() == full_title


def test_in_buffer_detection_renders_trace_and_ratio(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    latest = UTCDateTime("2026-06-01T00:01:00")
    samples = _buffer_ending_at(latest, 60.0)  # 60 s buffer ending at latest
    det = _det("2026-06-01T00:00:30", "2026-06-01T00:00:33")  # 30 s into the window

    pane.show_detection(det, samples, _FS, latest)

    assert pane._is_showing_plots_for_test()
    # Trace curve has the buffer's samples.
    xt, yt = pane._trace_curve_for_test().getData()
    assert xt is not None and len(xt) == len(samples)
    assert yt is not None and len(yt) == len(samples)
    # Ratio curve was recomputed (non-empty, finite) — the "why it fired" curve.
    xr, yr = pane._ratio_curve_for_test().getData()
    assert xr is not None and len(xr) == len(samples)
    assert np.all(np.isfinite(yr))
    assert float(np.nanmax(yr)) > 0.0


def test_scrolled_out_detection_shows_message(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    latest = UTCDateTime("2026-06-01T00:01:00")
    samples = _buffer_ending_at(latest, 20.0)  # buffer only covers last 20 s
    # Onset 40 s before latest → older than the buffer start → scrolled out.
    det = _det("2026-06-01T00:00:20", "2026-06-01T00:00:23")

    pane.show_detection(det, samples, _FS, latest)

    assert not pane._is_showing_plots_for_test()
    assert "scrolled out of the live buffer" in pane._message_text_for_test().lower()


def test_empty_buffer_shows_message(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    det = _det("2026-06-01T00:00:30", None)
    pane.show_detection(det, np.empty(0, dtype=np.float32), _FS, None)
    assert not pane._is_showing_plots_for_test()


def test_unknown_kind_clears_ratio_curve(qtbot) -> None:
    """A detection kind without recorded STA/LTA params clears the ratio
    curve rather than guessing; a subsequent STA/LTA detection re-renders it.
    """
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    latest = UTCDateTime("2026-06-01T00:01:00")
    samples = _buffer_ending_at(latest, 60.0)
    unknown = Detection(
        device="dev",
        nslc="IU.ANMO.00.BHZ",
        kind="future_detector",
        t_on=UTCDateTime("2026-06-01T00:00:30"),
        t_off=UTCDateTime("2026-06-01T00:00:33"),
        score=1.0,
        detected_at=UTCDateTime("2026-06-01T00:00:30"),
        meta={},
    )

    pane.show_detection(unknown, samples, _FS, latest)

    assert pane._is_showing_plots_for_test()
    # The trace still renders...
    xt, _yt = pane._trace_curve_for_test().getData()
    assert xt is not None and len(xt) == len(samples)
    # ...but the ratio curve is cleared (no STA/LTA params to recompute).
    xr, _yr = pane._ratio_curve_for_test().getData()
    assert xr is None or len(xr) == 0

    # A plain STA/LTA detection re-renders the amber ratio curve.
    pane.show_detection(_det("2026-06-01T00:00:30", "2026-06-01T00:00:33"), samples, _FS, latest)
    xr2, yr2 = pane._ratio_curve_for_test().getData()
    assert xr2 is not None and len(xr2) == len(samples)
    assert float(np.nanmax(yr2)) > 0.0
    assert pane._ratio_label_for_test() == "STA/LTA"


def test_clear_restores_placeholder(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    latest = UTCDateTime("2026-06-01T00:01:00")
    samples = _buffer_ending_at(latest, 60.0)
    pane.show_detection(_det("2026-06-01T00:00:30", "2026-06-01T00:00:33"), samples, _FS, latest)
    assert pane._is_showing_plots_for_test()

    pane.clear()
    assert not pane._is_showing_plots_for_test()
    assert "select a detection" in pane._message_text_for_test().lower()


# ----------------------------------------------------------------------
# M11 B — unit selector
# ----------------------------------------------------------------------
from echosmonitor.gui.widgets.detection_detail import NO_RESPONSE_TOOLTIP  # noqa: E402


def _shown_pane(qtbot) -> DetectionDetailPane:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    latest = UTCDateTime("2026-06-01T00:01:00")
    samples = _buffer_ending_at(latest, 60.0)
    pane.show_detection(_det("2026-06-01T00:00:30", "2026-06-01T00:00:33"), samples, _FS, latest)
    return pane


def test_default_unit_is_counts_with_counts_label(qtbot) -> None:
    pane = _shown_pane(qtbot)
    assert pane.current_unit_for_test() == "COUNTS"
    assert pane.top_axis_label_for_test() == "counts"


def test_show_physical_trace_swaps_ydata_and_label(qtbot) -> None:
    pane = _shown_pane(qtbot)
    _xt, y_counts = pane._trace_curve_for_test().getData()
    physical = np.asarray(y_counts, dtype=np.float64) * 1e-9 + 1.0

    pane.show_physical_trace("Velocity (m/s)", physical)

    xt2, yt2 = pane._trace_curve_for_test().getData()
    assert pane.top_axis_label_for_test() == "Velocity (m/s)"
    # Same X window (counts X axis reused), new Y data.
    assert len(xt2) == len(y_counts)
    assert not np.allclose(yt2, y_counts)
    assert np.allclose(yt2, physical)


def test_revert_to_counts_restores_ydata_and_label(qtbot) -> None:
    pane = _shown_pane(qtbot)
    _xt, y_counts = pane._trace_curve_for_test().getData()
    pane.show_physical_trace("Velocity (m/s)", np.asarray(y_counts) + 5.0)

    pane.revert_to_counts()

    _xt2, yt2 = pane._trace_curve_for_test().getData()
    assert pane.top_axis_label_for_test() == "counts"
    assert pane.current_unit_for_test() == "COUNTS"
    assert np.allclose(yt2, y_counts)


def test_unit_change_emits_request_but_not_on_programmatic_reset(qtbot) -> None:
    pane = _shown_pane(qtbot)
    received: list[str] = []
    pane.unitChangeRequested.connect(received.append)

    # User picks Velocity (index 1) → exactly one request with the code.
    pane._unit_combo.setCurrentIndex(1)
    assert received == ["VEL"]

    # A new detection re-renders and resets to Counts — must NOT emit.
    received.clear()
    latest = UTCDateTime("2026-06-01T00:02:00")
    samples = _buffer_ending_at(latest, 60.0)
    pane.show_detection(_det("2026-06-01T00:01:30", "2026-06-01T00:01:33"), samples, _FS, latest)
    assert received == []
    assert pane.current_unit_for_test() == "COUNTS"


def test_set_response_available_false_disables_physical_items_with_tooltip(qtbot) -> None:
    pane = _shown_pane(qtbot)
    pane.set_response_available(False, NO_RESPONSE_TOOLTIP)

    # Counts (idx 0) always enabled; the three physical items disabled.
    assert pane.unit_item_enabled_for_test(0) is True
    assert pane.unit_item_enabled_for_test(1) is False
    assert pane.unit_item_enabled_for_test(2) is False
    assert pane.unit_item_enabled_for_test(3) is False
    assert pane.unit_combo_tooltip_for_test() == NO_RESPONSE_TOOLTIP


def test_set_response_available_true_enables_physical_items(qtbot) -> None:
    pane = _shown_pane(qtbot)
    pane.set_response_available(False, NO_RESPONSE_TOOLTIP)
    pane.set_response_available(True, "")

    for i in range(4):
        assert pane.unit_item_enabled_for_test(i) is True
    assert pane.unit_combo_tooltip_for_test() == ""


def test_scrolled_out_disables_combo(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    latest = UTCDateTime("2026-06-01T00:01:00")
    samples = _buffer_ending_at(latest, 20.0)
    pane.show_detection(_det("2026-06-01T00:00:20", "2026-06-01T00:00:23"), samples, _FS, latest)
    assert not pane._is_showing_plots_for_test()
    assert not pane._unit_combo.isEnabled()


# ----------------------------------------------------------------------
# Archive 3-component view (static, off-thread-loaded)
# ----------------------------------------------------------------------
from echosmonitor.core.archive_detail_loader import ComponentTrace  # noqa: E402


def _component_trace(
    comp: str, t_start: float, seconds: float, *, gap: bool = False
) -> ComponentTrace:
    n = int(seconds * _FS)
    x = t_start + np.arange(n, dtype=np.float64) / _FS
    y = np.random.default_rng(ord(comp)).standard_normal(n).astype(np.float64)
    if gap:
        y[n // 3 : n // 2] = np.nan
    return ComponentTrace(
        comp=comp, nslc=f"IU.ANMO.00.BH{comp}", x=x, y=y, fs=_FS, start_epoch=t_start
    )


def _archive_traces(
    t_start: float, seconds: float, comps=("Z", "N", "E"), gap_comp: str | None = None
) -> list[ComponentTrace]:
    return [_component_trace(c, t_start, seconds, gap=(c == gap_comp)) for c in comps]


def test_show_archive_3c_renders_three_components(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    t_start = float(UTCDateTime("2026-06-01T00:00:20"))
    traces = _archive_traces(t_start, 40.0)
    det = _det("2026-06-01T00:00:30", "2026-06-01T00:00:33")

    pane.show_archive_3c(det, traces, "Z")

    assert pane.is_showing_archive()
    assert pane._is_showing_archive_for_test()
    assert not pane._is_showing_plots_for_test()  # the LIVE page is not current
    for comp in ("Z", "N", "E"):
        xs, _ys = pane._archive_curve_for_test(comp).getData()
        assert xs is not None and len(xs) == len(traces[0].x)
    # The trigger STA/LTA ratio was recomputed (the "why it fired" context).
    xr, yr = pane._arc_ratio_curve_for_test().getData()
    assert xr is not None and len(xr) > 0
    assert float(np.nanmax(np.asarray(yr))) > 0.0
    # Layout toggle is visible in archive mode; default stacked.
    assert pane._layout_toggle_visible_for_test()
    assert pane._component_layout_for_test() == "stacked"
    # Unit context comes from the trigger component (selector usable).
    assert pane.rendered_counts_context() is not None


def test_archive_3c_x_axis_fits_data_window(qtbot) -> None:
    """rule 10: the curves must be VISIBLE, not just fed.

    The shared X axis must cover the data span. An x-linked trigger-window
    region otherwise leaves the source view at its default ~[0, 1] range,
    rendering the wall-clock curves off-screen — a break no ``setData``
    assertion would catch.
    """
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    t_start = float(UTCDateTime("2026-06-01T00:00:20"))
    traces = _archive_traces(t_start, 40.0)
    det = _det("2026-06-01T00:00:30", "2026-06-01T00:00:33")

    pane.show_archive_3c(det, traces, "Z")

    x_lo, x_hi = pane._arc_x_range_for_test()
    data_lo, data_hi = float(traces[0].x[0]), float(traces[0].x[-1])
    assert x_lo <= data_lo + 1.0 and x_hi >= data_hi - 1.0  # spans the window
    assert x_hi - x_lo > 30.0  # ~40 s window, NOT a sub-second collapse


def _event_trace(comp: str, read_start: float, onset: float, read_end: float) -> ComponentTrace:
    """A noise trace with an impulsive damped wavelet beginning AT ``onset``.

    Spans ``[read_start, read_end]`` on a regular ``_FS`` grid (the archive
    read window, which includes warm-up pre-roll ahead of the inspect span).
    """
    n = round((read_end - read_start) * _FS)
    x = read_start + np.arange(n, dtype=np.float64) / _FS
    rng = np.random.default_rng(ord(comp))
    y = rng.standard_normal(n)
    rel = x - onset
    ev = (rel >= 0.0) & (rel < 2.0)
    y[ev] += 25.0 * np.sin(2.0 * np.pi * 5.0 * rel[ev]) * np.exp(-rel[ev] * 1.5)
    return ComponentTrace(
        comp=comp,
        nslc=f"IU.ANMO.00.BH{comp}",
        x=x,
        y=y.astype(np.float64),
        fs=_FS,
        start_epoch=read_start,
    )


def test_archive_ratio_peak_aligns_with_trigger_window(qtbot) -> None:
    """rule 10 / H3: the recomputed STA/LTA peak lines up with the amber band.

    A recursive LTA needs ~``lta_s`` of pre-roll to converge; with only the
    inspect pre-roll (10 s) the ratio is flat through the onset and peaks
    spuriously near the right edge (the time-axis bug class). The archive read
    supplies warm-up pre-roll AHEAD of the inspect window and the view is
    ranged to the inspect window so that pre-roll renders off-screen.

    Build a trace whose impulsive event begins exactly at ``t_on`` plus that
    warm-up pre-roll, then assert the rendered ratio curve PEAKS inside the
    trigger window in absolute time — not 25 s later at the far edge — and
    that the on-screen view is the inspect window, not the wider read span.
    """
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    # lta_s=30 makes the inspect pre-roll (10 s) only 1/3 of an LTA window, so
    # the OLD no-warm-up behaviour peaks ~30 s late (outside the band) while
    # the warm-up read converges on the onset — the test then discriminates.
    sta_s, lta_s = 1.0, 30.0
    onset = UTCDateTime("2026-06-01T00:02:00")
    t_on = float(onset)
    t_off = t_on + 3.0
    warmup = 2.0 * lta_s  # _ARCHIVE_RATIO_WARMUP_LTA_MULT * lta_s
    read_start = t_on - 10.0 - warmup
    read_end = t_off + 30.0
    view_start, view_end = t_on - 10.0, t_off + 30.0
    det = _det("2026-06-01T00:02:00", "2026-06-01T00:02:03")
    det.meta["lta_s"] = lta_s
    trig = _event_trace("Z", read_start, t_on, read_end)

    pane.show_archive_3c(det, [trig], "Z", view_start_epoch=view_start, view_end_epoch=view_end)

    xr, yr = pane._arc_ratio_curve_for_test().getData()
    xr = np.asarray(xr, dtype=np.float64)
    yr = np.asarray(yr, dtype=np.float64)
    assert xr.size > 0 and np.isfinite(yr).any()
    peak_x = float(xr[int(np.nanargmax(yr))])
    # The peak lands inside the trigger window (a couple of STA windows slack),
    # NOT drifted toward the right edge — the bug put it ~25 s late.
    assert t_on - sta_s <= peak_x <= t_off + 2.0 * sta_s, (
        f"STA/LTA peak at {peak_x - t_on:+.1f}s rel-onset; expected within the band"
    )
    # The amber band the user sees coincides with [t_on, t_off].
    lo, hi = pane._arc_ratio_region.getRegion()
    assert abs(float(lo) - t_on) < 0.5 and abs(float(hi) - t_off) < 0.5
    # The peak sits well inside the on-screen view, not at its right edge.
    assert peak_x < view_end - 10.0
    # The view is anchored to the inspect window: the warm-up pre-roll is
    # off-screen to the left (left edge is far right of the read start).
    x_lo, _x_hi = pane._arc_x_range_for_test()
    assert x_lo > read_start + warmup / 2.0


def test_archive_3c_nan_gaps_preserved(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    t_start = float(UTCDateTime("2026-06-01T00:00:20"))
    det = _det("2026-06-01T00:00:30", "2026-06-01T00:00:33")

    pane.show_archive_3c(det, _archive_traces(t_start, 40.0, gap_comp="N"), "Z")

    _xn, yn = pane._archive_curve_for_test("N").getData()
    assert np.isnan(np.asarray(yn)).any()  # gap kept as a break, never interpolated
    _xz, yz = pane._archive_curve_for_test("Z").getData()
    assert np.all(np.isfinite(np.asarray(yz)))


def test_archive_layout_toggle_switches_and_remembers(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    t_start = float(UTCDateTime("2026-06-01T00:00:20"))
    det = _det("2026-06-01T00:00:30", "2026-06-01T00:00:33")
    pane.show_archive_3c(det, _archive_traces(t_start, 40.0), "Z")
    assert pane._component_layout_for_test() == "stacked"

    received: list[str] = []
    pane.componentLayoutChanged.connect(received.append)
    pane._overlaid_radio.setChecked(True)  # user toggles to overlaid
    assert received == ["overlaid"]
    assert pane._component_layout_for_test() == "overlaid"

    # The choice is remembered across a later 3C render (per-session).
    pane.show_archive_3c(det, _archive_traces(t_start, 40.0), "Z")
    assert pane._component_layout_for_test() == "overlaid"


def test_show_no_archive_data_message_is_honest(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    det = _det("2026-06-01T00:00:30", "2026-06-01T00:00:33")

    pane.show_no_archive_data(det)

    msg = pane._message_text_for_test().lower()
    assert "no archived" in msg
    assert "archive replay" not in msg  # the old "later milestone" prose is gone
    assert "later milestone" not in msg
    assert not pane.is_showing_archive()
    assert not pane._layout_toggle_visible_for_test()


def test_set_loading_shows_loading_state(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    det = _det("2026-06-01T00:00:30", "2026-06-01T00:00:33")

    pane.set_loading(det)

    assert "loading" in pane._message_text_for_test().lower()
    assert not pane.is_showing_archive()
    assert not pane._unit_combo.isEnabled()
    assert not pane._layout_toggle_visible_for_test()


def test_show_physical_component_swaps_only_that_curve(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    t_start = float(UTCDateTime("2026-06-01T00:00:20"))
    traces = _archive_traces(t_start, 40.0)
    det = _det("2026-06-01T00:00:30", "2026-06-01T00:00:33")
    pane.show_archive_3c(det, traces, "Z")
    _xz0, yz0 = pane._archive_curve_for_test("Z").getData()
    _xn0, yn0 = pane._archive_curve_for_test("N").getData()
    physical = np.asarray(yz0, dtype=np.float64) * 1e-9 + 1.0

    pane.show_physical_component("Z", "Velocity (m/s)", physical)

    _xz1, yz1 = pane._archive_curve_for_test("Z").getData()
    assert np.allclose(np.asarray(yz1), physical)
    _xn1, yn1 = pane._archive_curve_for_test("N").getData()  # N untouched
    assert np.allclose(np.asarray(yn1), np.asarray(yn0), equal_nan=True)


def test_revert_archive_to_counts_restores_all(qtbot) -> None:
    pane = DetectionDetailPane()
    qtbot.addWidget(pane)
    t_start = float(UTCDateTime("2026-06-01T00:00:20"))
    traces = _archive_traces(t_start, 40.0)
    det = _det("2026-06-01T00:00:30", "2026-06-01T00:00:33")
    pane.show_archive_3c(det, traces, "Z")
    _xz0, yz0 = pane._archive_curve_for_test("Z").getData()
    pane.show_physical_component("Z", "Velocity (m/s)", np.asarray(yz0) + 5.0)

    pane.revert_archive_to_counts()

    _xz1, yz1 = pane._archive_curve_for_test("Z").getData()
    assert np.allclose(np.asarray(yz1), np.asarray(yz0), equal_nan=True)
    assert pane.current_unit_for_test() == "COUNTS"
