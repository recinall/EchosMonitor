"""Integration tests for :class:`EchosStatusWorker` on a real ``QThread``.

Mirrors ``test_info_worker.py``: the worker lives on a real QThread,
``configure`` crosses the boundary via QueuedConnection exactly as the
production GUI drives it, and snapshots come back the same way. The
device is the M1-A fake firmware behind ``httpx.MockTransport``,
injected through the worker's ``client_factory``.

Per the qt-worker-threading skill, new workers must pin:
a start→stop→start cycle (fresh worker per cycle — like InfoWorker,
a stopped worker stays stopped by design) and a stop-during-busy-slot
case (here: a transport that hangs forever inside asyncio — only the
task-cancel path in ``stop()`` can unwind it, since httpx timeouts
live in the real transport, not in a custom mock).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, Signal

from echosmonitor.core.echos_api import EchosApiClient
from echosmonitor.core.echos_status import EchosStatusWorker
from echosmonitor.core.models import ClockHealth, EchosDeviceSnapshot, EchosPollTarget
from tests.core.echos_fake import FakeEchosFirmware

_SNAPSHOT_DEADLINE_MS = 5000
_THREAD_JOIN_MS = 2000


class _Trigger(QObject):
    """Test-thread emitter so ``configure`` crosses the thread boundary
    queued, exactly as the GUI's ``_echosTargetsChanged`` signal does."""

    configureRequested = Signal(object)  # noqa: N815
    fetchRequested = Signal(object)  # noqa: N815 — StationXML one-shot (M6.6-B)
    streamingChanged = Signal(object)  # noqa: N815 — poll backoff (M6.6-C)


