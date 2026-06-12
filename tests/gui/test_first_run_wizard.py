"""FirstRunWizard (M6 Echos rewrite) — paths, prefill, finish, teardown.

Both workers are injected fakes (no network, no keyring): the wizard's
own contract is what's pinned — page flow, the probe-confirmed-only
selection, the DeviceConfig written through the real ConfigStore, and
the bounded credential-store finish.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from echosmonitor.config.schema import AppConfig, DeviceConfig, RootConfig, UiConfig
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.core.models import DiscoveredEchos
from echosmonitor.gui.dialogs import first_run_wizard as wizard_mod
from echosmonitor.gui.dialogs.first_run_wizard import (
    FirstRunWizard,
    device_config_for,
    suggest_device_name,
)

_CHANNELS = ("XX.ECH01.00.HHZ", "XX.ECH01.00.HHN", "XX.ECH01.00.HHE")


class _FakeDiscovery(QObject):
    deviceDiscovered = Signal(object)  # noqa: N815
    discoveryFinished = Signal(int)  # noqa: N815
    discoveryFailed = Signal(str, str)  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self.scan_calls = 0
        self.probe_calls: list[tuple[str, int]] = []
        self.stop_calls = 0

    @Slot()
    def discover(self) -> None:
        self.scan_calls += 1

    @Slot(str, int)
    def probe_host(self, host: str, http_port: int) -> None:
        self.probe_calls.append((host, int(http_port)))

    def stop(self) -> None:
        self.stop_calls += 1


class _FakeDeviceWorker(QObject):
    credentialStored = Signal(str)  # noqa: N815

    def __init__(self, *, emit_stored: bool = True) -> None:
        super().__init__()
        self._emit_stored = emit_stored
        self.store_calls: list[tuple[str, str]] = []

    @Slot(str, str)
    def storeCredential(self, device_key: str, password: str) -> None:  # noqa: N802
        self.store_calls.append((device_key, password))
        if self._emit_stored:
            self.credentialStored.emit(device_key)

    def stop(self) -> None:
        pass


def _discovered(**overrides: Any) -> DiscoveredEchos:
    base: dict[str, Any] = {
        "instance": "ADS131M04-WebServer",
        "hostname": "echos.local",
        "address": "192.0.2.10",
        "http_port": 80,
        "seedlink_port": 18000,
        "firmware_version": "1.4.2",
        "project_name": "Echos_lite_seedlink",
        "board": "ESP32-S3",
        "channels": _CHANNELS,
    }
    base.update(overrides)
    return DiscoveredEchos(**base)


def _make_store(tmp_path: Path, devices: list[DeviceConfig] | None = None) -> ConfigStore:
    return ConfigStore(
        RootConfig(app=AppConfig(), ui=UiConfig(), devices=devices or []),
        tmp_path / "config.yaml",
    )


def _make(
    qtbot: Any,
    store: ConfigStore,
    *,
    device_worker: _FakeDeviceWorker | None = None,
) -> tuple[FirstRunWizard, _FakeDiscovery, _FakeDeviceWorker]:
    discovery = _FakeDiscovery()
    worker = device_worker or _FakeDeviceWorker()
    wizard = FirstRunWizard(
        store=store,
        discovery_worker=discovery,  # type: ignore[arg-type]
        device_worker=worker,  # type: ignore[arg-type]
    )
    qtbot.addWidget(wizard)
    wizard.restart()  # enter the start page (next() is a no-op before)
    return wizard, discovery, worker


def test_device_config_for_maps_probe_exactly() -> None:
    """Pure mapping: mDNS hostname, PROBED seedlink port, REST port,
    StationXML-exact selectors (malformed NSLCs skipped)."""
    device = _discovered(channels=(*_CHANNELS, "bad-nslc"))
    cfg = device_config_for(device, "echos")
    assert cfg.name == "echos"
    assert cfg.host == "echos.local"
    assert cfg.port == 18000
    assert cfg.echos is not None and cfg.echos.http_port == 80
    assert [(s.network, s.station, s.location, s.channel) for s in cfg.selectors] == [
        ("XX", "ECH01", "00", "HHZ"),
        ("XX", "ECH01", "00", "HHN"),
        ("XX", "ECH01", "00", "HHE"),
    ]
    # No hostname → the probed address is the config host.
    assert device_config_for(_discovered(hostname=""), "x").host == "192.0.2.10"


def test_suggest_device_name_decollides() -> None:
    device = _discovered()
    assert suggest_device_name(device, set()) == "echos"
    assert suggest_device_name(device, {"echos"}) == "echos-2"
    assert suggest_device_name(device, {"echos", "echos-2"}) == "echos-3"
    assert suggest_device_name(_discovered(hostname=""), set()) == "echos"


def test_skip_path_writes_no_device(qtbot, tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    wizard, _discovery, worker = _make(qtbot, store)
    wizard._welcome._skip_radio.setChecked(True)
    assert wizard._welcome.nextId() == -1  # Next becomes Finish
    wizard.accept()
    assert store.root.devices == []
    assert worker.store_calls == []
    assert not wizard._thread.isRunning()


def test_scan_path_writes_probed_device(qtbot, tmp_path: Path) -> None:
    """Welcome(default scan) → Find auto-scans, a confirmed node streams
    in and auto-selects → Details prefills the name → Finish writes the
    exact DeviceConfig through the real ConfigStore."""
    store = _make_store(tmp_path)
    wizard, discovery, worker = _make(qtbot, store)
    try:
        wizard.next()  # → Find: auto-scan fires
        qtbot.waitUntil(lambda: discovery.scan_calls == 1, timeout=2000)
        discovery.deviceDiscovered.emit(_discovered())
        discovery.discoveryFinished.emit(1)
        qtbot.waitUntil(lambda: wizard._find.selected_device() is not None, timeout=2000)
        wizard.next()  # → Details
        assert wizard._details.device_name() == "echos"
        assert "3 channels" in wizard._details._summary.text()
        wizard.accept()  # no password → immediate finish
        (device,) = store.root.devices
        assert device.name == "echos"
        assert device.host == "echos.local"
        assert device.port == 18000
        assert device.echos is not None and device.echos.http_port == 80
        assert len(device.selectors) == 3
        assert worker.store_calls == []  # no password entered
    finally:
        wizard.done(0)


def test_ap_path_prefills_gateway_and_probes(qtbot, tmp_path: Path) -> None:
    """AP mode: no auto-scan, host prefilled 192.168.4.1, Check device
    drives the manual probe; the confirmed node becomes selectable."""
    store = _make_store(tmp_path)
    wizard, discovery, _worker = _make(qtbot, store)
    try:
        wizard._welcome._ap_radio.setChecked(True)
        wizard.next()  # → Find
        assert discovery.scan_calls == 0
        assert wizard._find._host_edit.text() == "192.168.4.1"
        wizard._find._on_probe_clicked()
        qtbot.waitUntil(lambda: discovery.probe_calls == [("192.168.4.1", 80)], timeout=2000)
        discovery.deviceDiscovered.emit(
            _discovered(instance="192.168.4.1", hostname="", address="192.168.4.1")
        )
        qtbot.waitUntil(lambda: wizard._find.selected_device() is not None, timeout=2000)
        selected = wizard._find.selected_device()
        assert selected is not None and selected.address == "192.168.4.1"
    finally:
        wizard.done(0)


def test_password_is_stored_off_thread_before_accept(qtbot, tmp_path: Path) -> None:
    """A typed password goes to the device worker (keyring lives off the
    GUI thread); the wizard accepts only after credentialStored."""
    store = _make_store(tmp_path)
    wizard, discovery, worker = _make(qtbot, store)
    accepted: list[int] = []
    wizard.accepted.connect(lambda: accepted.append(1))
    try:
        wizard.next()
        discovery.deviceDiscovered.emit(_discovered())
        qtbot.waitUntil(lambda: wizard._find.selected_device() is not None, timeout=2000)
        wizard.next()
        wizard._details._password_edit.setText("hunter22!pw")
        wizard.accept()
        # The device is written immediately; acceptance waits for the
        # queued keyring round-trip.
        assert len(store.root.devices) == 1
        qtbot.waitUntil(lambda: accepted == [1], timeout=4000)
        assert worker.store_calls == [("echos", "hunter22!pw")]
    finally:
        wizard.done(0)


def test_credential_timeout_accepts_with_warning(qtbot, tmp_path: Path, monkeypatch) -> None:
    """Rule 7: a hung keyring cannot wedge the Finish — the wizard
    accepts after the bounded wait and tells the user to store later."""
    monkeypatch.setattr(wizard_mod, "_CREDENTIAL_TIMEOUT_MS", 100)
    warnings: list[str] = []
    monkeypatch.setattr(
        wizard_mod.QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: warnings.append(str(a[2]))),
    )
    store = _make_store(tmp_path)
    wizard, discovery, _worker = _make(
        qtbot, store, device_worker=_FakeDeviceWorker(emit_stored=False)
    )
    accepted: list[int] = []
    wizard.accepted.connect(lambda: accepted.append(1))
    try:
        wizard.next()
        discovery.deviceDiscovered.emit(_discovered())
        qtbot.waitUntil(lambda: wizard._find.selected_device() is not None, timeout=2000)
        wizard.next()
        wizard._details._password_edit.setText("pw-that-hangs")
        wizard.accept()
        qtbot.waitUntil(lambda: accepted == [1], timeout=4000)
        assert warnings and "keyring" in warnings[0]
        assert len(store.root.devices) == 1  # the device write survived
    finally:
        wizard.done(0)


def test_name_collision_blocks_finish_until_renamed(qtbot, tmp_path: Path) -> None:
    store = _make_store(
        tmp_path, devices=[DeviceConfig(name="echos", host="10.0.0.9")]
    )
    wizard, discovery, _worker = _make(qtbot, store)
    try:
        wizard.next()
        discovery.deviceDiscovered.emit(_discovered())
        qtbot.waitUntil(lambda: wizard._find.selected_device() is not None, timeout=2000)
        wizard.next()
        # Suggestion already avoids the collision.
        assert wizard._details.device_name() == "echos-2"
        wizard._details._name_edit.setText("echos")
        assert not wizard._details.isComplete()
        wizard._details._name_edit.setText("echos-field")
        assert wizard._details.isComplete()
    finally:
        wizard.done(0)


def test_teardown_joins_worker_thread(qtbot, tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    wizard, discovery, _worker = _make(qtbot, store)
    wizard.done(0)
    assert discovery.stop_calls == 1
    assert not wizard._thread.isRunning()
    wizard.done(0)  # latch: second pass is a no-op
    assert discovery.stop_calls == 1


def test_undriven_wizard_owns_no_running_thread(qtbot, tmp_path: Path) -> None:
    """The thread starts lazily on the first page action: a wizard that
    is opened and closed without being driven (Help menu, patched exec)
    never reaches done() — an eagerly-started thread would then be GC'd
    while running, which is a hard Qt abort (the menubar-test crash)."""
    store = _make_store(tmp_path)
    wizard, _discovery, _worker = _make(qtbot, store)
    assert not wizard._thread.isRunning()
    wizard.done(0)
