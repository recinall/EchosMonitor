"""Worker-level integration tests for the ``protocol_rejected`` failure kind.

A SeedLink server that accepts the TCP handshake but rejects every
``STATION`` command (e.g. wrong NET/STA selector) sits in a class of
its own: obspy's ``SeedLinkConnection.collect()`` raises
``SeedLinkException("no stations accepted")`` and then **catches and
swallows it**, so the exception never reaches our ``client.run()``
caller. The worker therefore detects the rejection via a
``logging.Filter`` installed on the ``obspy.clients.seedlink`` logger
and surfaces it as ``FailureKind = "protocol_rejected"`` with a
structured ``last_failure_detail = {"rejected_selectors": [...],
"rejection_count": int}`` payload. See POSTMORTEMS 2026-05-10 entry
"Silent SeedLink protocol rejection".

These tests use the asyncio fake server with ``reject_all_stations=True``
to drive the rejection path deterministically.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QObject, Qt, QThread, Slot

from echosmonitor.config.schema import ReconnectConfig
from echosmonitor.core.models import ConnState, StreamSelector, WorkerDiagnostics
from echosmonitor.core.seedlink_worker import SeedLinkWorker
from tests.core.fakes import FakeSeedLinkServer, FakeSeedLinkServerConfig
from tests.core.test_seedlink_worker import _LoopThread, loop_thread  # noqa: F401


# ---------------------------------------------------------------------------
# Harness — mirrors test_seedlink_worker_timeout._Harness but also captures
# the ``diagnosticsUpdated`` stream because every assertion in this file
# inspects ``last_failure_kind`` / ``last_failure_detail``.
# ---------------------------------------------------------------------------
class _Harness(QObject):
    def __init__(self, worker: SeedLinkWorker) -> None:
        super().__init__()
        self.worker = worker
        self.thread = QThread()
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run, type=Qt.ConnectionType.QueuedConnection)

        self.states: list[tuple[int, str]] = []
        self.diagnostics: list[WorkerDiagnostics] = []
        self.errors: list[str] = []
        worker.stateChanged.connect(self._on_state, type=Qt.ConnectionType.QueuedConnection)
        worker.diagnosticsUpdated.connect(self._on_diag, type=Qt.ConnectionType.QueuedConnection)
        worker.errorOccurred.connect(self._on_error, type=Qt.ConnectionType.QueuedConnection)

    @Slot(int, str)
    def _on_state(self, state: int, msg: str) -> None:
        self.states.append((state, msg))

    @Slot(object)
    def _on_diag(self, diag: object) -> None:
        if isinstance(diag, WorkerDiagnostics):
            self.diagnostics.append(diag)

    @Slot(str)
    def _on_error(self, msg: str) -> None:
        self.errors.append(msg)

    def start(self) -> None:
        self.thread.start()

    def shutdown(self, deadline_s: float = 3.0) -> float:
        t0 = time.monotonic()
        self.worker.stop()
        self.thread.quit()
        self.thread.wait(int(deadline_s * 1000))
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

    def latest_diag_with_kind(self, kind: str) -> WorkerDiagnostics | None:
        for diag in reversed(self.diagnostics):
            if diag.last_failure_kind == kind:
                return diag
        return None


@pytest.fixture
def rejecting_server(loop_thread: _LoopThread) -> Iterator[FakeSeedLinkServer]:  # noqa: F811
    """Fake SeedLink server that responds ``ERROR\\r\\n`` to every STATION."""
    cfg = FakeSeedLinkServerConfig(
        packet_interval_s=0.05,
        samples_per_record=50,
        reject_all_stations=True,
    )
    server = FakeSeedLinkServer(config=cfg)
    loop_thread.submit(server.start()).result(timeout=2.0)
    try:
        yield server
    finally:
        with contextlib.suppress(Exception):
            loop_thread.submit(server.stop()).result(timeout=3.0)


def _make_worker(server: FakeSeedLinkServer) -> SeedLinkWorker:
    """Worker pointed at ``server`` with a tight backoff window for fast tests.

    ``initial_delay_s=1.0`` gives the rejection cycle just enough room
    to flow through the run() loop without the test wallclock budget
    blowing out; ``max_delay_s=2.0`` caps the backoff so the recovery
    test doesn't have to wait minutes.
    """
    return SeedLinkWorker(
        name="reject",
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
        reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=2.0, connect_timeout_s=2.0),
    )


def test_worker_classifies_protocol_rejected_within_5s(
    qtbot,
    rejecting_server: FakeSeedLinkServer,
) -> None:
    """A server that ``ERROR\\r\\n``s every STATION must trigger
    ``last_failure_kind == "protocol_rejected"`` and a non-empty
    ``rejected_selectors`` list within 5 s — well below the
    ``connect_timeout_s`` budget, since the TCP handshake itself
    succeeds and the rejection happens during negotiation."""
    worker = _make_worker(rejecting_server)
    harness = _Harness(worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: harness.latest_diag_with_kind("protocol_rejected") is not None,
            timeout_s=5.0,
            qtbot=qtbot,
        ), (
            "worker never classified protocol_rejected; "
            f"states={harness.states} errors={harness.errors} "
            f"diag_kinds={[d.last_failure_kind for d in harness.diagnostics]}"
        )

        diag = harness.latest_diag_with_kind("protocol_rejected")
        assert diag is not None
        assert diag.last_failure_detail is not None, (
            "protocol_rejected diagnostics missing last_failure_detail payload"
        )
        sels = diag.last_failure_detail.get("rejected_selectors")
        assert isinstance(sels, list) and sels, (
            f"rejected_selectors missing or empty: {diag.last_failure_detail!r}"
        )
        # Sanity: the selector strings carry the configured NET.STA.LOC.CHA
        # so the GUI can render something the operator recognises.
        assert any(rejecting_server.config.station in s for s in sels), (
            f"rejected_selectors do not reference the configured station: {sels!r}"
        )
        # The rejection feeds straight into the standard backoff path.
        assert harness.wait_until(
            lambda: any(s == int(ConnState.WAITING_RETRY) for s, _ in harness.states),
            timeout_s=2.0,
            qtbot=qtbot,
        ), f"never reached WAITING_RETRY after rejection; states={harness.states}"
    finally:
        harness.shutdown(deadline_s=3.0)


def test_worker_stop_during_protocol_rejected_returns_within_1s(
    qtbot,
    rejecting_server: FakeSeedLinkServer,
) -> None:
    """``stop()`` while the worker is in the rejection-driven WAITING_RETRY
    must return well under one second. The 50 ms ``_sleep_interruptible``
    poll grants ~50-100 ms wake latency; we assert <= 1.0 s for slow CI
    headroom while still catching a state-machine regression."""
    worker = _make_worker(rejecting_server)
    harness = _Harness(worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: harness.latest_diag_with_kind("protocol_rejected") is not None,
            timeout_s=5.0,
            qtbot=qtbot,
        )
        assert harness.wait_until(
            lambda: any(s == int(ConnState.WAITING_RETRY) for s, _ in harness.states),
            timeout_s=2.0,
            qtbot=qtbot,
        )
        # Tiny pad so the worker is firmly inside the sleep, not at its boundary.
        qtbot.wait(50)
    finally:
        elapsed = harness.shutdown(deadline_s=2.0)
    assert elapsed <= 1.0, (
        f"stop() took {elapsed:.3f}s during protocol_rejected WAITING_RETRY — "
        "rejection path is not interrupting cleanly"
    )


def test_two_workers_one_misconfigured_other_unaffected(
    qtbot,
    loop_thread: _LoopThread,  # noqa: F811
) -> None:
    """A misconfigured worker MUST NOT contaminate a sibling worker's
    rejection-detection state. ``logging.getLogger("obspy.clients.seedlink")``
    is a process-global singleton, so every installed
    ``_StationRejectionFilter`` sees every worker's log records. The
    filter guards against this by capturing ``threading.get_ident()``
    at construction and short-circuiting records emitted from a
    different thread (each worker's session runs ``client.run()`` on
    its own QThread). Without that guard, the healthy worker's filter
    would observe the rejecting worker's "no stations accepted"
    markers and falsely classify its own next session-end as
    ``protocol_rejected``.

    Concretely:
      * Worker A points at a fake server that accepts STATION and streams.
      * Worker B points at a fake server that rejects every STATION.
      * After both have been running for a few seconds, A's diagnostics
        MUST NOT carry ``last_failure_kind == "protocol_rejected"``.
    """
    cfg_ok = FakeSeedLinkServerConfig(
        packet_interval_s=0.05, samples_per_record=50, reject_all_stations=False
    )
    cfg_reject = FakeSeedLinkServerConfig(
        packet_interval_s=0.05, samples_per_record=50, reject_all_stations=True
    )
    server_ok = FakeSeedLinkServer(config=cfg_ok)
    server_reject = FakeSeedLinkServer(config=cfg_reject)
    loop_thread.submit(server_ok.start()).result(timeout=2.0)
    loop_thread.submit(server_reject.start()).result(timeout=2.0)
    try:
        worker_ok = _make_worker(server_ok)
        worker_reject = _make_worker(server_reject)
        h_ok = _Harness(worker_ok)
        h_reject = _Harness(worker_reject)
        h_ok.start()
        h_reject.start()
        try:
            # Wait until both workers have settled into their
            # respective steady states.
            assert h_reject.wait_until(
                lambda: h_reject.latest_diag_with_kind("protocol_rejected") is not None,
                timeout_s=5.0,
                qtbot=qtbot,
            ), "rejecting worker never classified protocol_rejected"
            assert h_ok.wait_until(
                lambda: any(s == int(ConnState.CONNECTED) for s, _ in h_ok.states),
                timeout_s=5.0,
                qtbot=qtbot,
            ), "healthy worker never reached CONNECTED"

            # Give the healthy worker headroom to receive packets
            # while the rejecting worker is still cycling. If the
            # filter were process-global, the healthy worker's
            # diagnostics would pick up the rejecting marker during
            # this window.
            qtbot.wait(2000)

            # The healthy worker MUST NOT carry a protocol_rejected
            # classification anywhere in its diagnostics history.
            ok_kinds = {d.last_failure_kind for d in h_ok.diagnostics}
            assert "protocol_rejected" not in ok_kinds, (
                "healthy worker was contaminated by rejecting worker's "
                f"obspy log records; saw kinds={ok_kinds!r}"
            )
        finally:
            h_ok.shutdown(deadline_s=3.0)
            h_reject.shutdown(deadline_s=3.0)
    finally:
        with contextlib.suppress(Exception):
            loop_thread.submit(server_ok.stop()).result(timeout=3.0)
        with contextlib.suppress(Exception):
            loop_thread.submit(server_reject.stop()).result(timeout=3.0)


def test_worker_recovers_when_server_starts_accepting(
    qtbot,
    rejecting_server: FakeSeedLinkServer,
    loop_thread: _LoopThread,  # noqa: F811
) -> None:
    """Once the rejection has been observed, flipping the fake's
    ``reject_all_stations`` to ``False`` must let the next backoff
    cycle reach CONNECTED — proving the rejection path doesn't
    permanently poison the worker. Mutates the server config from the
    asyncio loop thread (the fake reads ``self._cfg.reject_all_stations``
    on every STATION command, so a write from the loop is observable
    without restarting the server)."""
    worker = _make_worker(rejecting_server)
    harness = _Harness(worker)
    harness.start()
    try:
        # Observe at least one rejection before flipping.
        assert harness.wait_until(
            lambda: harness.latest_diag_with_kind("protocol_rejected") is not None,
            timeout_s=5.0,
            qtbot=qtbot,
        )

        async def _accept() -> None:
            rejecting_server.config.reject_all_stations = False

        loop_thread.submit(_accept()).result(timeout=2.0)

        # The next attempt (after the current backoff window of up to 2 s)
        # must succeed. Budget = backoff + connect + headroom.
        assert harness.wait_until(
            lambda: any(s == int(ConnState.CONNECTED) for s, _ in harness.states),
            timeout_s=10.0,
            qtbot=qtbot,
        ), (
            "worker never recovered after server flipped to accepting; "
            f"states={harness.states} errors={harness.errors}"
        )
    finally:
        harness.shutdown(deadline_s=3.0)
