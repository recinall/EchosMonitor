"""Tests for :class:`HvsrArrayWidget` (M5-B) and the MainWindow wiring.

The widget never computes anything — it drives a fake array engine and
consumes frozen ``ArrayHvsrResult`` payloads, exactly like the
single-station widget's tests. Pinned: start passes the checked device →
group mapping plus the provider's geometry snapshot; a duplicate-device
selection is refused loudly; results render one mean curve per device
and an honest per-device table row (f0, SESAME, response verdict,
error); the no-position note comes from the geometry diff (rule 16);
stale results from a stopped run are ignored.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Qt, Signal

from echosmonitor.core.hvsr import HvsrResult, HvsrSettings, SesameCriterion
from echosmonitor.core.hvsr_array import ArrayHvsrResult
from echosmonitor.core.models import device_stream_key
from echosmonitor.core.positions import ResolvedPosition, StationGeometry, station_geometry
from echosmonitor.gui.widgets.hvsr_array_widget import HvsrArrayWidget

_POS_A = ResolvedPosition("alpha", 45.0, 11.0, 100.0, "stationxml", 0.0)


class _FakeEngine(QObject):
    newStreamSeen = Signal(str, str)  # noqa: N815
    devicesChanged = Signal()  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self._buffers: dict[str, object] = {}

    def add_stream(self, device: str, nslc: str) -> None:
        self._buffers[device_stream_key(device, nslc)] = object()
        self.devicesChanged.emit()
        self.newStreamSeen.emit(device, nslc)


class _FakeArrayEngine(QObject):
    arrayMeasurementStarted = Signal(str, object)  # noqa: N815
    arrayMeasurementStopped = Signal(str)  # noqa: N815
    arrayUpdated = Signal(object)  # noqa: N815
    arrayWindowCounts = Signal(str, object)  # noqa: N815
    arrayStateChanged = Signal(str, str)  # noqa: N815
    arrayBackpressure = Signal(str, int)  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self.start_calls: list[tuple[dict[str, dict[str, str]], HvsrSettings, object]] = []
        self.stop_calls: list[str | None] = []

    def start_measurement(
        self,
        devices: dict[str, dict[str, str]],
        settings: HvsrSettings,
        geometry: object,
    ) -> str:
        self.start_calls.append((dict(devices), settings, geometry))
        return "hvsr-array-1"

    def stop_measurement(self, measurement_id: str | None = None) -> None:
        self.stop_calls.append(measurement_id)
        self.arrayMeasurementStopped.emit(measurement_id or "hvsr-array-1")

    def active_measurement(self) -> object | None:
        return None  # the widget falls back to zero counts


def _group(sta: str) -> dict[str, str]:
    return {c: f"XX.{sta}.00.HH{c}" for c in ("Z", "N", "E")}


def _add_3c(engine: _FakeEngine, device: str, sta: str) -> None:
    for orient in ("Z", "N", "E"):
        engine.add_stream(device, f"XX.{sta}.00.HH{orient}")


def _make(qtbot, positions: dict[str, ResolvedPosition] | None = None):
    engine = _FakeEngine()
    array = _FakeArrayEngine()
    pos = positions if positions is not None else {"alpha": _POS_A}
    provider_calls: list[tuple[str, ...]] = []

    def provider(devices) -> StationGeometry:
        names = tuple(devices)
        provider_calls.append(names)
        return station_geometry(pos, names)

    widget = HvsrArrayWidget(engine, array, provider)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    return widget, engine, array, provider_calls


def _check_all(widget: HvsrArrayWidget) -> None:
    for i in range(widget._device_list.count()):
        item = widget._device_list.item(i)
        assert item is not None
        item.setCheckState(Qt.CheckState.Checked)


def _station_result(device: str, *, f0: float = 2.0, n: int = 4) -> HvsrResult:
    freq = np.geomspace(0.5, 20.0, 16)
    rng = np.random.default_rng(0)
    curves = np.abs(rng.standard_normal((n, 16))) + 1.0
    mask = np.ones(n, dtype=bool)
    crit3 = tuple(SesameCriterion(f"r{i}", True, f"d{i}") for i in range(3))
    crit6 = tuple(SesameCriterion(f"c{i}", i < 5, f"d{i}") for i in range(6))
    psd = (freq, np.linspace(-180.0, -120.0, 16))
    return HvsrResult(
        frequency=freq,
        window_curves=curves,
        mean_curve=np.exp(np.mean(np.log(curves), axis=0)),
        median_curve=np.median(curves, axis=0),
        lognormal_sigma=np.std(np.log(curves), axis=0),
        f0_hz=f0,
        f0_sigma=0.1,
        a0=3.0,
        window_ids=tuple(range(n)),
        auto_accept_mask=mask,
        manual_override_mask=np.zeros(n, dtype=bool),
        effective_mask=mask,
        reliability=crit3,
        clarity=crit6,
        reliability_passed=True,
        clarity_passed=False,
        psd_z=psd,
        psd_n=psd,
        psd_e=psd,
        same_response=True,
        same_response_detail="Same-response assumed (single 3C station).",
        provenance="live",
        settings=HvsrSettings(window_length_s=60.0),
        n_windows_total=n,
        n_windows_valid=n,
        device=device,
        station_key=f"XX.{device}",
        t_start=UTCDateTime(0),
        t_end=UTCDateTime(240),
    )


def _array_result(
    measurement_id: str,
    devices: tuple[str, ...],
    results: dict[str, HvsrResult],
    errors: dict[str, str],
    geometry: StationGeometry,
) -> ArrayHvsrResult:
    return ArrayHvsrResult(
        measurement_id=measurement_id,
        devices=devices,
        results=results,
        errors=errors,
        geometry=geometry,
        settings=HvsrSettings(window_length_s=60.0),
        provenance="live",
        elapsed_ms=12.0,
    )


# ----------------------------------------------------------------------
def test_no_selection_disables_start(qtbot) -> None:
    widget, engine, _array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    qtbot.wait(20)
    assert not widget._start_button.isEnabled()
    _check_all(widget)
    qtbot.wait(20)
    assert widget._start_button.isEnabled()


def test_start_passes_groups_and_geometry(qtbot) -> None:
    widget, engine, array, provider_calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    _add_3c(engine, "beta", "STB")
    qtbot.wait(20)
    _check_all(widget)
    widget._on_start_clicked()
    assert widget.is_running()
    assert len(array.start_calls) == 1
    devices, settings, geometry = array.start_calls[0]
    assert set(devices) == {"alpha", "beta"}
    assert set(devices["alpha"]) == {"Z", "N", "E"}
    assert devices["beta"]["Z"].endswith("HHZ")
    assert isinstance(settings, HvsrSettings)
    # The geometry snapshot came from the injected provider, for exactly
    # the selected devices.
    assert provider_calls == [("alpha", "beta")]
    assert isinstance(geometry, StationGeometry)
    assert geometry.devices == ("alpha",)  # beta has no position
    # Rule 16: the unpositioned device is said, not guessed.
    assert "beta" in widget._position_label.text()
    assert not widget._device_list.isEnabled()  # controls locked while running
    # Stop re-enables.
    widget._on_start_clicked()
    assert array.stop_calls == ["hvsr-array-1"]
    qtbot.wait(20)
    assert not widget.is_running()
    assert widget._device_list.isEnabled()


def test_duplicate_device_selection_refused(qtbot) -> None:
    widget, engine, array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    _add_3c(engine, "alpha", "STB")  # second station, same device
    qtbot.wait(20)
    _check_all(widget)
    widget._on_start_clicked()
    assert array.start_calls == []
    assert not widget.is_running()
    assert "alpha" in widget.status_text()


def test_array_updated_renders_curves_and_table(qtbot) -> None:
    widget, engine, array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    _add_3c(engine, "beta", "STB")
    qtbot.wait(20)
    _check_all(widget)
    widget._on_start_clicked()
    geometry = station_geometry({"alpha": _POS_A}, ("alpha", "beta"))
    result = _array_result(
        "hvsr-array-1",
        ("alpha", "beta"),
        {"alpha": _station_result("alpha")},
        {"beta": "boom"},
        geometry,
    )
    array.arrayUpdated.emit(result)
    qtbot.wait(20)
    # One named mean curve (alpha); beta has no result.
    named = [i for i in widget._hv_plot.listDataItems() if i.name()]
    assert [i.name() for i in named] == ["alpha"]
    # Table: alpha row carries the numbers + verdicts, beta row the error.
    assert widget._table.rowCount() == 2
    assert widget._table.item(0, 0).text() == "alpha"
    assert "2.000" in widget._table.item(0, 1).text()  # f0 +/- sigma
    assert widget._table.item(0, 5).text().startswith("✓")  # reliability passed
    assert widget._table.item(0, 6).text().startswith("✗")  # clarity failed (5/6)
    assert widget._table.item(0, 7).text() == "assumed"
    assert widget._table.item(1, 0).text() == "beta"
    assert widget._table.item(1, 8).text() == "boom"
    # The A0 column carries the response-sensitivity annotation.
    assert "response-sensitive" in widget._table.item(0, 3).toolTip()
    # Rule 16 note from the result's geometry diff.
    assert "beta" in widget._position_label.text()


def test_show_windows_toggle_redraws_with_faint_curves(qtbot) -> None:
    widget, engine, array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    qtbot.wait(20)
    _check_all(widget)
    widget._on_start_clicked()
    geometry = station_geometry({"alpha": _POS_A}, ("alpha",))
    result = _array_result(
        "hvsr-array-1", ("alpha",), {"alpha": _station_result("alpha")}, {}, geometry
    )
    array.arrayUpdated.emit(result)
    qtbot.wait(20)
    before = len(widget._hv_plot.listDataItems())
    widget._show_windows_button.setChecked(True)
    qtbot.wait(20)
    after = len(widget._hv_plot.listDataItems())
    # ONE NaN-separated faint item per device (auditor F2: never one item
    # per window — a long run would stall the GUI thread).
    assert after == before + 1
    faint = next(
        i for i in widget._hv_plot.listDataItems() if not i.name() and i.xData is not None
    )
    # All 4 window curves ride that single item, NaN-separated.
    assert np.count_nonzero(np.isnan(np.asarray(faint.yData, dtype=float))) >= 4


def test_window_counts_update_status(qtbot) -> None:
    widget, engine, array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    qtbot.wait(20)
    _check_all(widget)
    widget._on_start_clicked()
    array.arrayWindowCounts.emit("hvsr-array-1", {"alpha": (2, 5)})
    qtbot.wait(20)
    assert "alpha 2/5" in widget.status_text()


def test_stale_result_from_stopped_run_ignored(qtbot) -> None:
    widget, engine, array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    qtbot.wait(20)
    _check_all(widget)
    widget._on_start_clicked()
    widget._on_start_clicked()  # stop
    qtbot.wait(20)
    geometry = station_geometry({"alpha": _POS_A}, ("alpha",))
    stale = _array_result(
        "hvsr-array-1", ("alpha",), {"alpha": _station_result("alpha")}, {}, geometry
    )
    array.arrayUpdated.emit(stale)
    qtbot.wait(20)
    assert widget._result is None
    # No curve drawn from the stale payload (the table keeps its honest
    # start-time "accumulating…" placeholder row).
    assert [i for i in widget._hv_plot.listDataItems() if i.name()] == []


def test_archive_button_invokes_handler_and_tracks_id(qtbot) -> None:
    """M5-D: the archive click hands (groups, range, settings, geometry) to
    the host handler, tracks the returned id (results render), and an
    archive run is never a LIVE measurement."""
    widget, engine, _array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    qtbot.wait(20)
    _check_all(widget)
    calls: list[tuple] = []

    def handler(groups, t_start, t_end, settings, geometry) -> str:
        calls.append((groups, t_start, t_end, settings, geometry))
        return "hvsr-array-arch"

    widget.set_archive_request_handler(handler)
    widget._on_archive_clicked()
    assert len(calls) == 1
    groups, t_start, t_end, settings, geometry = calls[0]
    assert set(groups) == {"alpha"} and set(groups["alpha"]) == {"Z", "N", "E"}
    assert isinstance(t_start, UTCDateTime) and t_end > t_start
    assert isinstance(settings, HvsrSettings)
    assert isinstance(geometry, StationGeometry)
    assert widget._measurement_id == "hvsr-array-arch"
    assert not widget.is_running()  # archive is not a LIVE measurement
    assert widget._device_list.isEnabled()  # controls stay usable
    assert "(archive" in widget._title.text()


def test_archive_click_during_live_run_resolves_cleanly(qtbot) -> None:
    """Stopping the live run inside the archive handler re-enters
    _on_stopped mid-click (same-thread direct emit); the widget must come
    out tracking the ARCHIVE id with live state fully cleared."""
    widget, engine, array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    qtbot.wait(20)
    _check_all(widget)
    widget._on_start_clicked()  # live run active
    assert widget.is_running()

    def handler(groups, t_start, t_end, settings, geometry) -> str:
        # The real handler's engine call stops the prior (live) run, which
        # fires arrayMeasurementStopped synchronously — reproduce that.
        array.stop_measurement("hvsr-array-1")
        return "hvsr-array-arch"

    widget.set_archive_request_handler(handler)
    widget._on_archive_clicked()
    assert widget._measurement_id == "hvsr-array-arch"
    assert not widget.is_running()
    assert widget._device_list.isEnabled()
    assert widget._start_button.text() == "Start array measurement"
    assert "(archive" in widget._title.text()


def test_archive_no_data_reports_clearly(qtbot) -> None:
    widget, engine, _array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    qtbot.wait(20)
    _check_all(widget)
    widget.set_archive_request_handler(lambda *_a: "")
    widget._on_archive_clicked()
    assert widget._measurement_id is None
    assert "no archived data" in widget.status_text().lower()


def test_export_buttons_gated_and_context_derives_period(qtbot) -> None:
    """M5-C glue: Save/Export enable only on a result with >=1 valid
    station, survive stop (the result stays valid), reset on a new start;
    the report context derives its period from the station spans."""
    widget, engine, array, _calls = _make(qtbot)
    _add_3c(engine, "alpha", "STA")
    qtbot.wait(20)
    _check_all(widget)
    widget._on_start_clicked()
    assert not widget._save_pdf_button.isEnabled()
    assert not widget._export_button.isEnabled()
    geometry = station_geometry({"alpha": _POS_A}, ("alpha",))
    result = _array_result(
        "hvsr-array-1", ("alpha",), {"alpha": _station_result("alpha")}, {}, geometry
    )
    array.arrayUpdated.emit(result)
    qtbot.wait(20)
    assert widget._save_pdf_button.isEnabled()
    assert widget._export_button.isEnabled()
    ctx = widget._array_report_context()
    # Period = min t_start to max t_end over valid stations (the builder
    # stamps 0..240); the groups are the start-time selection.
    assert ctx.period_label == f"{UTCDateTime(0)} to {UTCDateTime(240)}"
    assert set(ctx.group_by_device) == {"alpha"}
    assert ctx.group_by_device["alpha"]["Z"].endswith("HHZ")
    # Stop keeps the buttons enabled — the last result is still valid.
    widget._on_start_clicked()
    qtbot.wait(20)
    assert widget._save_pdf_button.isEnabled()
    # A new start clears the views and the gates.
    widget._on_start_clicked()
    assert not widget._save_pdf_button.isEnabled()
    assert not widget._export_button.isEnabled()


def test_main_window_array_archive_root_resolution(qtbot, tmp_path, monkeypatch) -> None:
    """M5-D root seam (rule 14): with a session selected in the Archive
    tab every device reads the SESSION root (one shared reader); with no
    selection each device falls back to its live engine root."""
    from types import SimpleNamespace

    from echosmonitor.config.schema import (
        AppConfig,
        DeviceConfig,
        RootConfig,
        StreamSelectorConfig,
        UiConfig,
    )
    from echosmonitor.core.hvsr import HvsrSettings
    from echosmonitor.gui.main_window import MainWindow

    cfg = RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name="alpha",
                host="127.0.0.1",
                port=18000,
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
            )
        ],
    )
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    try:
        recorded: dict[str, dict[str, object]] = {}

        def fake_start(groups, t0, t1, settings, geometry, readers) -> str:
            recorded["readers"] = dict(readers)
            return "hvsr-array-arch"

        monkeypatch.setattr(
            window._hvsr_array_engine, "start_archive_measurement", fake_start
        )
        groups = {"alpha": _group("STA")}
        geometry = station_geometry({}, ("alpha",))
        # No session selected → the device's live engine root.
        monkeypatch.setattr(window._archive_tab, "selected_session_entry", lambda: None)
        mid = window._run_hvsr_array_archive(
            groups, UTCDateTime(0), UTCDateTime(10), HvsrSettings(), geometry
        )
        assert mid == "hvsr-array-arch"
        reader = recorded["readers"]["alpha"]
        assert reader._root == window._engine.archive_root("alpha")
        # Session selected → its root, shared across devices. The stub
        # carries every attribute the Archive tab itself touches when the
        # session-discovery worker later calls selected_session_entry().
        session_root = tmp_path / "myproject"
        entry = SimpleNamespace(
            session_root=str(session_root),
            db_path=str(session_root / "archive.db"),
            record=SimpleNamespace(id=1),
        )
        monkeypatch.setattr(window._archive_tab, "selected_session_entry", lambda: entry)
        window._run_hvsr_array_archive(
            groups, UTCDateTime(0), UTCDateTime(10), HvsrSettings(), geometry
        )
        assert recorded["readers"]["alpha"]._root == session_root
    finally:
        window.close()


def test_main_window_array_tab_and_f0_route(qtbot) -> None:
    """MainWindow wiring: the tab exists; array f0 routes to the map
    overlay; a new array run clears the previous overlay."""
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
                name="alpha",
                host="127.0.0.1",
                port=18000,
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
            )
        ],
    )
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    try:
        tabs = [window._central_tabs.tabText(i) for i in range(window._central_tabs.count())]
        assert "HVSR Array" in tabs
        window._map_widget.set_devices(("alpha",))
        window._map_widget.on_position(
            ResolvedPosition("alpha", 45.0, 11.0, 100.0, "stationxml", time.monotonic())
        )
        geometry = station_geometry({"alpha": _POS_A}, ("alpha",))
        result = _array_result(
            "hvsr-array-1",
            ("alpha",),
            {"alpha": _station_result("alpha", f0=2.5)},
            {},
            geometry,
        )
        # Through the REAL engine signal, so the connect wiring is exercised.
        window._hvsr_array_engine.arrayUpdated.emit(result)
        qtbot.wait(20)
        assert window._map_widget._f0_overlay == {"alpha": 2.5}
        # A new array run clears the stale overlay.
        window._hvsr_array_engine.arrayMeasurementStarted.emit("hvsr-array-2", None)
        qtbot.wait(20)
        assert window._map_widget._f0_overlay == {}
    finally:
        window.close()
