"""Archive tab — static view (Stage B).

A loaded window renders its 3 components + a spectrogram, gaps stay NaN breaks,
the rendered X range fits the loaded window (the detail-pane regression: setData
correct but the view stuck at [0, 1]), and switching to physical units re-labels
+ re-renders a component.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import numpy as np
import pytest
from obspy import UTCDateTime
from obspy.core.util import get_example_file

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    ResponseMetadataConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.archive_browser_loader import ArchiveBrowserLoader
from echosmonitor.core.archive_window_loader import (
    ArchiveWindowResult,
    ComponentTrace,
)
from echosmonitor.gui.main_window import MainWindow
from echosmonitor.gui.widgets.archive_tab import ArchiveTab

_FS = 100.0
_T0 = float(UTCDateTime("2026-05-10T12:00:00").timestamp)
_DUR = 30.0


@pytest.fixture
def browser():
    """A real (idle) browser loader; the tab's ctor needs one (M3-A)."""
    loader = ArchiveBrowserLoader()
    yield loader
    loader.shutdown()


def _component(comp: str, *, gap: bool = False) -> ComponentTrace:
    n = int(_FS * _DUR)
    x = _T0 + np.arange(n, dtype=np.float64) / _FS
    rng = np.random.default_rng(abs(hash(comp)) % (2**32))
    y = rng.standard_normal(n).astype(np.float64) * 100.0
    if gap:
        y[n // 3 : n // 2] = np.nan  # a real gap → NaN break
    return ComponentTrace(comp=comp, nslc=f"XX.STA.00.HH{comp}", x=x, y=y, fs=_FS, start_epoch=_T0)


def _result(*, gap_on: str | None = None, with_spec: bool = True) -> ArchiveWindowResult:
    traces = [_component(c, gap=(c == gap_on)) for c in ("Z", "N", "E")]
    if with_spec:
        # A synthetic, non-degenerate power image (n_freq, n_cols).
        rng = np.random.default_rng(1)
        power = (rng.random((33, 40)).astype(np.float32) + 0.01) * 1e3
        freqs = np.linspace(0.0, _FS / 2, 33).astype(np.float64)
        spec_t_end = _T0 + _DUR
    else:
        power = None
        freqs = None
        spec_t_end = _T0 + _DUR
    return ArchiveWindowResult(
        token=1,
        traces=traces,
        primary_comp="Z",
        spec_power=power,
        spec_freqs=freqs,
        spec_t_start=_T0,
        spec_t_end=spec_t_end,
        elapsed_ms=1.0,
    )


def _make_tab(qtbot, tmp_path, browser) -> ArchiveTab:
    tab = ArchiveTab(browser, tmp_path)
    qtbot.addWidget(tab)
    # Pretend a window for this selection was loaded.
    tab._loaded_device = "dev"
    tab._loaded_group = {"Z": "XX.STA.00.HHZ", "N": "XX.STA.00.HHN", "E": "XX.STA.00.HHE"}
    tab._win_t_start = _T0
    tab._win_t_end = _T0 + _DUR
    return tab


def test_renders_three_components_and_spectrogram(qtbot, tmp_path, browser) -> None:
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result())

    for comp in ("Z", "N", "E"):
        x, _y = tab.trace_curve_for_test(comp).getData()
        assert x is not None and len(x) == int(_FS * _DUR)

    img = tab.spectrogram_image_for_test()
    assert img is not None
    assert float(np.var(img)) > 0.0  # non-degenerate (rule 10)


def test_gap_is_a_break_not_interpolated(qtbot, tmp_path, browser) -> None:
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result(gap_on="Z"))
    _x, y = tab.trace_curve_for_test("Z").getData()
    assert np.isnan(np.asarray(y, dtype=np.float64)).any()


def test_x_range_fits_loaded_window(qtbot, tmp_path, browser) -> None:
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result())
    lo, hi = tab.trace_x_range_for_test()
    # The rendered X range matches the data window, not the default [0, 1].
    assert lo == pytest.approx(_T0, abs=1.0)
    assert hi == pytest.approx(_T0 + _DUR, abs=1.0)


def test_short_window_renders_traces_without_spectrogram(qtbot, tmp_path, browser) -> None:
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result(with_spec=False))
    x, _y = tab.trace_curve_for_test("Z").getData()
    assert x is not None and len(x) > 0
    # No spectrogram image set when the window was too short for one STFT.
    assert tab.spectrogram_image_for_test() is None


