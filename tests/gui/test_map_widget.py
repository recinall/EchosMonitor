"""Tests for the Map tab widget (M4-B) and its MainWindow wiring.

The widget is a pure consumer: positions/states are fed through its
slots exactly as the production wiring does, so these tests never touch
the network. The MainWindow integration case uses a generic SeedLink
device (no ``echos`` section), whose position is honestly
``unavailable`` without any HTTP round-trip.
"""

from __future__ import annotations

import time
from pathlib import Path

from pytestqt.qtbot import QtBot

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import AcquisitionState, ConnState
from echosmonitor.core.positions import ResolvedPosition, haversine_m
from echosmonitor.gui.main_window import MainWindow
from echosmonitor.gui.widgets.map_widget import (
    _COLOR_IDLE,
    _COLOR_MONITORING,
    _COLOR_RECORDING,
    _COLOR_TROUBLE,
    MapWidget,
)

# Two real-world-ish points ~786 m apart east-west at 45°N.
_POS_A = (45.0, 11.0, 100.0)
_POS_B = (45.0, 11.01, 120.0)


def _resolved(device: str, coords: tuple[float, float, float]) -> ResolvedPosition:
    return ResolvedPosition(
        device=device,
        latitude=coords[0],
        longitude=coords[1],
        elevation_m=coords[2],
        source="stationxml",
        resolved_at=time.monotonic(),
    )


def _widget(qtbot: QtBot, devices: tuple[str, ...]) -> MapWidget:
    widget = MapWidget()
    qtbot.addWidget(widget)
    widget.set_devices(devices)
    return widget


def test_positions_become_spots_in_local_frame(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.on_position(_resolved("dev-b", _POS_B))
    assert widget._spot_count_for_test() == 2
    pos_a = widget._spot_pos_for_test("dev-a")
    pos_b = widget._spot_pos_for_test("dev-b")
    assert pos_a is not None and pos_b is not None
    # Same latitude → same north; B is east of A by ~786 m, centred on
    # the centroid so each sits ~±393 m from the origin.
    assert abs(pos_a[1] - pos_b[1]) < 1e-6
    assert pos_b[0] > pos_a[0]
    expected = haversine_m(*_POS_A[:2], *_POS_B[:2])
    assert abs((pos_b[0] - pos_a[0]) - expected) < 1.0
    assert "2 of 2 devices positioned" in widget._status_text_for_test()


def test_distance_table_lists_pairs(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.on_position(_resolved("dev-b", _POS_B))
    rows = widget._distance_rows_for_test()
    assert len(rows) == 1
    name_a, name_b, distance_text = rows[0]
    assert {name_a, name_b} == {"dev-a", "dev-b"}
    meters = float(distance_text.split()[0])
    assert 780.0 < meters < 793.0  # ~786 m at 45°N


def test_marker_colors_follow_acquisition_and_connection_state(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a",))
    widget.on_position(_resolved("dev-a", _POS_A))
    assert widget._spot_color_for_test("dev-a") == _COLOR_IDLE

    widget.on_acquisition_state("dev-a", int(AcquisitionState.MONITORING))
    widget.on_device_state("dev-a", int(ConnState.CONNECTED))
    assert widget._spot_color_for_test("dev-a") == _COLOR_MONITORING

    widget.on_acquisition_state("dev-a", int(AcquisitionState.RECORDING))
    assert widget._spot_color_for_test("dev-a") == _COLOR_RECORDING

    # Non-idle with a struggling socket → amber trouble tint.
    widget.on_device_state("dev-a", int(ConnState.WAITING_RETRY))
    assert widget._spot_color_for_test("dev-a") == _COLOR_TROUBLE

    widget.on_device_state("dev-a", int(ConnState.CONNECTED))
    widget.on_acquisition_state("dev-a", int(AcquisitionState.IDLE))
    assert widget._spot_color_for_test("dev-a") == _COLOR_IDLE


def test_marker_click_emits_device_selected(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a",))
    widget.on_position(_resolved("dev-a", _POS_A))
    points = widget._scatter.points()
    assert len(points) == 1
    with qtbot.waitSignal(widget.deviceSelected, timeout=1000) as blocker:
        widget._on_spot_clicked(widget._scatter, list(points))
    assert blocker.args == ["dev-a"]


def test_unpositioned_devices_are_listed_with_kind(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    widget.on_position(_resolved("dev-a", _POS_A))
    assert "dev-b (pending)" in widget._unpositioned_text_for_test()
    widget.on_position_failed("dev-b", "unavailable", "no source")
    assert "dev-b (unavailable)" in widget._unpositioned_text_for_test()
    assert "1 of 2 devices positioned" in widget._status_text_for_test()


def test_removed_device_drops_marker_and_state(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.on_position(_resolved("dev-b", _POS_B))
    widget.set_devices(("dev-a",))
    assert widget._spot_count_for_test() == 1
    assert widget._spot_pos_for_test("dev-b") is None
    assert widget._distance_rows_for_test() == []


def test_stale_results_for_unknown_devices_are_ignored(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a",))
    widget.on_position(_resolved("ghost", _POS_A))
    widget.on_position_failed("ghost", "unreachable", "boom")
    widget.on_acquisition_state("ghost", int(AcquisitionState.RECORDING))
    assert widget._spot_count_for_test() == 0
    assert "ghost" not in widget._unpositioned_text_for_test()


def test_refresh_button_emits_request(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ())
    with qtbot.waitSignal(widget.refreshRequested, timeout=1000):
        widget._refresh_button.click()


def test_main_window_map_integration(qtbot: QtBot) -> None:
    """End-to-end wiring: a generic device (no echos section) shows up
    honestly unavailable without any network, and a marker click selects
    the device row in the Devices dock."""
    cfg = RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name="generic-dev",
                host="127.0.0.1",
                port=18000,
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
            )
        ],
    )
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    map_widget = window._map_widget
    assert map_widget is not None
    # The resolver round-trips through its worker thread; the failure is
    # emitted queued, so wait for it.
    qtbot.waitUntil(
        lambda: "generic-dev (unavailable)" in map_widget._unpositioned_text_for_test(),
        timeout=5000,
    )
    map_widget.deviceSelected.emit("generic-dev")
    assert window._device_panel is not None
    assert window._device_panel._selected_device_name() == "generic-dev"
    # Unknown name is a no-op (a marker click can race a config removal;
    # the row must not be resurrected and the selection must hold).
    window._device_panel.select_device("ghost")
    assert window._device_panel._selected_device_name() == "generic-dev"
    window.close()
