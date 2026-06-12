"""Shared device-position resolver (rule 16) — M4-A.

ONE instance of :class:`PositionResolver` (owned by the main window)
serves every position consumer: the Map tab (M4-B) and the multi-device
HVSR geometry (M5). Both read the same cache and the same signals, so a
device can never have two different positions in two views.

Skills: ``echos-rest-api`` (both sources are public credential-less
GETs — this module can never trip the auth lockout) and
``qt-worker-threading`` (the worker copies the ``EchosStatusWorker``
canon: queued request slot driving ``asyncio.run``, plain-method
``stop()`` with the lock-registered task-cancel nudge; the facade copies
the ``ArchiveDetailLoader`` owner shape with a latest-wins generation).

Source priority (decision log 2026-06-12):

1. **Manual override** from config (``echos.position_override``) — wins
   unconditionally, resolved without any network round-trip.
2. **StationXML** station coordinates (``GET /api/stationxml``) — the
   rule-16 canonical source; on the real firmware this is a 6-decimal
   snapshot of the GNSS fix taken when the document was generated.
3. **Live GNSS** from ``GET /api/status`` ``position`` — fallback when
   StationXML is unavailable, unparseable, or carries no usable
   coordinates (lat/lon exactly 0/0 = "null island" = the firmware had
   no fix; treated as absent).

A device with neither an override nor a REST API (no ``echos`` config
section) is reported ``unavailable`` — it simply has no position, which
is honest state, not an error.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

import structlog
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from echosmonitor.core.echos_api import EchosApiClient
from echosmonitor.core.exceptions import (
    EchosApiError,
    EchosTimeout,
    EchosUnreachable,
)

_log = structlog.get_logger(__name__)

# Bounded join wait on shutdown (rule 7). An in-flight resolve is
# cancelled via the asyncio task nudge, so the join only has to cover
# slot unwinding, never a full HTTP timeout.
_THREAD_JOIN_MS = 4000

# Where a resolved position came from (rendered by the Map tab so the
# user can tell a surveyed override from a live GNSS fix).
PositionSource = Literal["override", "stationxml", "gnss"]

# Failure vocabulary for ``positionFailed``: the transport classes of
# ``EchosErrorKind`` plus ``unavailable`` ("the device answered but has
# no position anywhere", or "this device has no position source at
# all"). Callers branch on this closed set, never on message text.
PositionFailureKind = Literal[
    "auth_failed",
    "locked_out",
    "unreachable",
    "timeout",
    "protocol",
    "unavailable",
]


@dataclass(frozen=True, slots=True)
class PositionQuery:
    """One device the resolver should position (GUI → worker payload).

    Built by the GUI from ``DeviceConfig`` (mirrors ``EchosPollTarget``
    construction): ``has_rest`` is True iff the device has an ``echos``
    config section; ``override`` is its ``position_override`` flattened
    to ``(lat, lon, elev_m)``. Frozen so a tuple of these crosses the
    thread boundary via a queued ``Signal(object)`` safely (rule 4).
    """

    name: str
    host: str
    http_port: int = 80
    has_rest: bool = True
    override: tuple[float, float, float] | None = None


@dataclass(frozen=True, slots=True)
class ResolvedPosition:
    """One device's resolved position (worker → GUI wire payload).

    ``source`` tags the provenance (rule 16: metadata, not guesswork).
    ``resolved_at`` is ``time.monotonic()`` at resolution — for
    staleness arithmetic on the GUI side, not wall-clock display.
    """

    device: str
    latitude: float
    longitude: float
    elevation_m: float
    source: PositionSource
    resolved_at: float


def stationxml_coordinates(xml: str) -> tuple[float, float, float] | None:
    """Extract ``(lat, lon, elev_m)`` from a StationXML document's first
    positioned station, or ``None`` when the document is unparseable or
    carries no usable coordinates.

    Worker-thread only by convention: obspy parsing is file/CPU work
    (rule 1; same reasoning as ``echos_device_worker._parse_channels``).
    Lat/lon exactly 0/0 is the firmware's no-fix placeholder ("null
    island") and is treated as absent so the caller falls back to the
    live GNSS fix.
    """
    from obspy import read_inventory

    try:
        inventory = read_inventory(io.BytesIO(xml.encode("utf-8")), format="STATIONXML")
    except Exception as exc:
        _log.warning("position_stationxml_unparseable", error_type=type(exc).__name__)
        return None
    for network in inventory:
        for station in network:
            latitude = float(station.latitude) if station.latitude is not None else 0.0
            longitude = float(station.longitude) if station.longitude is not None else 0.0
            if latitude == 0.0 and longitude == 0.0:
                continue
            elevation = float(station.elevation) if station.elevation is not None else 0.0
            return (latitude, longitude, elevation)
    return None


# Mean Earth radius (IUGG). Array-scale distances (metres to a few km)
# are insensitive to the ellipsoid; the haversine error at that scale is
# far below GNSS accuracy.
_EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS84 points.

    Pure (no Qt, no I/O) — shared by the Map tab's inter-device
    distance readout (M4-B) and the M5 array-geometry layer.
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    half_dphi = (phi2 - phi1) / 2.0
    half_dlambda = math.radians(lon2 - lon1) / 2.0
    a = math.sin(half_dphi) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(half_dlambda) ** 2
    return 2.0 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def local_east_north(
    lat: float, lon: float, lat0: float, lon0: float
) -> tuple[float, float]:
    """Project a point onto a local tangent frame centred on ``(lat0, lon0)``.

    Returns ``(east_m, north_m)``. Equirectangular small-area
    approximation — exact enough (≪ 1 m error) at deployment scale
    (a few km), which is all the Map tab and array HVSR ever see.
    The longitude delta is normalised into [-180°, 180°) so a pair
    straddling the antimeridian projects sanely (review note: a
    centroid averaged from raw longitudes near ±180° is still poor —
    don't deploy an array across the date line and expect a pretty
    map). Pure (no Qt, no I/O).
    """
    dlon = math.remainder(lon - lon0, 360.0)
    east = _EARTH_RADIUS_M * math.radians(dlon) * math.cos(math.radians(lat0))
    north = _EARTH_RADIUS_M * math.radians(lat - lat0)
    return east, north


@dataclass(frozen=True, slots=True)
class StationGeometry:
    """Resolved positions + pairwise distances for a station set (M4-C).

    The M5 array-HVSR hook: the multi-device UI, the map f0 overlay and
    the multi-station report all consume this one shape. ``devices``
    holds only stations WITH a resolved position, in the caller's order;
    ``distances_m`` is keyed by lexicographically ordered name pairs —
    use :meth:`distance` for order-free lookup. Pure data (no Qt).
    """

    devices: tuple[str, ...]
    positions: dict[str, ResolvedPosition]
    distances_m: dict[tuple[str, str], float]

    def distance(self, a: str, b: str) -> float:
        """Great-circle metres between two member stations (order-free).

        Raises ``KeyError`` when either name is not a positioned member —
        an absent station silently measuring 0 m away would be the kind
        of quiet lie rule 16 exists to prevent.
        """
        if a == b:
            if a not in self.positions:
                raise KeyError(a)
            return 0.0
        key = (a, b) if a <= b else (b, a)
        return self.distances_m[key]


def distance_matrix(
    positions: Mapping[str, ResolvedPosition],
) -> dict[tuple[str, str], float]:
    """Pairwise great-circle distances, one entry per unordered pair.

    Keys are ``(a, b)`` with ``a < b`` lexicographically. Pure.
    """
    matrix: dict[tuple[str, str], float] = {}
    names = sorted(positions)
    for index, a in enumerate(names):
        for b in names[index + 1 :]:
            pa, pb = positions[a], positions[b]
            matrix[(a, b)] = haversine_m(pa.latitude, pa.longitude, pb.latitude, pb.longitude)
    return matrix


def station_geometry(
    positions: Mapping[str, ResolvedPosition],
    devices: Iterable[str] | None = None,
) -> StationGeometry:
    """Build the geometry snapshot for ``devices`` (default: all positioned).

    Names without a resolved position are silently excluded — the caller
    reads ``devices`` to learn what actually made it in (M5 must render
    "station X has no position" from that difference, not guess).
    """
    if devices is None:
        names = tuple(positions)
    else:
        names = tuple(dict.fromkeys(n for n in devices if n in positions))
    subset = {name: positions[name] for name in names}
    return StationGeometry(
        devices=names, positions=subset, distances_m=distance_matrix(subset)
    )


def _default_client_factory(query: PositionQuery) -> EchosApiClient:
    # No password: both position sources are public GETs (rule 15 —
    # this path can never contribute to the device's auth lockout).
    return EchosApiClient(query.host, query.http_port)


class _PositionWorker(QObject):
    """Lives on the resolver's dedicated thread; never raises across it.

    ``resolve`` processes a tuple of queries sequentially, emitting one
    ``resolved``/``failed`` per device so the map populates
    progressively. ``_active_generation`` is written GIL-atomically by
    the facade (latest-wins, skill §2): the loop aborts between devices
    when superseded, and the facade discards late results by generation.
    """

    resolved = Signal(object, int)  # ResolvedPosition, generation
    # device, kind (closed PositionFailureKind set), message, generation
    failed = Signal(str, str, str, int)

    def __init__(
        self,
        client_factory: Callable[[PositionQuery], EchosApiClient] | None = None,
    ) -> None:
        super().__init__()  # parentless — moveToThread requires no parent
        self._client_factory = client_factory or _default_client_factory
        self._stop_flag = False
        self._active_generation = 0
        # Guards the read-modify-write of ``_in_flight`` so ``stop()``
        # on the GUI thread can't observe a half-installed task
        # (EchosStatusWorker canon).
        self._lock = threading.Lock()
        self._in_flight: tuple[asyncio.AbstractEventLoop, asyncio.Task[object]] | None = None

    # ------------------------------------------------------------------
    # Slot — runs on the worker thread (queued from the facade)
    # ------------------------------------------------------------------
    @Slot(object, int)
    def resolve(self, queries: object, generation: int) -> None:
        """Resolve each query in turn (rule 4 isinstance guard on entry)."""
        if not isinstance(queries, tuple) or not all(
            isinstance(q, PositionQuery) for q in queries
        ):
            _log.warning("position_bad_resolve_payload", payload_type=type(queries).__name__)
            return
        for query in queries:
            if self._stop_flag or generation != self._active_generation:
                _log.debug("position_resolve_superseded", generation=generation)
                return
            self._resolve_one(query, generation)

    # ------------------------------------------------------------------
    # Plain method (NOT a Slot). Callable from any thread.
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Stop resolving and cancel any in-flight fetch. Idempotent."""
        with self._lock:
            self._stop_flag = True
            in_flight = self._in_flight
        if in_flight is not None:
            loop, task = in_flight
            # The loop may finish between the lock release and this
            # call; a closed loop raises RuntimeError — the fetch is
            # already over, which is what we wanted.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)

    # ------------------------------------------------------------------
    # Internals — worker thread only
    # ------------------------------------------------------------------
    def _resolve_one(self, query: PositionQuery, generation: int) -> None:
        if query.override is not None:
            if self._stop_flag:  # same pre-emit check as the network path
                return
            latitude, longitude, elevation = query.override
            self.resolved.emit(
                ResolvedPosition(
                    device=query.name,
                    latitude=latitude,
                    longitude=longitude,
                    elevation_m=elevation,
                    source="override",
                    resolved_at=time.monotonic(),
                ),
                generation,
            )
            return
        if not query.has_rest:
            self.failed.emit(
                query.name,
                "unavailable",
                "device has no REST API and no manual position override",
                generation,
            )
            return
        started = time.monotonic()
        try:
            position = asyncio.run(self._fetch_async(query))
        except asyncio.CancelledError:
            _log.info("position_resolve_canceled", device=query.name)
            return
        except EchosApiError as exc:
            _log.warning(
                "position_resolve_failed",
                device=query.name,
                kind=exc.kind,
                error=str(exc),
                elapsed_s=round(time.monotonic() - started, 3),
            )
            self.failed.emit(query.name, exc.kind, str(exc), generation)
            return
        except Exception as exc:
            _log.exception("position_resolve_unexpected_error", device=query.name, error=str(exc))
            self.failed.emit(
                query.name, "protocol", f"unexpected: {type(exc).__name__}: {exc}", generation
            )
            return
        if self._stop_flag:
            return
        if position is None:
            self.failed.emit(
                query.name,
                "unavailable",
                "no coordinates in StationXML and no GNSS fix in /api/status",
                generation,
            )
            return
        _log.info(
            "position_resolved",
            device=query.name,
            source=position.source,
            elapsed_s=round(time.monotonic() - started, 3),
        )
        self.resolved.emit(position, generation)

    async def _fetch_async(self, query: PositionQuery) -> ResolvedPosition | None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None  # always inside asyncio.run
        with self._lock:
            if self._stop_flag:
                return None
            self._in_flight = (loop, task)
        try:
            async with self._client_factory(query) as client:
                # Sequential on one keep-alive connection — the ESP32
                # serves requests serially (EchosStatusWorker note).
                coordinates: tuple[float, float, float] | None = None
                source: PositionSource = "stationxml"
                xml: str | None = None
                try:
                    xml = await client.get_stationxml()
                except (EchosUnreachable, EchosTimeout):
                    # The device is down — /api/status would only burn a
                    # second timeout on the same dead host.
                    raise
                except EchosApiError as exc:
                    _log.info(
                        "position_stationxml_unavailable", device=query.name, kind=exc.kind
                    )
                if xml is not None:
                    coordinates = stationxml_coordinates(xml)
                if coordinates is None:
                    status = await client.get_status()
                    gnss = status.position
                    if gnss is not None and not (
                        gnss.latitude == 0.0 and gnss.longitude == 0.0
                    ):
                        coordinates = (gnss.latitude, gnss.longitude, gnss.altitude)
                        source = "gnss"
                if coordinates is None:
                    return None
                return ResolvedPosition(
                    device=query.name,
                    latitude=coordinates[0],
                    longitude=coordinates[1],
                    elevation_m=coordinates[2],
                    source=source,
                    resolved_at=time.monotonic(),
                )
        finally:
            with self._lock:
                self._in_flight = None


