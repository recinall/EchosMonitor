"""EchosDiscoveryWorker — browse/probe pipeline + worker canon (M6).

The mDNS browse is injected (no multicast in tests); the probe runs
against the pinned :class:`FakeEchosFirmware` transport, so what is
asserted here is the REAL gate: only candidates whose typed public probe
validates become :class:`DiscoveredEchos` results.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, Signal

from echosmonitor.core.discovery import (
    DiscoveryUnavailableError,
    EchosDiscoveryWorker,
    _Candidate,
    is_echos_candidate,
)
from echosmonitor.core.echos_api import EchosApiClient
from echosmonitor.core.models import DiscoveredEchos

from .echos_fake import FakeEchosFirmware

_THREAD_JOIN_MS = 5000
_SCAN_DEADLINE_MS = 8000

_ECHOS_ADDR = "192.0.2.10"
_PRINTER_ADDR = "192.0.2.99"

_ECHOS_CANDIDATE = _Candidate(
    instance="ADS131M04-WebServer",
    hostname="echos.local",
    address=_ECHOS_ADDR,
    http_port=80,
    board="ESP32-S3",
)
# Passes the prefilter (ESP32 board) but is NOT an Echos node — the typed
# probe must reject it.
_IMPOSTOR_CANDIDATE = _Candidate(
    instance="SomeOther-WebServer",
    hostname="other.local",
    address=_PRINTER_ADDR,
    http_port=80,
    board="ESP32-C3",
)


class _Trigger(QObject):
    scanRequested = Signal()  # noqa: N815
    probeRequested = Signal(str, int)  # noqa: N815


def _factory_for(fw: FakeEchosFirmware) -> Any:
    """Echos address → fake firmware; anything else → 404 on every path."""
    not_echos = httpx.MockTransport(lambda request: httpx.Response(404))

    def factory(address: str, http_port: int) -> EchosApiClient:
        transport = fw.transport if address == _ECHOS_ADDR else not_echos
        return EchosApiClient(
            address, http_port, transport=transport, get_retries=0, retry_delay_s=0.0
        )

    return factory


def _spawn(qtbot: Any, worker: EchosDiscoveryWorker) -> tuple[QThread, _Trigger]:
    thread = QThread()
    thread.setObjectName("echos-discovery-test")
    worker.moveToThread(thread)
    trigger = _Trigger()
    trigger.scanRequested.connect(worker.discover, type=Qt.ConnectionType.QueuedConnection)
    trigger.probeRequested.connect(worker.probe_host, type=Qt.ConnectionType.QueuedConnection)
    thread.start()
    qtbot.waitUntil(thread.isRunning, timeout=1000)
    return thread, trigger


def _shutdown(worker: EchosDiscoveryWorker, thread: QThread) -> None:
    worker.stop()
    thread.quit()
    assert thread.wait(_THREAD_JOIN_MS), "discovery thread did not join in time"


def test_prefilter_pins_the_real_advert() -> None:
    """Pinned 2026-06-12 from fw 1aa72cbe: instance ADS131M04-WebServer,
    TXT board=ESP32-S3. Substring/prefix matching, never exact — a second
    device on the LAN gets mDNS-conflict-renamed."""
    assert is_echos_candidate("ADS131M04-WebServer", "ESP32-S3")
    assert is_echos_candidate("ADS131M04-WebServer (2)", "ESP32-S3")  # conflict rename
    assert is_echos_candidate("ads131m04-webserver", "")  # name alone
    assert is_echos_candidate("Whatever", "ESP32-C3")  # board alone (probe decides)
    assert not is_echos_candidate("BrotherPrinter", "")
    assert not is_echos_candidate("nas-box", "x86_64")


def test_probe_confirms_echos_and_rejects_impostor(qtbot: Any) -> None:
    """Only the candidate whose typed public probe validates is reported;
    the result carries the PROBED SeedLink port (the DeviceConfig.port)."""
    fw = FakeEchosFirmware()

    async def browse() -> list[_Candidate]:
        return [_ECHOS_CANDIDATE, _IMPOSTOR_CANDIDATE]

    worker = EchosDiscoveryWorker(client_factory=_factory_for(fw), browse=browse)
    thread, trigger = _spawn(qtbot, worker)
    found: list[object] = []
    worker.deviceDiscovered.connect(found.append)
    try:
        with qtbot.waitSignal(worker.discoveryFinished, timeout=_SCAN_DEADLINE_MS) as blocker:
            trigger.scanRequested.emit()
        assert blocker.args == [1]
        (device,) = found
        assert isinstance(device, DiscoveredEchos)
        assert device.instance == "ADS131M04-WebServer"
        assert device.hostname == "echos.local"
        assert device.address == _ECHOS_ADDR
        assert device.http_port == 80
        assert device.seedlink_port == 18000  # probed from /api/seedlink/config
        assert device.firmware_version == "1.4.2"
        assert device.project_name == "Echos_lite_seedlink"
        assert device.board == "ESP32-S3"
    finally:
        _shutdown(worker, thread)


def test_unavailable_zeroconf_degrades_to_failed_signal(qtbot: Any) -> None:
    """A stripped install (no zeroconf) reports 'unavailable' — never a
    crashed worker thread; manual add keeps working."""

    async def browse() -> list[_Candidate]:
        raise DiscoveryUnavailableError("zeroconf is not installed")

    worker = EchosDiscoveryWorker(client_factory=_factory_for(FakeEchosFirmware()), browse=browse)
    thread, trigger = _spawn(qtbot, worker)
    try:
        with qtbot.waitSignal(worker.discoveryFailed, timeout=_SCAN_DEADLINE_MS) as blocker:
            trigger.scanRequested.emit()
        assert blocker.args[0] == "unavailable"
    finally:
        _shutdown(worker, thread)


def test_stop_cancels_inflight_scan_bounded(qtbot: Any) -> None:
    """Rule 7: stop() cancels the in-flight asyncio scan from the GUI
    thread — the join never waits out the browse window or an HTTP
    timeout, and a cancelled scan announces nothing."""
    started: list[int] = []

    async def browse() -> list[_Candidate]:
        started.append(1)
        await asyncio.sleep(60.0)  # never finishes on its own
        return []

    worker = EchosDiscoveryWorker(client_factory=_factory_for(FakeEchosFirmware()), browse=browse)
    thread, trigger = _spawn(qtbot, worker)
    announced: list[object] = []
    worker.deviceDiscovered.connect(announced.append)
    worker.discoveryFinished.connect(lambda n: announced.append(n))
    worker.discoveryFailed.connect(lambda *a: announced.append(a))
    trigger.scanRequested.emit()
    qtbot.waitUntil(lambda: bool(started), timeout=2000)  # browse in flight
    t0 = time.monotonic()
    _shutdown(worker, thread)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.5, f"teardown took {elapsed:.1f}s (cancel should be ms)"
    qtbot.wait(50)
    assert announced == []


def test_start_stop_start_cycle_not_supported_after_stop(qtbot: Any) -> None:
    """stop() is terminal for THIS worker (dialog-lifetime ownership):
    a queued discover after stop is a no-op, never a half-scan."""
    fw = FakeEchosFirmware()

    async def browse() -> list[_Candidate]:
        return [_ECHOS_CANDIDATE]

    worker = EchosDiscoveryWorker(client_factory=_factory_for(fw), browse=browse)
    thread, trigger = _spawn(qtbot, worker)
    announced: list[object] = []
    worker.discoveryFinished.connect(lambda n: announced.append(n))
    try:
        worker.stop()
        trigger.scanRequested.emit()
        qtbot.wait(150)  # give a (wrong) scan time to run
        assert announced == []
    finally:
        _shutdown(worker, thread)


def test_discover_runs_off_the_gui_thread(qtbot: Any) -> None:
    """The browse+probe coroutine executes on the worker thread (rule 1)."""
    fw = FakeEchosFirmware()
    threads: list[QThread] = []

    async def browse() -> list[_Candidate]:
        threads.append(QThread.currentThread())
        return [_ECHOS_CANDIDATE]

    worker = EchosDiscoveryWorker(client_factory=_factory_for(fw), browse=browse)
    thread, trigger = _spawn(qtbot, worker)
    try:
        with qtbot.waitSignal(worker.discoveryFinished, timeout=_SCAN_DEADLINE_MS):
            trigger.scanRequested.emit()
        assert threads == [thread]
        assert thread is not QThread.currentThread()
    finally:
        _shutdown(worker, thread)


def test_queued_discover_invokable_by_name(qtbot: Any) -> None:
    """The dialog kicks scans via a queued signal; the slot must also be
    invokable by name (QMetaObject) like the status worker's start."""
    fw = FakeEchosFirmware()

    async def browse() -> list[_Candidate]:
        return [_ECHOS_CANDIDATE]

    worker = EchosDiscoveryWorker(client_factory=_factory_for(fw), browse=browse)
    thread, _trigger = _spawn(qtbot, worker)
    try:
        with qtbot.waitSignal(worker.discoveryFinished, timeout=_SCAN_DEADLINE_MS):
            QMetaObject.invokeMethod(worker, "discover", Qt.ConnectionType.QueuedConnection)
    finally:
        _shutdown(worker, thread)


