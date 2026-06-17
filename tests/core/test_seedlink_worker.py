"""Integration tests for `SeedLinkWorker` against a fake SeedLink server.

These exercise the real ObsPy `EasySeedLinkClient` over loopback TCP, so
they validate both the worker's state machine and the fake-server fixture.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future

import pytest
from PySide6.QtCore import QObject, Qt, QThread, Slot

from echosmonitor.config.schema import ReconnectConfig
from echosmonitor.core.models import ConnState, StreamSelector
from echosmonitor.core.seedlink_worker import SeedLinkWorker
from tests.core.fakes import FakeSeedLinkServer, FakeSeedLinkServerConfig

# Worker/thread pairs whose bounded join timed out at shutdown. Held for the
# rest of the process so a still-running QThread is never garbage-collected
# (a "QThread destroyed while running" abort) — see `_WorkerHarness.shutdown`.
_ABANDONED_THREADS: list[tuple[QThread, SeedLinkWorker]] = []


# ----------------------------------------------------------------------
# Async loop / fake-server fixtures
# ----------------------------------------------------------------------
class _LoopThread:
    """Background asyncio loop usable from the main (Qt) thread."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        def _run() -> None:
            # Force a SelectorEventLoop. On Windows the default is the
            # ProactorEventLoop, where transport.get_extra_info("socket")
            # does not expose the raw socket the way inject_disconnect()
            # needs to set SO_LINGER and force a TCP RST — so the worker
            # never sees the server-side disconnect (the reconnect test).
            self.loop = asyncio.SelectorEventLoop()
            asyncio.set_event_loop(self.loop)
            self._ready.set()
            self.loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True, name="fake-sl-loop")
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def submit(self, coro: object) -> Future[object]:
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop)  # type: ignore[arg-type]

    def stop(self) -> None:
        if self.loop is None or self._thread is None:
            return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=2.0)


@pytest.fixture
def loop_thread() -> Iterator[_LoopThread]:
    lt = _LoopThread()
    lt.start()
    try:
        yield lt
    finally:
        lt.stop()


@pytest.fixture
def fake_server(loop_thread: _LoopThread) -> Iterator[FakeSeedLinkServer]:
    cfg = FakeSeedLinkServerConfig(packet_interval_s=0.05, samples_per_record=50)
    server = FakeSeedLinkServer(config=cfg)
    fut = loop_thread.submit(server.start())
    fut.result(timeout=2.0)
    try:
        yield server
    finally:
        stop_fut = loop_thread.submit(server.stop())
        with contextlib.suppress(Exception):
            stop_fut.result(timeout=3.0)


# ----------------------------------------------------------------------
# Worker thread harness
# ----------------------------------------------------------------------
class _WorkerHarness(QObject):
    """QObject living in the test (main) thread — receives queued signals."""

    def __init__(self, worker: SeedLinkWorker) -> None:
        super().__init__()
        self.worker = worker
        self.thread = QThread()
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run, type=Qt.ConnectionType.QueuedConnection)

        self.states: list[tuple[int, str]] = []
        self.packet_count = 0
        self.errors: list[str] = []

        worker.stateChanged.connect(self._on_state, type=Qt.ConnectionType.QueuedConnection)
        worker.packetReceived.connect(self._on_packet, type=Qt.ConnectionType.QueuedConnection)
        worker.errorOccurred.connect(self._on_error, type=Qt.ConnectionType.QueuedConnection)

    @Slot(int, str)
    def _on_state(self, state: int, msg: str) -> None:
        self.states.append((state, msg))

    @Slot(object)
    def _on_packet(self, trace: object) -> None:
        self.packet_count += 1

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self.errors.append(msg)

    def start(self) -> None:
        self.thread.start()

    def shutdown(self, deadline_s: float = 2.0) -> float:
        t0 = time.monotonic()
        self.worker.stop()
        self.thread.quit()
        finished = self.thread.wait(int(deadline_s * 1000))
        if not finished:
            # The join timed out — obspy's blocking recv has not unwound yet
            # (seen on macOS loopback, where stop()'s socket-close takes
            # longer to surface). Dropping the last reference to a RUNNING
            # QThread is a hard Qt abort ("destroyed while running") that
            # crashes the whole interpreter — and it crashed a LATER test's
            # GC, not this one. Retain the pair so it is never GC'd while
            # running (the same precaution the HVSR engines take on a
            # timed-out join — see the M6-0 decision-log entry). The thread
            # finishes on its own once the fake_server fixture tears down.
            _ABANDONED_THREADS.append((self.thread, self.worker))
        return time.monotonic() - t0

    def wait_until(self, predicate: object, timeout_s: float, qtbot: object) -> bool:
        from pytestqt.qtbot import QtBot

        assert isinstance(qtbot, QtBot)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            qtbot.wait(50)
            if predicate():  # type: ignore[operator]
                return True
        return False


