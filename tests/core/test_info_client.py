"""Tests for the InfoClient (``core/info.py``).

Driven against the extended FakeSeedLinkServer with INFO support. Each
test spins up an isolated fake server with a known stations / streams
configuration and exercises one path through the client:

  * Stations / Streams / ID parsing into typed dataclasses.
  * Server-side filtering for ``fetch_streams(network=…, station=…)``.
  * Bounded wall-clock deadline (``info_silent_mode`` fake).
  * Cooperative cancellation via ``CancellationToken``.
  * Protocol-error path on malformed XML.
  * Real-network timeout classification against an unrouted host.

The async / Qt loop fixtures are reused from
``tests/core/test_seedlink_worker``; this file otherwise stays free of
Qt so it can run on a headless CI container without a display.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from echosmonitor.core.exceptions import (
    InfoCanceled,
    InfoError,
    InfoProtocolError,
    InfoTimeout,
)
from echosmonitor.core.info import (
    CancellationToken,
    ServerIdentity,
    StationInfo,
    StreamInfo,
    fetch,
    fetch_streams,
)
from tests.core.fakes import (
    FakeSeedLinkServer,
    FakeSeedLinkServerConfig,
    FakeStation,
    FakeStream,
)
from tests.core.test_seedlink_worker import _LoopThread, loop_thread  # noqa: F401

# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def make_fake_server(
    loop_thread: _LoopThread,  # noqa: F811  pytest fixture parameter shadows import
) -> Iterator[Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer]]:
    """Factory yielding OS-assigned-port fake servers, torn down on exit.

    Same shape as the fixture in ``test_streaming_engine_multi`` but
    duplicated here so the InfoClient tests stay independent of the
    Qt-heavy multi-device test module.
    """
    started: list[FakeSeedLinkServer] = []

    def _factory(cfg: FakeSeedLinkServerConfig) -> FakeSeedLinkServer:
        server = FakeSeedLinkServer(config=cfg)
        loop_thread.submit(server.start()).result(timeout=2.0)
        started.append(server)
        return server

    yield _factory

    for server in started:
        with contextlib.suppress(Exception):
            loop_thread.submit(server.stop()).result(timeout=3.0)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_fetch_stations_parses_typed_dataclasses(
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """STATIONS XML → ``list[StationInfo]`` with the configured fields."""
    cfg = FakeSeedLinkServerConfig(
        stations=(
            FakeStation(
                network="IU",
                station="ANMO",
                description="Albuquerque NM",
                latitude=34.945,
                longitude=-106.457,
            ),
            FakeStation(network="IV", station="MILN", description="Milan IT"),
        ),
    )
    server = make_fake_server(cfg)

    result = fetch(server.host, server.port, "STATIONS", timeout_s=5.0)
    assert isinstance(result, list)
    assert len(result) == 2

    by_key = {(s.network, s.station): s for s in result}
    anmo = by_key[("IU", "ANMO")]
    assert isinstance(anmo, StationInfo)
    assert anmo.description == "Albuquerque NM"
    assert anmo.latitude == pytest.approx(34.945)
    assert anmo.longitude == pytest.approx(-106.457)

    miln = by_key[("IV", "MILN")]
    assert miln.description == "Milan IT"
    # Latitude/longitude were not set on the fake → should round-trip as None.
    assert miln.latitude is None
    assert miln.longitude is None


def test_fetch_streams_filtered_by_station(
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """``fetch_streams(network=, station=)`` returns only the matching rows."""
    cfg = FakeSeedLinkServerConfig(
        streams=(
            FakeStream(network="IU", station="ANMO", location="00", channel="BHZ"),
            FakeStream(network="IU", station="ANMO", location="00", channel="BHN"),
            FakeStream(network="IU", station="ANMO", location="00", channel="BHE"),
            FakeStream(network="IU", station="COLA", location="00", channel="BHZ"),
            FakeStream(network="IV", station="MILN", location="", channel="HHZ"),
        ),
    )
    server = make_fake_server(cfg)

    streams = fetch_streams(server.host, server.port, network="IU", station="ANMO", timeout_s=5.0)
    assert all(isinstance(s, StreamInfo) for s in streams)
    assert {s.channel for s in streams} == {"BHZ", "BHN", "BHE"}
    assert {(s.network, s.station) for s in streams} == {("IU", "ANMO")}


def test_fetch_id_parses_server_identity(
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """ID XML → ``ServerIdentity`` with version, organization, capabilities."""
    cfg = FakeSeedLinkServerConfig()
    server = make_fake_server(cfg)

    result = fetch(server.host, server.port, "ID", timeout_s=5.0)
    assert isinstance(result, ServerIdentity)
    assert "FakeSeedLink" in result.version
    assert result.organization == "FakeOrg"
    # capabilities is a tuple — order-preserving — so check membership.
    assert "multistation" in result.capabilities
    assert "info:streams" in result.capabilities


def test_fetch_timeout(
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """Server accepts the connection but never replies → ``InfoTimeout``.

    The slack accounts for obspy's internal ``time.sleep(0.5)`` between
    recv attempts inside ``collect()``: a 1.5 s deadline can land at
    ~2.0 s of wall clock before the watchdog flips terminate_flag and
    the next collect() iteration unwinds.
    """
    cfg = FakeSeedLinkServerConfig(info_silent_mode=True)
    server = make_fake_server(cfg)

    timeout_s = 1.0
    slack_s = 1.5
    t0 = time.monotonic()
    with pytest.raises(InfoTimeout):
        fetch(server.host, server.port, "STATIONS", timeout_s=timeout_s)
    elapsed = time.monotonic() - t0
    assert elapsed < timeout_s + slack_s, (
        f"InfoTimeout raised after {elapsed:.2f}s, expected < {timeout_s + slack_s:.2f}s"
    )


def test_fetch_canceled_mid_request(
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """``cancel.set()`` mid-request raises ``InfoCanceled`` quickly."""
    cfg = FakeSeedLinkServerConfig(info_silent_mode=True)
    server = make_fake_server(cfg)

    cancel = CancellationToken()

    def _cancel_after(delay_s: float) -> None:
        time.sleep(delay_s)
        cancel.set()

    threading.Thread(target=_cancel_after, args=(0.1,), daemon=True).start()

    t0 = time.monotonic()
    with pytest.raises(InfoCanceled):
        fetch(
            server.host,
            server.port,
            "STATIONS",
            timeout_s=10.0,
            cancel=cancel,
        )
    elapsed = time.monotonic() - t0
    # Cancel should propagate within ~one obspy collect-loop sleep
    # (0.5 s) plus the watchdog poll period; budget 1.5 s comfortably.
    assert elapsed < 1.5, f"InfoCanceled raised after {elapsed:.2f}s, expected < 1.5s"


def test_fetch_bad_xml_raises_protocol_error(
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """Server replies with malformed XML → ``InfoProtocolError``."""
    cfg = FakeSeedLinkServerConfig(info_bad_xml=b"<not really xml")
    server = make_fake_server(cfg)

    with pytest.raises(InfoProtocolError):
        fetch(server.host, server.port, "STATIONS", timeout_s=5.0)


def _blackhole_route_available(host: str, port: int) -> bool:
    """Return True only if ``host:port`` actually SYN-blackholes.

    Some sandboxes (notably containers with strict egress rules) reply
    with ICMP unreachable instantly rather than dropping the SYN, which
    surfaces as ``"refused"`` or ``"unknown"`` rather than ``"timeout"``
    and would falsify the test. A 0.1 s probe is enough to distinguish
    "instantly classified" from "kernel has to wait".
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.1)
    try:
        sock.connect((host, port))
    except TimeoutError:
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    return False


