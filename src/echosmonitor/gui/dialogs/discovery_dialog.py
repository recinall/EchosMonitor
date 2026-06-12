"""Discover-devices dialog — mDNS scan results + add-device hand-off (M6).

Owns one :class:`~echosmonitor.core.discovery.EchosDiscoveryWorker` on a
dedicated QThread for the dialog's lifetime (skill ``qt-worker-threading``:
queued connections both ways, plain-method ``stop()`` + bounded join on
close). Confirmed nodes stream into the table; "Add…" hands the selection
to the existing :class:`DeviceDialog` with an exact prefill (mDNS hostname,
probed SeedLink port, REST port) so the user only confirms a name.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.config.schema import DeviceConfig, EchosDeviceConfig
from echosmonitor.core.discovery import EchosDiscoveryWorker
from echosmonitor.core.models import DiscoveredEchos

if TYPE_CHECKING:
    from PySide6.QtGui import QCloseEvent

    from echosmonitor.core.config_store import ConfigStore
    from echosmonitor.core.streaming_engine import StreamingEngine

_log = structlog.get_logger(__name__)

_THREAD_JOIN_MS = 5000

# Threads whose bounded join timed out at dialog close: keep them
# referenced for the process lifetime — dropping the last reference to a
# running QThread aborts (the M6-0 lesson). stop() cancels the in-flight
# HTTP in milliseconds, so this list staying empty is the norm.
_ABANDONED: list[tuple[EchosDiscoveryWorker, QThread]] = []

_COL_NAME = 0
_COL_HOST = 1
_COL_FIRMWARE = 2
_COL_PROJECT = 3
_COL_STATUS = 4


class DiscoveryDialog(QDialog):
    """Modal "Discover devices" dialog over one scan worker."""

    # Dialog → worker (queued: the slot body runs on the worker thread).
    _scanRequested = Signal()  # noqa: N815

    def __init__(
        self,
        parent: QWidget,
        store: ConfigStore,
        *,
        engine: StreamingEngine | None = None,
        worker: EchosDiscoveryWorker | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._engine = engine
        self._devices: list[DiscoveredEchos] = []
        self._torn_down = False

        self.setWindowTitle("Discover Echos devices")
        self.resize(640, 360)

        layout = QVBoxLayout(self)
        self._status_label = QLabel("Scanning the local network…", self)
        layout.addWidget(self._status_label)

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            ["Device", "Host", "Firmware", "Project", "Status"]
        )
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._table, 1)

        buttons = QHBoxLayout()
        self._scan_button = QPushButton("Scan again", self)
        self._scan_button.setEnabled(False)  # a scan starts immediately
        self._add_button = QPushButton("Add device…", self)
        self._add_button.setEnabled(False)
        buttons.addWidget(self._scan_button)
        buttons.addWidget(self._add_button)
        buttons.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.addWidget(box)
        layout.addLayout(buttons)

        # Worker + thread (injectable for tests).
        self._worker = worker or EchosDiscoveryWorker()
        self._thread = QThread()
        self._thread.setObjectName("echos-discovery")
        self._worker.moveToThread(self._thread)
        self._scanRequested.connect(
            self._worker.discover, Qt.ConnectionType.QueuedConnection
        )
        self._worker.deviceDiscovered.connect(
            self._on_device, Qt.ConnectionType.QueuedConnection
        )
        self._worker.discoveryFinished.connect(
            self._on_finished, Qt.ConnectionType.QueuedConnection
        )
        self._worker.discoveryFailed.connect(
            self._on_failed, Qt.ConnectionType.QueuedConnection
        )
        self._thread.start()

        self._scan_button.clicked.connect(self._start_scan)
        self._add_button.clicked.connect(self._on_add_clicked)
        self._table.itemSelectionChanged.connect(self._update_add_enabled)
        box.rejected.connect(self.reject)

        self._start_scan()

    # ------------------------------------------------------------------
    # Scan lifecycle
    # ------------------------------------------------------------------
    @Slot()
    def _start_scan(self) -> None:
        self._devices.clear()
        self._table.setRowCount(0)
        self._scan_button.setEnabled(False)
        self._add_button.setEnabled(False)
        self._status_label.setText("Scanning the local network…")
        self._scanRequested.emit()

    @Slot(object)
    def _on_device(self, payload: object) -> None:
        if not isinstance(payload, DiscoveredEchos):  # rule 4 guard
            return
        self._devices.append(payload)
        row = self._table.rowCount()
        self._table.insertRow(row)
        configured = self._is_configured(payload)
        status = "already configured" if configured else "new"
        for column, text in (
            (_COL_NAME, payload.instance),
            (_COL_HOST, payload.hostname or payload.address),
            (_COL_FIRMWARE, payload.firmware_version),
            (_COL_PROJECT, payload.project_name),
            (_COL_STATUS, status),
        ):
            item = QTableWidgetItem(text)
            if configured:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._table.setItem(row, column, item)

    @Slot(int)
    def _on_finished(self, count: int) -> None:
        self._scan_button.setEnabled(True)
        if count:
            self._status_label.setText(f"Found {count} Echos node(s).")
        else:
            self._status_label.setText(
                "No Echos nodes found. Nodes that do not advertise on mDNS "
                "(or sit on another subnet) can be added manually."
            )
        self._update_add_enabled()

    @Slot(str, str)
    def _on_failed(self, kind: str, message: str) -> None:
        self._scan_button.setEnabled(True)
        self._status_label.setText(f"Discovery failed ({kind}): {message}")

    # ------------------------------------------------------------------
    # Add hand-off
    # ------------------------------------------------------------------
    def _selected_device(self) -> DiscoveredEchos | None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._devices):
            return None
        return self._devices[row]

    @Slot()
    def _update_add_enabled(self) -> None:
        device = self._selected_device()
        self._add_button.setEnabled(device is not None and not self._is_configured(device))

    def _is_configured(self, device: DiscoveredEchos) -> bool:
        """Already in the config? Matched on host (mDNS name OR address).

        Case/trailing-dot normalized: ``ECHOS.local`` and ``echos.local.``
        are the same host (a stale DHCP IP in the config still defeats the
        match — the probe address covers the common case).
        """

        def norm(host: str) -> str:
            return host.casefold().rstrip(".")

        hosts = {norm(d.host) for d in self._store.root.devices}
        keys = {norm(device.hostname), norm(device.address)} - {""}
        return bool(hosts & keys)

    def _suggest_name(self, device: DiscoveredEchos) -> str:
        """First mDNS label ("echos"), de-collided against the config."""
        base = (device.hostname.split(".")[0] if device.hostname else "") or device.instance
        existing = {d.name for d in self._store.root.devices}
        name = base
        suffix = 2
        while name in existing:
            name = f"{base}-{suffix}"
            suffix += 1
        return name

    def prefill_for(self, device: DiscoveredEchos) -> DeviceConfig:
        """The exact DeviceConfig seed for the add dialog.

        Host prefers the mDNS hostname (survives DHCP lease changes);
        ``port`` is the PROBED SeedLink port and ``echos.http_port`` the
        advertised REST port — the user only confirms the name.
        """
        return DeviceConfig(
            name=self._suggest_name(device),
            host=device.hostname or device.address,
            port=device.seedlink_port,
            echos=EchosDeviceConfig(http_port=device.http_port),
        )

    @Slot()
    def _on_add_clicked(self) -> None:
        device = self._selected_device()
        if device is None:
            return
        from echosmonitor.gui.dialogs.device_dialog import DeviceDialog

        DeviceDialog.add(self, self._store, prefill=self.prefill_for(device), engine=self._engine)
        # Re-mark rows: an accepted add makes its row "already configured".
        for row, discovered in enumerate(self._devices):
            if self._is_configured(discovered):
                status = self._table.item(row, _COL_STATUS)
                if status is not None:
                    status.setText("already configured")
                for column in range(self._table.columnCount()):
                    item = self._table.item(row, column)
                    if item is not None:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
        self._update_add_enabled()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self._teardown()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        # ``done`` covers every exit path (Close button, Esc, exec return).
        self._teardown()
        super().done(result)

    def _teardown(self) -> None:
        # Latch, not isRunning(): the X-button path runs closeEvent AND
        # done — a re-run after a join timeout would freeze the GUI for a
        # second full wait and double-append to _ABANDONED.
        if self._torn_down:
            return
        self._torn_down = True
        self._worker.stop()  # cancels in-flight HTTP in milliseconds
        self._thread.quit()
        joined = self._thread.wait(_THREAD_JOIN_MS)
        # Disconnect AFTER the join (skill §3): nothing may emit into a
        # dialog the panel is about to deleteLater.
        for signal, slot in (
            (self._scanRequested, self._worker.discover),
            (self._worker.deviceDiscovered, self._on_device),
            (self._worker.discoveryFinished, self._on_finished),
            (self._worker.discoveryFailed, self._on_failed),
        ):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)
        if not joined:
            _log.warning("discovery_thread_join_timeout")
            # Keep the pair referenced for the process lifetime (M6-0:
            # dropping a running QThread reference is a hard abort).
            _ABANDONED.append((self._worker, self._thread))
