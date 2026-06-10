"""MainWindow wiring tests for M11 B physical-unit inspection.

Asserts observable behaviour (rule 10): the deconvolution worker lives
off the GUI thread AND off the engine's science DSP thread (rule 11); a
unit change drives the top trace to physical units end-to-end through the
REAL worker with the bundled IU.ANMO StationXML; stale results are
dropped; and a worker failure reverts the pane to counts.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import numpy as np
from obspy import UTCDateTime
from obspy.core.util import get_example_file
from pytestqt.qtbot import QtBot

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    ResponseMetadataConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import Detection
from echosmonitor.gui.main_window import MainWindow

_NSLC = "IU.ANMO.00.BHZ"
_FS = 20.0
# Inside the bundled IU.ANMO response epoch so a real response matches.
_LATEST = UTCDateTime("2014-01-01T00:01:00")


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


def _cfg_without_response(tmp_path: Path) -> tuple[RootConfig, Path]:
    dev = DeviceConfig(
        name="bare",
        host="127.0.0.1",
        port=18000,
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
    )
    cfg = RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[dev],
    )
    return cfg, tmp_path / "config.yaml"


def _det(device: str, nslc: str) -> Detection:
    return Detection(
        device=device,
        nslc=nslc,
        kind="sta_lta",
        t_on=UTCDateTime("2014-01-01T00:00:30"),
        t_off=UTCDateTime("2014-01-01T00:00:33"),
        score=8.0,
        detected_at=UTCDateTime("2014-01-01T00:00:30"),
        meta={"sta_s": 1.0, "lta_s": 10.0, "on_thr": 3.5, "off_thr": 1.5},
    )


def _render_counts(window: MainWindow) -> Detection:
    """Render a counts window in the detail pane WITHOUT touching the
    engine (which has no live data here): feed the pane + ctx directly,
    mirroring what ``_on_detection_selected`` does after a read."""
    det = _det("anmo", _NSLC)
    samples = np.random.default_rng(0).standard_normal(int(_FS * 60)).astype(np.float32)
    window._detail_pane.show_detection(det, samples, _FS, _LATEST)
    ctx = window._detail_pane.rendered_counts_context()
    assert ctx is not None
    ctx_fs, start_epoch = ctx
    window._detail_ctx = {
        "device": det.device,
        "nslc": det.nslc,
        "fs": float(ctx_fs),
        "start_epoch": float(start_epoch),
        "samples": window._detail_pane.counts_samples(),
    }
    return det


def test_decon_worker_off_gui_and_off_dsp_thread(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg_with_response(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        worker_thread = window._decon_worker.thread()
        # Off the GUI thread.
        assert worker_thread is window._decon_thread
        assert worker_thread is not window.thread()
        # NOT the engine's science DSP thread (rule 11).
        assert worker_thread is not window._engine._dsp_thread
    finally:
        window.close()


def test_unit_change_renders_physical_trace_end_to_end(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg_with_response(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        _render_counts(window)
        _xt, y_counts = window._detail_pane._trace_curve_for_test().getData()
        y_counts = np.asarray(y_counts, dtype=np.float64).copy()

        # Drive the unit-change path (as the combo would).
        window._on_unit_change_requested("VEL")

        # The real worker runs on its own thread; wait for the result.
        def physical_shown() -> bool:
            return window._detail_pane.top_axis_label_for_test() == "Velocity (m/s)"

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not physical_shown():
            qtbot.wait(50)
        assert physical_shown(), "physical trace never rendered"

        _xt2, yt2 = window._detail_pane._trace_curve_for_test().getData()
        assert not np.allclose(np.asarray(yt2), y_counts)
        assert np.all(np.isfinite(np.asarray(yt2)))

        # Back to counts restores the original y-data + label.
        window._on_unit_change_requested("COUNTS")
        assert window._detail_pane.top_axis_label_for_test() == "counts"
        _xt3, yt3 = window._detail_pane._trace_curve_for_test().getData()
        assert np.allclose(np.asarray(yt3), y_counts)
    finally:
        window.close()


def test_stale_result_is_dropped(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg_with_response(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        _render_counts(window)
        _xt, y_counts = window._detail_pane._trace_curve_for_test().getData()
        y_counts = np.asarray(y_counts, dtype=np.float64).copy()

        # Bump the token so this result looks stale (superseded).
        window._decon_token = 99
        stale_token = 5
        physical = y_counts * 1e-9 + 1.0
        window._on_deconvolved(stale_token, "Velocity (m/s)", physical)

        # Top trace unchanged (stale dropped).
        _xt2, yt2 = window._detail_pane._trace_curve_for_test().getData()
        assert np.allclose(np.asarray(yt2), y_counts)
        assert window._detail_pane.top_axis_label_for_test() == "counts"
    finally:
        window.close()


def test_no_response_disables_physical_items(qtbot: QtBot, tmp_path: Path) -> None:
    from echosmonitor.gui.widgets.detection_detail import NO_RESPONSE_TOOLTIP

    cfg, cfg_path = _cfg_without_response(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        det = _det("bare", _NSLC)
        samples = np.random.default_rng(0).standard_normal(int(_FS * 60)).astype(np.float32)
        window._detail_pane.show_detection(det, samples, _FS, _LATEST)
        available = window._response_provider.available_for(
            det.device, det.nslc, UTCDateTime("2014-01-01T00:00:00")
        )
        assert available is False
        window._detail_pane.set_response_available(available, NO_RESPONSE_TOOLTIP)

        pane = window._detail_pane
        assert pane.unit_item_enabled_for_test(0) is True
        assert pane.unit_item_enabled_for_test(1) is False
        assert pane.unit_item_enabled_for_test(2) is False
        assert pane.unit_item_enabled_for_test(3) is False
        assert pane.unit_combo_tooltip_for_test() == NO_RESPONSE_TOOLTIP
    finally:
        window.close()


def test_worker_failure_reverts_to_counts(qtbot: QtBot, tmp_path: Path) -> None:
    cfg, cfg_path = _cfg_with_response(tmp_path)
    window = MainWindow(cfg, cfg_path)
    qtbot.addWidget(window)
    try:
        _render_counts(window)
        _xt, y_counts = window._detail_pane._trace_curve_for_test().getData()
        y_counts = np.asarray(y_counts, dtype=np.float64).copy()
        # Pretend a physical trace is showing, then a failure arrives for the
        # current token.
        window._detail_pane.show_physical_trace("Velocity (m/s)", y_counts + 5.0)
        window._on_deconvolution_failed(window._decon_token, "gappy window")

        assert window._detail_pane.top_axis_label_for_test() == "counts"
        _xt2, yt2 = window._detail_pane._trace_curve_for_test().getData()
        assert np.allclose(np.asarray(yt2), y_counts)
    finally:
        window.close()
