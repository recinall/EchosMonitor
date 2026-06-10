"""Integration test for :class:`InfoWorker` on a real ``QThread``.

The unit-level station-browser tests (``tests/gui/test_station_browser.py``)
stub the worker on the test thread, so they exercise the UI signal-handling
logic but cannot catch a deadlock between the GUI's queued-connection
emit and the worker's own ``run()`` consumer.

This module pins the cross-thread contract end-to-end:

* A fresh :class:`InfoWorker` lives on a real ``QThread``.
* Requests cross the thread boundary via Qt's ``QueuedConnection`` —
  exactly the way the production GUI talks to it.
* Replies cross the boundary in the other direction the same way.

A queued request that never dispatches (because the worker thread's
event loop is parked) would time out in :meth:`qtbot.waitSignal` and
fail the test — which is what code-reviewer flagged on the first
implementation, where ``run()`` blocked the worker thread inside
``queue.get()`` and the queued ``requestStations`` slot could not fire.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from PySide6.QtCore import QObject, Qt, QThread, Signal

from echosmonitor.core.info import StationInfo, StreamInfo
from echosmonitor.core.info_worker import InfoWorker
from tests.core.fakes import (
    FakeSeedLinkServer,
    FakeSeedLinkServerConfig,
    FakeStation,
    FakeStream,
)
from tests.core.test_seedlink_worker import _LoopThread, loop_thread  # noqa: F401

_FETCH_DEADLINE_MS = 5000
_THREAD_JOIN_MS = 2000


@pytest.fixture
def make_fake_server(
    loop_thread: _LoopThread,  # noqa: F811  pytest fixture parameter shadows import
) -> Iterator[Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer]]:
    """OS-assigned-port fake-server factory (mirrors the InfoClient tests).

    The fixture is duplicated rather than shared with
    ``tests/core/test_info_client.py`` because pytest module-scoped
    sharing across both files requires a conftest layer that's not
    necessary for this minimal integration check.
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


class _Trigger(QObject):
    """Test-thread QObject used to emit queued request signals into the worker.

    Calling ``worker.requestStations(...)`` directly from the test
    thread would invoke the slot synchronously on the test thread,
    which is exactly NOT what the production code does. The trigger's
    signals are connected to the worker's slots via
    ``Qt.ConnectionType.QueuedConnection`` so the call crosses the
    thread boundary the same way the GUI's queued connection does.
    """

    stationsRequested = Signal(str, str, str, int)  # noqa: N815
    streamsRequested = Signal(str, str, str, int, str, str)  # noqa: N815
    idRequested = Signal(str, str, str, int)  # noqa: N815


def _spawn_worker(qtbot: Any) -> tuple[InfoWorker, QThread, _Trigger]:
    """Build an InfoWorker on a real QThread, with a trigger to drive it.

    Returns the worker, its thread, and the test-thread trigger. The
    caller is responsible for tearing them down via :func:`_shutdown`.
    """
    thread = QThread()
    thread.setObjectName("info-worker-test")
    worker = InfoWorker()
    worker.moveToThread(thread)
    trigger = _Trigger()
    trigger.stationsRequested.connect(
        worker.requestStations, type=Qt.ConnectionType.QueuedConnection
    )
    trigger.streamsRequested.connect(worker.requestStreams, type=Qt.ConnectionType.QueuedConnection)
    trigger.idRequested.connect(worker.requestId, type=Qt.ConnectionType.QueuedConnection)
    thread.start()
    qtbot.waitUntil(lambda: thread.isRunning(), timeout=1000)
    return worker, thread, trigger


def _shutdown(worker: InfoWorker, thread: QThread) -> None:
    """Stop the worker and join its thread within the project's 2 s budget."""
    worker.stop()
    thread.quit()
    assert thread.wait(_THREAD_JOIN_MS), "info-worker thread did not join in time"


