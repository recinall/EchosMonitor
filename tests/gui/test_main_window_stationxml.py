"""M6.6-B: MainWindow StationXML fetch/persist orchestration (GUI glue).

The off-thread fetch, engine persistence, and response resolution are
covered by focused unit tests; here we assert the MainWindow wiring:
entering acquisition requests one fetch (de-duped), a fetched blob is
registered with the response provider, and Idle resets the de-dup flag.
"""

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
from echosmonitor.core.models import AcquisitionState, EchosPollTarget
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
            DeviceConfig(name="plain", host="seedlink.example"),  # no echos section
        ],
    )


def test_acquisition_triggers_single_stationxml_fetch(qtbot: QtBot) -> None:
    window = MainWindow(_cfg(), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)

    requested: list[object] = []
    window._stationXmlFetchRequested.connect(requested.append)

    # Entering monitoring requests exactly one fetch carrying the target.
    window._on_acquisition_stationxml("echos", int(AcquisitionState.MONITORING))
    assert len(requested) == 1
    (targets,) = requested
    assert isinstance(targets, tuple)
    assert isinstance(targets[0], EchosPollTarget)
    assert targets[0].name == "echos"

    # Monitoring → recording does NOT re-fetch (already requested).
    window._on_acquisition_stationxml("echos", int(AcquisitionState.RECORDING))
    assert len(requested) == 1

    # Returning to idle resets the de-dup flag; the next acquisition re-fetches.
    window._on_acquisition_stationxml("echos", int(AcquisitionState.IDLE))
    window._on_acquisition_stationxml("echos", int(AcquisitionState.MONITORING))
    assert len(requested) == 2

    window.close()


def test_recording_reuses_cached_blob_to_persist(qtbot: QtBot) -> None:
    """Monitoring→Recording reuses the already-fetched blob: entering
    Recording persists it without issuing a new fetch (M6.6-B reuse path)."""
    window = MainWindow(_cfg(), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)

    persisted: list[tuple[str, str]] = []
    window._engine.persist_session_stationxml = (  # type: ignore[method-assign]
        lambda device, xml: persisted.append((device, xml)) or True
    )

    # Fetch arrives during monitoring → cached.
    window._on_acquisition_stationxml("echos", int(AcquisitionState.MONITORING))
    window._on_stationxml_ready("echos", "<FDSNStationXML/>")
    # Entering recording reuses the cached blob to persist it.
    persisted.clear()
    window._on_acquisition_stationxml("echos", int(AcquisitionState.RECORDING))
    assert persisted == [("echos", "<FDSNStationXML/>")]
    window.close()


def test_non_echos_device_never_fetches(qtbot: QtBot) -> None:
    window = MainWindow(_cfg(), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    requested: list[object] = []
    window._stationXmlFetchRequested.connect(requested.append)
    window._on_acquisition_stationxml("plain", int(AcquisitionState.MONITORING))
    assert requested == []
    assert window._echos_target_for("plain") is None
    window.close()


def test_stationxml_ready_registers_blob_with_provider(qtbot: QtBot) -> None:
    window = MainWindow(_cfg(), Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)

    assert window._response_provider.is_configured("echos") is False
    window._on_stationxml_ready("echos", "<FDSNStationXML/>")
    # The blob is cached and registered so live decon can use it.
    assert window._stationxml_blobs["echos"] == "<FDSNStationXML/>"
    assert window._response_provider.is_configured("echos") is True

    # A failed fetch (None) is ignored — no crash, no registration.
    window._on_stationxml_ready("plain", None)
    assert "plain" not in window._stationxml_blobs
    window.close()
