"""Server-side tabs of the device dialog (M1-D; skill: echos-rest-api).

Four QWidget tabs — Acquisition, SeedLink server, Network, Maintenance —
each editing ONE firmware write endpoint, so one Apply maps to exactly
one confirmed read-modify-write (the roadmap sketch placed ``emit_hn1``
on Acquisition; it lives on the SeedLink tab here because it is a
``/api/seedlink/config`` field — recorded in ROADMAP).

The tabs are pure widgets: they never talk to the network. The dialog
feeds them ``EchosDeviceState`` via :meth:`apply_state`, they emit
``*Requested`` signals carrying full config objects (read-modify-write:
``model_copy`` of the last-loaded model), and the dialog routes those to
the :class:`EchosDeviceWorker`. Confirmation goes through the instance
method :meth:`_EchosTabBase.confirm` so tests can stub it without
patching QMessageBox statics.

Lockout honesty (rule 15): the dialog calls :meth:`set_write_enabled`
on every tab when the device reports 429; each tab disables its
mutating buttons and shows why.
"""

from __future__ import annotations

import structlog
from PySide6.QtCore import QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.echos_api import (
    CalibrationStatus,
    EchosAcquisitionConfig,
    EchosNetworkConfig,
    RestartStatus,
    SeedlinkServerConfig,
)
from echosmonitor.core.echos_device_worker import EchosDeviceState

_log = structlog.get_logger(__name__)

# Candidate OSR values offered in the combo. The loaded device value is
# inserted if the firmware reports something outside this list, so the
# UI never silently rewrites an unknown-but-valid setting.
_OSR_CHOICES = (16, 32, 64, 128, 256)

# Per-channel PGA gain bounds (ADC PGA steps; firmware validates the
# exact set — the spin keeps obviously-wrong values out).
_GAIN_MIN = 1
_GAIN_MAX = 128

_RING_MIN = 64
_RING_MAX = 1_048_576

# Calibration progress poll cadence while a sweep is running. GUI-thread
# QTimer; each tick emits one queued request to the worker (never blocks).
_CAL_POLL_MS = 1000

_STATUS_OK_STYLE = "QLabel { color: #3aa371; }"
_STATUS_ERR_STYLE = "QLabel { color: #c0392b; }"
_STATUS_BUSY_STYLE = "QLabel { color: #888; }"

_NOT_LOADED_TEXT = "Not loaded — open this tab with a reachable Echos device, or hit Reload."


def _valid_device_password(text: str) -> bool:
    """Mirror the firmware's 8-64 printable-ASCII password constraint,
    so an invalid value is rejected by a disabled button instead of an
    "unexpected ValueError" failure after the click."""
    return 8 <= len(text) <= 64 and all(32 <= ord(c) <= 126 for c in text)


