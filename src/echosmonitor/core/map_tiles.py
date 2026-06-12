"""Web-Mercator XYZ tile math + worker-thread tile fetcher (M6.5-D).

The Map tab's satellite basemap: raster XYZ tiles (Esri World Imagery)
fetched on a dedicated worker thread with httpx, disk-cached, and handed
to the GUI as decoded RGBA arrays for pyqtgraph ``ImageItem``s drawn
UNDER the device scatter. This keeps M4-B's decision intact — no
QtWebEngine, no tile *stack*; the basemap is a static backdrop fetched
once per array extent, not a slippy map.

Networking lives here per CLAUDE.md rule 2 (this module joins
``seedlink_worker`` / ``info*`` / ``echos_api`` on the sanctioned list).
The worker follows the qt-worker-threading skill: parentless QObject,
queued request signal, latest-wins generation token written directly by
the owner, every HTTP call timeout-bounded, never raises across the
signal boundary.

Tile source / usage terms (decision log 2026-06-12): Esri World Imagery
public tile endpoint. Esri's terms require attribution — the widget
renders the attribution string verbatim; tiles are cached on disk so
field laptops keep their last basemap offline.

Pure tile math lives at module top (no Qt, testable standalone).
"""

from __future__ import annotations

import contextlib
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np
import structlog
from platformdirs import user_cache_dir
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

_log = structlog.get_logger(__name__)

# Esri World Imagery XYZ endpoint ({z}/{y}/{x} order!) + mandated credit.
TILE_URL_TEMPLATE = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
ATTRIBUTION = "Esri — Esri, Maxar, Earthstar Geographics, and the GIS User Community"

# Esri World Imagery serves up to z=19 in most areas; clamp there.
MAX_ZOOM = 19
MIN_ZOOM = 1

# Hard cap on tiles per request: 6x6 covers any sane array extent with
# margin at the chosen zoom; anything more means the zoom selection is
# wrong, not that we should hammer the server (rule 5: every batch
# bounded).
MAX_TILES_PER_REQUEST = 36

# Per-tile HTTP timeout. One slow tile must not stall the batch forever
# (rule 7); the worker checks the generation token between tiles so a
# superseded batch aborts at the next boundary.
_HTTP_TIMEOUT_S = 10.0

_TILE_PX = 256
# Equatorial Web-Mercator ground resolution at z=0 (metres per pixel).
_GROUND_RES_Z0 = 156_543.033_928_041


# ---------------------------------------------------------------------------
# Pure tile math (slippy-map convention)
# ---------------------------------------------------------------------------


def tile_xy(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """Slippy-map tile indices containing (lat, lon) at ``zoom``."""
    lat = min(85.0511, max(-85.0511, lat))
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


def tile_bounds(zoom: int, x: int, y: int) -> tuple[float, float, float, float]:
    """(lat_north, lon_west, lat_south, lon_east) of tile (x, y) at ``zoom``."""
    n = 1 << zoom
    lon_w = x / n * 360.0 - 180.0
    lon_e = (x + 1) / n * 360.0 - 180.0
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))
    return lat_n, lon_w, lat_s, lon_e


def zoom_for_span(span_m: float, lat: float, target_px: int = 1024) -> int:
    """Zoom level at which ``span_m`` metres fill roughly ``target_px`` pixels.

    Derived from the Web-Mercator ground resolution
    ``156543.03 * cos(lat) / 2**z`` m/px, clamped to the source's
    supported range. Small/degenerate spans land on ``MAX_ZOOM`` (a
    single-station "array" still gets imagery).
    """
    span_m = max(span_m, 1.0)
    res_needed = span_m / max(target_px, 1)
    z = math.floor(math.log2(_GROUND_RES_Z0 * math.cos(math.radians(lat)) / res_needed))
    return min(MAX_ZOOM, max(MIN_ZOOM, z))


