"""M1-D dialog tests: Echos client-side form group + the server tabs.

The end-to-end tests ARE the M1 acceptance criterion: against the fake
firmware (real EchosDeviceWorker on a real QThread, real EchosApiClient
over httpx.MockTransport), a full round-trip edit of the acquisition
and seedlink configs works — including the simulated 7-step restart —
driven through the actual dialog widgets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from echosmonitor.config.credentials import CredentialsStore
from echosmonitor.config.schema import (
    DeviceConfig,
    EchosDeviceConfig,
    PositionOverride,
)
from echosmonitor.core.echos_api import (
    CalibrationStatus,
    EchosAcquisitionConfig,
    EchosApiClient,
    EchosNetworkConfig,
    OtaStatus,
    RestartStatus,
    SeedlinkServerConfig,
)
from echosmonitor.core.echos_device_worker import EchosDeviceState
from echosmonitor.core.models import EchosPollTarget
from echosmonitor.gui.dialogs.device_dialog import DeviceDialog, DeviceForm
from echosmonitor.gui.dialogs.echos_tabs import MaintenanceTab, SeedlinkTab
from tests.config.test_credentials import FakeKeyring
from tests.core.echos_fake import FakeEchosFirmware
from tests.gui.test_device_dialog import StubConfigStore

_DEVICE = "echos-field-01"
_DEADLINE_MS = 5000


def _echos_device(**echos_overrides: Any) -> DeviceConfig:
    return DeviceConfig(
        name=_DEVICE,
        host="echos-test.local",
        port=18000,
        echos=EchosDeviceConfig(**echos_overrides),
    )


# ----------------------------------------------------------------------
# Form-level: the echos client-side group (no worker, no network)
# ----------------------------------------------------------------------


def test_form_without_echos_round_trips_none(qtbot: Any) -> None:
    form = DeviceForm(initial=None)
    qtbot.addWidget(form)
    form._name_edit.setText("generic")
    form._host_edit.setText("example.org")
    assert form.is_valid()
    assert form.to_config().echos is None
    assert form.echos_target() is None


def test_form_echos_section_round_trips(qtbot: Any) -> None:
    initial = _echos_device(
        http_port=8080,
        poll_interval_s=10.0,
        position_override=PositionOverride(lat=45.4, lon=11.9, elev_m=20.0),
    )
    form = DeviceForm(initial=initial, editing_name=_DEVICE)
    qtbot.addWidget(form)
    config = form.to_config()
    assert config.echos is not None
    assert config.echos.http_port == 8080
    assert config.echos.poll_interval_s == 10.0
    assert config.echos.position_override == PositionOverride(lat=45.4, lon=11.9, elev_m=20.0)
    target = form.echos_target()
    assert target == EchosPollTarget(
        name=_DEVICE, host="echos-test.local", http_port=8080, poll_interval_s=10.0
    )


def test_form_echos_disabled_by_default_for_new_device(qtbot: Any) -> None:
    form = DeviceForm(initial=None)
    qtbot.addWidget(form)
    assert form.echos_enabled() is False


def test_use_device_channels_fills_selectors(qtbot: Any) -> None:
    form = DeviceForm(initial=_echos_device(), editing_name=_DEVICE)
    qtbot.addWidget(form)
    assert form._use_channels_button.isEnabled() is False
    form.set_device_channels(("XX.ECH01..HHZ", "XX.ECH01..HHN", "XX.ECH01..HHE"))
    assert form._use_channels_button.isEnabled()
    form._use_channels_button.click()
    selectors = form._read_selectors()
    assert [(s.network, s.station, s.location, s.channel) for s in selectors] == [
        ("XX", "ECH01", "", "HHZ"),
        ("XX", "ECH01", "", "HHN"),
        ("XX", "ECH01", "", "HHE"),
    ]
    assert form.is_valid()


# ----------------------------------------------------------------------
# Dialog-level: tabs + worker (fake firmware end-to-end)
# ----------------------------------------------------------------------


@pytest.fixture
def fw() -> FakeEchosFirmware:
    return FakeEchosFirmware()


@pytest.fixture
def dialog(qtbot: Any, fw: FakeEchosFirmware, tmp_path: Path) -> Any:
    """Edit-mode dialog for an Echos device, wired to the fake firmware."""
    credentials = CredentialsStore(fallback_dir=tmp_path, keyring_module=FakeKeyring())
    credentials.set_password(_DEVICE, fw.admin_password)
    store = StubConfigStore([_echos_device()])

    def factory(target: EchosPollTarget, password: str | None) -> EchosApiClient:
        return EchosApiClient(
            target.host,
            target.http_port,
            password,
            transport=fw.transport,
            retry_delay_s=0.0,
        )

    form = DeviceForm(initial=_echos_device(), editing_name=_DEVICE)
    dlg = DeviceDialog(
        title="Edit device — test",
        store=store,  # type: ignore[arg-type]
        form=form,
        on_accept=lambda cfg: None,
        credentials=credentials,
        client_factory=factory,
        restart_poll_interval_s=0.0,
    )
    qtbot.addWidget(dlg)
    yield dlg
    dlg._teardown_worker()


def _load(qtbot: Any, dlg: Any) -> None:
    dlg._request_load()
    qtbot.waitUntil(lambda: dlg._state_loaded, timeout=_DEADLINE_MS)


def test_server_tabs_track_echos_enablement(qtbot: Any, dialog: Any) -> None:
    # Edit-mode echos device → all four server tabs enabled.
    for index in range(1, 5):
        assert dialog._tabs.isTabEnabled(index)
    dialog._form._echos_group.setChecked(False)
    for index in range(1, 5):
        assert not dialog._tabs.isTabEnabled(index)


def test_load_populates_all_tabs_and_channels(qtbot: Any, dialog: Any) -> None:
    _load(qtbot, dialog)
    assert dialog._acq_tab._osr_combo.currentData() == 64
    assert [s.value() for s in dialog._acq_tab._gain_spins] == [1, 1, 1, 8]
    assert dialog._sl_tab._port_spin.value() == 18000
    assert dialog._net_tab._ssid_edit.text() == "field-net"
    assert dialog._maint_tab._ota_version_label.text() == "1.4.2"
    assert dialog._form._use_channels_button.isEnabled()
    assert "stored" in dialog._form._credential_status_label.text()


def test_acquisition_roundtrip_acceptance(
    qtbot: Any, dialog: Any, fw: FakeEchosFirmware
) -> None:
    """M1 acceptance, part 1: edit + apply the acquisition config."""
    _load(qtbot, dialog)
    tab = dialog._acq_tab
    tab.confirm = lambda text: True  # the confirmation gate, auto-accepted
    tab._osr_combo.setCurrentIndex(tab._osr_combo.findData(128))
    tab._gain_spins[0].setValue(4)
    tab._apply_button.click()
    qtbot.waitUntil(lambda: fw.acquisition.get("osr") == 128, timeout=_DEADLINE_MS)
    assert fw.acquisition["gains"][0] == 4
    qtbot.waitUntil(
        lambda: "applied" in tab._status_label.text().lower(), timeout=_DEADLINE_MS
    )


def test_seedlink_roundtrip_with_seven_step_restart_acceptance(
    qtbot: Any, dialog: Any, fw: FakeEchosFirmware
) -> None:
    """M1 acceptance, part 2: seedlink config via the simulated restart."""
    _load(qtbot, dialog)
    tab = dialog._sl_tab
    tab.confirm = lambda text: True
    tab._port_spin.setValue(18001)
    tab._ring_spin.setValue(4096)
    tab._apply_button.click()
    qtbot.waitUntil(lambda: fw.seedlink.get("port") == 18001, timeout=_DEADLINE_MS)
    assert fw.seedlink["ring_records"] == 4096
    assert fw.restart_state == "done"
    qtbot.waitUntil(
        lambda: "reloaded" in tab._status_label.text().lower(), timeout=_DEADLINE_MS
    )
    # Progress UI saw the full 7-step ladder.
    assert tab._restart_bar.maximum() == 7
    assert tab._restart_bar.value() == 7
    # Skill: a server-side port change syncs the client-side SeedLink
    # port so OK saves the endpoint the worker must reconnect to.
    assert dialog._form._port_spin.value() == 18001
    # The applied config is the new baseline for the next read-modify-write.
    follow_up = tab.edited_config()
    assert follow_up is not None and follow_up.port == 18001


def test_failed_restart_keeps_old_config_and_says_so(
    qtbot: Any, dialog: Any, fw: FakeEchosFirmware
) -> None:
    _load(qtbot, dialog)
    fw.fail_restart_at_step = 4
    tab = dialog._sl_tab
    tab.confirm = lambda text: True
    tab._port_spin.setValue(18002)
    tab._apply_button.click()
    qtbot.waitUntil(
        lambda: "failed" in tab._status_label.text().lower(), timeout=_DEADLINE_MS
    )
    assert fw.seedlink["port"] == 18000  # device kept its old config
    assert "kept its old config" in tab._status_label.text()


def test_lockout_disables_writes_with_countdown_banner(
    qtbot: Any, dialog: Any, fw: FakeEchosFirmware
) -> None:
    _load(qtbot, dialog)
    fw.locked = True
    tab = dialog._acq_tab
    tab.confirm = lambda text: True
    tab._apply_button.click()
    # The dialog itself is never show()n in tests, so isVisible() stays
    # False for all children — isHidden() flips on setVisible(True).
    qtbot.waitUntil(lambda: not dialog._lockout_label.isHidden(), timeout=_DEADLINE_MS)
    assert "lockout" in dialog._lockout_label.text().lower()
    # Every server tab's mutating surface is disabled (rule 15: honest,
    # and the client never hammers a locked device).
    assert not dialog._acq_tab._apply_button.isEnabled()
    assert not dialog._sl_tab._apply_button.isEnabled()
    assert not dialog._net_tab._apply_button.isEnabled()
    assert not dialog._maint_tab._reboot_button.isEnabled()


def test_network_apply_passes_wifi_password_write_only(
    qtbot: Any, dialog: Any, fw: FakeEchosFirmware
) -> None:
    _load(qtbot, dialog)
    tab = dialog._net_tab
    tab.confirm = lambda text: True
    tab._ssid_edit.setText("new-net")
    tab._wifi_password_edit.setText("wifi-secret-9")
    tab._apply_button.click()
    qtbot.waitUntil(lambda: fw.network.get("ssid") == "new-net", timeout=_DEADLINE_MS)
    body = fw.last_post_body["/api/network/config"]
    assert body["password"] == "wifi-secret-9"
    assert "has_password" not in body
    qtbot.waitUntil(
        lambda: tab._wifi_password_edit.text() == "", timeout=_DEADLINE_MS
    )  # cleared after apply


def test_store_credential_through_dialog(qtbot: Any, dialog: Any) -> None:
    form = dialog._form
    form._admin_password_edit.setText("fresh-pass-77")
    form._store_password_button.click()
    qtbot.waitUntil(
        lambda: "stored for" in form._credential_status_label.text().lower(),
        timeout=_DEADLINE_MS,
    )
    assert dialog._credentials.get_password(_DEVICE) == "fresh-pass-77"
    assert form._admin_password_edit.text() == ""  # field cleared, write-only


def test_dialog_done_tears_worker_down(qtbot: Any, dialog: Any) -> None:
    _load(qtbot, dialog)
    assert dialog._worker is not None
    dialog.done(0)
    assert dialog._worker is None
    assert dialog._worker_thread is None


# ----------------------------------------------------------------------
# Review/audit regression tests (M1-D findings)
# ----------------------------------------------------------------------


def _cal(state: str, phase: int = 1) -> CalibrationStatus:
    return CalibrationStatus(
        state=state, phase=phase, total_phases=3, progress_pct=phase / 3 * 100.0
    )


def _device_state() -> EchosDeviceState:
    return EchosDeviceState(
        target=EchosPollTarget(name=_DEVICE, host="echos-test.local"),
        acquisition=EchosAcquisitionConfig(osr=64, gains=(1, 1, 1)),
        seedlink=SeedlinkServerConfig(
            port=18000,
            ring_records=2048,
            record_size=512,
            auth_enabled=False,
            emit_hn1=False,
            network="XX",
            station="ECH01",
            stationxml_profile="default",
        ),
        network=EchosNetworkConfig(mode="sta", ssid="field-net", hostname="echos"),
        ota=OtaStatus(running_partition="ota_0", ota_state="valid", app_version="1.4.2"),
        calibration=_cal("idle", phase=0),
        channels=(),
        has_credentials=True,
    )


def test_late_calibration_status_after_done_cannot_resurrect_worker(
    qtbot: Any, dialog: Any
) -> None:
    # Audit F1: a queued calibrationStatus("running") delivered AFTER
    # done() must not restart the poll timer — one tick later it would
    # silently spin up a NEW worker thread on a closed dialog.
    _load(qtbot, dialog)
    dialog.done(0)
    assert dialog._worker is None
    dialog._maint_tab.on_calibration_status(_cal("running"))  # late queued delivery
    assert not dialog._maint_tab._cal_poll_timer.isActive()
    qtbot.wait(1100)  # one poll-tick interval, had the timer restarted
    assert dialog._worker is None
    assert dialog._worker_thread is None


def test_calibration_poll_failure_stops_timer(qtbot: Any, dialog: Any) -> None:
    # Audit F2: a dead device must not be polled at 1 Hz forever.
    _load(qtbot, dialog)
    tab = dialog._maint_tab
    tab.on_calibration_status(_cal("running"))
    assert tab._cal_poll_timer.isActive()
    tab.on_failed("calibrate_poll", "unreachable", "device gone")
    assert not tab._cal_poll_timer.isActive()


def test_calibration_poll_is_ping_pong_gated(qtbot: Any) -> None:
    # Audit F2: at most one calibration poll outstanding at a time.
    tab = MaintenanceTab()
    qtbot.addWidget(tab)
    tab.apply_state(_device_state())
    emitted: list[int] = []
    tab.calibrationPollRequested.connect(lambda: emitted.append(1))
    tab._on_poll_tick()
    tab._on_poll_tick()  # gated: previous poll still outstanding
    assert len(emitted) == 1
    tab.on_calibration_status(_cal("running"))  # response clears the gate
    tab._on_poll_tick()
    assert len(emitted) == 2


def test_seedlink_rebaseline_uses_posted_config_not_live_widgets(qtbot: Any) -> None:
    # Review finding 1: edits made DURING the multi-second restart must
    # not poison the rebaseline or the port-sync.
    tab = SeedlinkTab()
    qtbot.addWidget(tab)
    tab.apply_state(_device_state())
    tab.confirm = lambda text: True
    tab._port_spin.setValue(18001)
    tab._on_apply()
    assert not tab._apply_button.isEnabled()  # in flight: no double-apply
    tab._port_spin.setValue(19999)  # user meddles mid-restart
    ports: list[int] = []
    tab.portChanged.connect(ports.append)
    tab.on_seedlink_applied(
        RestartStatus(state="done", step=7, total_steps=7, step_name="ready")
    )
    assert ports == [18001]  # the POSTed port, not the widget value
    assert tab._loaded_state is not None
    assert tab._loaded_state.seedlink.port == 18001
    assert tab._apply_button.isEnabled()


def test_declined_confirmation_sends_nothing(
    qtbot: Any, dialog: Any, fw: FakeEchosFirmware
) -> None:
    # Review finding 9: the confirm() gate IS the rule-15 write gate.
    _load(qtbot, dialog)
    tab = dialog._acq_tab
    tab.confirm = lambda text: False
    tab._apply_button.click()
    qtbot.wait(200)
    assert fw.post_count("/api/config") == 0


def test_host_change_drops_loaded_baseline(qtbot: Any, dialog: Any) -> None:
    # Review finding 5: a loaded baseline belongs to the previous host.
    _load(qtbot, dialog)
    dialog._form._host_edit.setText("other-device.local")
    assert dialog._state_loaded is False
    assert dialog._acq_tab._loaded_state is None
    assert not dialog._acq_tab._apply_button.isEnabled()
    assert not dialog._form._use_channels_button.isEnabled()


def test_password_button_mirrors_firmware_constraints(qtbot: Any, dialog: Any) -> None:
    # Review finding 8: 8-64 printable ASCII enforced at the button.
    _load(qtbot, dialog)
    tab = dialog._maint_tab
    for bad in ("short", "x" * 70, "ottimo-ma-non-è-ascii"):
        tab._new_password_edit.setText(bad)
        tab._confirm_password_edit.setText(bad)
        assert not tab._password_button.isEnabled(), bad
    tab._new_password_edit.setText("good-pass-1")
    tab._confirm_password_edit.setText("good-pass-1")
    assert tab._password_button.isEnabled()


def test_credential_row_hidden_without_sink(qtbot: Any, dialog: Any) -> None:
    # Review finding 3: a bare DeviceForm (first-run wizard embedding)
    # has no credential sink — the Store button must not exist for the
    # user there. The DeviceDialog enables it because it wires a worker.
    bare = DeviceForm(initial=_echos_device(), editing_name=_DEVICE)
    qtbot.addWidget(bare)
    assert bare._credential_box.isHidden()
    assert not dialog._form._credential_box.isHidden()