def test_unit_relabel_and_rerender_on_physical(qtbot, tmp_path, browser) -> None:
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result())
    _x, y_counts = tab.trace_curve_for_test("Z").getData()
    y_counts = np.asarray(y_counts, dtype=np.float64).copy()

    # Simulate the host's decon result landing on the Z component.
    physical = y_counts * 1e-9
    tab.show_physical_component("Z", "Velocity (m/s)", physical)

    assert "Velocity (m/s)" in tab.top_unit_label_for_test("Z")
    _x2, y2 = tab.trace_curve_for_test("Z").getData()
    assert not np.allclose(np.asarray(y2), y_counts)


# ---------------------------------------------------------------------------
# M3-B: zoom/pan ergonomics
# ---------------------------------------------------------------------------


def test_spectrogram_x_linked_to_traces(qtbot, tmp_path, browser) -> None:
    """Zooming the traces keeps the spectrogram's time axis in sync.

    pyqtgraph pixel-aligns overlapping linked views (ranges differ by the
    axis-width offset so the DATA lines up on screen), so the assertion is
    span-relative follow, not exact equality — without the link the
    spectrogram would sit at the full 30 s window (error ≈ 5 s ≫ 5 %).
    """
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result())

    tab._stacked_plots["Z"].setXRange(_T0 + 5.0, _T0 + 10.0, padding=0.0)
    lo, hi = tab._spec_plot.viewRange()[0]
    assert lo == pytest.approx(_T0 + 5.0, abs=0.25)  # 5 % of the 5 s span
    assert hi == pytest.approx(_T0 + 10.0, abs=0.25)


def test_layout_toggle_preserves_time_zoom(qtbot, tmp_path, browser) -> None:
    """Stacked↔Overlaid keeps the zoom; the spectrogram follows the visible
    trace view (a static link through the hidden plot distorts ranges —
    the offscreen-geometry trap this stage hit)."""
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result())

    tab._stacked_plots["Z"].setXRange(_T0 + 5.0, _T0 + 10.0, padding=0.0)
    tab._overlaid_radio.setChecked(True)
    lo, hi = tab._overlay_plot.viewRange()[0]
    assert lo == pytest.approx(_T0 + 5.0, abs=0.1)
    assert hi == pytest.approx(_T0 + 10.0, abs=0.1)

    tab._overlay_plot.setXRange(_T0 + 2.0, _T0 + 8.0, padding=0.0)
    s_lo, s_hi = tab._spec_plot.viewRange()[0]
    assert s_lo == pytest.approx(_T0 + 2.0, abs=0.1)
    assert s_hi == pytest.approx(_T0 + 8.0, abs=0.1)

    tab._stacked_radio.setChecked(True)
    lo, hi = tab._stacked_plots["Z"].viewRange()[0]
    assert lo == pytest.approx(_T0 + 2.0, abs=0.1)
    assert hi == pytest.approx(_T0 + 8.0, abs=0.1)


def test_pan_is_bounded_near_loaded_window(qtbot, tmp_path, browser) -> None:
    """Panning cannot fly off to epoch-nowhere: the view is clamped to the
    loaded window ± one window-width."""
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result())

    tab._stacked_plots["Z"].setXRange(_T0 - 100_000.0, _T0 - 99_000.0, padding=0.0)
    lo, _hi = tab._stacked_plots["Z"].viewRange()[0]
    assert lo >= _T0 - _DUR - 1.0


# ---------------------------------------------------------------------------
# M3-B: unit switching with gaps stays honest per component
# ---------------------------------------------------------------------------


def test_partial_unit_switch_labels_each_component_honestly(qtbot, tmp_path, browser) -> None:
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result(gap_on="Z"))
    tab.set_response_available(True, "")
    tab._unit_combo.setCurrentIndex(1)  # VEL (no host wired — no decon runs)

    # Simulate the host's partial dispatch: N/E deconvolved, Z skipped.
    for comp in ("N", "E"):
        _x, y = tab.trace_curve_for_test(comp).getData()
        tab.show_physical_component(comp, "Velocity (m/s)", np.asarray(y) * 1e-9)
    tab.mark_components_left_in_counts(["Z"])

    assert "counts — gaps" in tab.top_unit_label_for_test("Z")
    assert "Velocity (m/s)" in tab.top_unit_label_for_test("N")
    assert "mixed units" in tab.overlay_unit_label_for_test()
    assert "Z left in counts" in tab.status_text_for_test()

    # The readout reports each component in ITS unit, not a global one.
    tab._readout_combo.setCurrentIndex(0)  # Z
    assert "counts" in tab.readout_text_for_test()
    tab._readout_combo.setCurrentIndex(1)  # N
    assert "m/s" in tab.readout_text_for_test()

    # Reverting to counts clears the mixed state everywhere.
    tab.revert_to_counts()
    assert tab.top_unit_label_for_test("Z").endswith("(counts)")
    assert tab.overlay_unit_label_for_test() == "counts"


