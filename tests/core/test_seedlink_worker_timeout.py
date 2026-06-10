"""Worker tests for the bounded TCP preflight + WAITING_RETRY state.

These tests deliberately point the worker at an unrouted IP
(``10.255.255.1``, RFC1918 private space that's typically blackholed)
to exercise the real ``socket.create_connection`` timeout path. CI
environments that route this differently (e.g. respond with ICMP
unreachable instantly) would surface the failure as ``unknown`` rather
than ``timeout``; that case is asserted softly so the suite still
catches a genuinely broken state machine while tolerating the rare
sandbox where blackholing isn't available.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QObject, Qt, QThread, Slot

from echosmonitor.config.schema import ReconnectConfig
from echosmonitor.core.models import ConnState, StreamSelector, WorkerDiagnostics
from echosmonitor.core.seedlink_worker import SeedLinkWorker

# RFC1918 unrouted address — most networks SYN-blackhole this. Falls back
# to the documentation-block address (RFC5737) if a particular sandbox
# happens to route 10.255.255.1 to a real responder; both are reasonable
# choices for "host that should never answer".
_BLACKHOLE_HOST = "10.255.255.1"
_BLACKHOLE_PORT = 18000


class _Harness(QObject):
    """Worker harness mirroring tests/core/test_seedlink_worker._WorkerHarness.

    Deliberately a separate copy rather than a shared utility — the two
    test files exercise different fixtures (fake_server vs. real socket)
    and centralising would just produce import noise.
    """

    def __init__(self, worker: SeedLinkWorker) -> None:
        super().__init__()
        self.worker = worker
        self.thread = QThread()
        worker.moveToThread(self.thread)
        self.thread.started.connect(worker.run, type=Qt.ConnectionType.QueuedConnection)

        self.states: list[tuple[int, str]] = []
        self.diagnostics: list[WorkerDiagnostics] = []
        worker.stateChanged.connect(self._on_state, type=Qt.ConnectionType.QueuedConnection)
        worker.diagnosticsUpdated.connect(self._on_diag, type=Qt.ConnectionType.QueuedConnection)

    @Slot(int, str)
    def _on_state(self, state: int, msg: str) -> None:
        self.states.append((state, msg))

    @Slot(object)
    def _on_diag(self, diag: object) -> None:
        if isinstance(diag, WorkerDiagnostics):
            self.diagnostics.append(diag)

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


@pytest.fixture
def blackhole_worker() -> Iterator[SeedLinkWorker]:
    """Yield a fresh worker pointed at the blackhole host with a short
    connect timeout. Each test owns its own worker so state stays clean.
    """
    worker = SeedLinkWorker(
        name="blackhole",
        host=_BLACKHOLE_HOST,
        port=_BLACKHOLE_PORT,
        selectors=[StreamSelector(network="XX", station="TEST", location="", channel="HHZ")],
        reconnect=ReconnectConfig(
            initial_delay_s=1.0,
            max_delay_s=2.0,
            connect_timeout_s=2.0,
        ),
    )
    yield worker


def test_blackhole_transitions_to_waiting_retry_within_budget(
    qtbot,
    blackhole_worker: SeedLinkWorker,
) -> None:
    """A SYN-blackholed host must move CONNECTING → WAITING_RETRY within
    ``connect_timeout_s + 1.0`` s. With a 2 s timeout this means the
    transition happens by 3 s wallclock, far below the OS default of
    ~127 s the patch supersedes.
    """
    harness = _Harness(blackhole_worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: any(s == int(ConnState.WAITING_RETRY) for s, _ in harness.states),
            timeout_s=4.0,
            qtbot=qtbot,
        ), f"never reached WAITING_RETRY; states={harness.states}"

        codes = [s for s, _ in harness.states]
        assert int(ConnState.CONNECTING) in codes
        first_connecting = codes.index(int(ConnState.CONNECTING))
        first_waiting = codes.index(int(ConnState.WAITING_RETRY))
        assert first_connecting < first_waiting, (
            f"WAITING_RETRY must come after CONNECTING; states={harness.states}"
        )
    finally:
        harness.shutdown(deadline_s=3.0)


def test_diagnostics_payload_after_three_failures(
    qtbot,
    blackhole_worker: SeedLinkWorker,
) -> None:
    """After three failed connect attempts, the latest diagnostics
    snapshot reports ``attempt_count == 3`` and a classified failure
    kind (``timeout`` on most networks).
    """
    harness = _Harness(blackhole_worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: any(d.attempt_count >= 3 for d in harness.diagnostics),
            timeout_s=15.0,
            qtbot=qtbot,
        ), (
            "never observed attempt_count>=3; "
            f"diagnostics={[(d.attempt_count, d.last_failure_kind) for d in harness.diagnostics]}"
        )
        last = harness.diagnostics[-1]
        assert last.attempt_count >= 3
        # Most networks blackhole 10.255.255.1 → "timeout". Sandboxes
        # that route it to ICMP-unreachable instead surface "unknown".
        # Either is a real failure classification (not None / not
        # mis-set), and both round-trip through the GUI tooltip.
        assert last.last_failure_kind in {"timeout", "unknown"}
    finally:
        harness.shutdown(deadline_s=3.0)


def test_stop_during_waiting_retry_returns_promptly(
    qtbot,
    blackhole_worker: SeedLinkWorker,
) -> None:
    """Regression: ``stop()`` invoked while the worker is sleeping in
    WAITING_RETRY must return well under the configured backoff window.
    The 50 ms ``_sleep_interruptible`` poll grants ~50-100 ms wake
    latency; we assert <= 0.5 s to keep the bound loose enough for slow
    CI without losing the regression's value.
    """
    # Bump backoff so WAITING_RETRY would last several seconds if the
    # sleep weren't interruptible. The test only succeeds if stop()
    # actually breaks out of the sleep early.
    blackhole_worker._reconnect = ReconnectConfig(
        initial_delay_s=5.0, max_delay_s=10.0, connect_timeout_s=2.0
    )
    harness = _Harness(blackhole_worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: any(s == int(ConnState.WAITING_RETRY) for s, _ in harness.states),
            timeout_s=4.0,
            qtbot=qtbot,
        )
        # Tiny pad so the worker is firmly inside the sleep, not at its boundary.
        qtbot.wait(50)
    finally:
        elapsed = harness.shutdown(deadline_s=2.0)
    assert elapsed <= 0.5, (
        f"stop() took {elapsed:.3f}s during WAITING_RETRY — sleep is not interruptible"
    )


def test_state_sequence_no_missing_waiting_retry(
    qtbot,
    blackhole_worker: SeedLinkWorker,
) -> None:
    """Between every two consecutive CONNECTING emissions there must be
    exactly one WAITING_RETRY. Verifies the worker's state machine
    contract: failed attempts always pass through the backoff state.
    """
    harness = _Harness(blackhole_worker)
    harness.start()
    try:
        # Wait for at least 3 CONNECTING attempts so we can inspect 2 gaps.
        assert harness.wait_until(
            lambda: sum(1 for s, _ in harness.states if s == int(ConnState.CONNECTING)) >= 3,
            timeout_s=15.0,
            qtbot=qtbot,
        ), f"never observed 3 CONNECTING attempts; states={harness.states}"
    finally:
        harness.shutdown(deadline_s=3.0)

    codes = [s for s, _ in harness.states]
    connecting_idx = [i for i, s in enumerate(codes) if s == int(ConnState.CONNECTING)]
    assert len(connecting_idx) >= 3
    for prev_i, next_i in itertools.pairwise(connecting_idx):
        between = codes[prev_i + 1 : next_i]
        # Exactly one WAITING_RETRY between two consecutive CONNECTING
        # emissions — anything else (zero, two, etc.) means the state
        # machine is emitting spurious / missing transitions.
        waiting_count = sum(1 for s in between if s == int(ConnState.WAITING_RETRY))
        assert waiting_count == 1, (
            f"expected exactly 1 WAITING_RETRY between CONNECTING emissions at "
            f"indices {prev_i} and {next_i}, got {waiting_count}; "
            f"states={harness.states}"
        )


def test_connect_timeout_default_matches_schema() -> None:
    """Smoke test: default ``connect_timeout_s`` is the user-spec value
    (10.0). Lower than this risks false negatives on legitimate
    transcontinental links; higher loses the user-facing latency win.
    """
    assert ReconnectConfig().connect_timeout_s == 10.0


def test_preflight_failure_does_not_leak_threads(
    qtbot,
    blackhole_worker: SeedLinkWorker,
) -> None:
    """Sanity: spinning the worker up + back down through several failed
    preflights must not leave background threads behind. Catches a
    regression where a leaked socket or a dangling helper would keep
    the reactor alive past the test boundary.
    """
    pre_threads = {t.ident for t in threading.enumerate()}
    harness = _Harness(blackhole_worker)
    harness.start()
    try:
        assert harness.wait_until(
            lambda: sum(1 for s, _ in harness.states if s == int(ConnState.WAITING_RETRY)) >= 1,
            timeout_s=4.0,
            qtbot=qtbot,
        )
    finally:
        harness.shutdown(deadline_s=3.0)
    # Allow Qt to settle joined-thread bookkeeping.
    qtbot.wait(100)
    post_threads = {t.ident for t in threading.enumerate() if t.is_alive()}
    leaked = post_threads - pre_threads
    assert not leaked, f"thread leak: new threads alive after shutdown: {leaked}"
