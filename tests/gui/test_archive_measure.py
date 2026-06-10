"""Archive tab — measurement tools (Stage B).

Two draggable cursors placed at known sample positions yield the correct UTC
time + amplitude readouts, and the between-cursor Δt, Δamp, and frequency =
1/Δt are computed correctly. Offscreen Qt cannot deliver a real drag, so the
cursor is driven to a known epoch and the OBSERVABLE readout numbers are
asserted (rule 10).
"""

from __future__ import annotations

import numpy as np
from obspy import UTCDateTime
from PySide6.QtCore import QObject, Signal

from echosmonitor.core.archive_window_loader import (
    ArchiveWindowResult,
    ComponentTrace,
)
from echosmonitor.gui.widgets.archive_tab import ArchiveTab

_FS = 100.0
_T0 = float(UTCDateTime("2026-05-10T12:00:00").timestamp)
_DUR = 30.0


class _FakeEngine(QObject):
    newStreamSeen = Signal(str, str)  # noqa: N815
    devicesChanged = Signal()  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self._buffers: dict[str, object] = {}


def _make_tab_with_ramp(qtbot) -> tuple[ArchiveTab, np.ndarray, np.ndarray]:
    """Z is a known ramp y = index so amplitude readouts are predictable."""
    n = int(_FS * _DUR)
    x = _T0 + np.arange(n, dtype=np.float64) / _FS
    y = np.arange(n, dtype=np.float64)  # amplitude == sample index
    traces = [
        ComponentTrace(comp="Z", nslc="XX.STA.00.HHZ", x=x, y=y, fs=_FS, start_epoch=_T0),
        ComponentTrace(comp="N", nslc="XX.STA.00.HHN", x=x, y=np.zeros(n), fs=_FS, start_epoch=_T0),
        ComponentTrace(comp="E", nslc="XX.STA.00.HHE", x=x, y=np.zeros(n), fs=_FS, start_epoch=_T0),
    ]
    res = ArchiveWindowResult(
        token=1,
        traces=traces,
        primary_comp="Z",
        spec_power=None,
        spec_freqs=None,
        spec_t_start=_T0,
        spec_t_end=_T0 + _DUR,
        elapsed_ms=1.0,
    )
    tab = ArchiveTab(_FakeEngine(), None)  # type: ignore[arg-type]
    qtbot.addWidget(tab)
    tab._loaded_device = "dev"
    tab._loaded_group = {"Z": "XX.STA.00.HHZ", "N": "XX.STA.00.HHN", "E": "XX.STA.00.HHE"}
    tab._win_t_start = _T0
    tab._win_t_end = _T0 + _DUR
    tab.show_result(res)
    return tab, x, y


def test_cursor_readout_time_and_amplitude(qtbot) -> None:
    tab, _x, _y = _make_tab_with_ramp(qtbot)
    # Cursor A at +5 s (sample 500 → amp 500), B at +7 s (sample 700 → amp 700).
    tab.set_cursor_epoch_for_test("A", _T0 + 5.0)
    tab.set_cursor_epoch_for_test("B", _T0 + 7.0)
    pos = tab.cursor_pos_for_test()
    assert pos["A"] == _T0 + 5.0
    assert pos["B"] == _T0 + 7.0

    text = tab.readout_text_for_test()
    # Amplitudes at the two cursors (ramp: amp == sample index).
    assert "500" in text
    assert "700" in text
    # Δt = 2 s, frequency = 1/Δt = 0.5 Hz.
    assert "Δt=2" in text
    assert "0.5 Hz" in text
    # Δamp = |700 - 500| = 200.
    assert "200" in text


def test_reset_view_refits_window(qtbot) -> None:
    tab, _x, _y = _make_tab_with_ramp(qtbot)
    # Zoom in to a sub-range, then reset.
    tab._stacked_plots["Z"].setXRange(_T0 + 10.0, _T0 + 12.0, padding=0.0)
    tab._reset_view()
    lo, hi = tab.trace_x_range_for_test()
    assert abs(lo - _T0) < 1.0
    assert abs(hi - (_T0 + _DUR)) < 1.0


def test_readout_reports_gap_when_cursor_on_missing_sample(qtbot) -> None:
    tab, _x, _y = _make_tab_with_ramp(qtbot)
    # Put a gap in Z and re-render, then place a cursor inside it.
    n = int(_FS * _DUR)
    x = _T0 + np.arange(n, dtype=np.float64) / _FS
    y = np.arange(n, dtype=np.float64)
    y[500:800] = np.nan
    tab._display["Z"] = (x, y)
    tab._stacked_curves["Z"].setData(x, y, connect="finite")
    tab.set_cursor_epoch_for_test("A", _T0 + 6.0)  # sample 600 → NaN
    tab._refresh_readout()
    assert "gap" in tab.readout_text_for_test()