def test_empty_result_resets_unit_labels(qtbot, tmp_path, browser) -> None:
    """Review major: an empty re-load must not keep claiming the previous
    window's 'Velocity' / 'counts — gaps' / 'mixed units' over empty axes."""
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result(gap_on="Z"))
    tab.set_response_available(True, "")
    tab._unit_combo.setCurrentIndex(1)  # VEL
    _x, y = tab.trace_curve_for_test("N").getData()
    tab.show_physical_component("N", "Velocity (m/s)", np.asarray(y) * 1e-9)
    tab.mark_components_left_in_counts(["Z"])
    assert "mixed units" in tab.overlay_unit_label_for_test()

    tab.show_empty()

    for comp in ("Z", "N", "E"):
        assert tab.top_unit_label_for_test(comp).endswith("(counts)")
    assert tab.overlay_unit_label_for_test() == "counts"


def test_reload_relabels_absent_component(qtbot, tmp_path, browser) -> None:
    """Review minor: a component absent from the NEW load must not keep the
    previous window's unit label over an empty plot."""
    tab = _make_tab(qtbot, tmp_path, browser)
    tab.show_result(_result(gap_on="Z"))
    tab.mark_components_left_in_counts(["Z"])
    assert "counts — gaps" in tab.top_unit_label_for_test("Z")

    res = _result()
    res.traces = [t for t in res.traces if t.comp != "Z"]  # Z absent now
    tab.show_result(res)

    assert tab.top_unit_label_for_test("Z").endswith("(counts)")


def test_failed_or_empty_request_keeps_loaded_window_metadata(
    qtbot, tmp_path, browser
) -> None:
    """Review minor: exports/hand-offs must describe the window ON SCREEN —
    a later request that fails (or comes back empty) must not rebind it."""
    tab = _make_tab(qtbot, tmp_path, browser)
    tab._pending_window = ("devA", {"Z": "XX.STA.00.HHZ"}, _T0, _T0 + _DUR)
    tab.show_result(_result())
    assert tab.current_window()[0] == "devA"  # committed on render

    tab._pending_window = ("devB", {"Z": "YY.OTH.00.HHZ"}, _T0 + 999.0, _T0 + 1999.0)
    tab.show_failed("boom")
    device, _group, t_start, _t_end = tab.current_window()
    assert device == "devA"
    assert t_start == _T0

    tab._pending_window = ("devC", {"Z": "YY.OTH.00.HHZ"}, _T0 + 999.0, _T0 + 1999.0)
    tab.show_empty()
    assert tab.current_window()[0] == "devA"


# ---------------------------------------------------------------------------
# M3-B: PNG export
# ---------------------------------------------------------------------------


def test_export_png_writes_real_image_and_tracks_view_state(
    qtbot, tmp_path, browser
) -> None:
    from PySide6.QtGui import QPixmap

    tab = _make_tab(qtbot, tmp_path, browser)
    assert not tab.export_enabled_for_test()  # nothing loaded yet

    tab.show_result(_result())
    assert tab.export_enabled_for_test()

    out = tmp_path / "view.png"
    assert tab.export_png(out)
    assert out.is_file() and out.stat().st_size > 0
    image = QPixmap(str(out))
    assert not image.isNull()
    assert image.width() > 0 and image.height() > 0
    assert str(out) in tab.status_text_for_test()

    tab.show_empty()
    assert not tab.export_enabled_for_test()


# ---------------------------------------------------------------------------
# End-to-end unit change through the REAL deconvolution worker (M11 idiom):
# verifies the archive-window decon wiring (separate token map) routes the
# physical result to the Archive tab.
# ---------------------------------------------------------------------------

_ANMO_NSLC = "IU.ANMO.00.BHZ"
_ANMO_FS = 20.0
_ANMO_T0 = float(UTCDateTime("2014-01-01T00:01:00").timestamp)  # inside the response epoch


def _cfg_with_response(tmp_path: Path) -> tuple[RootConfig, Path]:
    src = Path(get_example_file("IU_ANMO_00_BHZ.xml"))
    dst = tmp_path / "anmo.xml"
    shutil.copyfile(src, dst)
    dev = DeviceConfig(
        name="anmo",
        host="127.0.0.1",
        port=18000,
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
        response_metadata=ResponseMetadataConfig(path=dst, format="stationxml"),
    )
    cfg = RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[dev],
    )
    return cfg, tmp_path / "config.yaml"


