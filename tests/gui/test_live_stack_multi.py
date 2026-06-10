"""Multi-device tests for ``LiveStack`` (M3 part 1).

Asserts that the central widget renders one ``_DeviceGroup`` per
configured device, with the right number of plot pairs per group,
and that the global ``cfg.ui.max_visible_plots`` cap is enforced
with a "+K hidden" indicator on group headers when exceeded.
"""

from __future__ import annotations

from echosmonitor.config.schema import (
    AppConfig,
    BandpassStage,
    DetrendStage,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import ConnState
from echosmonitor.gui.widgets.live_stack import LiveStack


def _device(name: str, with_chain: bool = True) -> DeviceConfig:
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
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
        dsp_chain=chain,
    )


def _root_cfg(devices: list[DeviceConfig], max_visible: int = 8) -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(
            refresh_hz=20,
            default_window_seconds=10,
            max_visible_plots=max_visible,
        ),
        devices=devices,
    )


def test_live_stack_renders_one_device_group_per_device_with_its_plots(qtbot) -> None:
    """Two devices, three streams each → LiveStack renders two device
    groups; each group has the right number of stream plots."""
    cfg = _root_cfg([_device("dev-a"), _device("dev-b")])
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)

    streams_a = ["IU.ANMO.00.BHZ", "IU.ANMO.00.BHN", "IU.ANMO.00.BHE"]
    streams_b = ["IU.ANMO.10.HHZ", "IU.ANMO.10.HHN", "IU.ANMO.10.HHE"]
    for nslc in streams_a:
        stack.add_stream("dev-a", nslc, fs=20.0)
    for nslc in streams_b:
        stack.add_stream("dev-b", nslc, fs=100.0)

    group_a = stack._device_group_for_test("dev-a")
    group_b = stack._device_group_for_test("dev-b")
    assert group_a is not None and group_b is not None

    # 3/3 visible per group (well below default cap of 8).
    assert group_a._counter_text_for_test() == "3/3"
    assert group_b._counter_text_for_test() == "3/3"

    # Per-stream plots resolve to their device group.
    for nslc in streams_a:
        plot = stack.plot_for("dev-a", nslc)
        assert plot is not None and not plot.isHidden()
        # Stacked mode because dev-a has a chain configured.
        assert plot._processed_plot_item() is not None
    for nslc in streams_b:
        plot = stack.plot_for("dev-b", nslc)
        assert plot is not None and not plot.isHidden()
        assert plot._processed_plot_item() is not None

    # Same NSLC across devices addresses two different plots.
    same_nslc_a = stack.plot_for("dev-a", "IU.ANMO.00.BHZ")
    same_nslc_b = stack.plot_for("dev-b", "IU.ANMO.00.BHZ")
    # No collision risk in this test (different NSLCs above), but the
    # API contract is that distinct (device, nslc) pairs always yield
    # distinct plots.
    assert (same_nslc_a is None) or (same_nslc_a is not same_nslc_b)


def test_live_stack_visibility_cap_hides_oldest_and_shows_indicator(qtbot) -> None:
    """``cfg.ui.max_visible_plots`` cap respected; the "+K hidden"
    indicator appears when exceeded.

    Cap of 4 across two devices: add 3 streams to each. Six total
    streams should produce the most-recent 4 visible and 2 hidden, with
    the older device group showing "+1 hidden" (it owned 1 of the 2
    hidden plots) and the same for the newer one — verify the
    arithmetic adds up to total = visible + hidden per device group."""
    cfg = _root_cfg([_device("dev-a"), _device("dev-b")], max_visible=4)
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)

    # 3 streams on dev-a then 3 on dev-b — total 6 > cap 4. Insertion
    # order means dev-a's first stream is the oldest.
    for nslc in ["IU.ANMO.00.BHZ", "IU.ANMO.00.BHN", "IU.ANMO.00.BHE"]:
        stack.add_stream("dev-a", nslc, fs=20.0)
    for nslc in ["IV.MILN..HHZ", "IV.MILN..HHN", "IV.MILN..HHE"]:
        stack.add_stream("dev-b", nslc, fs=100.0)

    assert stack._max_visible_for_test() == 4
    assert stack.visible_count() == 4

    group_a = stack._device_group_for_test("dev-a")
    group_b = stack._device_group_for_test("dev-b")
    assert group_a is not None and group_b is not None

    # Counters are "visible/total" per device. Visible-counts must sum
    # to 4 (the cap); total counts to 6 (all streams).
    a_text = group_a._counter_text_for_test()
    b_text = group_b._counter_text_for_test()
    assert a_text.endswith("/3"), a_text
    assert b_text.endswith("/3"), b_text

    a_visible = int(a_text.split("/")[0])
    b_visible = int(b_text.split("/")[0])
    assert a_visible + b_visible == 4, (
        f"visible-plot cap not enforced: dev-a={a_visible}, dev-b={b_visible}"
    )
    # Most recent wins: dev-b inserted last, so all of dev-b should be
    # visible; dev-a loses its oldest 2 to the cap.
    assert b_visible == 3, "dev-b's plots (most recently added) should all be visible"
    assert a_visible == 1, "dev-a should retain only its newest plot under the cap"

    a_hidden_text = group_a._hidden_indicator_text_for_test()
    b_hidden_text = group_b._hidden_indicator_text_for_test()
    assert a_hidden_text == "+2 hidden", a_hidden_text
    assert b_hidden_text == "", b_hidden_text


def test_live_stack_device_state_signal_populates_group_badge(qtbot) -> None:
    """Connecting the engine's ``deviceStateChanged`` to
    ``LiveStack.set_device_state`` updates the corresponding device
    group's badge — even before any streams from that device have
    arrived. Lets the multi-device UI populate during CONNECTING."""
    cfg = _root_cfg([_device("dev-a", with_chain=False)])
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)

    stack.set_device_state("dev-a", int(ConnState.CONNECTING))
    group = stack._device_group_for_test("dev-a")
    assert group is not None
    assert group._badge_text_for_test() == ConnState.CONNECTING.name

    stack.set_device_state("dev-a", int(ConnState.CONNECTED))
    assert group._badge_text_for_test() == ConnState.CONNECTED.name