class _EchosTabBase(QWidget):
    """Shared plumbing: load gating, status line, confirm hook, reload."""

    reloadRequested = Signal()  # noqa: N815

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loaded_state: EchosDeviceState | None = None
        self._write_enabled = True
        self._status_label = QLabel(_NOT_LOADED_TEXT, self)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(_STATUS_BUSY_STYLE)
        self._reload_button = QPushButton("Reload from device", self)
        self._reload_button.clicked.connect(self.reloadRequested.emit)

    # -- dialog-facing API -------------------------------------------------
    def apply_state(self, state: EchosDeviceState) -> None:
        self._loaded_state = state
        self._populate(state)
        self._set_status("Loaded from device.", style=_STATUS_OK_STYLE)
        self._refresh_enabled()

    def set_write_enabled(self, enabled: bool, reason: str = "") -> None:
        """Lockout / busy gate for every mutating control on this tab."""
        self._write_enabled = enabled
        if not enabled and reason:
            self._set_status(reason, style=_STATUS_ERR_STYLE)
        self._refresh_enabled()

    def on_failed(self, op: str, kind: str, message: str) -> None:
        """Surface a failure for an op this tab owns (dialog routes)."""
        self._set_status(f"{op} failed ({kind}): {message}", style=_STATUS_ERR_STYLE)
        self._refresh_enabled()

    def reset_loaded(self) -> None:
        """Drop the read-modify-write baseline (host/port changed).

        The loaded state belongs to the PREVIOUS device; applying it to a
        new host would write another device's config (review finding).
        """
        self._loaded_state = None
        self._set_status(_NOT_LOADED_TEXT, style=_STATUS_BUSY_STYLE)
        self._refresh_enabled()

    # -- subclass contract ---------------------------------------------------
    def _populate(self, state: EchosDeviceState) -> None:
        raise NotImplementedError

    def _refresh_enabled(self) -> None:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------
    def confirm(self, text: str) -> bool:
        """Read-modify-write confirmation gate (stub me in tests)."""
        answer = QMessageBox.question(
            self,
            "Apply to device?",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _set_status(self, text: str, *, style: str) -> None:
        self._status_label.setText(text)
        self._status_label.setStyleSheet(style)

    def _footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(self._reload_button)
        row.addStretch(1)
        return row

    @property
    def _ready(self) -> bool:
        return self._loaded_state is not None and self._write_enabled


class AcquisitionTab(_EchosTabBase):
    """Server-side acquisition config: OSR + per-channel gains."""

    applyRequested = Signal(object)  # noqa: N815  # EchosAcquisitionConfig

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._osr_combo = QComboBox(self)
        for osr in _OSR_CHOICES:
            self._osr_combo.addItem(str(osr), userData=osr)
        form.addRow("Oversampling (OSR):", self._osr_combo)

        self._gains_box = QWidget(self)
        self._gains_layout = QHBoxLayout(self._gains_box)
        self._gains_layout.setContentsMargins(0, 0, 0, 0)
        self._gain_spins: list[QSpinBox] = []
        form.addRow("Channel gains:", self._gains_box)
        layout.addLayout(form)

        self._apply_button = QPushButton("Apply acquisition config", self)
        self._apply_button.clicked.connect(self._on_apply)
        layout.addWidget(self._apply_button)
        layout.addWidget(self._status_label)
        layout.addStretch(1)
        layout.addLayout(self._footer())
        self._refresh_enabled()

    def _populate(self, state: EchosDeviceState) -> None:
        config = state.acquisition
        if self._osr_combo.findData(config.osr) < 0:
            self._osr_combo.addItem(str(config.osr), userData=config.osr)
        self._osr_combo.setCurrentIndex(self._osr_combo.findData(config.osr))
        # Rebuild gain spins to match the channel count (3 or 4).
        for spin in self._gain_spins:
            spin.deleteLater()
        self._gain_spins = []
        for index, gain in enumerate(config.gains):
            spin = QSpinBox(self._gains_box)
            spin.setRange(_GAIN_MIN, _GAIN_MAX)
            spin.setValue(int(gain))
            spin.setPrefix(f"ch{index}: ")
            self._gains_layout.addWidget(spin)
            self._gain_spins.append(spin)

    def _refresh_enabled(self) -> None:
        self._apply_button.setEnabled(self._ready)

    def edited_config(self) -> EchosAcquisitionConfig | None:
        if self._loaded_state is None:
            return None
        return self._loaded_state.acquisition.model_copy(
            update={
                "osr": int(self._osr_combo.currentData()),
                "gains": tuple(int(s.value()) for s in self._gain_spins),
            }
        )

    @Slot()
    def _on_apply(self) -> None:
        config = self.edited_config()
        if config is None or not self._write_enabled:
            return
        if not self.confirm(
            f"Write acquisition config (OSR {config.osr}, gains {list(config.gains)}) "
            "to the device?"
        ):
            return
        self._set_status("Applying…", style=_STATUS_BUSY_STYLE)
        self.applyRequested.emit(config)

    @Slot()
    def on_applied(self) -> None:
        self._set_status("Acquisition config applied.", style=_STATUS_OK_STYLE)


class SeedlinkTab(_EchosTabBase):
    """SeedLink server config — the hot-reload write with progress UI."""

    applyRequested = Signal(object)  # noqa: N815  # SeedlinkServerConfig
    # Emitted after a successful apply whose port differs from the loaded
    # one, so the dialog can sync the Connection tab's SeedLink port.
    portChanged = Signal(int)  # noqa: N815

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # The config actually POSTed, captured at apply time. The
        # rebaseline/port-sync on completion must use THIS, never the
        # live widgets — the user can edit fields during the multi-second
        # restart (review finding). Doubles as the in-flight flag.
        self._pending_apply: SeedlinkServerConfig | None = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._port_spin = QSpinBox(self)
        self._port_spin.setRange(1, 65535)
        form.addRow("SeedLink port:", self._port_spin)
        self._ring_spin = QSpinBox(self)
        self._ring_spin.setRange(_RING_MIN, _RING_MAX)
        form.addRow("Ring size (records):", self._ring_spin)
        self._record_combo = QComboBox(self)
        for size in (512, 4096):
            self._record_combo.addItem(str(size), userData=size)
        form.addRow("Record size:", self._record_combo)
        self._auth_check = QCheckBox("Require USER/PASSWORD on the SeedLink TCP port", self)
        form.addRow("Auth gate:", self._auth_check)
        self._hn1_check = QCheckBox("Emit the optional HN1 channel", self)
        form.addRow("HN1:", self._hn1_check)
        self._network_edit = QLineEdit(self)
        self._network_edit.setMaxLength(2)
        form.addRow("Network code:", self._network_edit)
        self._station_edit = QLineEdit(self)
        self._station_edit.setMaxLength(5)
        form.addRow("Station code:", self._station_edit)
        self._profile_edit = QLineEdit(self)
        form.addRow("StationXML profile:", self._profile_edit)
        layout.addLayout(form)

        self._apply_button = QPushButton("Apply + hot-reload server", self)
        self._apply_button.clicked.connect(self._on_apply)
        layout.addWidget(self._apply_button)

        # 7-step in-place restart progress (skill: 202 + restart-status
        # poll). Hidden until an apply is in flight.
        self._restart_bar = QProgressBar(self)
        self._restart_bar.setTextVisible(True)
        self._restart_bar.setVisible(False)
        self._restart_step_label = QLabel("", self)
        self._restart_step_label.setVisible(False)
        layout.addWidget(self._restart_bar)
        layout.addWidget(self._restart_step_label)

        layout.addWidget(self._status_label)
        layout.addStretch(1)
        layout.addLayout(self._footer())
        self._refresh_enabled()

    def _populate(self, state: EchosDeviceState) -> None:
        config = state.seedlink
        self._port_spin.setValue(config.port)
        self._ring_spin.setValue(config.ring_records)
        self._record_combo.setCurrentIndex(self._record_combo.findData(config.record_size))
        self._auth_check.setChecked(config.auth_enabled)
        self._hn1_check.setChecked(config.emit_hn1)
        self._network_edit.setText(config.network)
        self._station_edit.setText(config.station)
        self._profile_edit.setText(config.stationxml_profile)

    def _refresh_enabled(self) -> None:
        self._apply_button.setEnabled(self._ready and self._pending_apply is None)

    def reset_loaded(self) -> None:
        self._pending_apply = None
        self._restart_bar.setVisible(False)
        self._restart_step_label.setVisible(False)
        super().reset_loaded()

    def on_failed(self, op: str, kind: str, message: str) -> None:
        self._pending_apply = None
        self._restart_bar.setVisible(False)
        self._restart_step_label.setVisible(False)
        super().on_failed(op, kind, message)

    def edited_config(self) -> SeedlinkServerConfig | None:
        if self._loaded_state is None:
            return None
        return self._loaded_state.seedlink.model_copy(
            update={
                "port": int(self._port_spin.value()),
                "ring_records": int(self._ring_spin.value()),
                "record_size": int(self._record_combo.currentData()),
                "auth_enabled": self._auth_check.isChecked(),
                "emit_hn1": self._hn1_check.isChecked(),
                "network": self._network_edit.text().strip(),
                "station": self._station_edit.text().strip(),
                "stationxml_profile": self._profile_edit.text().strip(),
            }
        )

    @Slot()
    def _on_apply(self) -> None:
        config = self.edited_config()
        if config is None or not self._write_enabled or self._pending_apply is not None:
            return
        if not self.confirm(
            "Write the SeedLink server config and hot-reload the server?\n\n"
            "Connected SeedLink clients (including this app's live view) drop "
            "during the in-place restart and reconnect automatically."
        ):
            return
        self._pending_apply = config
        self._refresh_enabled()
        self._restart_bar.setVisible(True)
        self._restart_bar.setRange(0, 1)
        self._restart_bar.setValue(0)
        self._restart_step_label.setVisible(True)
        self._restart_step_label.setText("Posting config…")
        self._set_status("Applying (hot-reload)…", style=_STATUS_BUSY_STYLE)
        self.applyRequested.emit(config)

    @Slot(object)
    def on_restart_progress(self, status: object) -> None:
        if not isinstance(status, RestartStatus):
            return
        self._restart_bar.setRange(0, status.total_steps)
        self._restart_bar.setValue(status.step)
        self._restart_step_label.setText(
            f"Step {status.step}/{status.total_steps}: {status.step_name}"
        )

    @Slot(object)
    def on_seedlink_applied(self, final: object) -> None:
        if not isinstance(final, RestartStatus):
            return
        # What the device actually accepted is what was POSTed at apply
        # time — NOT the live widgets, which the user may have edited
        # during the multi-second restart.
        applied = self._pending_apply
        self._pending_apply = None
        if final.state == "done":
            self._set_status("SeedLink server reloaded.", style=_STATUS_OK_STYLE)
            loaded = self._loaded_state
            if applied is not None and loaded is not None:
                if applied.port != loaded.seedlink.port:
                    self.portChanged.emit(applied.port)
                # The applied config is the device's new truth — rebuild
                # the frozen state so the next read-modify-write starts
                # from what is actually live.
                self._loaded_state = EchosDeviceState(
                    target=loaded.target,
                    acquisition=loaded.acquisition,
                    seedlink=applied,
                    network=loaded.network,
                    ota=loaded.ota,
                    calibration=loaded.calibration,
                    channels=loaded.channels,
                    has_credentials=loaded.has_credentials,
                )
        else:
            self._set_status(
                f"Restart FAILED at step {final.step}/{final.total_steps}: "
                f"{final.error or 'unknown error'} — the device kept its old config.",
                style=_STATUS_ERR_STYLE,
            )
        self._restart_step_label.setVisible(final.state != "done")
        self._restart_bar.setVisible(final.state != "done")
        self._refresh_enabled()


class NetworkTab(_EchosTabBase):
    """WiFi / network config (credential-safe read; write-only password)."""

    applyRequested = Signal(object, str)  # noqa: N815  # EchosNetworkConfig, wifi password ("" = keep)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._mode_combo = QComboBox(self)
        self._mode_combo.addItem("Station (join a WiFi network)", userData="sta")
        self._mode_combo.addItem("Access point (device hosts its own)", userData="ap")
        form.addRow("Mode:", self._mode_combo)
        self._ssid_edit = QLineEdit(self)
        form.addRow("SSID:", self._ssid_edit)
        self._hostname_edit = QLineEdit(self)
        form.addRow("Hostname:", self._hostname_edit)
        self._wifi_password_edit = QLineEdit(self)
        self._wifi_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._wifi_password_edit.setPlaceholderText("(unchanged)")
        form.addRow("WiFi password:", self._wifi_password_edit)
        self._has_password_label = QLabel("", self)
        form.addRow("", self._has_password_label)
        layout.addLayout(form)

        self._apply_button = QPushButton("Apply network config", self)
        self._apply_button.clicked.connect(self._on_apply)
        layout.addWidget(self._apply_button)
        layout.addWidget(self._status_label)
        layout.addStretch(1)
        layout.addLayout(self._footer())
        self._refresh_enabled()

    def _populate(self, state: EchosDeviceState) -> None:
        config = state.network
        self._mode_combo.setCurrentIndex(self._mode_combo.findData(config.mode))
        self._ssid_edit.setText(config.ssid)
        self._hostname_edit.setText(config.hostname)
        self._wifi_password_edit.clear()
        self._has_password_label.setText(
            "A WiFi password is set on the device."
            if config.has_password
            else "No WiFi password set on the device."
        )

    def _refresh_enabled(self) -> None:
        self._apply_button.setEnabled(self._ready)

    def edited_config(self) -> EchosNetworkConfig | None:
        if self._loaded_state is None:
            return None
        return self._loaded_state.network.model_copy(
            update={
                "mode": str(self._mode_combo.currentData()),
                "ssid": self._ssid_edit.text().strip(),
                "hostname": self._hostname_edit.text().strip(),
            }
        )

    @Slot()
    def _on_apply(self) -> None:
        config = self.edited_config()
        if config is None or not self._write_enabled:
            return
        if not self.confirm(
            "Write the network config to the device?\n\n"
            "A wrong SSID/password can take the device off the network — "
            "recovery is the AP-mode button (hold B ≥ 5 s)."
        ):
            return
        self._set_status("Applying…", style=_STATUS_BUSY_STYLE)
        self.applyRequested.emit(config, self._wifi_password_edit.text())

    @Slot()
    def on_applied(self) -> None:
        self._wifi_password_edit.clear()
        self._set_status("Network config applied.", style=_STATUS_OK_STYLE)


class MaintenanceTab(_EchosTabBase):
    """Calibration sweep, OTA status, admin password rotation, reboot."""

    calibrateRequested = Signal()  # noqa: N815
    calibrationPollRequested = Signal()  # noqa: N815
    passwordChangeRequested = Signal(str)  # noqa: N815
    rebootRequested = Signal()  # noqa: N815

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Ping-pong gate (rule 5 spirit): at most ONE calibration poll in
        # flight — a slow/unreachable device must not accumulate queued
        # polls in the worker (audit finding). Cleared when the status or
        # a failure comes back.
        self._poll_outstanding = False
        # Latched by ``stop_polling`` at dialog teardown: a late queued
        # calibrationStatus("running") delivered AFTER done() must not
        # restart the timer and resurrect a worker thread on a closed
        # dialog (audit finding F1).
        self._stopped = False
        layout = QVBoxLayout(self)

        cal_box = QGroupBox("Calibration (3-phase full sweep)", self)
        cal_layout = QVBoxLayout(cal_box)
        self._cal_button = QPushButton("Start full calibration", cal_box)
        self._cal_button.clicked.connect(self._on_calibrate)
        cal_layout.addWidget(self._cal_button)
        self._cal_bar = QProgressBar(cal_box)
        self._cal_bar.setRange(0, 3)
        self._cal_bar.setValue(0)
        cal_layout.addWidget(self._cal_bar)
        self._cal_state_label = QLabel("idle", cal_box)
        cal_layout.addWidget(self._cal_state_label)
        layout.addWidget(cal_box)

        ota_box = QGroupBox("OTA / firmware", self)
        ota_form = QFormLayout(ota_box)
        self._ota_version_label = QLabel("—", ota_box)
        ota_form.addRow("Running version:", self._ota_version_label)
        self._ota_partition_label = QLabel("—", ota_box)
        ota_form.addRow("Partition:", self._ota_partition_label)
        self._ota_state_label = QLabel("—", ota_box)
        ota_form.addRow("Image state:", self._ota_state_label)
        layout.addWidget(ota_box)

        pw_box = QGroupBox("Admin password rotation", self)
        pw_form = QFormLayout(pw_box)
        self._new_password_edit = QLineEdit(pw_box)
        self._new_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        pw_form.addRow("New password:", self._new_password_edit)
        self._confirm_password_edit = QLineEdit(pw_box)
        self._confirm_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        pw_form.addRow("Confirm:", self._confirm_password_edit)
        self._password_button = QPushButton("Change device password", pw_box)
        self._password_button.clicked.connect(self._on_change_password)
        pw_form.addRow("", self._password_button)
        layout.addWidget(pw_box)
        self._new_password_edit.textChanged.connect(self._refresh_enabled)
        self._confirm_password_edit.textChanged.connect(self._refresh_enabled)

        self._reboot_button = QPushButton("Reboot device", self)
        self._reboot_button.clicked.connect(self._on_reboot)
        layout.addWidget(self._reboot_button)

        layout.addWidget(self._status_label)
        layout.addStretch(1)
        layout.addLayout(self._footer())

        # GUI-thread poll timer driving queued worker requests while a
        # calibration sweep runs; each tick is a non-blocking emit,
        # gated so only one poll is ever outstanding.
        self._cal_poll_timer = QTimer(self)
        self._cal_poll_timer.setInterval(_CAL_POLL_MS)
        self._cal_poll_timer.timeout.connect(self._on_poll_tick)
        self._refresh_enabled()

    @Slot()
    def _on_poll_tick(self) -> None:
        if self._poll_outstanding or self._stopped:
            return
        self._poll_outstanding = True
        self.calibrationPollRequested.emit()

    def _populate(self, state: EchosDeviceState) -> None:
        self._ota_version_label.setText(state.ota.app_version)
        self._ota_partition_label.setText(state.ota.running_partition)
        self._ota_state_label.setText(state.ota.ota_state)
        self._render_calibration(state.calibration)

    def _refresh_enabled(self) -> None:
        ready = self._ready
        self._cal_button.setEnabled(ready and not self._cal_poll_timer.isActive())
        self._reboot_button.setEnabled(ready)
        new = self._new_password_edit.text()
        self._password_button.setEnabled(
            ready
            and _valid_device_password(new)
            and new == self._confirm_password_edit.text()
        )

    def on_failed(self, op: str, kind: str, message: str) -> None:
        self._poll_outstanding = False
        if op == "calibrate_poll":
            # An unreachable device must not be polled at 1 Hz forever;
            # the user restarts via the Start button / a reload.
            self._cal_poll_timer.stop()
        super().on_failed(op, kind, message)

    def _render_calibration(self, status: CalibrationStatus) -> None:
        self._cal_bar.setRange(0, status.total_phases)
        self._cal_bar.setValue(status.phase)
        self._cal_state_label.setText(
            f"{status.state} — phase {status.phase}/{status.total_phases} "
            f"({status.progress_pct:.0f}%)"
            + (f" — {status.error}" if status.error else "")
        )
        if status.state == "running":
            if not self._cal_poll_timer.isActive() and not self._stopped:
                self._cal_poll_timer.start()
        else:
            self._cal_poll_timer.stop()
        self._refresh_enabled()

    @Slot(object)
    def on_calibration_status(self, status: object) -> None:
        self._poll_outstanding = False
        if isinstance(status, CalibrationStatus):
            self._render_calibration(status)

    @Slot()
    def on_calibration_started(self) -> None:
        self._set_status("Calibration sweep started.", style=_STATUS_OK_STYLE)
        if not self._cal_poll_timer.isActive() and not self._stopped:
            self._cal_poll_timer.start()
        self._refresh_enabled()

    @Slot()
    def on_password_changed(self) -> None:
        self._new_password_edit.clear()
        self._confirm_password_edit.clear()
        self._set_status(
            "Device password changed; the stored credential was updated.",
            style=_STATUS_OK_STYLE,
        )

    @Slot()
    def on_reboot_requested(self) -> None:
        self._set_status(
            "Reboot requested — the device drops off the network briefly.",
            style=_STATUS_OK_STYLE,
        )

    def stop_polling(self) -> None:
        """Dialog teardown hook: never leave the timer ticking.

        Latches ``_stopped`` so a queued calibrationStatus delivered
        AFTER teardown cannot restart the timer (audit finding F1).
        """
        self._stopped = True
        self._cal_poll_timer.stop()

    @Slot()
    def _on_calibrate(self) -> None:
        if not self._write_enabled:
            return
        if not self.confirm(
            "Start the full 3-phase calibration sweep? Acquisition output is "
            "disturbed while it runs."
        ):
            return
        self._set_status("Starting calibration…", style=_STATUS_BUSY_STYLE)
        self.calibrateRequested.emit()

    @Slot()
    def _on_change_password(self) -> None:
        if not self._write_enabled:
            return
        new = self._new_password_edit.text()
        if not _valid_device_password(new) or new != self._confirm_password_edit.text():
            return
        if not self.confirm(
            "Change the device admin password? The stored credential on this "
            "machine is updated only after the device confirms."
        ):
            return
        self._set_status("Changing password…", style=_STATUS_BUSY_STYLE)
        self.passwordChangeRequested.emit(new)

    @Slot()
    def _on_reboot(self) -> None:
        if not self._write_enabled:
            return
        if not self.confirm("Reboot the device now? Streaming stops until it is back up."):
            return
        self._set_status("Rebooting…", style=_STATUS_BUSY_STYLE)
        self.rebootRequested.emit()


__all__ = ["AcquisitionTab", "MaintenanceTab", "NetworkTab", "SeedlinkTab"]