def test_unit_change_end_to_end_through_real_worker(qtbot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg_with_response(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        n = int(_ANMO_FS * 60)
        x = _ANMO_T0 + np.arange(n, dtype=np.float64) / _ANMO_FS
        y = np.random.default_rng(0).standard_normal(n).astype(np.float64) * 1000.0
        z = ComponentTrace(comp="Z", nslc=_ANMO_NSLC, x=x, y=y, fs=_ANMO_FS, start_epoch=_ANMO_T0)
        res = ArchiveWindowResult(
            token=1,
            traces=[z],
            primary_comp="Z",
            spec_power=None,
            spec_freqs=None,
            spec_t_start=_ANMO_T0,
            spec_t_end=_ANMO_T0 + 60.0,
            elapsed_ms=1.0,
        )
        tab = window._archive_tab
        tab._loaded_device = "anmo"
        tab._loaded_group = {"Z": _ANMO_NSLC}
        tab._win_t_start = _ANMO_T0
        tab._win_t_end = _ANMO_T0 + 60.0
        window._archive_window_token = 1
        window._archive_window_traces = {"Z": z}
        tab.show_result(res)
        _x0, y_counts = tab.trace_curve_for_test("Z").getData()
        y_counts = np.asarray(y_counts, dtype=np.float64).copy()

        window._on_archive_window_unit_change("VEL")

        def physical_shown() -> bool:
            return "Velocity (m/s)" in tab.top_unit_label_for_test("Z")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not physical_shown():
            qtbot.wait(50)
        assert physical_shown(), "archive window never rendered physical units"
        _x1, y2 = tab.trace_curve_for_test("Z").getData()
        y2 = np.asarray(y2, dtype=np.float64)
        assert not np.allclose(y2, y_counts)
        assert np.all(np.isfinite(y2))
    finally:
        window.close()


def test_unit_change_partial_dispatch_marks_gappy_component(qtbot, tmp_path: Path) -> None:
    """M3-B: when one component carries gaps, its siblings still switch units
    through the REAL decon worker while the gappy one is explicitly marked
    as left in counts (host-side skip → tab-side honesty)."""
    cfg, cfg_path = _cfg_with_response(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        n = int(_ANMO_FS * 60)
        x = _ANMO_T0 + np.arange(n, dtype=np.float64) / _ANMO_FS
        rng = np.random.default_rng(0)
        y_clean = rng.standard_normal(n).astype(np.float64) * 1000.0
        y_gappy = rng.standard_normal(n).astype(np.float64) * 1000.0
        y_gappy[n // 3 : n // 2] = np.nan
        z = ComponentTrace(
            comp="Z", nslc=_ANMO_NSLC, x=x, y=y_clean, fs=_ANMO_FS, start_epoch=_ANMO_T0
        )
        # N maps to the same responsive NSLC; only its GAPS make it skip.
        n_comp = ComponentTrace(
            comp="N", nslc=_ANMO_NSLC, x=x, y=y_gappy, fs=_ANMO_FS, start_epoch=_ANMO_T0
        )
        res = ArchiveWindowResult(
            token=1,
            traces=[z, n_comp],
            primary_comp="Z",
            spec_power=None,
            spec_freqs=None,
            spec_t_start=_ANMO_T0,
            spec_t_end=_ANMO_T0 + 60.0,
            elapsed_ms=1.0,
        )
        tab = window._archive_tab
        tab._loaded_device = "anmo"
        tab._loaded_group = {"Z": _ANMO_NSLC, "N": _ANMO_NSLC}
        tab._win_t_start = _ANMO_T0
        tab._win_t_end = _ANMO_T0 + 60.0
        window._archive_window_token = 1
        window._archive_window_traces = {"Z": z, "N": n_comp}
        tab.show_result(res)

        window._on_archive_window_unit_change("VEL")

        def z_physical() -> bool:
            return "Velocity (m/s)" in tab.top_unit_label_for_test("Z")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not z_physical():
            qtbot.wait(50)
        assert z_physical(), "clean component never rendered physical units"
        # The gappy sibling is marked, not silently mislabelled.
        assert "counts — gaps" in tab.top_unit_label_for_test("N")
        assert "N left in counts" in tab.status_text_for_test()
        # And its samples are untouched counts (NaN gap intact).
        _xn, yn = tab.trace_curve_for_test("N").getData()
        assert np.isnan(np.asarray(yn, dtype=np.float64)).any()
    finally:
        window.close()