def _make_worker(server: FakeSeedLinkServer) -> SeedLinkWorker:
    return SeedLinkWorker(
        name="fake",
        host=server.host,
        port=server.port,
        selectors=[
            StreamSelector(
                network=server.config.network,
                station=server.config.station,
                location=server.config.location,
                channel=server.config.channel,
            )
        ],
        reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
    )


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------
def test_worker_connects_and_receives_packets(qtbot, fake_server: FakeSeedLinkServer) -> None:
    worker = _make_worker(fake_server)
    harness = _WorkerHarness(worker)
    harness.start()
    try:
        # Connects within 2 s
        assert harness.wait_until(
            lambda: any(s == int(ConnState.CONNECTED) for s, _ in harness.states),
            timeout_s=3.0,
            qtbot=qtbot,
        ), f"never reached CONNECTED; states={harness.states} errors={harness.errors}"

        # Receives at least 3 packets
        assert harness.wait_until(
            lambda: harness.packet_count >= 3,
            timeout_s=3.0,
            qtbot=qtbot,
        ), f"received only {harness.packet_count} packets"

        # State machine ordering: CONNECTING comes before CONNECTED
        codes = [s for s, _ in harness.states]
        first_connecting = codes.index(int(ConnState.CONNECTING))
        first_connected = codes.index(int(ConnState.CONNECTED))
        assert first_connecting < first_connected
    finally:
        harness.shutdown()


def test_worker_reconnects_after_server_disconnect(
    qtbot,
    fake_server: FakeSeedLinkServer,
    loop_thread: _LoopThread,
) -> None:
    worker = _make_worker(fake_server)
    harness = _WorkerHarness(worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: any(s == int(ConnState.CONNECTED) for s, _ in harness.states),
            timeout_s=3.0,
            qtbot=qtbot,
        )

        first_connected_count = sum(1 for s, _ in harness.states if s == int(ConnState.CONNECTED))

        # Inject server-side disconnect.
        loop_thread.submit(fake_server.inject_disconnect()).result(timeout=2.0)

        # Worker should reach CONNECTED again within 5 s.
        assert harness.wait_until(
            lambda: (
                sum(1 for s, _ in harness.states if s == int(ConnState.CONNECTED))
                > first_connected_count
            ),
            timeout_s=5.0,
            qtbot=qtbot,
        ), f"never reconnected; states={harness.states}"

        # Verify that RECONNECTING was observed in between.
        codes = [s for s, _ in harness.states]
        assert int(ConnState.RECONNECTING) in codes
    finally:
        harness.shutdown(deadline_s=3.0)


def test_empty_selectors_stops_without_retry_loop(qtbot) -> None:
    """A device configured with NO selectors must report a clear config error
    and STOP — not connect, fail with obspy's 'No streams specified', and
    retry-loop forever (the confirmed Bug 1 field failure; no data, the device
    looks 'never connects'). The host is never reached: the guard fires first.
    """
    worker = SeedLinkWorker(
        name="noselectors", host="127.0.0.1", port=18000, selectors=[],
        reconnect=ReconnectConfig(),
    )
    harness = _WorkerHarness(worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: any(s == int(ConnState.STOPPED) for s, _ in harness.states),
            timeout_s=3.0,
            qtbot=qtbot,
        )
        codes = [s for s, _ in harness.states]
        # No connection attempt and no reconnect backoff were ever entered.
        assert int(ConnState.CONNECTING) not in codes
        assert int(ConnState.WAITING_RETRY) not in codes
        # A clear, actionable error was surfaced exactly once.
        assert any("selector" in e.lower() for e in harness.errors)
    finally:
        harness.shutdown(deadline_s=3.0)


def test_worker_stop_returns_within_one_second(
    qtbot,
    fake_server: FakeSeedLinkServer,
) -> None:
    worker = _make_worker(fake_server)
    harness = _WorkerHarness(worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: any(s == int(ConnState.CONNECTED) for s, _ in harness.states),
            timeout_s=3.0,
            qtbot=qtbot,
        )
        # Make sure we actually started reading data first.
        assert harness.wait_until(lambda: harness.packet_count > 0, timeout_s=2.0, qtbot=qtbot)
    finally:
        elapsed = harness.shutdown(deadline_s=2.0)
        assert elapsed <= 1.0, f"stop() took {elapsed:.3f}s, exceeds 1s budget"