class _HangingTransport(httpx.AsyncBaseTransport):
    """A device that accepts the request and never answers.

    httpx timeout config is enforced by the real transport (httpcore),
    so a custom mock sleeping forever is NOT bounded by the client's
    timeouts — only ``EchosStatusWorker.stop()``'s task-cancel can
    unwind it. That makes this the sharpest stop-during-busy probe.
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60.0)
        raise AssertionError("unreachable")  # pragma: no cover


def _target(poll_interval_s: float = 1.0) -> EchosPollTarget:
    return EchosPollTarget(
        name="echos-field-01",
        host="echos-test.local",
        http_port=80,
        poll_interval_s=poll_interval_s,
    )


def _factory_for(fw: FakeEchosFirmware) -> Any:
    def factory(target: EchosPollTarget) -> EchosApiClient:
        return EchosApiClient(
            target.host,
            target.http_port,
            transport=fw.transport,
            get_retries=0,
            retry_delay_s=0.0,
        )

    return factory


def _spawn(qtbot: Any, factory: Any) -> tuple[EchosStatusWorker, QThread, _Trigger]:
    thread = QThread()
    thread.setObjectName("echos-status-test")
    worker = EchosStatusWorker(client_factory=factory)
    worker.moveToThread(thread)
    trigger = _Trigger()
    trigger.configureRequested.connect(
        worker.configure, type=Qt.ConnectionType.QueuedConnection
    )
    trigger.fetchRequested.connect(
        worker.fetch_stationxml, type=Qt.ConnectionType.QueuedConnection
    )
    trigger.streamingChanged.connect(
        worker.set_streaming, type=Qt.ConnectionType.QueuedConnection
    )
    thread.start()
    qtbot.waitUntil(thread.isRunning, timeout=1000)
    # Same queued start the production MainWindow uses — the QTimer must
    # be constructed on the worker thread (skill §5).
    QMetaObject.invokeMethod(worker, "start", Qt.ConnectionType.QueuedConnection)
    return worker, thread, trigger


def _shutdown(worker: EchosStatusWorker, thread: QThread) -> None:
    worker.stop()
    # Skill §3 barrier: stop the worker-thread QTimer on its own thread
    # before quit, exactly as MainWindow.closeEvent does.
    QMetaObject.invokeMethod(worker, "release", Qt.ConnectionType.BlockingQueuedConnection)
    thread.quit()
    assert thread.wait(_THREAD_JOIN_MS), "echos-status thread did not join in time"


def test_snapshot_crosses_real_thread(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS) as blocker:
            trigger.configureRequested.emit((_target(),))
        (snapshot,) = blocker.args
        assert isinstance(snapshot, EchosDeviceSnapshot)
        assert snapshot.device == "echos-field-01"
        assert snapshot.firmware_version == "1.4.2"
        assert snapshot.gnss_fix is True
        assert snapshot.gnss_satellites == 9
        assert snapshot.pps_locked is True
        assert snapshot.clients_connected == 1
        assert snapshot.ring_used_pct == 12.5
        assert snapshot.calibration_state == "idle"
        # Clock sync (M6) — mapped 1:1 from the fake's pinned real shapes.
        assert snapshot.time_synchronized is True
        assert snapshot.ntp_synchronized is True
        assert snapshot.time_sync_type == "RMC+PPS+NTP"
        assert snapshot.pps_offset_us == -4
        assert snapshot.clock_health() is ClockHealth.PPS
        assert snapshot.polled_at > 0.0
    finally:
        _shutdown(worker, thread)


def test_device_is_repolled_on_interval(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    try:
        # 1 s interval → the second poll lands two scheduler ticks later.
        with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS):
            trigger.configureRequested.emit((_target(poll_interval_s=1.0),))
        with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS):
            pass  # second poll arrives without any new configure
        status_polls = [r for r in fw.requests if r == ("GET", "/api/status")]
        assert len(status_polls) >= 2
    finally:
        _shutdown(worker, thread)


def test_poll_failure_emits_closed_kind(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    fw.flaky["/api/status"] = 10**6  # unreachable forever
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.pollFailed, timeout=_SNAPSHOT_DEADLINE_MS) as blocker:
            trigger.configureRequested.emit((_target(),))
        device, kind, message = blocker.args
        assert device == "echos-field-01"
        assert kind == "unreachable"
        assert "echos-test.local" in message
    finally:
        _shutdown(worker, thread)


def test_bad_configure_payload_is_ignored(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    try:
        # Garbage payloads must not crash the worker thread (rule 4
        # isinstance guard) — and a valid configure afterwards works.
        trigger.configureRequested.emit("not a tuple")
        trigger.configureRequested.emit(("not", "targets"))
        with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS):
            trigger.configureRequested.emit((_target(),))
    finally:
        _shutdown(worker, thread)


def test_start_stop_start_cycle(qtbot: Any) -> None:
    """Two full worker lifecycles against the same fake (skill §7)."""
    fw = FakeEchosFirmware()
    for _cycle in (1, 2):
        worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
        try:
            with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS):
                trigger.configureRequested.emit((_target(),))
        finally:
            _shutdown(worker, thread)


def test_stop_interrupts_in_flight_poll(qtbot: Any) -> None:
    """``stop()`` from the test (GUI) thread unwinds a hung poll promptly.

    The hanging transport would block the poll slot for 60 s; the join
    below succeeds within the 2 s budget only if ``stop()`` actually
    cancels the in-flight asyncio task (rule 7: every wait bounded,
    observable, interruptible).
    """

    def factory(target: EchosPollTarget) -> EchosApiClient:
        return EchosApiClient(
            target.host, target.http_port, transport=_HangingTransport(), get_retries=0
        )

    worker, thread, trigger = _spawn(qtbot, factory)
    trigger.configureRequested.emit((_target(),))
    # Wait until the poll is genuinely in flight — the cancel must reach
    # an installed task, not win a race against poll start.
    qtbot.waitUntil(lambda: worker._in_flight is not None, timeout=3000)
    _shutdown(worker, thread)  # asserts the bounded join


def test_shutdown_stops_worker_timer_via_release_barrier(qtbot: Any) -> None:
    """Regression for the release() barrier (M1-D hygiene fix).

    Without the BlockingQueuedConnection release() in ``_shutdown``, the
    worker's QTimer is still ACTIVE after the join; whenever Python later
    collects the worker, the timer is destroyed from a foreign thread and
    Qt warns "Timers cannot be stopped from another thread". Asserting
    the timer is inactive right after shutdown pins the barrier
    deterministically. (A qtlog-based pin is impossible: pytest-qt's own
    waitSignal cleanup emits the same warning text benignly whenever a
    cross-thread signal ends its blocker, so the message is not ours to
    assert on.)
    """
    fw = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS):
        trigger.configureRequested.emit((_target(),))
    assert worker._timer is not None and worker._timer.isActive()
    _shutdown(worker, thread)
    assert worker._timer is not None
    assert not worker._timer.isActive()


def test_stopped_worker_emits_nothing(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    try:
        worker.stop()
        emitted: list[object] = []
        worker.snapshotReady.connect(emitted.append)
        trigger.configureRequested.emit((_target(),))
        qtbot.wait(700)  # > one scheduler tick
        assert emitted == []
        assert fw.requests == []
    finally:
        _shutdown(worker, thread)


class _StationXmlErrorTransport(httpx.AsyncBaseTransport):
    """Answers /api/stationxml with a 500 so the fetch helper returns None."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")