def test_note_advert_dedupes_and_caps() -> None:
    """Rule 5: the advert list is bounded (drop logged) and an
    Added→Removed→Added flap never probes twice."""
    from echosmonitor.core.discovery import _MAX_ADVERTS, _note_advert

    names: list[str] = []
    _note_advert(names, "a._http._tcp.local.")
    _note_advert(names, "a._http._tcp.local.")  # duplicate flap
    assert names == ["a._http._tcp.local."]
    for i in range(_MAX_ADVERTS + 10):
        _note_advert(names, f"svc-{i}._http._tcp.local.")
    assert len(names) == _MAX_ADVERTS


def test_results_stream_while_scan_still_running(qtbot: Any) -> None:
    """Confirmed devices are emitted AS each probe lands, not in one
    burst at the end — the dialog's rows render live."""
    import threading

    fw = FakeEchosFirmware()
    second_addr = "192.0.2.11"
    release_second = threading.Event()

    async def _gated(request: httpx.Request) -> httpx.Response:
        # Block the SECOND candidate's probe until the test releases it.
        # Poll a threading.Event: the test (GUI) thread has no handle on
        # the worker's transient asyncio.run loop to set an asyncio.Event.
        while not release_second.is_set():  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        return fw.transport.handler(request)  # type: ignore[attr-defined]

    gated_transport = httpx.MockTransport(_gated)

    def factory(address: str, http_port: int) -> EchosApiClient:
        transport = fw.transport if address == _ECHOS_ADDR else gated_transport
        return EchosApiClient(
            address, http_port, transport=transport, get_retries=0, retry_delay_s=0.0
        )

    second = _Candidate(
        instance="ADS131M04-WebServer (2)",
        hostname="echos-2.local",
        address=second_addr,
        http_port=80,
        board="ESP32-S3",
    )

    async def browse() -> list[_Candidate]:
        return [_ECHOS_CANDIDATE, second]

    worker = EchosDiscoveryWorker(client_factory=factory, browse=browse)
    thread, trigger = _spawn(qtbot, worker)
    found: list[object] = []
    finished: list[int] = []
    worker.deviceDiscovered.connect(found.append)
    worker.discoveryFinished.connect(finished.append)
    try:
        trigger.scanRequested.emit()
        # First device streams out while the second probe is still gated.
        qtbot.waitUntil(lambda: len(found) == 1, timeout=_SCAN_DEADLINE_MS)
        assert finished == []
        release_second.set()
        qtbot.waitUntil(lambda: finished == [2], timeout=_SCAN_DEADLINE_MS)
        assert len(found) == 2
    finally:
        release_second.set()
        _shutdown(worker, thread)