class PositionResolver(QObject):
    """Owns the position worker + thread; caches per-device results.

    Created and owned by the main window; the Map tab and the
    multi-device HVSR both consume THIS instance (rule 16: one shared
    resolver). All public methods run on the GUI thread.

    Semantics:

    * :meth:`configure` — full replacement of the device set (mirrors
      ``EchosStatusWorker.configure``). Removed devices and devices
      whose query changed drop their cache entry; only devices without
      a cached position are (re)resolved — the cache holds otherwise.
    * :meth:`refresh` / :meth:`refresh_device` — explicit on-demand
      re-resolution; the cached value stays visible until the fresh
      result replaces it (a failed refresh keeps the last known
      position; ``positionFailed`` reports the failure).
    * ``configure`` and ``refresh`` bump the latest-wins generation, so
      a superseded in-flight sweep aborts between devices and its late
      results are discarded on receipt; ``refresh_device`` does not
      (see its docstring).
    """

    positionResolved = Signal(object)  # noqa: N815  # ResolvedPosition (GUI thread)
    # device, kind (PositionFailureKind), message (GUI thread)
    positionFailed = Signal(str, str, str)  # noqa: N815

    # Facade → worker (QueuedConnection → slot body runs on the worker).
    _resolveRequested = Signal(object, int)  # noqa: N815

    def __init__(
        self,
        client_factory: Callable[[PositionQuery], EchosApiClient] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._queries: dict[str, PositionQuery] = {}
        self._cache: dict[str, ResolvedPosition] = {}
        self._generation = 0
        self._shutdown = False

        self._worker = _PositionWorker(client_factory)
        self._thread = QThread()
        self._thread.setObjectName("position-resolver")
        self._worker.moveToThread(self._thread)

        self._resolveRequested.connect(
            self._worker.resolve, Qt.ConnectionType.QueuedConnection
        )
        self._worker.resolved.connect(self._on_resolved, Qt.ConnectionType.QueuedConnection)
        self._worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------
    # GUI-thread API
    # ------------------------------------------------------------------
    def configure(self, queries: tuple[PositionQuery, ...]) -> None:
        """Replace the device set; resolve whatever is not yet cached."""
        new_queries = {q.name: q for q in queries}
        for name in list(self._cache):
            if new_queries.get(name) != self._queries.get(name):
                del self._cache[name]
        self._queries = new_queries
        # Supersede even when nothing is dispatched (empty or fully-cached
        # set): an in-flight sweep must stop fetching removed devices, not
        # just have its results discarded (review finding, 2026-06-12).
        self._supersede()
        pending = tuple(q for q in queries if q.name not in self._cache)
        _log.info(
            "positions_configured", device_count=len(queries), pending_count=len(pending)
        )
        self._dispatch(pending)

    def refresh(self) -> None:
        """Re-resolve every configured device (on-demand, rule 16).

        Bumps the latest-wins generation: a refresh re-resolves the full
        set, so any queued/in-flight sweep is wholly redundant — N rapid
        refresh clicks run one sweep, not N (rule 5; review finding).
        """
        if not self._queries:
            return
        self._supersede()
        self._dispatch(tuple(self._queries.values()))

    def refresh_device(self, name: str) -> None:
        """Re-resolve one configured device; unknown names are a no-op.

        Deliberately does NOT bump the generation: that would discard
        other devices' in-flight results for the cost of avoiding one
        duplicate single-device fetch (benign — last write wins).
        """
        query = self._queries.get(name)
        if query is not None:
            self._dispatch((query,))

    def position(self, name: str) -> ResolvedPosition | None:
        """Last resolved position for one device, or None."""
        return self._cache.get(name)

    def positions(self) -> dict[str, ResolvedPosition]:
        """Snapshot of every resolved position (copy — safe to hold)."""
        return dict(self._cache)

    def geometry(self, devices: Iterable[str] | None = None) -> StationGeometry:
        """Station geometry over the current cache (M4-C, the M5 hook).

        ``devices`` restricts to a selection (e.g. the array-HVSR device
        multi-select); names without a resolved position are excluded —
        compare ``geometry.devices`` against the request to find them.
        """
        return station_geometry(self._cache, devices)

    def shutdown(self) -> None:
        """Tear down for app exit — stop the worker and join the thread.

        Terminal: the worker's stop flag is sticky, so later dispatches
        are refused (logged) instead of restarting the thread into a
        worker that would silently no-op every resolve.

        No release barrier needed: this worker owns no QTimer (the
        ``EchosStatusWorker`` barrier exists only for its poll timer).
        """
        self._shutdown = True
        self._worker.stop()
        if self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(_THREAD_JOIN_MS):
                _log.warning("positions_thread_join_timeout")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _supersede(self) -> None:
        """Bump the generation and write it to the worker GIL-atomically
        (skill §2 latest-wins): an in-flight resolve loop for an older
        generation aborts between devices, and its late results are
        discarded on receipt by the generation guard."""
        self._generation += 1
        self._worker._active_generation = self._generation

    def _dispatch(self, queries: tuple[PositionQuery, ...]) -> None:
        if self._shutdown:
            _log.warning("positions_dispatch_after_shutdown")
            return
        if not queries:
            return
        if not self._thread.isRunning():
            self._thread.start()
        self._resolveRequested.emit(queries, self._generation)

    @Slot(object, int)
    def _on_resolved(self, payload: object, generation: int) -> None:
        if not isinstance(payload, ResolvedPosition):  # rule 4 guard
            return
        # _shutdown: a result already queued to the GUI thread when
        # shutdown() ran must not re-emit into torn-down consumers.
        if self._shutdown or generation != self._generation or payload.device not in self._queries:
            _log.debug("position_result_stale", device=payload.device)
            return
        self._cache[payload.device] = payload
        self.positionResolved.emit(payload)

    @Slot(str, str, str, int)
    def _on_failed(self, device: str, kind: str, message: str, generation: int) -> None:
        if self._shutdown or generation != self._generation or device not in self._queries:
            _log.debug("position_failure_stale", device=device)
            return
        self.positionFailed.emit(device, kind, message)


__all__ = [
    "PositionFailureKind",
    "PositionQuery",
    "PositionResolver",
    "PositionSource",
    "ResolvedPosition",
    "StationGeometry",
    "distance_matrix",
    "haversine_m",
    "local_east_north",
    "station_geometry",
    "stationxml_coordinates",
]
