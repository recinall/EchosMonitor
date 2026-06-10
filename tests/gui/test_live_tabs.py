"""Tests for the M7 Stage B LiveTabs facade.

Covers: device-tab lifecycle (create / prune / dim-on-disconnect), the
always-present "All" tab, per-stream chips scoping, tab-pause render
gating (setData-count proxy), and active-tab persistence by device name.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QSettings
from pytestqt.qtbot import QtBot

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
from echosmonitor.gui.widgets.live_tabs import LiveTabs

_PACKET = (np.arange(200, dtype=np.float32) - 100.0) * 0.1


@pytest.fixture
def isolated_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[], QSettings]]:
    """File-backed QSettings factory, mirroring test_menubar.py.

    Returns the same zero-arg callable LiveTabs takes as its
    ``settings_provider`` so chip + active-tab state never pollute the
    real user config and is reproducible across save/restore.
    """
    settings_path = tmp_path / "settings.ini"

    def _provider() -> QSettings:
        return QSettings(str(settings_path), QSettings.Format.IniFormat)

    yield _provider


def _device(name: str, with_chain: bool = False) -> DeviceConfig:
    chain: list = []
    if with_chain:
        chain = [
            DetrendStage(type="detrend", kind="constant"),
            BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0, corners=4, zerophase=False),
        ]
    return DeviceConfig(
        name=name,
        host="localhost",
        port=18000,
        reconnect=ReconnectConfig(),
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
        dsp_chain=chain,
    )


def _cfg(devices: list[DeviceConfig]) -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10, max_visible_plots=8),
        devices=devices,
    )


def _build(
    qtbot: QtBot,
    provider: Callable[[], QSettings],
    devices: list[DeviceConfig],
) -> LiveTabs:
    tabs = LiveTabs(window_seconds=10.0, cfg=_cfg(devices), settings_provider=provider)
    qtbot.addWidget(tabs)
    return tabs


# ----------------------------------------------------------------------
# Tab lifecycle
# ----------------------------------------------------------------------
def test_all_tab_present_at_index_zero(qtbot: QtBot, isolated_settings) -> None:
    tabs = _build(qtbot, isolated_settings, [_device("dev-a")])
    assert tabs.tabText(0) == "All"
    assert tabs.widget(0) is tabs.all_stack


def test_add_stream_creates_device_tab(qtbot: QtBot, isolated_settings) -> None:
    tabs = _build(qtbot, isolated_settings, [_device("dev-a")])
    assert not tabs._has_device_tab_for_test("dev-a")
    tabs.add_stream("dev-a", "IU.ANMO.00.BHZ", fs=100.0)
    assert tabs._has_device_tab_for_test("dev-a")
    # Stream lands on BOTH the All tab and the device tab.
    assert tabs.all_stack.plot_for("dev-a", "IU.ANMO.00.BHZ") is not None
    dev_tab = tabs._device_tab_for_test("dev-a")
    assert dev_tab is not None
    assert dev_tab.stack.plot_for("dev-a", "IU.ANMO.00.BHZ") is not None


def test_prune_removes_device_tab_keeps_all(qtbot: QtBot, isolated_settings) -> None:
    tabs = _build(qtbot, isolated_settings, [_device("dev-a"), _device("dev-b")])
    tabs.add_stream("dev-a", "IU.ANMO.00.BHZ", fs=100.0)
    tabs.add_stream("dev-b", "IU.ANMO.10.HHZ", fs=100.0)
    assert tabs._has_device_tab_for_test("dev-a")
    assert tabs._has_device_tab_for_test("dev-b")

    # Config now only contains dev-b.
    tabs.prune_to({"dev-b"})
    assert not tabs._has_device_tab_for_test("dev-a")
    assert tabs._has_device_tab_for_test("dev-b")
    # The All tab survives at index 0.
    assert tabs.tabText(0) == "All"
    assert tabs.widget(0) is tabs.all_stack


def test_disconnect_dims_but_keeps_tab(qtbot: QtBot, isolated_settings) -> None:
    tabs = _build(qtbot, isolated_settings, [_device("dev-a")])
    tabs.set_device_state("dev-a", int(ConnState.CONNECTED))
    dev_tab = tabs._device_tab_for_test("dev-a")
    assert dev_tab is not None
    index = tabs.indexOf(dev_tab)
    assert tabs.tabText(index) == "dev-a"

    # Disconnect: tab stays, label gains an offline suffix.
    tabs.set_device_state("dev-a", int(ConnState.DISCONNECTED))
    assert tabs._has_device_tab_for_test("dev-a")
    assert tabs.tabText(index).endswith("(offline)")


# ----------------------------------------------------------------------
# Per-stream chips
# ----------------------------------------------------------------------
def test_chip_toggle_scopes_to_one_device_only(qtbot: QtBot, isolated_settings) -> None:
    tabs = _build(qtbot, isolated_settings, [_device("dev-a"), _device("dev-b")])
    nslc = "IU.ANMO.00.BHZ"
    tabs.add_stream("dev-a", nslc, fs=100.0)
    tabs.add_stream("dev-b", nslc, fs=100.0)

    dev_a = tabs._device_tab_for_test("dev-a")
    assert dev_a is not None
    chip = dev_a._chip_for_test(nslc)
    assert chip is not None and chip.isChecked()

    # Hide the stream in dev-a only.
    chip.setChecked(False)
    assert dev_a.stack.is_stream_user_visible("dev-a", nslc) is False
    assert dev_a.stack.plot_for("dev-a", nslc).isHidden()

    # dev-b's identically-named stream is unaffected.
    dev_b = tabs._device_tab_for_test("dev-b")
    assert dev_b is not None
    assert dev_b.stack.is_stream_user_visible("dev-b", nslc) is True
    assert not dev_b.stack.plot_for("dev-b", nslc).isHidden()

    # The All tab is unaffected too.
    assert not tabs.all_stack.plot_for("dev-a", nslc).isHidden()


def test_chip_state_persists_across_rebuild(qtbot: QtBot, isolated_settings) -> None:
    nslc = "IU.ANMO.00.BHZ"
    tabs = _build(qtbot, isolated_settings, [_device("dev-a")])
    tabs.add_stream("dev-a", nslc, fs=100.0)
    chip = tabs._device_tab_for_test("dev-a")._chip_for_test(nslc)
    assert chip is not None
    chip.setChecked(False)  # persists False through the provider

    # New widget over the same settings picks up the hidden state.
    tabs2 = _build(qtbot, isolated_settings, [_device("dev-a")])
    tabs2.add_stream("dev-a", nslc, fs=100.0)
    dev_a2 = tabs2._device_tab_for_test("dev-a")
    assert dev_a2 is not None
    chip2 = dev_a2._chip_for_test(nslc)
    assert chip2 is not None
    assert not chip2.isChecked()
    assert dev_a2.stack.plot_for("dev-a", nslc).isHidden()


# ----------------------------------------------------------------------
# Tab-pause render gating (setData-count proxy)
# ----------------------------------------------------------------------
def test_hidden_tabs_are_render_paused(qtbot: QtBot, isolated_settings) -> None:
    """8 device tabs, only one visible: real setData calls scale with the
    one visible device's streams, not 8x."""
    devices = [_device(f"dev-{i}") for i in range(8)]
    tabs = _build(qtbot, isolated_settings, devices)
    qtbot.addWidget(tabs)

    nslc = "IU.ANMO.00.BHZ"
    for dev in devices:
        tabs.add_stream(dev.name, nslc, fs=100.0)

    # Make dev-0 the visible tab; all others paused.
    dev0_tab = tabs._device_tab_for_test("dev-0")
    assert dev0_tab is not None
    tabs.setCurrentIndex(tabs.indexOf(dev0_tab))

    n_packets = 20
    for _ in range(n_packets):
        for dev in devices:
            tabs.push_raw(dev.name, nslc, _PACKET)

    # The visible device tab's plot renders every packet.
    visible_plot = dev0_tab.stack.plot_for("dev-0", nslc)
    assert visible_plot is not None
    assert visible_plot._is_render_active_for_test()

    # A hidden device tab's plot renders ~0 (only the activation flush at
    # most — but it was never activated, so exactly 0 after the initial
    # paused construction).
    hidden_tab = tabs._device_tab_for_test("dev-7")
    assert hidden_tab is not None
    hidden_plot = hidden_tab.stack.plot_for("dev-7", nslc)
    assert hidden_plot is not None
    assert not hidden_plot._is_render_active_for_test()
    assert hidden_plot._set_data_call_count_for_test() == 0

    # Visible-tab plot incremented per packet (single mode → 1 setData
    # per push). The All tab is hidden (we switched to dev-0), so the
    # All-tab plots are paused too.
    assert visible_plot._set_data_call_count_for_test() == n_packets

    # Total real setData across ALL device-tab plots == the one visible
    # device's count, NOT 8x.
    total = 0
    for dev in devices:
        tab = tabs._device_tab_for_test(dev.name)
        assert tab is not None
        plot = tab.stack.plot_for(dev.name, nslc)
        assert plot is not None
        total += plot._set_data_call_count_for_test()
    assert total == n_packets, f"expected {n_packets} (1 visible device), got {total}"