def test_probe_host_confirms_and_carries_hostname(qtbot: Any) -> None:
    """M6 wizard manual path: a user-entered .local host probes through
    the same typed gate; the hostname rides the payload (DHCP-stable
    config host), and the StationXML channels come along."""
    fw = FakeEchosFirmware()
    not_used = httpx.MockTransport(lambda request: httpx.Response(404))

    def factory(address: str, http_port: int) -> EchosApiClient:
        transport = fw.transport if address.casefold() == "echos.local" else not_used
        return EchosApiClient(
            address, http_port, transport=transport, get_retries=0, retry_delay_s=0.0
        )

    worker = EchosDiscoveryWorker(client_factory=factory)
    thread, trigger = _spawn(qtbot, worker)
    found: list[object] = []
    worker.deviceDiscovered.connect(found.append)
    try:
        with qtbot.waitSignal(worker.deviceDiscovered, timeout=_SCAN_DEADLINE_MS):
            # Case + trailing dot must normalize.
            trigger.probeRequested.emit("ECHOS.local.", 80)
        (device,) = found
        assert isinstance(device, DiscoveredEchos)
        assert device.hostname == "ECHOS.local"  # .local detected case-insensitively
        assert device.seedlink_port == 18000
        assert device.channels  # StationXML parsed on the worker
    finally:
        _shutdown(worker, thread)


def test_probe_host_failure_surfaces_kind(qtbot: Any) -> None:
    """Unlike the scan's silent reject, a MANUAL probe failure is the
    user's answer — discoveryFailed carries the transport kind."""
    worker = EchosDiscoveryWorker(client_factory=_factory_for(FakeEchosFirmware()))
    thread, trigger = _spawn(qtbot, worker)
    try:
        with qtbot.waitSignal(worker.discoveryFailed, timeout=_SCAN_DEADLINE_MS) as blocker:
            # 404s every path → protocol-class error.
            trigger.probeRequested.emit(_PRINTER_ADDR, 80)
        assert blocker.args[0]  # a closed-set kind, never empty
    finally:
        _shutdown(worker, thread)


def test_stop_cancels_inflight_manual_probe_bounded(qtbot: Any) -> None:
    """Skill §7 stop-during-busy-slot for the NEW probe path: stop()
    cancels the in-flight probe; nothing is announced; bounded join."""
    import threading

    probing = threading.Event()

    async def _hang(request: httpx.Request) -> httpx.Response:
        probing.set()
        await asyncio.sleep(60.0)
        return httpx.Response(404)

    hung = httpx.MockTransport(_hang)

    def factory(address: str, http_port: int) -> EchosApiClient:
        return EchosApiClient(
            address, http_port, transport=hung, get_retries=0, retry_delay_s=0.0
        )

    worker = EchosDiscoveryWorker(client_factory=factory)
    thread, trigger = _spawn(qtbot, worker)
    announced: list[object] = []
    worker.deviceDiscovered.connect(announced.append)
    worker.discoveryFailed.connect(lambda *a: announced.append(a))
    trigger.probeRequested.emit("10.0.0.9", 80)
    qtbot.waitUntil(probing.is_set, timeout=2000)  # probe in flight
    t0 = time.monotonic()
    _shutdown(worker, thread)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.5, f"teardown took {elapsed:.1f}s (cancel should be ms)"
    qtbot.wait(50)
    assert announced == []