def test_worker_dispatches_request_across_real_thread(
    qtbot: Any,
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """Queued ``requestStations`` from the test thread fires on the worker thread.

    A queued slot must dispatch on the receiver's thread when its
    event loop is free. The Stage A bug surfaced by code-reviewer
    parked the worker thread inside ``queue.get()``, which prevented
    the queued slot from ever running. This test would have timed
    out under that implementation.
    """
    cfg = FakeSeedLinkServerConfig(
        stations=(
            FakeStation(network="IU", station="ANMO", description="Albuquerque NM"),
            FakeStation(network="IV", station="MILN", description="Milan IT"),
        ),
    )
    server = make_fake_server(cfg)

    worker, thread, trigger = _spawn_worker(qtbot)
    try:
        request_id = uuid.uuid4().hex
        with qtbot.waitSignal(worker.stationsReceived, timeout=_FETCH_DEADLINE_MS) as blocker:
            trigger.stationsRequested.emit(request_id, "dev-a", server.host, server.port)

        rid, device_id, payload = blocker.args
        assert rid == request_id
        assert device_id == "dev-a"
        assert isinstance(payload, list)
        assert {(s.network, s.station) for s in payload} == {("IU", "ANMO"), ("IV", "MILN")}
        assert all(isinstance(s, StationInfo) for s in payload)
    finally:
        _shutdown(worker, thread)


def test_worker_streams_request_filters_server_side(
    qtbot: Any,
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """``requestStreams`` with a NET_STA filter only returns the matching streams."""
    cfg = FakeSeedLinkServerConfig(
        streams=(
            FakeStream(network="IU", station="ANMO", location="00", channel="BHZ"),
            FakeStream(network="IU", station="ANMO", location="00", channel="BHN"),
            FakeStream(network="IV", station="MILN", location="", channel="HHZ"),
        ),
    )
    server = make_fake_server(cfg)

    worker, thread, trigger = _spawn_worker(qtbot)
    try:
        request_id = uuid.uuid4().hex
        with qtbot.waitSignal(worker.streamsReceived, timeout=_FETCH_DEADLINE_MS) as blocker:
            trigger.streamsRequested.emit(
                request_id, "dev-a", server.host, server.port, "IU", "ANMO"
            )

        rid, device_id, payload = blocker.args
        assert rid == request_id
        assert device_id == "dev-a"
        assert isinstance(payload, list)
        assert all(isinstance(s, StreamInfo) for s in payload)
        assert {(s.network, s.station) for s in payload} == {("IU", "ANMO")}
        assert {s.channel for s in payload} == {"BHZ", "BHN"}
    finally:
        _shutdown(worker, thread)


def test_worker_stop_cancels_in_flight_fetch(
    qtbot: Any,
    make_fake_server: Callable[[FakeSeedLinkServerConfig], FakeSeedLinkServer],
) -> None:
    """``stop()`` from the GUI thread aborts an in-flight fetch promptly.

    The fake server is configured with ``info_silent_mode=True`` so it
    accepts the connection then never replies; without cancellation the
    fetch would block until the worker's per-fetch ``timeout_s = 30 s``.
    The test asserts the worker thread joins within the project's 2 s
    teardown budget, which can only happen if ``stop()`` actually
    interrupts the in-flight ``info.fetch`` via the cancellation token.
    """
    cfg = FakeSeedLinkServerConfig(
        stations=(FakeStation(network="IU", station="ANMO", description=""),),
        info_silent_mode=True,
    )
    server = make_fake_server(cfg)

    worker, thread, trigger = _spawn_worker(qtbot)
    failure_seen = False
    try:
        # Drive the fetch — server will accept then go silent.
        request_id = uuid.uuid4().hex
        with qtbot.waitSignal(worker.infoFailed, timeout=_FETCH_DEADLINE_MS) as blocker:
            trigger.stationsRequested.emit(request_id, "dev-a", server.host, server.port)
            # Give the worker thread a moment to receive the queued slot
            # and start the fetch — the cancel must reach an in-flight
            # call, not arrive before the fetch starts.
            qtbot.wait(150)
            worker.stop()

        rid, _device_id, kind, reason = blocker.args
        assert rid == request_id
        assert kind == "STATIONS"
        # Either the cancel won the race ("canceled") or the timeout did
        # ("timeout: ..."); both are acceptable proof that the wait was
        # bounded. What we MUST NOT see is a 30 s wait — the join below
        # caps the whole test at well under that.
        assert "canceled" in reason.lower() or "timeout" in reason.lower()
        failure_seen = True
    finally:
        # Worker is already stopped if the body ran; calling stop again
        # is idempotent. Thread quit + join must succeed within budget.
        _shutdown(worker, thread)

    assert failure_seen, "infoFailed never emitted"