def test_pruning_visible_device_tab_reactivates_new_current(
    qtbot: QtBot, isolated_settings
) -> None:
    """Pruning the currently-visible device tab must not leave the new
    current tab render-paused.

    Removing a tab makes Qt emit ``currentChanged`` for the surviving
    current widget; ``_on_current_changed`` must re-activate it. Guards
    against a stuck-paused state where the now-visible tab renders nothing.
    """
    tabs = _build(qtbot, isolated_settings, [_device("dev-a"), _device("dev-b")])
    nslc = "IU.ANMO.00.BHZ"
    tabs.add_stream("dev-a", nslc, fs=100.0)
    tabs.add_stream("dev-b", nslc, fs=100.0)

    # Make dev-a the visible tab, then prune it (removed from config).
    tabs.setCurrentIndex(tabs.indexOf(tabs._device_tab_for_test("dev-a")))
    tabs.prune_to({"dev-b"})
    assert not tabs._has_device_tab_for_test("dev-a")

    # Whatever tab is current now must render the next packet at full rate.
    current = tabs.currentWidget()
    stack = (
        tabs.all_stack if current is tabs.all_stack else tabs._device_tab_for_test("dev-b").stack
    )
    plot = stack.plot_for("dev-b", nslc)
    assert plot is not None
    before = plot._set_data_call_count_for_test()
    tabs.push_raw("dev-b", nslc, _PACKET)
    assert plot._set_data_call_count_for_test() == before + 1


