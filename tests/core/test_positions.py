"""Tests for the shared device-position resolver (M4-A, rule 16).

Mirrors ``test_echos_status.py``: the worker lives on the resolver's
real QThread, requests cross the boundary queued exactly as production
drives them, and results come back the same way. The device is the M1-A
fake firmware behind ``httpx.MockTransport``, injected through the
resolver's ``client_factory``.

Per the qt-worker-threading skill, new workers must pin a
start→stop→start cycle (fresh resolver per cycle — shutdown is
terminal, like the loaders) and a stop-during-busy-slot case (a
transport that hangs forever inside asyncio — only the task-cancel path
in ``stop()`` can unwind it within the bounded join).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import httpx

from echosmonitor.core.echos_api import EchosApiClient
from echosmonitor.core.positions import (
    PositionQuery,
    PositionResolver,
    ResolvedPosition,
    haversine_m,
    local_east_north,
    stationxml_coordinates,
)
from tests.core.echos_fake import FakeEchosFirmware

_RESOLVE_DEADLINE_MS = 5000


class _GatedTransport(httpx.AsyncBaseTransport):
    """Serves the fake firmware, but holds every request until released.

    ``entered`` counts requests that reached the device (set-checked from
    the test thread); the gate is polled with short awaits so the
    worker's asyncio loop stays live and the fetch stays cancellable.
    """

    def __init__(self, fw: FakeEchosFirmware) -> None:
        self.fw = fw
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.request_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        self.entered.set()
        # Deliberate poll (ASYNC110): the gate is a *threading* Event set
        # from the test thread, and each worker fetch runs in its own
        # asyncio.run loop — an asyncio.Event cannot cross either boundary.
        while not self.gate.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        return self.fw.handle(request)


class _HangingTransport(httpx.AsyncBaseTransport):
    """A device that accepts the request and never answers.

    httpx timeout config is enforced by the real transport (httpcore),
    so a mock sleeping forever is NOT bounded by the client's timeouts —
    only the worker's task-cancel can unwind it (the sharpest
    stop-during-busy probe, same as the status-poller suite).
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60.0)
        raise AssertionError("unreachable")  # pragma: no cover


def _query(**kwargs: Any) -> PositionQuery:
    defaults: dict[str, Any] = {
        "name": "echos-field-01",
        "host": "echos-test.local",
        "http_port": 80,
        "has_rest": True,
        "override": None,
    }
    defaults.update(kwargs)
    return PositionQuery(**defaults)


def _factory_for(fw: FakeEchosFirmware) -> Any:
    def factory(query: PositionQuery) -> EchosApiClient:
        return EchosApiClient(
            query.host,
            query.http_port,
            transport=fw.transport,
            get_retries=0,
            retry_delay_s=0.0,
        )

    return factory


def _resolver(fw: FakeEchosFirmware) -> PositionResolver:
    return PositionResolver(client_factory=_factory_for(fw))


# ----------------------------------------------------------------------
# Pure helper
# ----------------------------------------------------------------------


def test_stationxml_coordinates_parses_first_station() -> None:
    coords = stationxml_coordinates(FakeEchosFirmware().stationxml)
    assert coords == (45.4, 11.9, 20.0)


def test_stationxml_coordinates_rejects_garbage() -> None:
    assert stationxml_coordinates("this is not xml") is None


def test_stationxml_coordinates_treats_null_island_as_absent() -> None:
    xml = FakeEchosFirmware().stationxml.replace("45.4", "0.0").replace("11.9", "0.0")
    assert stationxml_coordinates(xml) is None


def test_haversine_known_distances() -> None:
    # One degree of longitude on the equator ≈ 111.195 km (mean-radius arc).
    assert abs(haversine_m(0.0, 0.0, 0.0, 1.0) - 111_195.0) < 50.0
    # Symmetric, zero on identity.
    assert haversine_m(45.4, 11.9, 45.4, 11.9) == 0.0
    assert haversine_m(45.0, 11.0, 45.1, 11.2) == haversine_m(45.1, 11.2, 45.0, 11.0)
    # ~0.01° of longitude at 45°N ≈ 786 m.
    assert abs(haversine_m(45.0, 11.0, 45.0, 11.01) - 786.0) < 2.0


