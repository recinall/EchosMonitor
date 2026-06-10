"""GUI tests for the station-browser's "Add to device..." flow (M4 stage B).

Covers the new wiring on top of the Stage-A test surface:

* The Add-to-device button enables once at least one stream is checked.
* Clicking with "existing" routes through ``store.add_selectors``.
* Clicking with "new" opens :class:`DeviceDialog.add` prefilled with
  the source endpoint + selected selectors.

Reuses the StubInfoWorker / StubEngine pattern from
``test_station_browser.py`` and a lightweight stub store.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal, Slot

from echosmonitor.config.schema import (
    DeviceConfig,
    StreamSelectorConfig,
)
from echosmonitor.core.info import StationInfo, StreamInfo
from echosmonitor.gui.widgets.station_browser import StationBrowser

# ----------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------


class StubInfoWorker(QObject):
    stationsReceived = Signal(str, str, object)  # noqa: N815
    streamsReceived = Signal(str, str, object)  # noqa: N815
    identityReceived = Signal(str, str, object)  # noqa: N815
    infoFailed = Signal(str, str, str, str)  # noqa: N815

    def __init__(self) -> None:
        super().__init__()

    @Slot(str, str, str, int)
    def requestStations(  # noqa: N802
        self, request_id: str, device_id: str, host: str, port: int
    ) -> None:
        return

    @Slot(str, str, str, int, str, str)
    def requestStreams(  # noqa: N802
        self,
        request_id: str,
        device_id: str,
        host: str,
        port: int,
        network: str,
        station: str,
    ) -> None:
        return

    @Slot(str, str, str, int)
    def requestId(  # noqa: N802
        self, request_id: str, label: str, host: str, port: int
    ) -> None:
        return

    @Slot()
    def stop(self) -> None:
        return


class StubEngine(QObject):
    devicesChanged = Signal()  # noqa: N815
    deviceStateChanged = Signal(str, int)  # noqa: N815

    def __init__(self, devices: Iterable[DeviceConfig] | None = None) -> None:
        super().__init__()
        self._devices: tuple[DeviceConfig, ...] = tuple(devices or ())

    def devices(self) -> tuple[DeviceConfig, ...]:
        return self._devices


class StubConfigStore(QObject):
    """Tiny ConfigStore stand-in. Records selector / device additions."""

    configChanged = Signal()  # noqa: N815

    def __init__(self, devices: list[DeviceConfig] | None = None) -> None:
        super().__init__()
        self._devices: list[DeviceConfig] = list(devices or [])
        self.add_selector_calls: list[tuple[str, list[StreamSelectorConfig]]] = []
        self.add_device_calls: list[DeviceConfig] = []

    @property
    def root(self) -> Any:
        class _Root:
            def __init__(self, devs: list[DeviceConfig]) -> None:
                self.devices = list(devs)

        return _Root(self._devices)

    def add_selectors(self, name: str, selectors: list[StreamSelectorConfig]) -> None:
        self.add_selector_calls.append((name, list(selectors)))
        for i, d in enumerate(self._devices):
            if d.name == name:
                merged = [*d.selectors, *selectors]
                self._devices[i] = d.model_copy(update={"selectors": merged})
                break
        self.configChanged.emit()

    def add_device(self, cfg: DeviceConfig) -> None:
        self.add_device_calls.append(cfg)
        self._devices.append(cfg)
        self.configChanged.emit()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _device(
    name: str = "iris",
    host: str = "rtserve.iris.washington.edu",
    port: int = 18000,
) -> DeviceConfig:
    return DeviceConfig(
        name=name,
        host=host,
        port=port,
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
    )


def _drain_events() -> None:
    app = QCoreApplication.instance()
    if app is None:
        return
    app.processEvents()


def _make_browser(
    qtbot,
    *,
    devices: list[DeviceConfig] | None = None,
    store: StubConfigStore | None = None,
) -> tuple[StationBrowser, StubEngine, StubInfoWorker, StubConfigStore]:
    engine = StubEngine(devices)
    worker = StubInfoWorker()
    if store is None:
        store = StubConfigStore(devices)
    browser = StationBrowser(
        engine=engine,  # type: ignore[arg-type]
        info_worker=worker,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
    )
    qtbot.addWidget(browser)
    return browser, engine, worker, store


def _populate_streams(browser: StationBrowser, worker: StubInfoWorker) -> None:
    """Drive a stations -> streams round-trip so the table has rows."""
    browser._on_refresh_clicked()
    _drain_events()
    stations_id = browser._pending_stations_request
    assert stations_id is not None
    worker.stationsReceived.emit(
        stations_id,
        "iris",
        [
            StationInfo(
                network="IU",
                station="ANMO",
                description=None,
                begin=None,
                end=None,
                latitude=None,
                longitude=None,
            )
        ],
    )
    _drain_events()
    browser._select_station_for_test("IU", "ANMO")
    _drain_events()
    streams_id = browser._pending_streams_request
    assert streams_id is not None
    worker.streamsReceived.emit(
        streams_id,
        "iris",
        [
            StreamInfo(
                network="IU",
                station="ANMO",
                location="00",
                channel="BHZ",
                type="D",
                begin=None,
                end=None,
                sampling_rate=100.0,
            ),
            StreamInfo(
                network="IU",
                station="ANMO",
                location="00",
                channel="BHN",
                type="D",
                begin=None,
                end=None,
                sampling_rate=100.0,
            ),
        ],
    )
    _drain_events()


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_add_to_device_button_enabled_when_streams_checked(qtbot) -> None:
    """Checking at least one stream enables the Add-to-device button."""
    dev = _device()
    browser, _engine, worker, _store = _make_browser(qtbot, devices=[dev])
    _populate_streams(browser, worker)

    # Initial state: nothing checked, button disabled.
    assert browser._add_to_device_button.isEnabled() is False
    # Check two streams; button enables.
    browser._set_check_state_for_test(0, True)
    browser._set_check_state_for_test(1, True)
    _drain_events()
    assert browser._add_to_device_button.isEnabled() is True


def test_add_to_existing_device_calls_add_selectors(qtbot, monkeypatch: pytest.MonkeyPatch) -> None:
    """OK with "existing" -> store.add_selectors with the checked selectors."""
    dev = _device()
    browser, _engine, worker, store = _make_browser(qtbot, devices=[dev])
    _populate_streams(browser, worker)
    browser._set_check_state_for_test(0, True)
    browser._set_check_state_for_test(1, True)
    _drain_events()

    # Force-accept the small popup. The dialog runs ``self.exec()``
    # which would block the test; we monkeypatch to auto-accept.
    from echosmonitor.gui.widgets import station_browser as sb_mod

    captured: list[Any] = []

    class _AutoAccept(sb_mod._AddToDeviceDialog):  # type: ignore[misc, name-defined]
        def exec(self) -> int:
            captured.append(self)
            # "Add to existing" radio is auto-checked when the endpoint
            # has matching devices, which it does here (the source dev).
            return int(sb_mod.QDialog.DialogCode.Accepted)

    monkeypatch.setattr(sb_mod, "_AddToDeviceDialog", _AutoAccept)

    browser._add_to_device_button.click()
    _drain_events()

    assert len(store.add_selector_calls) == 1
    name, selectors = store.add_selector_calls[0]
    assert name == "iris"
    nslc_set = {(s.network, s.station, s.location, s.channel) for s in selectors}
    assert nslc_set == {("IU", "ANMO", "00", "BHZ"), ("IU", "ANMO", "00", "BHN")}
    # After a successful add, the checkboxes were cleared.
    for row in range(browser._streams_row_count_for_test()):
        item = browser._streams_table.item(row, 0)
        assert item is not None
        assert item.checkState().value == 0  # Qt.CheckState.Unchecked


def test_add_as_new_device_opens_device_dialog_prefilled(
    qtbot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OK with "new" opens :class:`DeviceDialog.add` with prefill."""
    dev = _device()
    browser, _engine, worker, store = _make_browser(qtbot, devices=[dev])
    _populate_streams(browser, worker)
    browser._set_check_state_for_test(0, True)
    _drain_events()

    from echosmonitor.gui.widgets import station_browser as sb_mod

    # Force the popup to choose "new device" + accept.
    class _AutoAcceptNew(sb_mod._AddToDeviceDialog):  # type: ignore[misc, name-defined]
        def exec(self) -> int:
            self._new_radio.setChecked(True)
            return int(sb_mod.QDialog.DialogCode.Accepted)

    monkeypatch.setattr(sb_mod, "_AddToDeviceDialog", _AutoAcceptNew)

    # Capture the prefill the browser passes to DeviceDialog.add.
    # Wrap in a classmethod-shaped callable so the unbound vs bound
    # call site matches what `DeviceDialog.add(...)` expects.
    captured_prefills: list[DeviceConfig] = []

    def fake_add(cls: Any, parent: Any, store_arg: Any, *, prefill: Any = None) -> int:
        captured_prefills.append(prefill)
        # Simulate user cancellation so the test doesn't have to drive
        # the form widget; the prefill is what we want to assert on.
        from PySide6.QtWidgets import QDialog as _QDialog

        return int(_QDialog.DialogCode.Rejected)

    from echosmonitor.gui.dialogs import device_dialog as dd_mod

    monkeypatch.setattr(dd_mod.DeviceDialog, "add", classmethod(fake_add))

    browser._add_to_device_button.click()
    _drain_events()

    assert len(captured_prefills) == 1
    prefill = captured_prefills[0]
    assert prefill is not None
    assert prefill.host == dev.host
    assert int(prefill.port) == int(dev.port)
    assert len(prefill.selectors) == 1
    sel = prefill.selectors[0]
    assert (sel.network, sel.station, sel.location, sel.channel) == (
        "IU",
        "ANMO",
        "00",
        "BHZ",
    )
    # No add_selectors call happened: the user picked "new", not
    # "existing".
    assert store.add_selector_calls == []