def tiles_for_extent(
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    zoom: int,
) -> list[tuple[int, int]]:
    """All (x, y) tiles covering the lat/lon box at ``zoom``, capped.

    If the box needs more than :data:`MAX_TILES_PER_REQUEST` tiles the
    list is truncated from the box centre outward — never an unbounded
    fan-out (rule 5).
    """
    x0, y0 = tile_xy(lat_max, lon_min, zoom)  # NW corner
    x1, y1 = tile_xy(lat_min, lon_max, zoom)  # SE corner
    xs = range(min(x0, x1), max(x0, x1) + 1)
    ys = range(min(y0, y1), max(y0, y1) + 1)
    tiles = [(x, y) for x in xs for y in ys]
    if len(tiles) > MAX_TILES_PER_REQUEST:
        cx = (min(x0, x1) + max(x0, x1)) / 2.0
        cy = (min(y0, y1) + max(y0, y1)) / 2.0
        tiles.sort(key=lambda t: (t[0] - cx) ** 2 + (t[1] - cy) ** 2)
        # Rule 5: never truncate silently.
        _log.debug(
            "tile_batch_truncated",
            requested=len(tiles),
            kept=MAX_TILES_PER_REQUEST,
            zoom=zoom,
        )
        tiles = tiles[:MAX_TILES_PER_REQUEST]
    return tiles


# ---------------------------------------------------------------------------
# Request / result payloads (frozen, cross-thread per rule 4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TileRequest:
    """One basemap batch: every listed tile at one zoom level."""

    generation: int
    zoom: int
    tiles: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class TileResult:
    """One decoded tile. ``image`` is an (H, W, 4) uint8 RGBA array."""

    generation: int
    zoom: int
    x: int
    y: int
    image: np.ndarray


