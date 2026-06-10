"""GUI tests for :class:`StationBrowser` (M4 stage A4).

Drives the browser directly with a stub :class:`InfoWorker` and a
fake :class:`StreamingEngine`. The stub exposes the same signal
surface as the real worker so the browser's queued connections
behave identically — emitting from the stub on the test thread is
fine because pytest-qt processes events synchronously inside the
test.

No real threads, no network, no QThread — the InfoWorker thread
contract is covered by ``tests/core/test_info_client.py`` and the
worker class itself doesn't need integration coverage to assert
the UI's signal-handling logic here.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal, Slot

from echosmonitor.config.schema import (
    DeviceConfig,
    StreamSelectorConfig,
)
from echosmonitor.core.info import StationInfo, StreamInfo
from echosmonitor.core.models import ConnState
from echosmonitor.gui.widgets.station_browser import StationBrowser

# ----------------------------------------------------------------------
# Stubs
# ----------------------------------------------------------------------


class StubInfoWorker(QObject):
    """Minimal stand-in for :class:`InfoWorker`.

    Exposes the same four signals the browser connects to, plus the
    request slots — but slots merely record the call rather than
    dispatching to ``info.fetch``. Tests then drive the reply path by
    calling ``stub.<signal>.emit(...)`` directly.
    """

    stationsReceived = Signal(str, str, object)  # noqa: N815
    streamsReceived = Signal(str, str, object)  # noqa: N815
    identityReceived = Signal(str, str, object)  # noqa: N815
    infoFailed = Signal(str, str, str, str)  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self.station_calls: list[tuple[str, str, str, int]] = []
        self.stream_calls: list[tuple[str, str, str, int, str, str]] = []
        self.identity_calls: list[tuple[str, str, str, int]] = []

    @Slot(str, str, str, int)
    def requestStations(  # noqa: N802 — Qt signal-style naming
        self,
        request_id: str,
        device_id: str,
        host: str,
        port: int,
    ) -> None:
        self.station_calls.append((request_id, device_id, host, int(port)))

    @Slot(str, str, str, int, str, str)
    def requestStreams(  # noqa: N802 — Qt signal-style naming
        self,
        request_id: str,
        device_id: str,
        host: str,
        port: int,
        network: str,
        station: str,
    ) -> None:
        self.stream_calls.append((request_id, device_id, host, int(port), network, station))

    @Slot(str, str, str, int)
    def requestId(  # noqa: N802 — Qt signal-style naming
        self,
        request_id: str,
        label: str,
        host: str,
        port: int,
    ) -> None:
        self.identity_calls.append((request_id, label, host, int(port)))

    @Slot()
    def stop(self) -> None:
        return


class StubEngine(QObject):
    """Subset of :class:`StreamingEngine` the browser depends on."""

    devicesChanged = Signal()  # noqa: N815
    deviceStateChanged = Signal(str, int)  # noqa: N815

    def __init__(self, devices: Iterable[DeviceConfig] | None = None) -> None:
        super().__init__()
        self._devices: tuple[DeviceConfig, ...] = tuple(devices or ())

    def devices(self) -> tuple[DeviceConfig, ...]:
        return self._devices

    def set_devices(self, devices: Iterable[DeviceConfig]) -> None:
        self._devices = tuple(devices)
        self.devicesChanged.emit()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _device(name: str, host: str = "127.0.0.1", port: int = 18000) -> DeviceConfig:
    return DeviceConfig(
        name=name,
        host=host,
        port=port,
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
    )


def _make_browser(
    qtbot,
    *,
    devices: Iterable[DeviceConfig] | None = None,
) -> tuple[StationBrowser, StubEngine, StubInfoWorker]:
    engine = StubEngine(devices)
    worker = StubInfoWorker()
    # Casts: the browser's constructor is typed against the concrete
    # StreamingEngine / InfoWorker, but the stubs here expose the same
    # signal+method surface. The whole point of the stubs is to bypass
    # threads and real I/O while keeping the wiring intact.
    browser = StationBrowser(engine=engine, info_worker=worker)  # type: ignore[arg-type]
    qtbot.addWidget(browser)
    return browser, engine, worker


def _drain_events() -> None:
    """Flush queued cross-thread Qt signals on the test thread.

    The browser connects to the stub worker via
    ``Qt.ConnectionType.QueuedConnection`` so its emit calls land in
    the event queue rather than dispatching directly. Tests trigger
    a refresh / station-click and then drain the queue with this
    helper before asserting the stub has recorded the call.
    """
    app = QCoreApplication.instance()
    if app is None:
        return
    app.processEvents()


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


def test_initial_state_no_devices_shows_empty_state(qtbot) -> None:
    """No configured devices → "No devices configured…" empty state."""
    browser, _engine, _worker = _make_browser(qtbot, devices=())
    assert browser._device_combo.count() == 0
    # _empty_state_index_for_test returns the active empty-state index
    # or -1 when the browser page is showing instead.
    assert browser._empty_state_index_for_test() == 0  # _EMPTY_NO_DEVICES
    assert not browser._refresh_button.isEnabled()


def test_devices_populate_combo_and_refresh_works(qtbot) -> None:
    """One device configured → combo has one entry; Refresh enqueues a fetch."""
    dev = _device("iris", host="rtserve.iris.washington.edu", port=18000)
    browser, _engine, worker = _make_browser(qtbot, devices=[dev])
    assert browser._device_combo.count() == 1
    assert browser._device_combo.itemData(0) == "iris"
    assert browser._refresh_button.isEnabled()

    # Click the button via Qt's signal (offscreen platform doesn't
    # always honour synthetic mouseClick events; the click signal is
    # what we actually care about) then drain the event queue so the
    # queued ``_stationsRequested`` connection delivers to the stub.
    browser._refresh_button.click()
    _drain_events()

    assert len(worker.station_calls) == 1
    request_id, device_id, host, port = worker.station_calls[0]
    assert device_id == "iris"
    assert host == "rtserve.iris.washington.edu"
    assert port == 18000
    assert request_id == browser._pending_stations_request


def test_stations_received_renders_tree(qtbot) -> None:
    """``stationsReceived`` populates the network/station tree."""
    dev = _device("iris")
    browser, _engine, worker = _make_browser(qtbot, devices=[dev])
    # Simulate a refresh so the request id matches when we emit the reply.
    browser._on_refresh_clicked()
    _drain_events()
    request_id = browser._pending_stations_request
    assert request_id is not None

    stations: list[StationInfo] = [
        StationInfo(
            network="IU",
            station="ANMO",
            description="Albuquerque NM",
            begin=None,
            end=None,
            latitude=None,
            longitude=None,
        )
    ]
    worker.stationsReceived.emit(request_id, "iris", stations)
    _drain_events()

    assert browser._network_count_for_test() == 1
    assert browser._station_count_for_test("IU") == 1
    # Page should switch to the browser view (index 1).
    assert browser._empty_state_index_for_test() == -1


def test_stations_received_with_wrong_request_id_is_ignored(qtbot) -> None:
    """A stale stationsReceived (different request_id) is silently dropped."""
    dev = _device("iris")
    browser, _engine, worker = _make_browser(qtbot, devices=[dev])
    browser._on_refresh_clicked()
    pending = browser._pending_stations_request
    assert pending is not None

    stations: list[StationInfo] = [
        StationInfo(
            network="IU",
            station="ANMO",
            description=None,
            begin=None,
            end=None,
            latitude=None,
            longitude=None,
        )
    ]
    worker.stationsReceived.emit("not-the-pending-id", "iris", stations)

    # Tree should still be empty and spinner still running.
    assert browser._network_count_for_test() == 0
    assert browser._spinner_active is True
    assert browser._pending_stations_request == pending


def test_station_selection_triggers_streams_request(qtbot) -> None:
    """Selecting a station enqueues an ``INFO STREAMS`` fetch via the worker."""
    dev = _device("iris", host="example.com", port=18000)
    browser, _engine, worker = _make_browser(qtbot, devices=[dev])
    browser._on_refresh_clicked()
    _drain_events()
    request_id = browser._pending_stations_request
    assert request_id is not None
    stations: list[StationInfo] = [
        StationInfo(
            network="IU",
            station="ANMO",
            description="Albuquerque NM",
            begin=None,
            end=None,
            latitude=None,
            longitude=None,
        )
    ]
    worker.stationsReceived.emit(request_id, "iris", stations)
    _drain_events()

    # Pick the row out of the tree and trigger selection.
    browser._select_station_for_test("IU", "ANMO")
    _drain_events()

    assert len(worker.stream_calls) == 1
    _req_id, device_id, host, port, network, station = worker.stream_calls[0]
    assert device_id == "iris"
    assert host == "example.com"
    assert port == 18000
    assert network == "IU"
    assert station == "ANMO"


def test_streams_received_renders_table_with_checkboxes(qtbot) -> None:
    """``streamsReceived`` populates one table row per stream, all unchecked."""
    dev = _device("iris")
    browser, _engine, worker = _make_browser(qtbot, devices=[dev])
    # Stations first (so a station can be "selected" for the cache key).
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

    streams = [
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
        StreamInfo(
            network="IU",
            station="ANMO",
            location="00",
            channel="BHE",
            type="D",
            begin=None,
            end=None,
            sampling_rate=100.0,
        ),
    ]
    worker.streamsReceived.emit(streams_id, "iris", streams)
    _drain_events()

    assert browser._streams_row_count_for_test() == 3
    # Every checkbox starts unchecked.
    for row in range(3):
        item = browser._streams_table.item(row, 0)
        assert item is not None
        assert item.checkState() == Qt.CheckState.Unchecked


def test_checking_a_stream_updates_selection_label(qtbot) -> None:
    """Toggling a checkbox bumps the "X streams selected" label."""
    dev = _device("iris")
    browser, _engine, worker = _make_browser(qtbot, devices=[dev])
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
            )
        ],
    )
    _drain_events()

    assert "0 streams selected" in browser._selection_label.text()
    browser._set_check_state_for_test(0, True)
    assert "1 streams selected" in browser._selection_label.text()


def test_settings_round_trip_device_selection(qtbot) -> None:
    """Last-used device id round-trips through QSettings.

    The ``_redirect_qsettings`` autouse fixture isolates the per-test
    store so this exercises only the browser's persistence helpers,
    not the user's real settings.
    """
    dev_a = _device("alpha")
    dev_b = _device("beta")
    browser, _engine, _worker = _make_browser(qtbot, devices=[dev_a, dev_b])
    # Switch to the second device and trigger persistence directly.
    idx_b = browser._device_combo.findData("beta")
    assert idx_b >= 0
    browser._device_combo.setCurrentIndex(idx_b)
    browser._persist_settings()

    # Build a fresh widget with the same engine + a fresh stub worker
    # and assert it picks "beta" out of QSettings. ``browser`` itself
    # stays owned by ``qtbot`` so we just stop holding a reference.
    del browser
    fresh_engine = StubEngine([dev_a, dev_b])
    fresh_worker = StubInfoWorker()
    fresh = StationBrowser(engine=fresh_engine, info_worker=fresh_worker)  # type: ignore[arg-type]
    qtbot.addWidget(fresh)
    assert fresh._current_device_id() == "beta"


def test_device_offline_empty_state_after_state_change(qtbot) -> None:
    """A device with state != CONNECTED and no cached fetch shows the
    "Device offline" empty state. Covers the engine→browser state
    forwarding path so the dock surfaces the right message before the
    user clicks Refresh."""
    dev = _device("iris")
    browser, engine, _worker = _make_browser(qtbot, devices=[dev])
    engine.deviceStateChanged.emit("iris", int(ConnState.RECONNECTING))
    # Empty-state index 2 is "Device offline — refresh to retry."
    assert browser._empty_state_index_for_test() == 2
