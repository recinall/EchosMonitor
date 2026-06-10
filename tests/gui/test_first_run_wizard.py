"""Smoke tests for :class:`FirstRunWizard` (M4 stage C).

Covers each of the three radio paths via a stub :class:`InfoWorker`:

* Recommended → confirm page → Finish writes a device.
* Configure my own → DeviceForm fill → Finish writes a device.
* Skip → Finish writes nothing.

Stubs the worker so no real network probe runs. The dialog itself
construction-tests through ``qtbot.addWidget`` for cleanup.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from echosmonitor.config.schema import (
    AppConfig,
    RootConfig,
    UiConfig,
)
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.core.info import ServerIdentity
from echosmonitor.gui.dialogs.first_run_wizard import FirstRunWizard


class _StubInfoWorker(QObject):
    """Minimal stand-in for :class:`InfoWorker`.

    Records calls + lets tests drive the reply path by emitting from
    the test thread. Same shape used by Stage A's
    ``test_station_browser.py`` stub.
    """

    stationsReceived = Signal(str, str, object)  # noqa: N815
    streamsReceived = Signal(str, str, object)  # noqa: N815
    identityReceived = Signal(str, str, object)  # noqa: N815
    infoFailed = Signal(str, str, str, str)  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        self.id_calls: list[tuple[str, str, str, int]] = []

    @Slot(str, str, str, int)
    def requestId(self, request_id: str, label: str, host: str, port: int) -> None:  # noqa: N802
        self.id_calls.append((request_id, label, host, int(port)))


def _make_store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(
        RootConfig(app=AppConfig(), ui=UiConfig(), devices=[]),
        tmp_path / "config.yaml",
    )


def test_skip_path_writes_no_device(qtbot, tmp_path: Path) -> None:
    """Picking ``Skip`` and finishing leaves the store untouched."""
    store = _make_store(tmp_path)
    worker = _StubInfoWorker()
    wizard = FirstRunWizard(store=store, info_worker=worker)
    qtbot.addWidget(wizard)

    welcome_page = wizard.page(0)
    welcome_page._radio_skip.setChecked(True)  # type: ignore[attr-defined]
    # Move directly to the confirm page; nextId() routes recommended /
    # configure / skip via the radio state.
    wizard.next()
    # Now on confirm. Trigger Finish via accept().
    wizard.accept()

    assert wizard.result() == wizard.DialogCode.Accepted
    assert list(store.root.devices) == []


def test_recommended_path_writes_device(qtbot, tmp_path: Path) -> None:
    """Recommended → emit identity → confirm → finish; device exists in store."""
    store = _make_store(tmp_path)
    worker = _StubInfoWorker()
    wizard = FirstRunWizard(store=store, info_worker=worker)
    qtbot.addWidget(wizard)

    # Show the wizard so initializePage fires (which kicks off the
    # probe against the stub worker).
    wizard.show()
    qtbot.waitExposed(wizard)
    # The wizard's _WelcomePage uses ``Qt.ConnectionType.QueuedConnection``
    # to dispatch the probe ``_idRequested`` signal to the InfoWorker
    # slot. Same-thread queued connections fire on the next event-loop
    # iteration, so we need to pump events for the stub to record the
    # call. The 50 ms here is well above one frame at 60 Hz.
    qtbot.wait(50)
    # The welcome page recorded two ID requests against the stub.
    assert len(worker.id_calls) == 2
    gfz_request_id, gfz_label, _gfz_host, _gfz_port = worker.id_calls[0]
    # Simulate GFZ responding first; the wizard locks GFZ in.
    worker.identityReceived.emit(
        gfz_request_id,
        gfz_label,
        ServerIdentity(version="3.4.2", organization="GFZ", started_at=None, capabilities=()),
    )
    qtbot.wait(50)

    welcome_page = wizard.page(0)
    welcome_page._radio_recommended.setChecked(True)  # type: ignore[attr-defined]
    wizard.next()
    wizard.accept()

    assert wizard.result() == wizard.DialogCode.Accepted
    assert len(store.root.devices) == 1
    dev = store.root.devices[0]
    # GFZ's bundled config: host = geofon.gfz-potsdam.de, name = gfz-de.
    assert dev.host == "geofon.gfz-potsdam.de"
    assert dev.name == "gfz-de"


def test_configure_my_own_path_writes_form_device(qtbot, tmp_path: Path) -> None:
    """Configure path: fill DeviceForm → finish → store has the entered device."""
    store = _make_store(tmp_path)
    worker = _StubInfoWorker()
    wizard = FirstRunWizard(store=store, info_worker=worker)
    qtbot.addWidget(wizard)
    # ``QWizard.next()`` and ``currentPage()`` require the wizard to
    # have been shown at least once so the page-flow state machine is
    # initialised. Showing it briefly is the standard pytest-qt pattern.
    wizard.show()
    qtbot.waitExposed(wizard)
    # The wizard's _WelcomePage uses ``Qt.ConnectionType.QueuedConnection``
    # to dispatch the probe ``_idRequested`` signal to the InfoWorker
    # slot. Same-thread queued connections fire on the next event-loop
    # iteration, so we need to pump events for the stub to record the
    # call. The 50 ms here is well above one frame at 60 Hz.
    qtbot.wait(50)

    welcome_page = wizard.page(0)
    welcome_page._radio_configure.setChecked(True)  # type: ignore[attr-defined]
    wizard.next()  # → Configure page
    # ``page(1)`` resolves to the configure page deterministically
    # regardless of whether the wizard exposes ``currentPage()`` at
    # this point in the flow.
    config_page = wizard.page(1)
    # Drive the embedded DeviceForm directly. ``_form`` is a leading-
    # underscore attribute used as a test seam — same pattern Stage B's
    # device-dialog tests use.
    form = config_page._form  # type: ignore[attr-defined]
    form._name_edit.setText("my-server")  # type: ignore[attr-defined]
    form._host_edit.setText("seedlink.example.com")  # type: ignore[attr-defined]
    form._port_spin.setValue(18000)  # type: ignore[attr-defined]
    qtbot.wait(50)

    assert form.is_valid()
    wizard.next()  # → Confirm page
    wizard.accept()

    assert wizard.result() == wizard.DialogCode.Accepted
    assert len(store.root.devices) == 1
    dev = store.root.devices[0]
    assert dev.name == "my-server"
    assert dev.host == "seedlink.example.com"


def test_recommended_falls_back_to_gfz_when_both_probes_fail(qtbot, tmp_path: Path) -> None:
    """Both probes fail → wizard still finishes with a deterministic GFZ device."""
    store = _make_store(tmp_path)
    worker = _StubInfoWorker()
    wizard = FirstRunWizard(store=store, info_worker=worker)
    qtbot.addWidget(wizard)
    wizard.show()
    qtbot.waitExposed(wizard)
    # The wizard's _WelcomePage uses ``Qt.ConnectionType.QueuedConnection``
    # to dispatch the probe ``_idRequested`` signal to the InfoWorker
    # slot. Same-thread queued connections fire on the next event-loop
    # iteration, so we need to pump events for the stub to record the
    # call. The 50 ms here is well above one frame at 60 Hz.
    qtbot.wait(50)

    # Fail both probes by emitting infoFailed for each request_id.
    for rid, label, _host, _port in worker.id_calls:
        worker.infoFailed.emit(rid, label, "ID", "timeout: probe simulated as unreachable")
    qtbot.wait(50)

    # The deterministic fallback locks GFZ in even after both fail.
    welcome = wizard.page(0)
    welcome._radio_recommended.setChecked(True)  # type: ignore[attr-defined]
    wizard.next()
    wizard.accept()

    assert wizard.result() == wizard.DialogCode.Accepted
    assert len(store.root.devices) == 1
    assert store.root.devices[0].name == "gfz-de"


def test_recommended_with_no_probe_response_uses_fallback(qtbot, tmp_path: Path) -> None:
    """User clicks Next BEFORE either probe completes → still gets GFZ.

    Catches the regression where the wizard would crash on
    ``self._welcome.winner() is None`` if the user moved fast.
    """
    store = _make_store(tmp_path)
    worker = _StubInfoWorker()
    wizard = FirstRunWizard(store=store, info_worker=worker)
    qtbot.addWidget(wizard)
    wizard.show()
    qtbot.waitExposed(wizard)
    # The wizard's _WelcomePage uses ``Qt.ConnectionType.QueuedConnection``
    # to dispatch the probe ``_idRequested`` signal to the InfoWorker
    # slot. Same-thread queued connections fire on the next event-loop
    # iteration, so we need to pump events for the stub to record the
    # call. The 50 ms here is well above one frame at 60 Hz.
    qtbot.wait(50)

    # Do NOT drive the worker — go straight to next.
    welcome = wizard.page(0)
    welcome._radio_recommended.setChecked(True)  # type: ignore[attr-defined]
    wizard.next()
    wizard.accept()

    assert wizard.result() == wizard.DialogCode.Accepted
    assert len(store.root.devices) == 1
    # Fallback is GFZ (deterministic).
    assert store.root.devices[0].host == "geofon.gfz-potsdam.de"


def test_request_id_filter_ignores_stale_probe_replies(qtbot, tmp_path: Path) -> None:
    """A reply with an unrelated request_id does NOT lock anything in.

    Pins the same request-id-filtering invariant that protects the
    Stations dock from late replies.
    """
    store = _make_store(tmp_path)
    worker = _StubInfoWorker()
    wizard = FirstRunWizard(store=store, info_worker=worker)
    qtbot.addWidget(wizard)
    wizard.show()
    qtbot.waitExposed(wizard)
    # The wizard's _WelcomePage uses ``Qt.ConnectionType.QueuedConnection``
    # to dispatch the probe ``_idRequested`` signal to the InfoWorker
    # slot. Same-thread queued connections fire on the next event-loop
    # iteration, so we need to pump events for the stub to record the
    # call. The 50 ms here is well above one frame at 60 Hz.
    qtbot.wait(50)

    welcome = wizard.page(0)
    # Emit with a foreign request_id — should not flip the winner.
    worker.identityReceived.emit(
        uuid.uuid4().hex,
        "spoof.example.com:18000",
        ServerIdentity(version="0", organization="spoof", started_at=None, capabilities=()),
    )
    qtbot.wait(50)
    assert welcome.winner() is None