def test_worker_stop_emits_no_warnings(
    qtbot,
    fake_server: FakeSeedLinkServer,
    capture_structlog: list[dict[str, object]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`stop()` on a connected worker must produce no WARNING+ records.

    Two channels are checked:
      * structlog `seedlink_worker` logger — the worker's own structured logs
        already drop to DEBUG when `_stop is True`, so no WARNING+ events
        should appear during teardown.
      * stdlib `obspy.clients.seedlink` logger — covered by the noise filter
        installed in `utils/logging.configure_logging`. Without it, obspy
        emits WARNING/ERROR for every torn-down socket.
    """
    # configure_logging() installs the obspy seedlink filter on the logger.
    # Force-install here so this test passes even if no other test has run
    # configure_logging() yet.
    from echosmonitor.utils.logging import _install_obspy_seedlink_filter

    _install_obspy_seedlink_filter()
    caplog.set_level(logging.DEBUG, logger="obspy.clients.seedlink")
    caplog.set_level(logging.DEBUG, logger="echosmonitor.core.seedlink_worker")

    worker = _make_worker(fake_server)
    harness = _WorkerHarness(worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: any(s == int(ConnState.CONNECTED) for s, _ in harness.states),
            timeout_s=3.0,
            qtbot=qtbot,
        )
        assert harness.wait_until(lambda: harness.packet_count > 0, timeout_s=2.0, qtbot=qtbot)

        # Mark the boundary so we only inspect records emitted from now on.
        capture_structlog.clear()
        caplog.clear()
    finally:
        harness.shutdown(deadline_s=2.0)
        # Give the connection thread a moment to fully unwind so any late
        # log records are emitted before we inspect.
        qtbot.wait(200)

    structlog_warnings = [
        rec
        for rec in capture_structlog
        if rec.get("log_level", "").lower() in {"warning", "error", "critical"}
    ]
    assert structlog_warnings == [], (
        f"structlog emitted WARNING+ records during stop(): {structlog_warnings}"
    )

    obspy_warnings = [
        rec
        for rec in caplog.records
        if rec.name.startswith("obspy.clients.seedlink") and rec.levelno >= logging.WARNING
    ]
    assert obspy_warnings == [], "obspy seedlink logger emitted WARNING+ during stop(): " + str(
        [(r.levelname, r.getMessage()) for r in obspy_warnings]
    )


def test_easyseedlinkclient_capabilities_bypass_skips_info_request(
    loop_thread: _LoopThread,
) -> None:
    """The worker pre-populates `_EasySeedLinkClient__capabilities` to avoid
    an `INFO:CAPABILITIES` round-trip. This regression test verifies the
    name-mangle attribute really does suppress the probe — a subclass with
    that attribute set must call `select_stream()` without the client ever
    sending an `INFO:CAPABILITIES` line. If obspy renames the private
    attribute under us, this test fails.
    """
    from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient

    received_lines: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                raw = await reader.readuntil(b"\r")
                received_lines.append(raw)
                upper = raw.rstrip(b"\r\n").upper()
                if upper == b"HELLO":
                    writer.write(b"SeedLink v3.2 FakeSeedLink\r\nFakeSeedLink\r\n")
                elif (
                    upper.startswith(b"STATION")
                    or upper.startswith(b"SELECT")
                    or upper == b"DATA"
                    or upper == b"END"
                ):
                    writer.write(b"OK\r\n")
                    if upper == b"END":
                        await writer.drain()
                        return
                else:
                    # INFO:CAPABILITIES, if it ever arrives, lands here. We
                    # don't reply — the test will assert this never happens.
                    writer.write(b"ERROR\r\n")
                await writer.drain()
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            BrokenPipeError,
            OSError,
            asyncio.CancelledError,
        ):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def boot() -> tuple[asyncio.base_events.Server, int]:
        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        return server, port

    server, port = loop_thread.submit(boot()).result(timeout=2.0)

    try:

        class _Probe(EasySeedLinkClient):  # type: ignore[misc]
            def __init__(self, url: str) -> None:
                super().__init__(url, autoconnect=False)
                # Prevent the INFO:CAPABILITIES round-trip the same way
                # production code does. Subclassing forces Python's
                # name-mangling rules to apply — the attribute is
                # `_EasySeedLinkClient__capabilities`.
                self._EasySeedLinkClient__capabilities = ["multistation"]

            def on_data(self, trace: object) -> None:  # pragma: no cover
                pass

            def on_terminate(self) -> None:  # pragma: no cover
                pass

        client = _Probe(f"127.0.0.1:{port}")
        client.connect()
        try:
            client.select_stream("IU", "ANMO", "00BHZ")
        finally:
            with contextlib.suppress(Exception):
                client.conn.disconnect()

    finally:

        async def shutdown() -> None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

        loop_thread.submit(shutdown()).result(timeout=2.0)

    info_lines = [line for line in received_lines if b"INFO" in line.upper()]
    assert info_lines == [], (
        f"expected zero INFO requests, got: {[line.decode(errors='replace') for line in info_lines]}"
    )
