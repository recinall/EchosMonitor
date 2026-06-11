"""Integration tests for :class:`EchosDeviceWorker` on a real ``QThread``.

Same harness shape as ``test_echos_status.py``: queued connections both
ways, the M1-A fake firmware behind ``httpx.MockTransport``, a fake
keyring so the real OS keyring is never touched, and the skill-mandated
start→stop→start + stop-during-busy probes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from PySide6.QtCore import QObject, Qt, QThread, Signal

from echosmonitor.config.credentials import CredentialsStore
from echosmonitor.core.echos_api import (
    EchosAcquisitionConfig,
    EchosApiClient,
    SeedlinkServerConfig,
)
from echosmonitor.core.echos_device_worker import EchosDeviceState, EchosDeviceWorker
from echosmonitor.core.models import EchosPollTarget
from tests.config.test_credentials import FakeKeyring
from tests.core.echos_fake import FakeEchosFirmware

_DEADLINE_MS = 5000
_THREAD_JOIN_MS = 2000

_DEVICE = "echos-field-01"


class _Trigger(QObject):
    loadRequested = Signal(object)  # noqa: N815
    acqRequested = Signal(object, object)  # noqa: N815
    slRequested = Signal(object, object)  # noqa: N815
    pwRequested = Signal(object, str)  # noqa: N815
    credRequested = Signal(str, str)  # noqa: N815
    rebootRequested = Signal(object)  # noqa: N815


class _HangingTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60.0)
        raise AssertionError("unreachable")  # pragma: no cover


def _target() -> EchosPollTarget:
    return EchosPollTarget(name=_DEVICE, host="echos-test.local", http_port=80)


@pytest.fixture
def fw() -> FakeEchosFirmware:
    return FakeEchosFirmware()


@pytest.fixture
def credentials(tmp_path: Path, fw: FakeEchosFirmware) -> CredentialsStore:
    store = CredentialsStore(fallback_dir=tmp_path, keyring_module=FakeKeyring())
    store.set_password(_DEVICE, fw.admin_password)
    return store


def _factory_for(fw: FakeEchosFirmware) -> Any:
    def factory(target: EchosPollTarget, password: str | None) -> EchosApiClient:
        return EchosApiClient(
            target.host,
            target.http_port,
            password,
            transport=fw.transport,
            retry_delay_s=0.0,
        )

    return factory


def _spawn(
    qtbot: Any, credentials: CredentialsStore, factory: Any
) -> tuple[EchosDeviceWorker, QThread, _Trigger]:
    thread = QThread()
    thread.setObjectName("echos-device-worker-test")
    worker = EchosDeviceWorker(credentials, client_factory=factory, restart_poll_interval_s=0.0)
    worker.moveToThread(thread)
    trigger = _Trigger()
    queued = Qt.ConnectionType.QueuedConnection
    trigger.loadRequested.connect(worker.requestLoad, type=queued)
    trigger.acqRequested.connect(worker.applyAcquisition, type=queued)
    trigger.slRequested.connect(worker.applySeedlink, type=queued)
    trigger.pwRequested.connect(worker.changePassword, type=queued)
    trigger.credRequested.connect(worker.storeCredential, type=queued)
    trigger.rebootRequested.connect(worker.requestReboot, type=queued)
    thread.start()
    qtbot.waitUntil(thread.isRunning, timeout=1000)
    return worker, thread, trigger


def _shutdown(worker: EchosDeviceWorker, thread: QThread) -> None:
    worker.stop()
    thread.quit()
    assert thread.wait(_THREAD_JOIN_MS), "echos-device worker thread did not join in time"


def test_load_aggregates_device_state(
    qtbot: Any, fw: FakeEchosFirmware, credentials: CredentialsStore
) -> None:
    worker, thread, trigger = _spawn(qtbot, credentials, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.loaded, timeout=_DEADLINE_MS) as blocker:
            trigger.loadRequested.emit(_target())
        (state,) = blocker.args
        assert isinstance(state, EchosDeviceState)
        assert state.acquisition.osr == 6
        assert state.seedlink.port == 18000
        assert state.network.known_networks[0].ssid == "field-net"
        assert state.ota.current_version == "1.4.2"
        assert state.calibration.phase == "idle"
        assert state.has_credentials is True
        # Selector derivation source: NSLCs parsed from StationXML.
        assert state.channels == ("XX.ECH01..HHZ", "XX.ECH01..HHN", "XX.ECH01..HHE")
    finally:
        _shutdown(worker, thread)


def test_load_reports_missing_credentials(
    qtbot: Any, fw: FakeEchosFirmware, tmp_path: Path
) -> None:
    empty_store = CredentialsStore(fallback_dir=tmp_path, keyring_module=FakeKeyring())
    worker, thread, trigger = _spawn(qtbot, empty_store, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.loaded, timeout=_DEADLINE_MS) as blocker:
            trigger.loadRequested.emit(_target())
        (state,) = blocker.args
        assert state.has_credentials is False  # public GETs still succeed
    finally:
        _shutdown(worker, thread)


def test_apply_acquisition_roundtrip(
    qtbot: Any, fw: FakeEchosFirmware, credentials: CredentialsStore
) -> None:
    worker, thread, trigger = _spawn(qtbot, credentials, _factory_for(fw))
    try:
        config = EchosAcquisitionConfig(osr=7, gain_ch0=2, gain_ch1=2, gain_ch2=2, gain_ch3=8)
        with qtbot.waitSignal(worker.applied, timeout=_DEADLINE_MS) as blocker:
            trigger.acqRequested.emit(_target(), config)
        assert blocker.args == ["acquisition"]
        assert fw.acquisition["osr"] == 7
        assert fw.acquisition["gain_ch3"] == 8
    finally:
        _shutdown(worker, thread)


def test_apply_seedlink_streams_restart_progress(
    qtbot: Any, fw: FakeEchosFirmware, credentials: CredentialsStore
) -> None:
    worker, thread, trigger = _spawn(qtbot, credentials, _factory_for(fw))
    progress: list[object] = []
    worker.restartProgress.connect(progress.append)
    try:
        config = SeedlinkServerConfig(port=18001, ring_buffer_kb=1024)
        with qtbot.waitSignal(worker.seedlinkApplied, timeout=_DEADLINE_MS) as blocker:
            trigger.slRequested.emit(_target(), config)
        (final,) = blocker.args
        assert final.is_done
        assert fw.seedlink["port"] == 18001
        assert fw.seedlink["ring_buffer_kb"] == 1024
        qtbot.waitUntil(lambda: len(progress) >= 7, timeout=_DEADLINE_MS)
        assert [s.step for s in progress] == [1, 2, 3, 4, 5, 6, 7]
    finally:
        _shutdown(worker, thread)


def test_wrong_password_fails_with_auth_kind(
    qtbot: Any, fw: FakeEchosFirmware, tmp_path: Path
) -> None:
    bad_store = CredentialsStore(fallback_dir=tmp_path, keyring_module=FakeKeyring())
    bad_store.set_password(_DEVICE, "not-the-password")
    worker, thread, trigger = _spawn(qtbot, bad_store, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.failed, timeout=_DEADLINE_MS) as blocker:
            trigger.rebootRequested.emit(_target())
        op, kind, _message, retry_after = blocker.args
        assert (op, kind, retry_after) == ("reboot", "auth_failed", 0.0)
    finally:
        _shutdown(worker, thread)


def test_lockout_carries_retry_after(
    qtbot: Any, fw: FakeEchosFirmware, credentials: CredentialsStore
) -> None:
    fw.locked = True
    worker, thread, trigger = _spawn(qtbot, credentials, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.failed, timeout=_DEADLINE_MS) as blocker:
            trigger.rebootRequested.emit(_target())
        op, kind, _message, retry_after = blocker.args
        assert (op, kind, retry_after) == ("reboot", "locked_out", 30.0)
    finally:
        _shutdown(worker, thread)


def test_change_password_updates_store_after_device(
    qtbot: Any, fw: FakeEchosFirmware, credentials: CredentialsStore
) -> None:
    worker, thread, trigger = _spawn(qtbot, credentials, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.passwordChanged, timeout=_DEADLINE_MS):
            trigger.pwRequested.emit(_target(), "rotated-pass-1")
        assert fw.admin_password == "rotated-pass-1"
        assert credentials.get_password(_DEVICE) == "rotated-pass-1"
    finally:
        _shutdown(worker, thread)


def test_failed_password_change_keeps_stored_credential(
    qtbot: Any, fw: FakeEchosFirmware, tmp_path: Path
) -> None:
    bad_store = CredentialsStore(fallback_dir=tmp_path, keyring_module=FakeKeyring())
    bad_store.set_password(_DEVICE, "not-the-password")
    worker, thread, trigger = _spawn(qtbot, bad_store, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.failed, timeout=_DEADLINE_MS):
            trigger.pwRequested.emit(_target(), "rotated-pass-1")
        # Skill ordering: the store updates only after the device's 200.
        assert bad_store.get_password(_DEVICE) == "not-the-password"
    finally:
        _shutdown(worker, thread)


def test_store_credential_off_gui_thread(
    qtbot: Any, fw: FakeEchosFirmware, tmp_path: Path
) -> None:
    store = CredentialsStore(fallback_dir=tmp_path, keyring_module=FakeKeyring())
    worker, thread, trigger = _spawn(qtbot, store, _factory_for(fw))
    try:
        with qtbot.waitSignal(worker.credentialStored, timeout=_DEADLINE_MS) as blocker:
            trigger.credRequested.emit(_DEVICE, "stored-pass-1")
        assert blocker.args == [_DEVICE]
        assert store.get_password(_DEVICE) == "stored-pass-1"
        assert fw.requests == []  # purely local — no device traffic
    finally:
        _shutdown(worker, thread)


def test_start_stop_start_cycle(
    qtbot: Any, fw: FakeEchosFirmware, credentials: CredentialsStore
) -> None:
    for _cycle in (1, 2):
        worker, thread, trigger = _spawn(qtbot, credentials, _factory_for(fw))
        try:
            with qtbot.waitSignal(worker.loaded, timeout=_DEADLINE_MS):
                trigger.loadRequested.emit(_target())
        finally:
            _shutdown(worker, thread)


def test_stop_interrupts_in_flight_request(
    qtbot: Any, credentials: CredentialsStore
) -> None:
    def factory(target: EchosPollTarget, password: str | None) -> EchosApiClient:
        return EchosApiClient(
            target.host, target.http_port, password, transport=_HangingTransport()
        )

    worker, thread, trigger = _spawn(qtbot, credentials, factory)
    trigger.loadRequested.emit(_target())
    qtbot.waitUntil(lambda: worker._in_flight is not None, timeout=3000)
    _shutdown(worker, thread)  # asserts the bounded join (rule 7)