class TileFetcher(QObject):
    """Worker QObject fetching + decoding basemap tiles off the GUI thread.

    Lifecycle (qt-worker-threading pattern 1/2): the owner moves this to
    a QThread, posts work via :meth:`fetch` over a QueuedConnection
    signal, and supersedes in-flight batches by writing
    ``_active_generation`` directly (GIL-atomic int). The worker checks
    the token at batch entry and between tiles (the batch-terminal
    ``batchDone``/``batchFailed`` emits are NOT re-checked — receivers
    guard on generation anyway). ``stop()`` is the owner-side
    synchronous flag flip for shutdown; once observed, the fetch loop
    also closes the httpx client on its own thread, so the queued
    ``shutdown`` slot is a best-effort duplicate.

    Cache: ``<user_cache_dir>/tiles/esri/{z}/{x}/{y}.jpg``, written
    atomically (tmp + replace) so a killed process can never leave a
    torn JPEG that poisons later runs. Cache hits never touch the
    network — the field-laptop offline path.
    """

    # ``tileReady(TileResult)`` / ``batchDone(generation, fetched, failed)``
    # / ``batchFailed(generation, reason)`` — all emitted on the worker
    # thread; receivers connect queued (rule 4 isinstance guard on the
    # object payloads).
    tileReady = Signal(object)  # noqa: N815
    batchDone = Signal(int, int, int)  # noqa: N815
    batchFailed = Signal(int, str)  # noqa: N815

    def __init__(
        self,
        cache_root: Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__()
        self._cache_root = (
            Path(cache_root)
            if cache_root is not None
            else Path(user_cache_dir("echosmonitor")) / "tiles" / "esri"
        )
        # Test seam: httpx.MockTransport drives the fetch paths without
        # a network. None → real HTTPTransport.
        self._transport = transport
        self._active_generation = 0
        self._stop = False
        self._client: httpx.Client | None = None
        self._cache_write_warned = False

    # -- owner-side controls (called from the GUI thread) ---------------
    def supersede(self, generation: int) -> None:
        """Mark ``generation`` as the only batch worth finishing."""
        self._active_generation = generation

    def stop(self) -> None:
        """Owner-side shutdown flag; safe to call from any thread."""
        self._stop = True

    # -- worker-side slot ------------------------------------------------
    @Slot(object)
    def fetch(self, request: object) -> None:
        if not isinstance(request, TileRequest):  # rule 4 guard
            return
        if self._stop or request.generation != self._active_generation:
            return
        t0 = time.monotonic()
        fetched = 0
        failed = 0
        first_error: str | None = None
        self._cache_write_warned = False  # one warn per batch (rule 5)
        for x, y in request.tiles[:MAX_TILES_PER_REQUEST]:
            if self._stop or request.generation != self._active_generation:
                _log.debug(
                    "tile_fetch_superseded",
                    generation=request.generation,
                    done=fetched,
                )
                if self._stop:
                    # Shutdown observed mid-batch: close the client on
                    # OUR thread now — the owner's queued ``shutdown``
                    # may be skipped by quit() (POSTMORTEMS 2026-05-10).
                    self.shutdown()
                return
            try:
                raw, from_cache = self._tile_bytes(request.zoom, x, y)
            except Exception as exc:
                failed += 1
                if first_error is None:
                    first_error = str(exc)
                continue
            image = _decode_tile(raw)
            if image is None and from_cache:
                # Poisoned cache entry (torn write from a pre-fsync
                # build, disk corruption): evict and retry the network
                # exactly once so the breakage cannot become permanent.
                self._evict_cached_tile(request.zoom, x, y)
                try:
                    raw, _ = self._tile_bytes(request.zoom, x, y)
                except Exception as exc:
                    failed += 1
                    if first_error is None:
                        first_error = str(exc)
                    continue
                image = _decode_tile(raw)
            if image is None:
                failed += 1
                if first_error is None:
                    first_error = "tile decode failed"
                continue
            fetched += 1
            self.tileReady.emit(
                TileResult(
                    generation=request.generation,
                    zoom=request.zoom,
                    x=x,
                    y=y,
                    image=image,
                )
            )
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)
        if fetched == 0 and failed > 0:
            _log.warning(
                "tile_batch_failed",
                generation=request.generation,
                zoom=request.zoom,
                failed=failed,
                error=first_error,
                elapsed_ms=elapsed_ms,
            )
            self.batchFailed.emit(request.generation, first_error or "no tiles")
        else:
            _log.info(
                "tile_batch_done",
                generation=request.generation,
                zoom=request.zoom,
                fetched=fetched,
                failed=failed,
                elapsed_ms=elapsed_ms,
            )
            self.batchDone.emit(request.generation, fetched, failed)

    @Slot()
    def shutdown(self) -> None:
        """Close the HTTP client on the worker thread (idempotent)."""
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- internals ---------------------------------------------------------
    def _cache_path(self, zoom: int, x: int, y: int) -> Path:
        return self._cache_root / str(zoom) / str(x) / f"{y}.jpg"

    def _evict_cached_tile(self, zoom: int, x: int, y: int) -> None:
        path = self._cache_path(zoom, x, y)
        with contextlib.suppress(OSError):
            path.unlink()
        _log.warning("tile_cache_evicted_undecodable", path=str(path))

    def _tile_bytes(self, zoom: int, x: int, y: int) -> tuple[bytes, bool]:
        """Return ``(raw_bytes, served_from_cache)`` for one tile."""
        cache_path = self._cache_path(zoom, x, y)
        if cache_path.is_file():
            return cache_path.read_bytes(), True
        if self._client is None:
            self._client = httpx.Client(
                timeout=_HTTP_TIMEOUT_S,
                headers={"User-Agent": "EchosMonitor/echos-map"},
                follow_redirects=True,
                transport=self._transport,
            )
        url = TILE_URL_TEMPLATE.format(z=zoom, y=y, x=x)
        response = self._client.get(url)
        response.raise_for_status()
        raw = response.content
        # Atomic cache write per the rule-8 recipe (tmp in same dir →
        # fsync → replace): a torn file would otherwise serve as a
        # permanently-broken "cache hit".
        tmp_name: str | None = None
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=cache_path.parent, suffix=".part")
            try:
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp_name, cache_path)
            tmp_name = None
        except OSError as exc:
            if tmp_name is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
            # Cache is an accelerator, not truth; one warn per batch
            # (the next tile will likely fail the same way).
            if not self._cache_write_warned:
                self._cache_write_warned = True
                _log.warning("tile_cache_write_failed", path=str(cache_path), error=str(exc))
        return raw, False


def _decode_tile(raw: bytes) -> np.ndarray | None:
    """JPEG/PNG bytes → (H, W, 4) uint8 RGBA array, or None."""
    qimage = QImage.fromData(raw)
    if qimage.isNull():
        return None
    qimage = qimage.convertToFormat(QImage.Format.Format_RGBA8888)
    width = qimage.width()
    height = qimage.height()
    ptr = qimage.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8, count=width * height * 4)
    return arr.reshape((height, width, 4)).copy()
