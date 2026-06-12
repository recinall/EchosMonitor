"""Tests for ``core/map_tiles.py`` (M6.5-D) — math pure, fetch mocked.

The tile math is checked against independently-computed slippy-map
values; the :class:`TileFetcher` runs its slot synchronously on the
test thread (slots are plain methods) against ``httpx.MockTransport``,
so no test ever touches the network.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import numpy as np
import pytest
from PySide6.QtCore import QBuffer
from PySide6.QtGui import QColor, QImage

from echosmonitor.core.map_tiles import (
    MAX_TILES_PER_REQUEST,
    MAX_ZOOM,
    TileFetcher,
    TileRequest,
    TileResult,
    tile_bounds,
    tile_xy,
    tiles_for_extent,
    zoom_for_span,
)

# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def test_tile_xy_known_values() -> None:
    # Rome (41.9 N, 12.5 E) at z=10 — computed by hand from the slippy
    # formulas: x = (12.5+180)/360*1024 = 547.5..., y = 380.8...
    assert tile_xy(41.9, 12.5, 10) == (547, 380)
    # Origin corner case at z=1: (0, 0) falls into the SE quadrant tile.
    assert tile_xy(0.0, 0.0, 1) == (1, 1)


def test_tile_xy_clamps_poles_and_edges() -> None:
    n = 1 << 5
    x, y = tile_xy(89.9, 179.999, 5)
    assert 0 <= x < n and 0 <= y < n
    x, y = tile_xy(-89.9, -180.0, 5)
    assert 0 <= x < n and 0 <= y < n


def test_tile_bounds_round_trip_contains_point() -> None:
    lat, lon, zoom = 45.1234, 11.4321, 15
    x, y = tile_xy(lat, lon, zoom)
    lat_n, lon_w, lat_s, lon_e = tile_bounds(zoom, x, y)
    assert lat_s <= lat <= lat_n
    assert lon_w <= lon <= lon_e


def test_zoom_for_span_scales_and_clamps() -> None:
    # Tiny array → max zoom.
    assert zoom_for_span(10.0, 45.0) == MAX_ZOOM
    # ~5 km at 45°N lands mid-range and grows as the span shrinks.
    z_5km = zoom_for_span(5_000.0, 45.0)
    z_500m = zoom_for_span(500.0, 45.0)
    assert 12 <= z_5km < z_500m <= MAX_ZOOM


def test_tiles_for_extent_is_bounded_and_centre_first() -> None:
    # A continent-sized box at high zoom would be millions of tiles —
    # the cap must hold and keep the centre-most tiles.
    tiles = tiles_for_extent(40.0, 50.0, 5.0, 15.0, 12)
    assert len(tiles) == MAX_TILES_PER_REQUEST
    cx, cy = tile_xy(45.0, 10.0, 12)
    # The centre tile must be in the kept set.
    assert any(abs(x - cx) <= 1 and abs(y - cy) <= 1 for x, y in tiles)


# ---------------------------------------------------------------------------
# TileFetcher against MockTransport
# ---------------------------------------------------------------------------


def _png_bytes(color: str = "#336699") -> bytes:
    image = QImage(256, 256, QImage.Format.Format_RGBA8888)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _collect(fetcher: TileFetcher) -> tuple[list[TileResult], list[tuple], list[tuple]]:
    ready: list[TileResult] = []
    done: list[tuple] = []
    failed: list[tuple] = []
    fetcher.tileReady.connect(lambda r: ready.append(r))
    fetcher.batchDone.connect(lambda *a: done.append(a))
    fetcher.batchFailed.connect(lambda *a: failed.append(a))
    return ready, done, failed


def test_fetch_decodes_and_caches(tmp_path: Path, qapp) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=_png_bytes())

    fetcher = TileFetcher(cache_root=tmp_path, transport=httpx.MockTransport(handler))
    ready, done, failed = _collect(fetcher)
    request = TileRequest(generation=1, zoom=15, tiles=((100, 200), (101, 200)))
    fetcher.supersede(1)
    fetcher.fetch(request)

    assert [(r.zoom, r.x, r.y) for r in ready] == [(15, 100, 200), (15, 101, 200)]
    assert done == [(1, 2, 0)]
    assert failed == []
    assert len(calls) == 2
    first = ready[0].image
    assert first.shape == (256, 256, 4) and first.dtype == np.uint8

    # Second fetch: served from the disk cache, zero network calls.
    fetcher.supersede(2)
    fetcher.fetch(TileRequest(generation=2, zoom=15, tiles=((100, 200),)))
    assert len(calls) == 2
    assert done[-1] == (2, 1, 0)


def test_superseded_batch_stops_and_stale_generation_never_emits(
    tmp_path: Path, qapp
) -> None:
    fetcher = TileFetcher(
        cache_root=tmp_path,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=_png_bytes())),
    )
    ready, done, failed = _collect(fetcher)
    # The fetcher only honours the generation it was last superseded to.
    fetcher.supersede(7)
    fetcher.fetch(TileRequest(generation=3, zoom=10, tiles=((1, 1),)))
    assert ready == [] and done == [] and failed == []


def test_batch_failure_emits_batch_failed_once(tmp_path: Path, qapp) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unreachable")

    fetcher = TileFetcher(cache_root=tmp_path, transport=httpx.MockTransport(handler))
    ready, done, failed = _collect(fetcher)
    fetcher.supersede(1)
    fetcher.fetch(TileRequest(generation=1, zoom=12, tiles=((5, 5), (6, 5))))
    assert ready == [] and done == []
    assert len(failed) == 1
    assert failed[0][0] == 1
    assert "unreachable" in failed[0][1]


def test_offline_with_warm_cache_serves_tiles(tmp_path: Path, qapp) -> None:
    ok = httpx.MockTransport(lambda _r: httpx.Response(200, content=_png_bytes()))
    warm = TileFetcher(cache_root=tmp_path, transport=ok)
    warm.supersede(1)
    warm.fetch(TileRequest(generation=1, zoom=14, tiles=((3, 4),)))

    def offline(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    fetcher = TileFetcher(cache_root=tmp_path, transport=httpx.MockTransport(offline))
    ready, done, failed = _collect(fetcher)
    fetcher.supersede(1)
    fetcher.fetch(TileRequest(generation=1, zoom=14, tiles=((3, 4),)))
    assert len(ready) == 1 and done == [(1, 1, 0)] and failed == []


def test_poisoned_cache_entry_is_evicted_and_refetched(tmp_path: Path, qapp) -> None:
    """A cached tile that no longer decodes (torn write from a pre-fsync
    build, disk corruption) must be evicted and refetched — never a
    permanently-broken 'cache hit'."""
    cache_file = tmp_path / "14" / "3" / "4.jpg"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(b"torn garbage")

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=_png_bytes())

    fetcher = TileFetcher(cache_root=tmp_path, transport=httpx.MockTransport(handler))
    ready, done, failed = _collect(fetcher)
    fetcher.supersede(1)
    fetcher.fetch(TileRequest(generation=1, zoom=14, tiles=((3, 4),)))
    assert len(calls) == 1, "evicted tile must be refetched from the network"
    assert len(ready) == 1 and done == [(1, 1, 0)] and failed == []
    # The cache now holds the good bytes.
    assert cache_file.read_bytes() != b"torn garbage"


def test_undecodable_tile_counts_as_failed(tmp_path: Path, qapp) -> None:
    fetcher = TileFetcher(
        cache_root=tmp_path,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"not an image")),
    )
    ready, done, failed = _collect(fetcher)
    fetcher.supersede(1)
    fetcher.fetch(TileRequest(generation=1, zoom=12, tiles=((9, 9),)))
    assert ready == [] and done == []
    assert failed
    assert "decode" in failed[0][1]


def test_http_error_status_fails_batch(tmp_path: Path, qapp) -> None:
    fetcher = TileFetcher(
        cache_root=tmp_path,
        transport=httpx.MockTransport(lambda _r: httpx.Response(404)),
    )
    ready, done, failed = _collect(fetcher)
    fetcher.supersede(1)
    fetcher.fetch(TileRequest(generation=1, zoom=12, tiles=((9, 9),)))
    assert ready == [] and done == [] and len(failed) == 1


def test_non_request_payload_is_ignored(tmp_path: Path, qapp) -> None:
    fetcher = TileFetcher(cache_root=tmp_path, transport=httpx.MockTransport(lambda _r: None))
    ready, done, failed = _collect(fetcher)
    fetcher.fetch("not a request")  # rule 4 guard
    assert ready == [] and done == [] and failed == []


def test_request_tile_count_is_capped(tmp_path: Path, qapp) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=_png_bytes())

    fetcher = TileFetcher(cache_root=tmp_path, transport=httpx.MockTransport(handler))
    _collect(fetcher)
    oversized = tuple((x, 0) for x in range(MAX_TILES_PER_REQUEST + 20))
    fetcher.supersede(1)
    fetcher.fetch(TileRequest(generation=1, zoom=8, tiles=oversized))
    assert len(calls) == MAX_TILES_PER_REQUEST


@pytest.mark.parametrize("lat", [0.0, 45.0, 60.0])
def test_zoom_never_exceeds_source_limits(lat: float) -> None:
    for span in (1.0, 100.0, 1e7):
        assert 1 <= zoom_for_span(span, lat) <= MAX_ZOOM
