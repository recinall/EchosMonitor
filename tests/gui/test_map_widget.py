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

import numpy as np
from pytestqt.qtbot import QtBot

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.map_tiles import (
    MAX_TILES_PER_REQUEST,
    TileRequest,
    TileResult,
    tile_xy,
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


# ----------------------------------------------------------------------
# f0 overlay (M5-B)
# ----------------------------------------------------------------------
def test_f0_overlay_colors_ramp_and_tip(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.on_position(_resolved("dev-b", _POS_B))
    widget.set_f0_overlay({"dev-a": 0.5, "dev-b": 5.0})
    # Ramp endpoints: lowest f0 = blue end, highest = red end.
    assert widget._spot_color_for_test("dev-a") == "#2860c0"
    assert widget._spot_color_for_test("dev-b") == "#d04040"
    # The hover tip carries the f0 line; the status mentions the overlay.
    assert "f₀ = 0.50 Hz" in widget._spot_tip(0, 0, "dev-a")
    assert "f₀ overlay" in widget._status_text_for_test()
    # isHidden ignores hidden ancestors (the widget is never show()n here),
    # so it pins the explicit setVisible toggle.
    assert not widget._clear_f0_button.isHidden()
    # Clearing restores the acquisition-state colour and hides the button.
    widget.clear_f0_overlay()
    assert widget._spot_color_for_test("dev-a") == _COLOR_IDLE
    assert "f₀ overlay" not in widget._status_text_for_test()
    assert widget._clear_f0_button.isHidden()


def test_f0_overlay_single_value_is_midpoint(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a",))
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.set_f0_overlay({"dev-a": 2.0})
    assert widget._spot_color_for_test("dev-a") == "#7c5080"


def test_f0_overlay_ignores_unknown_and_nonpositive(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a",))
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.set_f0_overlay({"dev-a": -1.0, "ghost": 2.0})
    assert widget._f0_overlay == {}
    assert widget._spot_color_for_test("dev-a") == _COLOR_IDLE


def test_f0_overlay_device_without_f0_keeps_state_color(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.on_position(_resolved("dev-b", _POS_B))
    widget.on_acquisition_state("dev-b", int(AcquisitionState.MONITORING))
    widget.on_device_state("dev-b", int(ConnState.CONNECTED))
    widget.set_f0_overlay({"dev-a": 2.0})
    # Not measured ≠ measured-low: dev-b keeps its state colour.
    assert widget._spot_color_for_test("dev-b") == _COLOR_MONITORING
    assert widget._spot_color_for_test("dev-a") == "#7c5080"


def test_set_devices_prunes_f0_overlay(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.set_f0_overlay({"dev-a": 2.0})
    widget.set_devices(("dev-b",))
    assert widget._f0_overlay == {}


# ----------------------------------------------------------------------
# Satellite basemap (M6.5-D)
# ----------------------------------------------------------------------
def _stub_tile_worker(widget: MapWidget, monkeypatch) -> list:
    """Replace the lazy worker creation with a no-thread stub and return
    the list that captures emitted TileRequests."""

    class _StubFetcher:
        def supersede(self, generation: int) -> None:
            self.generation = generation

        def stop(self) -> None:
            pass

    requests: list = []
    stub = _StubFetcher()
    monkeypatch.setattr(widget, "_ensure_tile_worker", lambda: stub)
    widget._tileRequested.connect(requests.append)
    return requests


def test_satellite_toggle_requests_bounded_batch(qtbot: QtBot, monkeypatch) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.on_position(_resolved("dev-b", _POS_B))
    assert requests == []  # off by default — nothing fetched
    widget._satellite_button.setChecked(True)
    assert len(requests) == 1
    req = requests[0]
    assert isinstance(req, TileRequest)
    assert 1 <= len(req.tiles) <= MAX_TILES_PER_REQUEST
    assert req.generation == widget._tile_generation
    # The chosen tiles cover the array centroid.
    lat0, lon0 = widget._frame_origin
    assert tile_xy(lat0, lon0, req.zoom) in req.tiles
    # Attribution is part of the imagery's usage terms.
    assert widget._attribution_label.isVisible() or widget._attribution_label.text()
    assert "Esri" in widget._attribution_label.text()


def test_tile_ready_draws_under_scatter_and_clears_on_toggle_off(
    qtbot: QtBot, monkeypatch
) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.on_position(_resolved("dev-b", _POS_B))
    widget._satellite_button.setChecked(True)
    req = requests[-1]
    x, y = req.tiles[0]
    image = np.zeros((256, 256, 4), dtype=np.uint8)
    widget._on_tile_ready(
        TileResult(generation=req.generation, zoom=req.zoom, x=x, y=y, image=image)
    )
    assert len(widget._tile_items) == 1
    item = next(iter(widget._tile_items.values()))
    # Under the scatter (z below the default-0 spots) and geometrically
    # sane: the tile rect must contain or touch the array's frame origin
    # region (it covers the centroid tile).
    assert item.zValue() < 0
    rect = item.boundingRect()
    assert rect.width() > 0 and rect.height() > 0
    # Toggle off: items dropped, attribution hidden, nothing fetched.
    widget._satellite_button.setChecked(False)
    assert widget._tile_items == {}
    assert not widget._attribution_label.isVisible()


def test_stale_generation_tile_is_ignored(qtbot: QtBot, monkeypatch) -> None:
    widget = _widget(qtbot, ("dev-a",))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._satellite_button.setChecked(True)
    req = requests[-1]
    stale = TileResult(
        generation=req.generation - 1,
        zoom=req.zoom,
        x=req.tiles[0][0],
        y=req.tiles[0][1],
        image=np.zeros((256, 256, 4), dtype=np.uint8),
    )
    widget._on_tile_ready(stale)
    assert widget._tile_items == {}


def test_position_change_rerequests_with_fresh_generation(
    qtbot: QtBot, monkeypatch
) -> None:
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._satellite_button.setChecked(True)
    first = requests[-1]
    # Second device arrives → centroid moves → tiles re-requested under
    # a NEW generation (the old placement would be misframed).
    widget.on_position(_resolved("dev-b", _POS_B))
    assert len(requests) >= 2
    assert requests[-1].generation > first.generation


def _feed_request_tiles(widget: MapWidget, req: TileRequest) -> None:
    """Deliver every tile of a request as a blank image (test helper)."""
    for x, y in req.tiles:
        widget._on_tile_ready(
            TileResult(
                generation=req.generation,
                zoom=req.zoom,
                x=x,
                y=y,
                image=np.zeros((256, 256, 4), dtype=np.uint8),
            )
        )


def test_state_only_rebuild_does_not_blank_or_refetch_basemap(
    qtbot: QtBot, monkeypatch
) -> None:
    """Marker recolours / connection flaps rebuild the scatter but must
    not churn the basemap: with every wanted tile already drawn, the
    same viewport wants nothing new → no TileRequest, tiles stay."""
    widget = _widget(qtbot, ("dev-a", "dev-b"))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget.on_position(_resolved("dev-b", _POS_B))
    widget._satellite_button.setChecked(True)
    _feed_request_tiles(widget, requests[-1])
    n_tiles = len(widget._tile_items)
    n_requests = len(requests)
    assert n_tiles >= 1
    # A flapping connection (the scatter-churn guard's own case).
    widget.on_acquisition_state("dev-a", int(AcquisitionState.MONITORING))
    widget.on_device_state("dev-a", int(ConnState.WAITING_RETRY))
    widget.set_f0_overlay({"dev-a": 2.0})
    assert len(requests) == n_requests, "state-only rebuilds must not refetch present tiles"
    assert len(widget._tile_items) == n_tiles, "state-only rebuilds must not blank the basemap"


def test_pan_fetches_newly_revealed_tiles_and_keeps_old(
    qtbot: QtBot, monkeypatch
) -> None:
    """M6.5-F: panning the viewport fetches the tiles for the newly
    visible region (the user's 'spostandosi la mappa non si aggiorna'
    report) while existing tiles stay (no blank, reused on pan-back)."""
    widget = _widget(qtbot, ("dev-a",))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._satellite_button.setChecked(True)
    _feed_request_tiles(widget, requests[-1])
    drawn_before = set(widget._tile_items)
    n_requests = len(requests)
    # Pan ~500 m east/north — well beyond the current viewport.
    view_box = widget._plot.getPlotItem().getViewBox()
    (e0, e1), (n0, n1) = view_box.viewRange()
    view_box.setRange(xRange=(e0 + 500, e1 + 500), yRange=(n0 + 500, n1 + 500), padding=0.0)
    widget._refresh_basemap_for_viewport()  # synchronous stand-in for the debounce
    assert len(requests) > n_requests, "pan did not fetch the newly-revealed region"
    new_req = requests[-1]
    # The new batch is tiles NOT already drawn.
    assert all((new_req.zoom, x, y) not in drawn_before for x, y in new_req.tiles)
    # Old tiles are still present (within the LRU cap).
    assert drawn_before.issubset(set(widget._tile_items))


def test_zoom_out_refetches_at_a_coarser_level(qtbot: QtBot, monkeypatch) -> None:
    """Zooming changes the tile zoom level; coarser tiles are fetched
    and layered under the finer ones (different zValue → no flash)."""
    widget = _widget(qtbot, ("dev-a",))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._satellite_button.setChecked(True)
    fine_zoom = requests[-1].zoom
    _feed_request_tiles(widget, requests[-1])
    # Zoom out 4x.
    view_box = widget._plot.getPlotItem().getViewBox()
    (e0, e1), (n0, n1) = view_box.viewRange()
    cx, cy = (e0 + e1) / 2, (n0 + n1) / 2
    hw, hh = (e1 - e0) * 2, (n1 - n0) * 2
    view_box.setRange(xRange=(cx - hw, cx + hw), yRange=(cy - hh, cy + hh), padding=0.0)
    widget._refresh_basemap_for_viewport()
    coarse = requests[-1]
    assert coarse.zoom < fine_zoom, "zoom-out did not drop to a coarser tile level"
    _feed_request_tiles(widget, coarse)
    zooms = {z for (z, _x, _y) in widget._tile_items}
    assert fine_zoom in zooms and coarse.zoom in zooms, "both levels should coexist (no flash)"
    # Finer tiles draw above coarser over the same ground.
    fine_item = next(it for (z, *_r), it in widget._tile_items.items() if z == fine_zoom)
    coarse_item = next(it for (z, *_r), it in widget._tile_items.items() if z == coarse.zoom)
    assert fine_item.zValue() > coarse_item.zValue()


def test_tile_items_bounded_by_lru_cap(qtbot: QtBot, monkeypatch) -> None:
    """Accumulating tiles across many pans never exceeds the LRU cap
    (rule 5/8: the seam is bounded)."""
    from echosmonitor.gui.widgets.map_widget import _MAX_TILE_ITEMS

    widget = _widget(qtbot, ("dev-a",))
    _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._satellite_button.setChecked(True)
    # Feed far more distinct tiles than the cap.
    for i in range(_MAX_TILE_ITEMS + 40):
        widget._on_tile_ready(
            TileResult(
                generation=widget._tile_generation,
                zoom=17,
                x=1000 + i,
                y=2000,
                image=np.zeros((256, 256, 4), dtype=np.uint8),
            )
        )
    assert len(widget._tile_items) <= _MAX_TILE_ITEMS


def test_batch_failure_shows_honest_note_and_recovery_restores_credit(
    qtbot: QtBot, monkeypatch
) -> None:
    widget = _widget(qtbot, ("dev-a",))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._satellite_button.setChecked(True)
    widget._on_tile_batch_failed(requests[-1].generation, "network unreachable")
    assert "unavailable" in widget._attribution_label.text()
    # A stale failure must not clobber the attribution of a newer batch.
    n_requests = len(requests)
    widget._on_tile_batch_failed(requests[0].generation - 1, "old noise")
    assert "unavailable" in widget._attribution_label.text()
    # Recovery: the failure cleared the request memo, so the NEXT
    # rebuild retries the same extent — and the fresh request restores
    # the Esri credit (imagery may arrive; usage terms).
    widget.on_acquisition_state("dev-a", int(AcquisitionState.MONITORING))
    assert len(requests) == n_requests + 1
    assert "Esri" in widget._attribution_label.text()


def test_shutdown_basemap_without_toggle_is_noop(qtbot: QtBot) -> None:
    widget = _widget(qtbot, ("dev-a",))
    widget.shutdown_basemap()  # never toggled: no thread, must not raise
    assert widget._tile_thread is None


def test_single_device_satellite_view_is_not_degenerate(qtbot: QtBot, monkeypatch) -> None:
    """First real Satellite use: ONE positioned device auto-ranges the
    view to a degenerate ~0 m span, so requested tiles were invisible
    ('la tendina map NON visualizza la mappa satellitare'). Toggling the
    basemap must leave the viewport showing the imagery extent."""
    widget = _widget(qtbot, ("dev-a",))
    _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._satellite_button.setChecked(True)
    (x0, x1), (y0, y1) = widget._plot.getPlotItem().getViewBox().viewRange()
    assert (x1 - x0) > 100.0 and (y1 - y0) > 100.0, (
        f"viewport still degenerate after Satellite toggle: {(x0, x1, y0, y1)}"
    )


def test_tile_arrival_rescues_view_collapsed_after_request(
    qtbot: QtBot, monkeypatch
) -> None:
    """pyqtgraph's auto-range collapses a single-marker view at PAINT
    time — i.e. AFTER `_update_basemap`'s request-time rescue ran. The
    arrival of an accepted tile must re-rescue the viewport."""
    widget = _widget(qtbot, ("dev-a",))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._satellite_button.setChecked(True)
    # Simulate the post-request auto-range collapse.
    view_box = widget._plot.getPlotItem().getViewBox()
    view_box.setRange(xRange=(-1e-12, 1e-12), yRange=(-1e-12, 1e-12), padding=0.0)
    req = requests[-1]
    x, y = req.tiles[0]
    widget._on_tile_ready(
        TileResult(
            generation=req.generation,
            zoom=req.zoom,
            x=x,
            y=y,
            image=np.zeros((256, 256, 4), dtype=np.uint8),
        )
    )
    (x0, x1), _y = view_box.viewRange()
    assert (x1 - x0) > 100.0, f"tile arrival did not rescue the collapsed view: {(x0, x1)}"
    # A healthy, user-chosen viewport inside the imagery is left alone
    # (compare before/after — the aspect lock reshapes requested ranges,
    # so absolute values are not stable to assert on).
    view_box.setRange(xRange=(-50.0, 50.0), yRange=(-50.0, 50.0), padding=0.0)
    healthy = view_box.viewRange()
    widget._on_tile_ready(
        TileResult(
            generation=req.generation,
            zoom=req.zoom,
            x=x + 1,
            y=y,
            image=np.zeros((256, 256, 4), dtype=np.uint8),
        )
    )
    assert view_box.viewRange() == healthy, "healthy viewport was disturbed"


def test_fit_view_floors_span_for_single_device(qtbot: QtBot) -> None:
    from echosmonitor.gui.widgets.map_widget import _FIT_MIN_SPAN_M

    widget = _widget(qtbot, ("dev-a",))
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._fit_view()
    (x0, x1), (y0, y1) = widget._plot.getPlotItem().getViewBox().viewRange()
    assert (x1 - x0) >= _FIT_MIN_SPAN_M * 0.99
    assert (y1 - y0) >= _FIT_MIN_SPAN_M * 0.99


def test_tile_orientation_north_is_up_when_rendered(qtbot: QtBot, monkeypatch) -> None:
    """Pin the row-flip: a tile whose NORTH half is red and SOUTH half
    is blue must render red-on-top. Sampled from an actual offscreen
    render of the plot, not from item internals."""
    from PySide6.QtGui import QColor

    widget = _widget(qtbot, ("dev-a",))
    requests = _stub_tile_worker(widget, monkeypatch)
    widget.resize(400, 400)
    widget.on_position(_resolved("dev-a", _POS_A))
    widget._satellite_button.setChecked(True)
    req = requests[-1]
    x, y = req.tiles[0]
    image = np.zeros((256, 256, 4), dtype=np.uint8)
    image[:, :, 3] = 255
    image[:128, :, 0] = 255  # rows 0..127 = tile's NORTH half → red
    image[128:, :, 2] = 255  # south half → blue
    widget._on_tile_ready(
        TileResult(generation=req.generation, zoom=req.zoom, x=x, y=y, image=image)
    )
    # Frame exactly this tile, render, and sample above/below centre.
    item = widget._tile_items[(req.zoom, x, y)]
    rect = item.mapRectToParent(item.boundingRect())
    view_box = widget._plot.getPlotItem().getViewBox()
    view_box.setRange(
        xRange=(rect.left(), rect.right()),
        yRange=(rect.top(), rect.bottom()),
        padding=0.0,
    )
    widget.show()
    qtbot.waitExposed(widget)
    pixmap = widget._plot.grab()
    img = pixmap.toImage()
    cx = img.width() // 2
    upper = QColor(img.pixel(cx, int(img.height() * 0.30)))
    lower = QColor(img.pixel(cx, int(img.height() * 0.70)))
    assert upper.red() > 200 and upper.blue() < 60, (
        f"north half not on top: upper pixel {upper.name()}"
    )
    assert lower.blue() > 200 and lower.red() < 60, (
        f"south half not at bottom: lower pixel {lower.name()}"
    )
