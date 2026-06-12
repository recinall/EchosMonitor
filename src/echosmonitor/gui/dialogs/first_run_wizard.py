"""First-run wizard, rewritten for Echos devices (M6).

Triggered when ``is_first_run(...)`` returns True at app startup (see
:mod:`echosmonitor.core.firstrun`; unchanged) and reachable any time via
Help → First-run wizard. Three short pages:

1. **Welcome** — three-way radio: scan the local network (mDNS,
   default), device in setup (AP) mode at ``http://192.168.4.1``
   (button B ≥ 5 s on the device), or skip.

2. **Find device** — an embedded mDNS scan (the M6-2
   :class:`~echosmonitor.core.discovery.EchosDiscoveryWorker`, reused
   verbatim) plus a manual host + "Check device" row driving the same
   typed PUBLIC probe (``probe_host``) — that one row covers BOTH the
   AP-mode path (host prefilled ``192.168.4.1``) and nodes that do not
   advertise on mDNS (e.g. Pi-hosted). Only a probe-confirmed
   :class:`DiscoveredEchos` can be selected.

3. **Name & password** — device name (de-collided suggestion) and the
   OPTIONAL admin password. First boot prints a random password to the
   device serial console; the password goes to the OS keyring (file
   fallback) via :class:`EchosDeviceWorker.storeCredential`, NEVER into
   the YAML (rule 15). Skippable — it can be stored later from the
   device dialog.

Finish writes one :class:`DeviceConfig` through :class:`ConfigStore`
(validation + atomic write, same as any runtime mutation): host is the
mDNS hostname when available (survives DHCP), ``port`` is the PROBED
SeedLink port, selectors are the device's exact StationXML channels
(``DiscoveredEchos.channels`` — empty degrades to manual selectors
later). NOTE the wizard performs NO device writes — storing the
password locally is keyring-only; changing the password ON the device
stays in the device dialog (that POST is still unexercised on real
firmware — M1 closure).

Threading: ONE wizard-owned QThread hosts both workers (discovery +
device); slots serialize, teardown is latch-guarded with the bounded
join + retained-on-timeout pattern (M6-0 canon). The thread starts
LAZILY on the first page action — an undriven wizard owns no running
thread (see ``_ensure_thread``).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from echosmonitor.config.credentials import CredentialsStore
from echosmonitor.config.schema import (
    DeviceConfig,
    EchosDeviceConfig,
    StreamSelectorConfig,
)
from echosmonitor.core.discovery import EchosDiscoveryWorker
from echosmonitor.core.echos_device_worker import EchosDeviceWorker
from echosmonitor.core.exceptions import ConfigError
from echosmonitor.core.models import DiscoveredEchos

if TYPE_CHECKING:
    from echosmonitor.core.config_store import ConfigStore

_log = structlog.get_logger(__name__)

_PAGE_WELCOME = 0
_PAGE_FIND = 1
_PAGE_DETAILS = 2

_AP_HOST = "192.168.4.1"

_THREAD_JOIN_MS = 5000

# Bounded wait for the keyring write at Finish (rule 7): a locked keyring
# can prompt the user, so this is generous — past it the wizard accepts
# anyway with a "store it later from the device dialog" warning.
_CREDENTIAL_TIMEOUT_MS = 15_000

# Worker/thread pairs whose bounded join timed out at teardown: keep them
# referenced for the process lifetime (M6-0: dropping the last reference
# to a running QThread is a hard abort).
_ABANDONED: list[tuple[QThread, tuple[object, ...]]] = []


def device_config_for(device: DiscoveredEchos, name: str) -> DeviceConfig:
    """Map one probe-confirmed node to the DeviceConfig the store gets.

    Host prefers the mDNS hostname (survives DHCP lease changes); the
    SeedLink ``port`` was PROBED from ``/api/seedlink/config``; selectors
    are the device's exact StationXML channels (empty when the document
    was unavailable — the device dialog's "Use device channels" can fill
    them later).
    """
    selectors = []
    for nslc in device.channels:
        parts = nslc.split(".")
        if len(parts) != 4:
            continue
        selectors.append(
            StreamSelectorConfig(
                network=parts[0], station=parts[1], location=parts[2], channel=parts[3]
            )
        )
    return DeviceConfig(
        name=name,
        host=device.hostname or device.address,
        port=device.seedlink_port,
        selectors=selectors,
        echos=EchosDeviceConfig(http_port=device.http_port),
    )


def suggest_device_name(device: DiscoveredEchos, existing: set[str]) -> str:
    """First mDNS label ("echos"), de-collided against the config."""
    base = (device.hostname.split(".")[0] if device.hostname else "") or "echos"
    name = base
    suffix = 2
    while name in existing:
        name = f"{base}-{suffix}"
        suffix += 1
    return name


class _WelcomePage(QWizardPage):
    """Page 1 — how do we reach the device?"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTitle("Welcome to EchosMonitor")
        layout = QVBoxLayout(self)
        blurb = QLabel(
            "EchosMonitor talks to Echos seismic nodes over your local "
            "network. Let's add your first device.\n",
            self,
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        self._scan_radio = QRadioButton(
            "Find my device on the network (recommended)", self
        )
        scan_hint = QLabel(
            "The device is powered on and joined to this WiFi/LAN — "
            "EchosMonitor will discover it automatically.",
            self,
        )
        self._ap_radio = QRadioButton("The device is in setup (AP) mode", self)
        ap_hint = QLabel(
            f"A factory-fresh device (or after holding button B for 5 s) "
            f"starts its own WiFi access point. Join that WiFi first; the "
            f"device then answers at http://{_AP_HOST}. Its initial admin "
            f"password is printed once on the device serial console.",
            self,
        )
        self._skip_radio = QRadioButton("Skip — I'll add devices later", self)
        for hint in (scan_hint, ap_hint):
            hint.setWordWrap(True)
            hint.setIndent(22)
            hint.setStyleSheet("color: palette(mid);")
        self._group = QButtonGroup(self)
        for radio in (self._scan_radio, self._ap_radio, self._skip_radio):
            self._group.addButton(radio)
        self._scan_radio.setChecked(True)
        layout.addWidget(self._scan_radio)
        layout.addWidget(scan_hint)
        layout.addWidget(self._ap_radio)
        layout.addWidget(ap_hint)
        layout.addWidget(self._skip_radio)
        layout.addStretch(1)

    def selected_path(self) -> str:
        if self._skip_radio.isChecked():
            return "skip"
        return "ap" if self._ap_radio.isChecked() else "scan"

    def nextId(self) -> int:  # noqa: N802 — Qt override
        return -1 if self.selected_path() == "skip" else _PAGE_FIND


class _FindPage(QWizardPage):
    """Page 2 — discover or manually probe; only confirmed nodes select."""

    # Page → workers (queued; the wizard connects them to the thread).
    scanRequested = Signal()  # noqa: N815
    probeRequested = Signal(str, int)  # host, http_port  # noqa: N815

    def __init__(
        self,
        welcome: _WelcomePage,
        configured_hosts: set[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setTitle("Find your Echos device")
        self._welcome = welcome
        # Normalized hosts already in the config (Help-menu re-runs): the
        # row is marked, not blocked — re-adding under a new name is legal.
        self._configured_hosts = configured_hosts
        self._devices: list[DiscoveredEchos] = []
        # Scan-busy and probe-busy are SEPARATE: a streaming scan row must
        # not re-enable "Scan network" mid-scan (the worker's no-queue
        # invariant assumes the button gates re-entry — auditor MED-1).
        self._scan_busy = False
        self._probe_busy = False

        layout = QVBoxLayout(self)
        self._status = QLabel("", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._table = QTableWidget(0, 4, self)
        self._table.setHorizontalHeaderLabels(["Device", "Host", "Firmware", "Project"])
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, 1)

        row = QHBoxLayout()
        self._scan_button = QPushButton("Scan network", self)
        row.addWidget(self._scan_button)
        row.addStretch(1)
        row.addWidget(QLabel("Host:", self))
        self._host_edit = QLineEdit(self)
        self._host_edit.setPlaceholderText("echos.local or 192.168.4.1")
        row.addWidget(self._host_edit, 1)
        self._port_spin = QSpinBox(self)
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(80)
        row.addWidget(self._port_spin)
        self._probe_button = QPushButton("Check device", self)
        row.addWidget(self._probe_button)
        layout.addLayout(row)

        self._scan_button.clicked.connect(self._on_scan_clicked)
        self._probe_button.clicked.connect(self._on_probe_clicked)
        self._host_edit.textChanged.connect(lambda _text: self._update_buttons())
        self._table.itemSelectionChanged.connect(self.completeChanged)

    def initializePage(self) -> None:  # noqa: N802 — Qt override
        path = self._welcome.selected_path()
        if path == "ap":
            self._host_edit.setText(_AP_HOST)
            self._status.setText(
                f"Join the device's WiFi access point, then press "
                f'"Check device" to reach it at {_AP_HOST}.'
            )
        elif not self._devices:
            self._on_scan_clicked()

    def isComplete(self) -> bool:  # noqa: N802 — Qt override
        return self.selected_device() is not None

    def selected_device(self) -> DiscoveredEchos | None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._devices):
            return None
        return self._devices[row]

    def _update_buttons(self) -> None:
        busy = self._scan_busy or self._probe_busy
        self._scan_button.setEnabled(not busy)
        self._probe_button.setEnabled(not busy and bool(self._host_edit.text().strip()))

    @Slot()
    def _on_scan_clicked(self) -> None:
        self._devices.clear()
        self._table.setRowCount(0)
        self._scan_busy = True
        self._status.setText("Scanning the local network…")
        self._update_buttons()
        self.scanRequested.emit()

    @Slot()
    def _on_probe_clicked(self) -> None:
        host = self._host_edit.text().strip()
        if not host:
            return
        self._probe_busy = True
        self._status.setText(f"Checking {host}…")
        self._update_buttons()
        self.probeRequested.emit(host, int(self._port_spin.value()))

    @staticmethod
    def _host_keys(device: DiscoveredEchos) -> set[str]:
        return {device.hostname.casefold().rstrip("."), device.address.casefold()} - {""}

    @Slot(object)
    def on_device(self, payload: object) -> None:
        if not isinstance(payload, DiscoveredEchos):  # rule 4 guard
            return
        # Probe-busy ends with its result; a streaming SCAN row must NOT
        # end scan-busy (the scan ends with on_scan_finished/on_failed).
        if self._probe_busy:
            self._probe_busy = False
            self._status.setText("Device confirmed — select it and continue.")
            self._update_buttons()
        # Repeats (manual re-probe, scan row + manual probe of the same
        # node under hostname vs address) replace, never stack: matched on
        # the normalized {hostname, address} set + REST port.
        keys = self._host_keys(payload)
        for row, existing in enumerate(self._devices):
            if existing.http_port == payload.http_port and keys & self._host_keys(existing):
                self._devices[row] = payload
                self._fill_row(row, payload)
                self._table.selectRow(row)
                return
        self._devices.append(payload)
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._fill_row(row, payload)
        if self._table.currentRow() < 0:
            self._table.selectRow(row)

    def _fill_row(self, row: int, device: DiscoveredEchos) -> None:
        name = device.instance
        if self._host_keys(device) & self._configured_hosts:
            # Help-menu re-runs: say so, don't block (re-adding under a
            # new name is a legitimate config).
            name += " (already configured)"
        for column, text in (
            (0, name),
            (1, device.hostname or device.address),
            (2, device.firmware_version),
            (3, device.project_name),
        ):
            self._table.setItem(row, column, QTableWidgetItem(text))

    @Slot(int)
    def on_scan_finished(self, count: int) -> None:
        self._scan_busy = False
        self._update_buttons()
        if count:
            self._status.setText(f"Found {count} Echos node(s) — select one and continue.")
        else:
            self._status.setText(
                "No Echos nodes found on this network. Enter the device's "
                "host below (it may not advertise on mDNS), or scan again."
            )

    @Slot(str, str)
    def on_failed(self, kind: str, message: str) -> None:
        self._scan_busy = False
        self._probe_busy = False
        self._update_buttons()
        self._status.setText(f"Could not confirm an Echos device ({kind}): {message}")


class _DetailsPage(QWizardPage):
    """Page 3 — name + optional admin password + summary."""

    def __init__(
        self, find: _FindPage, existing_names: set[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setTitle("Name and credentials")
        self._find = find
        self._existing = existing_names

        layout = QVBoxLayout(self)
        self._summary = QLabel("", self)
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Device name:", self))
        self._name_edit = QLineEdit(self)
        name_row.addWidget(self._name_edit, 1)
        layout.addLayout(name_row)
        self._name_hint = QLabel("", self)
        self._name_hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(self._name_hint)

        pw_row = QHBoxLayout()
        pw_row.addWidget(QLabel("Admin password:", self))
        self._password_edit = QLineEdit(self)
        self._password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        pw_row.addWidget(self._password_edit, 1)
        layout.addLayout(pw_row)
        pw_hint = QLabel(
            "Optional — needed only to CHANGE settings on the device "
            "(reading always works). On first boot the device prints a "
            "random password once on its serial console. Stored in the "
            "OS keyring, never in the config file. You can store or "
            "change it later from the device dialog.",
            self,
        )
        pw_hint.setWordWrap(True)
        pw_hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(pw_hint)

        self._finish_status = QLabel("", self)
        layout.addWidget(self._finish_status)
        layout.addStretch(1)

        self._name_edit.textChanged.connect(self._on_name_changed)

    def initializePage(self) -> None:  # noqa: N802 — Qt override
        device = self._find.selected_device()
        if device is None:
            return
        self._name_edit.setText(suggest_device_name(device, self._existing))
        channels = len(device.channels)
        selector_note = (
            f"{channels} channels read from the device's StationXML — the "
            "stream selectors are set up for you."
            if channels
            else "The device's channel list was not readable; configure the "
            "stream selectors from the device dialog afterwards."
        )
        self._summary.setText(
            f"Adding {device.hostname or device.address} — firmware "
            f"{device.firmware_version}, project {device.project_name!r}, "
            f"SeedLink port {device.seedlink_port}. {selector_note}\n"
        )

    def isComplete(self) -> bool:  # noqa: N802 — Qt override
        name = self._name_edit.text().strip()
        return bool(name) and name not in self._existing

    def device_name(self) -> str:
        return self._name_edit.text().strip()

    def password(self) -> str:
        return self._password_edit.text()

    def set_finish_status(self, text: str) -> None:
        self._finish_status.setText(text)

    @Slot()
    def _on_name_changed(self) -> None:
        name = self._name_edit.text().strip()
        self._name_hint.setText(
            "A device with this name already exists." if name in self._existing else ""
        )
        self.completeChanged.emit()


class FirstRunWizard(QWizard):
    """The Echos first-run wizard (M6 rewrite).

    Constructed when :func:`is_first_run` returns True at startup, and
    on demand from Help → First-run wizard. Owns ONE worker thread
    hosting the discovery worker (scan + manual probe) and the device
    worker (keyring credential store at Finish).
    """

    # Wizard → device worker (queued).
    _credStoreRequested = Signal(str, str)  # noqa: N815

    def __init__(
        self,
        *,
        store: ConfigStore,
        parent: QWidget | None = None,
        credentials: CredentialsStore | None = None,
        discovery_worker: EchosDiscoveryWorker | None = None,
        device_worker: EchosDeviceWorker | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("EchosMonitor — First Run")
        self.setModal(True)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)

        self._store = store
        self._torn_down = False
        self._pending_credential = False

        existing_names = {d.name for d in store.root.devices}
        configured_hosts = {
            d.host.casefold().rstrip(".") for d in store.root.devices if d.host
        }
        self._welcome = _WelcomePage(parent=self)
        self._find = _FindPage(self._welcome, configured_hosts, parent=self)
        self._details = _DetailsPage(self._find, existing_names, parent=self)
        self.setPage(_PAGE_WELCOME, self._welcome)
        self.setPage(_PAGE_FIND, self._find)
        self.setPage(_PAGE_DETAILS, self._details)
        self.setStartId(_PAGE_WELCOME)

        # One thread, two small workers (slots serialize — a credential
        # store can never race a scan into the same event loop).
        self._discovery = discovery_worker or EchosDiscoveryWorker()
        self._device_worker = device_worker or EchosDeviceWorker(
            credentials if credentials is not None else CredentialsStore()
        )
        self._thread = QThread()
        self._thread.setObjectName("firstrun-wizard")
        self._discovery.moveToThread(self._thread)
        self._device_worker.moveToThread(self._thread)
        queued = Qt.ConnectionType.QueuedConnection
        self._find.scanRequested.connect(self._discovery.discover, type=queued)
        self._find.probeRequested.connect(self._discovery.probe_host, type=queued)
        self._discovery.deviceDiscovered.connect(self._find.on_device, type=queued)
        self._discovery.discoveryFinished.connect(self._find.on_scan_finished, type=queued)
        self._discovery.discoveryFailed.connect(self._find.on_failed, type=queued)
        self._credStoreRequested.connect(self._device_worker.storeCredential, type=queued)
        self._device_worker.credentialStored.connect(self._on_credential_stored, type=queued)
        # The thread starts LAZILY on the first page action: a wizard that
        # is constructed but never driven past Welcome (Help-menu open +
        # immediate close, menubar tests) must not own a running thread —
        # an undriven QWizard never reaches done()/teardown when its exec
        # is bypassed, and a GC'd running QThread is a hard abort. Queued
        # events posted before the start are delivered once exec() runs.
        self._find.scanRequested.connect(self._ensure_thread)
        self._find.probeRequested.connect(self._ensure_thread)

        # Rule 7: the keyring write at Finish is observable and bounded.
        self._credential_timer = QTimer(self)
        self._credential_timer.setSingleShot(True)
        self._credential_timer.setInterval(_CREDENTIAL_TIMEOUT_MS)
        self._credential_timer.timeout.connect(self._on_credential_timeout)

    @Slot()
    def _ensure_thread(self) -> None:
        if not self._torn_down and not self._thread.isRunning():
            self._thread.start()

    # ------------------------------------------------------------------
    # Finish path
    # ------------------------------------------------------------------
    def accept(self) -> None:
        if self._welcome.selected_path() == "skip" or self.currentId() == _PAGE_WELCOME:
            _log.info("first_run_wizard_skipped", path=self._welcome.selected_path())
            super().accept()
            return
        if self._pending_credential:
            return  # already finishing — waiting on the keyring write
        device = self._find.selected_device()
        if device is None:
            super().accept()
            return
        name = self._details.device_name()
        try:
            self._store.add_device(device_config_for(device, name))
        except ConfigError as exc:
            # Keep the wizard open so the user can fix and retry (the
            # M4-C reviewer finding — silent reject loses their input).
            _log.warning("first_run_wizard_commit_failed", error=str(exc))
            QMessageBox.warning(
                self,
                "Could not save device",
                f"The device could not be saved:\n\n{exc}\n\n"
                "Adjust the name and try Finish again.",
            )
            return
        _log.info(
            "first_run_wizard_device_added",
            device=name,
            host=device.hostname or device.address,
            selectors=len(device.channels),
        )
        password = self._details.password()
        if not password:
            super().accept()
            return
        # Keyring writes run on the worker (a locked keyring blocks on a
        # system prompt — never the GUI thread, rule 1). The wizard
        # accepts on stored/timeout; the password can always be re-stored
        # from the device dialog.
        self._pending_credential = True
        self._details.set_finish_status("Storing the admin password…")
        # Freeze ALL navigation during the bounded wait: Back would let
        # the user change the selection under a finish already committed.
        for which in (
            QWizard.WizardButton.FinishButton,
            QWizard.WizardButton.BackButton,
            QWizard.WizardButton.CancelButton,
        ):
            button = self.button(which)
            if button is not None:
                button.setEnabled(False)
        self._ensure_thread()
        self._credential_timer.start()
        self._credStoreRequested.emit(name, password)

    @Slot(str)
    def _on_credential_stored(self, device_key: str) -> None:
        del device_key
        if not self._pending_credential:
            return
        self._pending_credential = False
        self._credential_timer.stop()
        _log.info("first_run_wizard_credential_stored")
        super().accept()

    @Slot()
    def _on_credential_timeout(self) -> None:
        if not self._pending_credential:
            return
        self._pending_credential = False
        _log.warning("first_run_wizard_credential_timeout")
        QMessageBox.warning(
            self,
            "Password not stored",
            "Storing the admin password timed out (the OS keyring may be "
            "locked). The device was added — store the password later "
            "from the device dialog.",
        )
        super().accept()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def done(self, result: int) -> None:
        self._teardown()
        super().done(result)

    def _teardown(self) -> None:
        if self._torn_down:
            return
        self._torn_down = True
        if self._pending_credential:
            # X/Esc during the bounded keyring wait: the DEVICE write is
            # already durable; only the password store is dropped — say
            # so (rule 7: every dropped wait is observable). It can be
            # re-stored from the device dialog.
            self._pending_credential = False
            _log.warning("first_run_wizard_credential_dropped_at_close")
        self._credential_timer.stop()
        self._discovery.stop()
        self._device_worker.stop()
        self._thread.quit()
        joined = self._thread.wait(_THREAD_JOIN_MS)
        for signal, slot in (
            (self._find.scanRequested, self._discovery.discover),
            (self._find.probeRequested, self._discovery.probe_host),
            (self._discovery.deviceDiscovered, self._find.on_device),
            (self._discovery.discoveryFinished, self._find.on_scan_finished),
            (self._discovery.discoveryFailed, self._find.on_failed),
            (self._credStoreRequested, self._device_worker.storeCredential),
            (self._device_worker.credentialStored, self._on_credential_stored),
        ):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)
        if not joined:
            _log.warning("first_run_wizard_thread_join_timeout")
            _ABANDONED.append((self._thread, (self._discovery, self._device_worker)))


__all__ = ["FirstRunWizard"]