def test_local_east_north_projection() -> None:
    east, north = local_east_north(45.0, 11.01, 45.0, 11.0)
    assert abs(east - 786.0) < 2.0  # matches the haversine at array scale
    assert abs(north) < 1e-9
    east, north = local_east_north(45.01, 11.0, 45.0, 11.0)
    assert abs(east) < 1e-9
    assert abs(north - 1112.0) < 2.0
    assert local_east_north(45.0, 11.0, 45.0, 11.0) == (0.0, 0.0)


# ----------------------------------------------------------------------
# Source priority (decision log 2026-06-12: override > stationxml > gnss)
# ----------------------------------------------------------------------


def test_override_wins_without_any_network(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        query = _query(override=(46.0, 12.5, 100.0))
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS) as blocker:
            resolver.configure((query,))
        (position,) = blocker.args
        assert isinstance(position, ResolvedPosition)
        assert position.source == "override"
        assert (position.latitude, position.longitude, position.elevation_m) == (46.0, 12.5, 100.0)
        assert fw.requests == []  # rule 16: the override never touches the device
    finally:
        resolver.shutdown()


def test_stationxml_is_the_primary_network_source(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS) as blocker:
            resolver.configure((_query(),))
        (position,) = blocker.args
        assert isinstance(position, ResolvedPosition)
        assert position.source == "stationxml"
        assert (position.latitude, position.longitude, position.elevation_m) == (45.4, 11.9, 20.0)
        assert ("GET", "/api/stationxml") in fw.requests
        assert ("GET", "/api/status") not in fw.requests  # no fallback needed
        assert resolver.position("echos-field-01") == position  # cached
    finally:
        resolver.shutdown()


def test_gnss_fallback_when_stationxml_endpoint_fails(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    fw.raw_responses["/api/stationxml"] = httpx.Response(404, json={"error": "not_found"})
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS) as blocker:
            resolver.configure((_query(),))
        (position,) = blocker.args
        assert isinstance(position, ResolvedPosition)
        assert position.source == "gnss"
        assert (position.latitude, position.longitude, position.elevation_m) == (45.4, 11.9, 20.0)
    finally:
        resolver.shutdown()


def test_gnss_fallback_when_stationxml_has_null_island_coords(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    fw.stationxml = fw.stationxml.replace("45.4", "0.0").replace("11.9", "0.0")
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS) as blocker:
            resolver.configure((_query(),))
        (position,) = blocker.args
        assert isinstance(position, ResolvedPosition)
        assert position.source == "gnss"
        assert position.latitude == 45.4
    finally:
        resolver.shutdown()


# ----------------------------------------------------------------------
# Failure vocabulary
# ----------------------------------------------------------------------


def test_no_rest_and_no_override_is_unavailable(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionFailed, timeout=_RESOLVE_DEADLINE_MS) as blocker:
            resolver.configure((_query(name="generic-seedlink", has_rest=False),))
        device, kind, _message = blocker.args
        assert device == "generic-seedlink"
        assert kind == "unavailable"
        assert fw.requests == []
        assert resolver.position("generic-seedlink") is None
    finally:
        resolver.shutdown()


def test_device_with_no_position_anywhere_is_unavailable(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    fw.raw_responses["/api/stationxml"] = httpx.Response(404, json={"error": "not_found"})
    fw.status["position"] = None
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionFailed, timeout=_RESOLVE_DEADLINE_MS) as blocker:
            resolver.configure((_query(),))
        _device, kind, _message = blocker.args
        assert kind == "unavailable"
    finally:
        resolver.shutdown()