@pytest.mark.skipif(
    "CI" in os.environ,
    reason="CI may not expose a real SYN-blackhole route to 10.255.255.1",
)
def test_fetch_against_unrouted_host_classified_timeout() -> None:
    """Real socket against a blackholed host → ``InfoError(kind='timeout')``.

    Mirrors the manual test in MANUAL_TESTS.md — verifies the
    InfoClient's preflight bound matches the streaming worker's, so a
    blackholed Stations-dock fetch never hangs the GUI for the OS
    default ``tcp_syn_retries`` window.
    """
    blackhole_host = "10.255.255.1"
    blackhole_port = 18000
    if not _blackhole_route_available(blackhole_host, blackhole_port):
        pytest.skip(
            f"{blackhole_host}:{blackhole_port} is not actually blackholed "
            "in this sandbox; the timeout-classification path can't be "
            "exercised here."
        )

    timeout_s = 1.5
    t0 = time.monotonic()
    with pytest.raises((InfoTimeout, InfoError)) as exc_info:
        fetch(blackhole_host, blackhole_port, "STATIONS", timeout_s=timeout_s)
    elapsed = time.monotonic() - t0
    assert elapsed < timeout_s + 1.0, f"timeout took {elapsed:.2f}s, expected ~{timeout_s}s"
    # Both InfoTimeout (subclass) and InfoError(kind='timeout') are
    # acceptable; assert the kind so a future refactor can't quietly
    # drop the classification.
    raised = exc_info.value
    assert isinstance(raised, InfoError)
    assert raised.kind == "timeout"
