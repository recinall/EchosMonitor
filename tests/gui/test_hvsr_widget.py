"""Tests for :class:`HvsrWidget` (Stage B).

The widget never computes HVSR itself — it drives an ``HvsrEngine`` via
``start_measurement`` / ``set_window_override`` and consumes the frozen
``HvsrResult`` the engine emits. These tests substitute a tiny
``_FakeEngine`` (stream enumeration) and ``_FakeHvsrEngine`` (the HVSR
signal/command surface the widget touches), and assert observable behavior
(rule 10): the Start button enables only for a 3-component station, the
status/SESAME line reflects the result, the pre/post toggle actually
changes the plotted mean curve, an override toggle reaches the engine, the
early raw PSD renders before the first full result, and the advanced params
flow into the settings used.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal

from echosmonitor.core.hvsr import HvsrResult, HvsrSettings, SesameCriterion
from echosmonitor.core.models import device_stream_key
from echosmonitor.gui.widgets.hvsr_widget import HvsrWidget


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
        self.override_calls: list[tuple[str, int, bool]] = []
        self.stop_calls: list[str | None] = []

    def start_measurement(self, device: str, group: dict[str, str], settings: HvsrSettings) -> str:
        self.start_calls.append((device, group, settings))
        return "hvsr-1"

    def stop_measurement(self, measurement_id: str | None = None) -> None:
        self.stop_calls.append(measurement_id)
        self.hvsrMeasurementStopped.emit(measurement_id or "hvsr-1")

    def set_window_override(self, measurement_id: str, window_id: int, accepted: bool) -> None:
        self.override_calls.append((measurement_id, window_id, accepted))


def _make_widget(qtbot) -> tuple[HvsrWidget, _FakeEngine, _FakeHvsrEngine]:
    engine = _FakeEngine()
    hv = _FakeHvsrEngine()
    widget = HvsrWidget(engine, hv)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    return widget, engine, hv


def _add_3c(engine: _FakeEngine, device: str = "dev", sta: str = "XX.STA.00.HH") -> None:
    for orient in ("Z", "N", "E"):
        engine.add_stream(device, f"{sta}{orient}")


def _result(*, reject_row: int | None = None, n: int = 4, f_bins: int = 16) -> HvsrResult:
    from obspy.core.utcdatetime import UTCDateTime

    freq = np.geomspace(0.5, 20.0, f_bins)
    rng = np.random.default_rng(0)
    curves = np.abs(rng.standard_normal((n, f_bins))) + 1.0
    if reject_row is not None:
        curves[reject_row] *= 10.0  # a clear outlier so pre != post
    auto = np.ones(n, dtype=bool)
    if reject_row is not None:
        auto[reject_row] = False
    effective = auto.copy()
    log_eff = np.log(curves[effective])
    mean = np.exp(np.mean(log_eff, axis=0))
    sigma = np.std(log_eff, axis=0)
    crit3 = tuple(SesameCriterion(f"r{i}", i < 2, f"d{i}") for i in range(3))
    crit6 = tuple(SesameCriterion(f"c{i}", True, f"d{i}") for i in range(6))
    psd = (freq, np.linspace(-180.0, -120.0, f_bins))
    return HvsrResult(
        frequency=freq,
        window_curves=curves,
        mean_curve=mean,
        median_curve=np.median(curves[effective], axis=0),
        lognormal_sigma=sigma,
        f0_hz=float(freq[int(np.argmax(mean))]),
        f0_sigma=0.1,
        a0=float(np.max(mean)),
        window_ids=tuple(range(n)),
        auto_accept_mask=auto,
        manual_override_mask=np.zeros(n, dtype=bool),
        effective_mask=effective,
        reliability=crit3,
        clarity=crit6,
        reliability_passed=False,
        clarity_passed=True,
        psd_z=psd,
        psd_n=psd,
        psd_e=psd,
        same_response=True,
        same_response_detail="Same-response assumed (single 3C station).",
        provenance="live",
        settings=HvsrSettings(window_length_s=60.0),
        n_windows_total=n,
        n_windows_valid=int(np.count_nonzero(effective)),
        device="dev",
        station_key="XX.STA",
        t_start=UTCDateTime(0),
        t_end=UTCDateTime(240),
    )


def _named_mean(widget: HvsrWidget) -> tuple[str, np.ndarray] | None:
    for item in widget._hv_plot.listDataItems():
        name = item.name() or ""
        if name.startswith("mean"):
            return name, np.asarray(item.yData)
    return None


# ----------------------------------------------------------------------
def test_no_3c_device_disables_start(qtbot) -> None:
    widget, engine, _hv = _make_widget(qtbot)
    engine.add_stream("dev", "XX.STA.00.HHZ")  # only vertical → not 3C
    qtbot.wait(20)
    assert not widget.start_enabled()
    assert widget.selected_group() is None


def test_3c_streams_enable_start_and_resolve_group(qtbot) -> None:
    widget, engine, _hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    assert widget.start_enabled()
    group = widget.selected_group()
    assert group is not None
    assert group["Z"].endswith("HHZ")
    assert {group["N"][-1], group["E"][-1]} == {"N", "E"}


def test_start_calls_engine_and_disables_controls(qtbot) -> None:
    widget, engine, hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    widget._on_start_clicked()
    assert widget.is_running()
    assert len(hv.start_calls) == 1
    device, group, settings = hv.start_calls[0]
    assert device == "dev"
    assert set(group) == {"Z", "N", "E"}
    assert isinstance(settings, HvsrSettings)
    assert not widget._device_combo.isEnabled()  # controls locked while running
    # Stopping re-enables.
    widget._on_start_clicked()
    assert hv.stop_calls == ["hvsr-1"]
    qtbot.wait(20)
    assert not widget.is_running()
    assert widget._device_combo.isEnabled()


def test_hvsr_updated_renders_status_sesame_and_window_list(qtbot) -> None:
    widget, engine, hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    res = _result(reject_row=1)
    hv.hvsrUpdated.emit(res)
    qtbot.wait(20)
    # Status reflects the real counts + f0.
    assert "3/4" in widget.status_text()  # one rejected
    assert "f" in widget.status_text()
    # SESAME verdict: reliability failed (2/3), clarity passed (6/6).
    assert "2/3" in widget.sesame_text()
    assert "6/6" in widget.sesame_text()
    # Window list: 4 rows, row 1 unchecked (rejected).
    assert widget._window_list.count() == 4
    assert widget._window_list.item(1).checkState() == Qt.CheckState.Unchecked
    assert widget._window_list.item(0).checkState() == Qt.CheckState.Checked
    # The same-response honesty note is surfaced verbatim.
    assert "assumed" in widget._same_response_label.text().lower()


def test_pre_post_toggle_changes_plotted_mean_curve(qtbot) -> None:
    widget, engine, hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    hv.hvsrUpdated.emit(_result(reject_row=1))
    qtbot.wait(20)
    post = _named_mean(widget)
    assert post is not None and "post" in post[0]
    widget._on_prepost_toggled()
    qtbot.wait(20)
    pre = _named_mean(widget)
    assert pre is not None and "pre" in pre[0]
    # The rejected outlier is in the pre mean but not the post mean.
    assert not np.allclose(pre[1], post[1])


def test_override_toggle_reaches_engine(qtbot) -> None:
    widget, engine, hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    widget._on_start_clicked()
    hv.hvsrUpdated.emit(_result(reject_row=1))
    qtbot.wait(20)
    # User unticks an accepted window (row 0 → exclude).
    widget._window_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    qtbot.wait(20)
    assert ("hvsr-1", 0, False) in hv.override_calls


def test_early_psd_renders_before_first_result(qtbot) -> None:
    """FIX 3: the PSD panel populates from an early raw PSD, before any result."""
    widget, engine, hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    widget._on_start_clicked()
    f = np.geomspace(0.5, 20.0, 32)
    db = np.linspace(-180.0, -120.0, 32)
    hv.hvsrPsdReady.emit({"Z": (f, db), "N": (f, db), "E": (f, db)})
    qtbot.wait(20)
    assert len(widget._psd_plot.listDataItems()) == 3  # Z/N/E drawn, no result yet
    assert widget._result is None


def test_full_result_supersedes_early_psd(qtbot) -> None:
    """Once a full result lands, its PSD owns the panel and early updates stop."""
    widget, engine, hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    widget._on_start_clicked()
    hv.hvsrUpdated.emit(_result())
    qtbot.wait(20)
    f = np.geomspace(0.5, 20.0, 32)
    db = np.linspace(-180.0, -120.0, 32)
    before = len(widget._psd_plot.listDataItems())
    hv.hvsrPsdReady.emit({"Z": (f, db), "N": (f, db), "E": (f, db)})  # ignored now
    qtbot.wait(20)
    assert len(widget._psd_plot.listDataItems()) == before


def test_archive_button_invokes_handler_and_tracks_id(qtbot) -> None:
    from obspy.core.utcdatetime import UTCDateTime

    widget, engine, _hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    calls: list[tuple] = []

    def handler(device, group, t_start, t_end, settings) -> str:
        calls.append((device, group, t_start, t_end, settings))
        return "hvsr-arch-1"

    widget.set_archive_request_handler(handler)
    widget._on_archive_clicked()
    assert len(calls) == 1
    device, group, t_start, t_end, settings = calls[0]
    assert device == "dev"
    assert set(group) == {"Z", "N", "E"}
    assert isinstance(t_start, UTCDateTime) and t_end > t_start
    assert isinstance(settings, HvsrSettings)
    # The widget tracks the returned id so override works on the archive run.
    assert widget._measurement_id == "hvsr-arch-1"
    assert not widget.is_running()  # archive is not a LIVE measurement


def test_advanced_params_feed_current_settings(qtbot) -> None:
    """FEATURE 6: the advanced controls flow into the HvsrSettings used."""
    widget, engine, _hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    idx = widget._horizontal_combo.findData("squared_average")
    widget._horizontal_combo.setCurrentIndex(idx)
    widget._detrend_combo.setCurrentIndex(widget._detrend_combo.findData("constant"))
    widget._rejection_n_spin.setValue(3.0)
    widget._resample_spin.setValue(256)
    widget._psd_smooth_check.setChecked(False)
    widget._psd_b_spin.setValue(20.0)
    s = widget.current_settings()
    assert s.horizontal_method == "squared_average"
    assert s.detrend == "constant"
    assert s.rejection_n == 3.0
    assert s.resample_n == 256
    assert s.psd_smoothing is False
    assert s.psd_konno_ohmachi_b == 20.0


def test_archive_no_data_reports_clearly(qtbot) -> None:
    widget, engine, _hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    widget.set_archive_request_handler(lambda *_a: "")  # no window in range
    widget._on_archive_clicked()
    assert widget._measurement_id is None
    assert "no archived data" in widget.status_text().lower()


def test_export_buttons_gated_on_result(qtbot) -> None:
    widget, engine, hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    # No result yet → export disabled.
    assert not widget._save_pdf_button.isEnabled()
    assert not widget._export_button.isEnabled()
    widget._on_start_clicked()
    hv.hvsrUpdated.emit(_result())
    qtbot.wait(20)
    # A valid result → export enabled.
    assert widget._save_pdf_button.isEnabled()
    assert widget._export_button.isEnabled()


def test_window_list_disabled_after_stop(qtbot) -> None:
    widget, engine, hv = _make_widget(qtbot)
    _add_3c(engine)
    qtbot.wait(20)
    widget._on_start_clicked()
    hv.hvsrUpdated.emit(_result())
    qtbot.wait(20)
    assert widget._window_list.isEnabled()
    widget._on_start_clicked()  # stop
    qtbot.wait(20)
    # Overrides are inert once stopped — the list is disabled for honesty.
    assert not widget._window_list.isEnabled()