def test_switching_tab_flushes_now_visible_plot(qtbot: QtBot, isolated_settings) -> None:
    tabs = _build(qtbot, isolated_settings, [_device("dev-a"), _device("dev-b")])
    nslc = "IU.ANMO.00.BHZ"
    tabs.add_stream("dev-a", nslc, fs=100.0)
    tabs.add_stream("dev-b", nslc, fs=100.0)

    # Visible = dev-a; push to dev-b while it is hidden.
    tabs.setCurrentIndex(tabs.indexOf(tabs._device_tab_for_test("dev-a")))
    tabs.push_raw("dev-b", nslc, _PACKET)
    dev_b_plot = tabs._device_tab_for_test("dev-b").stack.plot_for("dev-b", nslc)
    assert dev_b_plot is not None
    assert dev_b_plot._set_data_call_count_for_test() == 0  # paused

    # Switch to dev-b: a single flush setData fires so it shows recent data.
    tabs.setCurrentIndex(tabs.indexOf(tabs._device_tab_for_test("dev-b")))
    assert dev_b_plot._is_render_active_for_test()
    assert dev_b_plot._set_data_call_count_for_test() == 1


# ----------------------------------------------------------------------
# Active-tab persistence
# ----------------------------------------------------------------------
def test_active_tab_persists_by_device_name(qtbot: QtBot, isolated_settings) -> None:
    tabs = _build(qtbot, isolated_settings, [_device("dev-a"), _device("dev-b")])
    tabs.add_stream("dev-a", "IU.ANMO.00.BHZ", fs=100.0)
    tabs.add_stream("dev-b", "IU.ANMO.10.HHZ", fs=100.0)
    tabs.setCurrentIndex(tabs.indexOf(tabs._device_tab_for_test("dev-b")))
    tabs.save_active_tab()

    # Rebuild + recreate the same device's tab → restore reopens dev-b.
    tabs2 = _build(qtbot, isolated_settings, [_device("dev-a"), _device("dev-b")])
    tabs2.restore_active_tab()
    # dev-b's tab is created lazily; the pending target switches on create.
    tabs2.add_stream("dev-a", "IU.ANMO.00.BHZ", fs=100.0)
    tabs2.add_stream("dev-b", "IU.ANMO.10.HHZ", fs=100.0)
    assert tabs2.currentWidget() is tabs2._device_tab_for_test("dev-b")


def test_active_tab_falls_back_to_all_when_device_gone(qtbot: QtBot, isolated_settings) -> None:
    tabs = _build(qtbot, isolated_settings, [_device("dev-a")])
    tabs.add_stream("dev-a", "IU.ANMO.00.BHZ", fs=100.0)
    tabs.setCurrentIndex(tabs.indexOf(tabs._device_tab_for_test("dev-a")))
    tabs.save_active_tab()

    # Rebuild but the persisted device's tab is never created → All tab.
    tabs2 = _build(qtbot, isolated_settings, [_device("dev-a")])
    tabs2.restore_active_tab()
    assert tabs2.currentWidget() is tabs2.all_stack