def test_unreachable_device_fails_fast_without_status_fallback(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    fw.flaky["/api/stationxml"] = 10**6  # unreachable forever
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionFailed, timeout=_RESOLVE_DEADLINE_MS) as blocker:
            resolver.configure((_query(),))
        device, kind, _message = blocker.args
        assert device == "echos-field-01"
        assert kind == "unreachable"
        # A dead host must not burn a second timeout on /api/status.
        assert ("GET", "/api/status") not in fw.requests
    finally:
        resolver.shutdown()


def test_one_failing_device_does_not_block_the_next(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    fw.flaky["/api/stationxml"] = 1  # first GET fails, later ones succeed
    resolver = _resolver(fw)
    try:
        bad = _query(name="bad-device")
        good = _query(name="good-device")
        with qtbot.waitSignals(
            [resolver.positionFailed, resolver.positionResolved],
            timeout=_RESOLVE_DEADLINE_MS,
        ):
            resolver.configure((bad, good))
        assert resolver.position("bad-device") is None
        assert resolver.position("good-device") is not None
    finally:
        resolver.shutdown()


# ----------------------------------------------------------------------
# Cache + refresh-on-demand semantics
# ----------------------------------------------------------------------


def test_configure_with_unchanged_query_serves_from_cache(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        query = _query()
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
            resolver.configure((query,))
        requests_after_first = len(fw.requests)
        resolver.configure((query,))  # identical set → nothing to resolve
        qtbot.wait(300)
        assert len(fw.requests) == requests_after_first
        assert resolver.position("echos-field-01") is not None
    finally:
        resolver.shutdown()


def test_refresh_refetches_every_device(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
            resolver.configure((_query(),))
        requests_after_first = len(fw.requests)
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
            resolver.refresh()
        assert len(fw.requests) > requests_after_first
    finally:
        resolver.shutdown()


def test_changed_query_drops_cache_and_reresolves(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
            resolver.configure((_query(host="old-host.local"),))
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
            resolver.configure((_query(host="new-host.local"),))
        assert resolver.position("echos-field-01") is not None
    finally:
        resolver.shutdown()


def test_removed_device_is_dropped_from_cache(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
            resolver.configure((_query(),))
        assert resolver.position("echos-field-01") is not None
        resolver.configure(())
        assert resolver.position("echos-field-01") is None
        assert resolver.positions() == {}
    finally:
        resolver.shutdown()


def test_stale_generation_and_unknown_device_results_are_ignored(qtbot: Any) -> None:
    """Late worker results from a superseded configure must not resurrect
    cache entries (latest-wins receipt guard). Driven directly on the GUI
    thread — the guards are pure facade logic."""
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        resolver.configure((_query(override=(46.0, 12.5, 100.0)),))
        stale = ResolvedPosition(
            device="echos-field-01",
            latitude=1.0,
            longitude=2.0,
            elevation_m=3.0,
            source="gnss",
            resolved_at=0.0,
        )
        resolver._on_resolved(stale, resolver._generation - 1)  # stale generation
        assert resolver.position("echos-field-01") != stale
        unknown = ResolvedPosition(
            device="ghost-device",
            latitude=1.0,
            longitude=2.0,
            elevation_m=3.0,
            source="gnss",
            resolved_at=0.0,
        )
        resolver._on_resolved(unknown, resolver._generation)  # not configured
        assert resolver.position("ghost-device") is None
    finally:
        resolver.shutdown()


def test_failed_refresh_keeps_last_known_position(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
            resolver.configure((_query(),))
        known = resolver.position("echos-field-01")
        assert known is not None
        fw.flaky["/api/stationxml"] = 10**6
        with qtbot.waitSignal(resolver.positionFailed, timeout=_RESOLVE_DEADLINE_MS):
            resolver.refresh()
        assert resolver.position("echos-field-01") == known
    finally:
        resolver.shutdown()


def test_configure_to_empty_aborts_in_flight_sweep(qtbot: Any) -> None:
    """Removing devices mid-sweep must STOP the network work, not merely
    discard its results (review finding 2026-06-12: the supersede write
    must happen even when the new dispatch set is empty)."""
    gated = _GatedTransport(FakeEchosFirmware())
    fw_second = FakeEchosFirmware()

    def factory(query: PositionQuery) -> EchosApiClient:
        transport = gated if query.name == "gated-device" else fw_second.transport
        return EchosApiClient(
            query.host, query.http_port, transport=transport, get_retries=0, retry_delay_s=0.0
        )

    resolver = PositionResolver(client_factory=factory)
    try:
        resolver.configure((_query(name="gated-device"), _query(name="second-device")))
        assert gated.entered.wait(3.0), "first device's fetch never started"
        resolver.configure(())  # user removed every device mid-sweep
        gated.gate.set()
        qtbot.wait(500)
        # The sweep aborted between devices: the second was never fetched.
        assert fw_second.requests == []
        assert resolver.positions() == {}
    finally:
        resolver.shutdown()


def test_rapid_refreshes_coalesce_to_one_sweep(qtbot: Any) -> None:
    """N refresh calls while a sweep is in flight run ONE fresh sweep,
    not N (review finding 2026-06-12: refresh bumps the generation)."""
    gated = _GatedTransport(FakeEchosFirmware())

    def factory(query: PositionQuery) -> EchosApiClient:
        return EchosApiClient(
            query.host, query.http_port, transport=gated, get_retries=0, retry_delay_s=0.0
        )

    resolver = PositionResolver(client_factory=factory)
    try:
        resolver.configure((_query(),))
        assert gated.entered.wait(3.0), "initial fetch never started"
        resolver.refresh()
        resolver.refresh()
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
            gated.gate.set()
        qtbot.wait(300)
        # Initial sweep + exactly ONE refresh sweep; the superseded
        # middle sweep aborted before touching the device.
        assert gated.request_count == 2
        assert resolver.position("echos-field-01") is not None
    finally:
        resolver.shutdown()


# ----------------------------------------------------------------------
# Worker lifecycle (qt-worker-threading skill requirements)
# ----------------------------------------------------------------------


def test_shutdown_is_terminal(qtbot: Any) -> None:
    """A dispatch after shutdown must not restart the thread into a
    stopped worker (review finding 2026-06-12)."""
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    resolver.shutdown()
    resolver.configure((_query(),))
    qtbot.wait(200)
    assert not resolver._thread.isRunning()
    assert fw.requests == []


def test_start_stop_start_cycle(qtbot: Any) -> None:
    """Two full resolver lifecycles against the same fake (skill §7)."""
    fw = FakeEchosFirmware()
    for _cycle in (1, 2):
        resolver = _resolver(fw)
        try:
            with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
                resolver.configure((_query(),))
        finally:
            resolver.shutdown()
            assert not resolver._thread.isRunning()


def test_shutdown_interrupts_in_flight_fetch(qtbot: Any) -> None:
    """``shutdown()`` from the GUI thread unwinds a hung fetch promptly.

    The hanging transport would block the resolve slot for 60 s; the
    bounded join inside ``shutdown`` succeeds only if ``stop()``
    actually cancels the in-flight asyncio task (rule 7).
    """

    def factory(query: PositionQuery) -> EchosApiClient:
        return EchosApiClient(
            query.host, query.http_port, transport=_HangingTransport(), get_retries=0
        )

    resolver = PositionResolver(client_factory=factory)
    resolver.configure((_query(),))
    # Wait until the fetch is genuinely in flight — the cancel must reach
    # an installed task, not win a race against fetch start.
    qtbot.waitUntil(lambda: resolver._worker._in_flight is not None, timeout=3000)
    resolver.shutdown()
    assert not resolver._thread.isRunning()


def test_bad_resolve_payload_is_ignored(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    resolver = _resolver(fw)
    try:
        # Garbage payloads must not crash the worker thread (rule 4
        # isinstance guard) — and a valid configure afterwards works.
        resolver._resolveRequested.emit("not a tuple", 1)
        resolver._resolveRequested.emit(("not", "queries"), 1)
        with qtbot.waitSignal(resolver.positionResolved, timeout=_RESOLVE_DEADLINE_MS):
            resolver.configure((_query(),))
    finally:
        resolver.shutdown()
