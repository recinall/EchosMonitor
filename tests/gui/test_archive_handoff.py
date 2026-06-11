"""Archive tab — HVSR hand-off (Stage C).

The Archive tab is a LAUNCHER: it transports the exact selected
``(device, group, t_start, t_end)`` into the existing HVSR archive flow
and does not reimplement the analysis. These tests prove the interval
round-trips exactly through every hop:

* the tab emits the exact selection on the hand-off button,
* ``HvsrWidget.prefill_archive`` fills the archive fields to the exact interval
  WITHOUT auto-running,
* the main-window router switches tabs and calls the existing entry point with
  the exact selection.
"""

from __future__ import annotations

import numpy as np
import pytest
from obspy import UTCDateTime
from PySide6.QtCore import QObject, Signal

from echosmonitor.core.archive_browser_loader import ArchiveBrowserLoader
from echosmonitor.core.archive_window_loader import (
    ArchiveWindowResult,
    ComponentTrace,
)
from echosmonitor.core.hvsr import HvsrSettings
from echosmonitor.core.models import device_stream_key
from echosmonitor.gui.widgets.archive_tab import ArchiveTab
from echosmonitor.gui.widgets.hvsr_widget import HvsrWidget

_FS = 100.0
_T0 = float(UTCDateTime("2026-05-10T12:00:00").timestamp)
_T1 = _T0 + 600.0
_GROUP = {"Z": "IU.ANMO.00.HHZ", "N": "IU.ANMO.00.HHN", "E": "IU.ANMO.00.HHE"}


class _FakeEngine(QObject):
    newStreamSeen = Signal(str, str)  # noqa: N815
    devicesChanged = Signal()  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self._buffers: dict[str, object] = {}

    def add_3c(self, device: str) -> None:
        for nslc in _GROUP.values():
            self._buffers[device_stream_key(device, nslc)] = object()


@pytest.fixture
def browser():
    """A real (idle) browser loader; the tab's ctor needs one (M3-A)."""
    loader = ArchiveBrowserLoader()
    yield loader
    loader.shutdown()


class _FakeHvsrEngine(QObject):
    hvsrUpdated = Signal(object)  # noqa: N815
    hvsrPsdReady = Signal(object)  # noqa: N815
    hvsrWindowCount = Signal(int, int)  # noqa: N815
    hvsrStateChanged = Signal(str, str)  # noqa: N815
    hvsrBackpressure = Signal(str, int)  # noqa: N815
    hvsrMeasurementStopped = Signal(str)  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self.start_calls: list[tuple[str, dict[str, str], HvsrSettings]] = []

    def start_measurement(self, device: str, group: dict[str, str], settings: HvsrSettings) -> str:
        self.start_calls.append((device, group, settings))
        return "hvsr-1"

    def start_archive_measurement(self, *args: object, **kwargs: object) -> str:
        self.start_calls.append(("archive", args, kwargs))  # type: ignore[arg-type]
        return "hvsr-arch"


# --------------------------------------------------------------------------
# 1. The tab emits the exact selection on the hand-off button.
# --------------------------------------------------------------------------
def test_handoff_button_emits_exact_selection(qtbot, tmp_path, browser) -> None:
    tab = ArchiveTab(browser, tmp_path)
    qtbot.addWidget(tab)
    n = int(_FS * 30)
    x = _T0 + np.arange(n, dtype=np.float64) / _FS
    traces = [
        ComponentTrace(comp=c, nslc=_GROUP[c], x=x, y=np.zeros(n), fs=_FS, start_epoch=_T0)
        for c in ("Z", "N", "E")
    ]
    tab._loaded_device = "dev"
    tab._loaded_group = dict(_GROUP)
    tab._win_t_start = _T0
    tab._win_t_end = _T1
    tab.show_result(
        ArchiveWindowResult(
            token=1,
            traces=traces,
            primary_comp="Z",
            spec_power=None,
            spec_freqs=None,
            spec_t_start=_T0,
            spec_t_end=_T0 + 30.0,
            elapsed_ms=1.0,
        )
    )
    hvsr: list[tuple] = []
    tab.hvsrRequested.connect(lambda *a: hvsr.append(a))

    tab._hvsr_button.click()

    assert hvsr == [("dev", _GROUP, _T0, _T1)]


# --------------------------------------------------------------------------
# 2. HvsrWidget.prefill_archive sets the fields to the exact interval, no run.
# --------------------------------------------------------------------------
def test_hvsr_prefill_sets_fields_without_running(qtbot) -> None:
    engine = _FakeEngine()
    engine.add_3c("dev")
    hv = _FakeHvsrEngine()
    widget = HvsrWidget(engine, hv)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    # New streams have appeared — refresh so the combos populate.
    engine.devicesChanged.emit()

    widget.prefill_archive("dev", _GROUP, _T0, _T1)

    assert widget._device_combo.currentData() == "dev"
    # The correct 3C station is selected (N/E labelling follows the widget's
    # own three_component_groups sort; the Archive tab uses the same function,
    # so the real hand-off groups are identical).
    sel = widget.selected_group()
    assert sel is not None
    assert sel["Z"] == _GROUP["Z"]
    assert set(sel.values()) == set(_GROUP.values())
    # The archive fields read back (same path _on_archive_clicked uses) to the
    # exact interval — UTC wall-clock round-trip.
    rs = UTCDateTime(widget._archive_start.dateTime().toString("yyyy-MM-ddTHH:mm:ss"))
    re = UTCDateTime(widget._archive_end.dateTime().toString("yyyy-MM-ddTHH:mm:ss"))
    assert abs(float(rs.timestamp) - _T0) < 1e-6
    assert abs(float(re.timestamp) - _T1) < 1e-6
    # Prefill must NOT auto-run a measurement.
    assert not widget.is_running()
    assert hv.start_calls == []


# --------------------------------------------------------------------------
# 3. The main-window router switches tabs + calls the existing entry point
#    with the exact selection.
# --------------------------------------------------------------------------
def test_main_window_router_prefills_existing_flow(qtbot, tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from echosmonitor.config.schema import (
        AppConfig,
        DeviceConfig,
        RootConfig,
        StreamSelectorConfig,
        UiConfig,
    )
    from echosmonitor.gui.main_window import MainWindow

    cfg = RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name="dev",
                host="127.0.0.1",
                port=18000,
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
            )
        ],
    )
    window = MainWindow(cfg, Path(tmp_path) / "config.yaml")
    qtbot.addWidget(window)
    try:
        hvsr_calls: list[tuple] = []
        monkeypatch.setattr(window._hvsr_widget, "prefill_archive", lambda *a: hvsr_calls.append(a))

        window._handoff_archive_to_hvsr("dev", dict(_GROUP), _T0, _T1)
        assert hvsr_calls == [("dev", _GROUP, _T0, _T1)]
        assert window._central_tabs.currentWidget() is window._hvsr_widget
    finally:
        window.close()
