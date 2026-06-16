"""M6.6-C: MainWindow drives the poll backoff from SeedLink CONNECTED state."""

from __future__ import annotations

from pathlib import Path

from pytestqt.qtbot import QtBot

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    EchosDeviceConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import ConnState
from echosmonitor.gui.main_window import MainWindow


def _cfg() -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(),
        devices=[
            DeviceConfig(
                name="echos",
                host="echos.local",
                selectors=[StreamSelectorConfig(network="XX", station="ECH01")],
                echos=EchosDeviceConfig(http_port=80),
            ),
        ],
    )


def test_connected_marks_streaming_and_emits_once(qtbot: QtBot) -> None:
    window = MainWindow(_cfg(), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)

    emitted: list[object] = []
    window._streamingDevicesChanged.connect(emitted.append)

    # CONNECTED → device joins the streaming set, pushed to the worker.
    window._on_device_state_streaming("echos", int(ConnState.CONNECTED))
    assert window._streaming_devices == {"echos"}
    assert emitted == [frozenset({"echos"})]

    # A second CONNECTED is a no-op (no redundant push).
    window._on_device_state_streaming("echos", int(ConnState.CONNECTED))
    assert emitted == [frozenset({"echos"})]

    # A drop resumes full cadence: device leaves the set, worker notified.
    window._on_device_state_streaming("echos", int(ConnState.WAITING_RETRY))
    assert window._streaming_devices == set()
    assert emitted[-1] == frozenset()
    window.close()


def test_non_connected_states_never_mark_streaming(qtbot: QtBot) -> None:
    window = MainWindow(_cfg(), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    emitted: list[object] = []
    window._streamingDevicesChanged.connect(emitted.append)
    for state in (ConnState.CONNECTING, ConnState.DISCONNECTED, ConnState.RECONNECTING):
        window._on_device_state_streaming("echos", int(state))
    assert window._streaming_devices == set()
    assert emitted == []  # never transitioned into streaming → no push
    window.close()
