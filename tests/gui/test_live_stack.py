"""Tests for `LiveStack` — single vs stacked TracePlot selection.

Multi-device coverage lives alongside in ``test_live_stack_multi.py``;
this module focuses on the single-device baseline and the
chain-vs-no-chain plot-mode selection.
"""

from __future__ import annotations

import numpy as np

from echosmonitor.config.schema import (
    AppConfig,
    BandpassStage,
    DetrendStage,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StaLtaStage,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.gui.widgets.live_stack import LiveStack


def _root_cfg(devices: list[DeviceConfig]) -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=devices,
    )


def _device(name: str, with_chain: bool) -> DeviceConfig:
    chain: list = []
    if with_chain:
        chain = [
            DetrendStage(type="detrend", kind="constant"),
            BandpassStage(
                type="bandpass",
                freqmin=1.0,
                freqmax=10.0,
                corners=4,
                zerophase=False,
            ),
        ]
    return DeviceConfig(
        name=name,
        host="localhost",
        port=18000,
        reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
        selectors=[
            StreamSelectorConfig(network="IU", station="ANMO", location="00", channel="BHZ")
        ],
        dsp_chain=chain,
    )


def _device_with_chain(name: str, chain: list) -> DeviceConfig:
    return DeviceConfig(
        name=name,
        host="localhost",
        port=18000,
        reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
        selectors=[
            StreamSelectorConfig(network="IU", station="ANMO", location="00", channel="BHZ")
        ],
        dsp_chain=chain,
    )


def test_live_stack_uses_single_mode_for_detector_only_chain(qtbot) -> None:
    """A chain that is purely a detector (sta_lta) has no displayable
    filtered waveform, so the second (stacked) plot must NOT be shown —
    it would only render an empty/duplicate lower pane."""
    chain = [StaLtaStage(type="sta_lta", sta=1.0, lta=10.0, on_threshold=3.5, off_threshold=1.5)]
    cfg = _root_cfg([_device_with_chain("detector", chain)])
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    plot = stack.add_stream("detector", "IU.ANMO.00.BHZ", fs=100.0)
    assert plot._processed_plot_item() is None, (
        "a detector-only (sta_lta) chain must use single-plot mode"
    )


def test_live_stack_uses_stacked_mode_for_detrend_then_detector_chain(qtbot) -> None:
    """As soon as the chain has a waveform-producing stage (detrend here),
    even alongside a detector, the filtered plot is meaningful and shown."""
    chain = [
        DetrendStage(type="detrend", kind="constant"),
        StaLtaStage(type="sta_lta", sta=1.0, lta=10.0, on_threshold=3.5, off_threshold=1.5),
    ]
    cfg = _root_cfg([_device_with_chain("detrend_detect", chain)])
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    plot = stack.add_stream("detrend_detect", "IU.ANMO.00.BHZ", fs=100.0)
    assert plot._processed_plot_item() is not None, (
        "a chain with a waveform-producing stage (detrend) must use stacked mode"
    )


def test_live_stack_uses_single_mode_when_device_has_no_chain(qtbot) -> None:
    cfg = _root_cfg([_device("plain", with_chain=False)])
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    nslc = "IU.ANMO.00.BHZ"
    plot = stack.add_stream("plain", nslc, fs=100.0)
    assert plot._processed_plot_item() is None, (
        "expected single-plot mode for stream whose device has no DSP chain"
    )


def test_live_stack_uses_stacked_mode_when_device_has_chain(qtbot) -> None:
    cfg = _root_cfg([_device("filtered", with_chain=True)])
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    nslc = "IU.ANMO.00.BHZ"
    plot = stack.add_stream("filtered", nslc, fs=100.0)
    assert plot._processed_plot_item() is not None, (
        "expected stacked mode for stream whose device has a DSP chain"
    )


def test_live_stack_stacked_plot_has_linked_x_axes(qtbot) -> None:
    cfg = _root_cfg([_device("filtered", with_chain=True)])
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    nslc = "IU.ANMO.00.BHZ"
    plot = stack.add_stream("filtered", nslc, fs=100.0)

    raw_plot = plot._raw_plot_item()
    proc_plot = plot._processed_plot_item()
    assert proc_plot is not None

    # In pyqtgraph, setXLink wires the second plot's view box X axis to
    # the first plot's view box. Read the link out via linkedView(0).
    linked_view = proc_plot.getViewBox().linkedView(0)
    assert linked_view is raw_plot.getViewBox(), (
        "processed PlotItem's X view should be linked to the raw PlotItem's view"
    )


def test_live_stack_stacked_plot_accepts_processed_pushes(qtbot) -> None:
    cfg = _root_cfg([_device("filtered", with_chain=True)])
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    nslc = "IU.ANMO.00.BHZ"
    plot = stack.add_stream("filtered", nslc, fs=100.0)

    raw = (np.arange(1000, dtype=np.float32) - 500.0) * 0.1
    processed = (np.arange(1000, dtype=np.float32) - 500.0) * 0.05
    plot.push_raw(raw)
    plot.push_processed(processed)

    raw_curve = plot._curve_for_test()
    proc_curve = plot._processed_curve_for_test()
    assert proc_curve is not None
    _, raw_y = raw_curve.getData()
    _, proc_y = proc_curve.getData()
    assert raw_y.shape == (1000,)
    assert proc_y.shape == (1000,)
    assert float(raw_y[-1]) == float(raw[-1])
    assert float(proc_y[-1]) == float(processed[-1])


def test_live_stack_drop_count_propagates_to_plot(qtbot) -> None:
    cfg = _root_cfg([_device("filtered", with_chain=True)])
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    nslc = "IU.ANMO.00.BHZ"
    plot = stack.add_stream("filtered", nslc, fs=100.0)
    assert plot._drop_count_recent == 0

    stack.set_drop_count("filtered", nslc, 7)
    assert plot._drop_count_recent == 7
    stack.set_drop_count("filtered", nslc, 0)
    assert plot._drop_count_recent == 0