def test_stationxml_fetch_crosses_thread(qtbot: Any) -> None:
    """M6.6-B: a one-shot fetch returns the device XML on the GUI thread."""
    fw = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.stationXmlReady, timeout=_SNAPSHOT_DEADLINE_MS) as blocker:
            trigger.fetchRequested.emit((_target(),))
        device, xml = blocker.args
        assert device == "echos-field-01"
        assert isinstance(xml, str)
        assert "FDSNStationXML" in xml
    finally:
        _shutdown(worker, thread)


def test_stationxml_fetch_failure_emits_none(qtbot: Any) -> None:
    """A transport error degrades to None (graceful) without raising."""

    def factory(target: EchosPollTarget) -> EchosApiClient:
        return EchosApiClient(
            target.host,
            target.http_port,
            transport=_StationXmlErrorTransport(),
            get_retries=0,
            retry_delay_s=0.0,
        )

    worker, thread, trigger = _spawn(qtbot, factory)
    try:
        with qtbot.waitSignal(worker.stationXmlReady, timeout=_SNAPSHOT_DEADLINE_MS) as blocker:
            trigger.fetchRequested.emit((_target(),))
        device, xml = blocker.args
        assert device == "echos-field-01"
        assert xml is None
    finally:
        _shutdown(worker, thread)


def test_stationxml_bad_payload_is_ignored(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    try:
        emitted: list[object] = []
        worker.stationXmlReady.connect(lambda *a: emitted.append(a))
        trigger.fetchRequested.emit("not-a-tuple")
        qtbot.wait(300)
        assert emitted == []
    finally:
        _shutdown(worker, thread)


def _fast_target(streaming_s: float = 10.0) -> EchosPollTarget:
    """A target that polls every tick normally but backs off hard when
    streaming — so a window count cleanly distinguishes the two cadences."""
    return EchosPollTarget(
        name="echos-field-01",
        host="echos-test.local",
        http_port=80,
        poll_interval_s=0.2,
        poll_interval_streaming_s=streaming_s,
    )


def test_streaming_device_backs_off_to_heartbeat(qtbot: Any) -> None:
    """M6.6-C: a CONNECTED device polls once then waits the slow heartbeat,
    while a non-streaming device re-polls every tick."""
    # Baseline: not streaming → many polls in the window.
    fw_a = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw_a))
    try:
        with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS):
            trigger.configureRequested.emit((_fast_target(),))
        qtbot.wait(1600)
        baseline = len([r for r in fw_a.requests if r == ("GET", "/api/status")])
    finally:
        _shutdown(worker, thread)
    assert baseline >= 3  # ~every 500 ms tick

    # Streaming: mark CONNECTED before configuring → one poll, then 10 s away.
    fw_b = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw_b))
    try:
        trigger.streamingChanged.emit(frozenset({"echos-field-01"}))
        with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS):
            trigger.configureRequested.emit((_fast_target(),))
        qtbot.wait(1600)
        streaming_polls = len([r for r in fw_b.requests if r == ("GET", "/api/status")])
    finally:
        _shutdown(worker, thread)
    assert streaming_polls == 1  # only the initial poll; heartbeat is 10 s


def test_stream_drop_resumes_full_cadence(qtbot: Any) -> None:
    """When a streaming device stops streaming it is made due immediately,
    so full-cadence polling resumes at once (reboot-vs-hiccup detection)."""
    fw = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    try:
        trigger.streamingChanged.emit(frozenset({"echos-field-01"}))
        with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS):
            trigger.configureRequested.emit((_fast_target(),))
        qtbot.wait(800)
        before = len([r for r in fw.requests if r == ("GET", "/api/status")])
        assert before == 1  # backed off
        # Stream drops → resume full cadence.
        trigger.streamingChanged.emit(frozenset())
        qtbot.wait(1200)
        after = len([r for r in fw.requests if r == ("GET", "/api/status")])
        assert after >= before + 2  # polling resumed at full cadence
    finally:
        _shutdown(worker, thread)


def test_set_streaming_bad_payload_is_ignored(qtbot: Any) -> None:
    fw = FakeEchosFirmware()
    worker, thread, trigger = _spawn(qtbot, _factory_for(fw))
    try:
        trigger.streamingChanged.emit("not-a-collection")
        trigger.streamingChanged.emit([1, 2, 3])  # non-str members
        with qtbot.waitSignal(worker.snapshotReady, timeout=_SNAPSHOT_DEADLINE_MS):
            trigger.configureRequested.emit((_target(),))
    finally:
        _shutdown(worker, thread)
