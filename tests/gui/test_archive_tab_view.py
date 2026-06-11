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
