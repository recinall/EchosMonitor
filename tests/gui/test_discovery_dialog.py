"""DiscoveryDialog (M6) — result rendering, add hand-off, teardown."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtWidgets import QWidget

from echosmonitor.config.schema import DeviceConfig, EchosDeviceConfig
from echosmonitor.core.models import DiscoveredEchos
from echosmonitor.gui.dialogs.discovery_dialog import DiscoveryDialog


class _FakeWorker(QObject):
    """Signal-compatible EchosDiscoveryWorker stand-in (no network)."""

    deviceDiscovered = Signal(object)  # noqa: N815
    discoveryFinished = Signal(int)  # noqa: N815
    discoveryFailed = Signal(str, str)  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self.discover_calls = 0
        self.stop_calls = 0

    @Slot()
    def discover(self) -> None:
        self.discover_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class _StubStore:
    def __init__(self, devices: list[DeviceConfig] | None = None) -> None:
        self._devices = list(devices or [])

    @property
    def root(self) -> Any:
        class _Root:
            def __init__(self, devices: list[DeviceConfig]) -> None:
                self.devices = list(devices)

        return _Root(self._devices)


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
    }
    base.update(overrides)
    return DiscoveredEchos(**base)


def _make(qtbot: Any, store: _StubStore | None = None) -> tuple[DiscoveryDialog, _FakeWorker]:
    # qtbot.addWidget holds only a weakref; parent the dialog to a widget
    # the dialog itself keeps alive, or Qt deletes both mid-test.
    parent = QWidget()
    qtbot.addWidget(parent)
    worker = _FakeWorker()
    dialog = DiscoveryDialog(parent, store or _StubStore(), worker=worker)  # type: ignore[arg-type]
    dialog._test_parent_keepalive = parent  # type: ignore[attr-defined]
    qtbot.addWidget(dialog)
    return dialog, worker


def test_scan_starts_on_open_and_rows_populate(qtbot: Any) -> None:
    dialog, worker = _make(qtbot)
    try:
        qtbot.waitUntil(lambda: worker.discover_calls == 1, timeout=2000)
        assert not dialog._scan_button.isEnabled()  # scan in flight
        worker.deviceDiscovered.emit(_discovered())
        worker.discoveryFinished.emit(1)
        qtbot.waitUntil(lambda: dialog._table.rowCount() == 1, timeout=2000)
        assert "Found 1" in dialog._status_label.text()
        assert dialog._scan_button.isEnabled()
        item = dialog._table.item(0, 0)
        assert item is not None and item.text() == "ADS131M04-WebServer"
        host = dialog._table.item(0, 1)
        assert host is not None and host.text() == "echos.local"
        # Selecting the new row enables Add.
        dialog._table.setCurrentCell(0, 0)
        assert dialog._add_button.isEnabled()
    finally:
        dialog.done(0)


def test_configured_device_row_is_disabled(qtbot: Any) -> None:
    """A node whose host already exists in the config is marked and
    cannot be re-added (matched on mDNS hostname OR probed address)."""
    store = _StubStore([DeviceConfig(name="echos", host="echos.local")])
    dialog, worker = _make(qtbot, store)
    try:
        worker.deviceDiscovered.emit(_discovered())
        qtbot.waitUntil(lambda: dialog._table.rowCount() == 1, timeout=2000)
        status = dialog._table.item(0, 4)
        assert status is not None and status.text() == "already configured"
        dialog._table.setCurrentCell(0, 0)
        assert not dialog._add_button.isEnabled()
    finally:
        dialog.done(0)


def test_prefill_maps_probe_results_exactly(qtbot: Any) -> None:
    """Host prefers the mDNS hostname (survives DHCP); port is the PROBED
    SeedLink port; echos.http_port is the advertised REST port; the name
    suggestion de-collides against the config."""
    store = _StubStore([DeviceConfig(name="echos", host="10.0.0.1")])
    dialog, _worker = _make(qtbot, store)
    try:
        prefill = dialog.prefill_for(_discovered(seedlink_port=18001, http_port=8080))
        assert prefill.host == "echos.local"
        assert prefill.port == 18001
        assert isinstance(prefill.echos, EchosDeviceConfig)
        assert prefill.echos.http_port == 8080
        assert prefill.name == "echos-2"  # "echos" is taken
        # Without a hostname the probed address is the fallback host.
        bare = dialog.prefill_for(_discovered(hostname="", address="192.0.2.10"))
        assert bare.host == "192.0.2.10"
    finally:
        dialog.done(0)


def test_add_hands_off_to_device_dialog_with_prefill(qtbot: Any, monkeypatch: Any) -> None:
    calls: list[Any] = []

    def _fake_add(parent: Any, store: Any, **kwargs: Any) -> int:
        calls.append(kwargs.get("prefill"))
        return 1

    from echosmonitor.gui.dialogs import device_dialog

    monkeypatch.setattr(device_dialog.DeviceDialog, "add", staticmethod(_fake_add))
    dialog, worker = _make(qtbot)
    try:
        worker.deviceDiscovered.emit(_discovered())
        qtbot.waitUntil(lambda: dialog._table.rowCount() == 1, timeout=2000)
        dialog._table.setCurrentCell(0, 0)
        dialog._on_add_clicked()
        assert len(calls) == 1
        prefill = calls[0]
        assert isinstance(prefill, DeviceConfig)
        assert prefill.host == "echos.local"
        assert prefill.port == 18000
    finally:
        dialog.done(0)


def test_failed_scan_reports_kind_and_reenables(qtbot: Any) -> None:
    dialog, worker = _make(qtbot)
    try:
        worker.discoveryFailed.emit("unavailable", "zeroconf is not installed")
        qtbot.waitUntil(
            lambda: "unavailable" in dialog._status_label.text(), timeout=2000
        )
        assert dialog._scan_button.isEnabled()
    finally:
        dialog.done(0)


def test_teardown_stops_worker_and_joins_thread(qtbot: Any) -> None:
    dialog, worker = _make(qtbot)
    dialog.done(0)
    assert worker.stop_calls == 1
    assert not dialog._thread.isRunning()


def test_double_teardown_is_latched(qtbot: Any) -> None:
    """X-button runs closeEvent AND done: the second pass must be a
    no-op (one stop, one join attempt — never a second GUI-thread wait)."""
    dialog, worker = _make(qtbot)
    dialog.close()  # closeEvent → _teardown
    dialog.done(0)  # done → _teardown again
    assert worker.stop_calls == 1


def test_configured_match_is_case_and_dot_insensitive(qtbot: Any) -> None:
    """ECHOS.local / echos.local. / probed-address configs all count as
    already configured (M6 audit F7)."""
    store = _StubStore([DeviceConfig(name="echos", host="ECHOS.local.")])
    dialog, worker = _make(qtbot, store)
    try:
        worker.deviceDiscovered.emit(_discovered())
        qtbot.waitUntil(lambda: dialog._table.rowCount() == 1, timeout=2000)
        status = dialog._table.item(0, 4)
        assert status is not None and status.text() == "already configured"
    finally:
        dialog.done(0)
