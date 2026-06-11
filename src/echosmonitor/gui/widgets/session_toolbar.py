"""Global session toolbar (M2-C, rules 13-14).

The single place the user drives acquisition from:

* **▶ Monitor** — start monitoring every idle device (live view, zero
  disk writes).
* **⏺ Record…** — open the new-session dialog (project name + which
  devices record) and start the session.
* **⏹ Stop** — stop everything: the active session closes cleanly and
  every device returns to Idle (``engine.stop()`` — bounded, rule 7).

A 1 Hz timer renders the session status (project · elapsed · bytes
written this session) from engine snapshots — pull-based, best-effort,
GUI-thread-budget reads only (skill ``qt-worker-threading`` §6).
Engine signals arrive via ``QueuedConnection`` so a toolbar update can
never run re-entrantly inside an engine emit.
"""

from __future__ import annotations

import sqlite3
import time
from typing import TYPE_CHECKING

import structlog
from obspy import UTCDateTime
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QLabel, QMessageBox, QToolBar, QWidget

from echosmonitor.core.exceptions import SessionError
from echosmonitor.core.models import AcquisitionState
from echosmonitor.gui.dialogs.new_session_dialog import NewSessionDialog
from echosmonitor.storage.sessions import ProjectNameCollisionError

if TYPE_CHECKING:
    from echosmonitor.core.streaming_engine import StreamingEngine

_log = structlog.get_logger(__name__)

_REFRESH_MS = 1000


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / (1024**2):.1f} MB"
    return f"{n / (1024**3):.1f} GB"


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


class SessionToolbar(QToolBar):
    """Monitor / Record / Stop controls + live session status label."""

    def __init__(self, engine: StreamingEngine, parent: QWidget | None = None) -> None:
        super().__init__("Session", parent)
        self.setObjectName("SessionToolbar")
        self.setMovable(False)
        self._engine = engine

        self._action_monitor = QAction("▶ Monitor", self)
        self._action_record = QAction("⏺ Record…", self)
        self._action_stop = QAction("⏹ Stop", self)
        self.addAction(self._action_monitor)
        self.addAction(self._action_record)
        self.addAction(self._action_stop)
        self.addSeparator()
        self._status_label = QLabel("Idle", self)
        self._status_label.setObjectName("SessionStatusLabel")
        self._status_label.setContentsMargins(8, 0, 8, 0)
        self.addWidget(self._status_label)

        self._action_monitor.triggered.connect(self._on_monitor_clicked)
        self._action_record.triggered.connect(self._on_record_clicked)
        self._action_stop.triggered.connect(self._on_stop_clicked)

        # Queued: the engine emits these mid-lifecycle on the same
        # thread; a queued hop guarantees the toolbar's handlers (which
        # call back into the engine for snapshots) never run inside an
        # engine emit (M2-B reentrancy postmortems).
        engine.sessionChanged.connect(
            self._on_session_changed, type=Qt.ConnectionType.QueuedConnection
        )
        engine.acquisitionStateChanged.connect(
            self._on_acquisition_changed, type=Qt.ConnectionType.QueuedConnection
        )
        engine.devicesChanged.connect(
            self._refresh, type=Qt.ConnectionType.QueuedConnection
        )

        # Per-session bytes baseline: DeviceStatus.archive_bytes_written
        # accumulates across sessions (counters carry forward), so the
        # toolbar snapshots each member's counter when it joins and
        # shows the delta.
        self._bytes_baseline: dict[str, int] = {}
        self._session_t0: float | None = None

        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self._refresh()

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------
    def _on_monitor_clicked(self) -> None:
        started = []
        for dev_cfg in self._engine.devices():
            if self._engine.acquisition_state(dev_cfg.name) is AcquisitionState.IDLE:
                self._engine.start_monitoring(dev_cfg.name)
                started.append(dev_cfg.name)
        _log.info("session_toolbar_monitor_clicked", started=started)
        self._refresh()

    def _on_record_clicked(self) -> None:
        if self._engine.active_session() is not None:
            return
        names = [d.name for d in self._engine.devices()]
        dialog = NewSessionDialog(names, self.parentWidget())
        if dialog.exec() != NewSessionDialog.DialogCode.Accepted:
            return
        project = dialog.project_name()
        devices = dialog.checked_devices()
        try:
            self._engine.start_session(project, devices)
        except (SessionError, ProjectNameCollisionError, KeyError, sqlite3.Error, OSError) as exc:
            _log.warning(
                "session_toolbar_start_failed",
                project=project,
                error=str(exc),
            )
            QMessageBox.warning(
                self.parentWidget(),
                "Cannot start session",
                str(exc),
            )
        self._refresh()

    def _on_stop_clicked(self) -> None:
        # Global stop: the session row closes cleanly inside
        # engine.stop() and every device returns to Idle — one bounded
        # call (rule 7), one unmistakable end state (rule 13).
        _log.info("session_toolbar_stop_clicked")
        self._engine.stop()
        self._refresh()

    # ------------------------------------------------------------------
    # Engine signals
    # ------------------------------------------------------------------
    @Slot(object)
    def _on_session_changed(self, payload: object) -> None:
        if payload is None:
            self._bytes_baseline.clear()
            self._session_t0 = None
        # Baselines and t0 are established inside _refresh on first
        # sight of each member (synchronously correct even when this
        # queued slot lags the session start); here we only clear.
        self._refresh()

    @Slot(str, int)
    def _on_acquisition_changed(self, _name: str, _state: int) -> None:
        self._refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        engine = self._engine
        session = engine.active_session()
        states = [engine.acquisition_state(d.name) for d in engine.devices()]
        any_idle = any(s is AcquisitionState.IDLE for s in states)
        any_active = any(s is not AcquisitionState.IDLE for s in states)

        self._action_monitor.setEnabled(any_idle)
        self._action_record.setEnabled(session is None and bool(states))
        self._action_stop.setEnabled(any_active or session is not None)

        if session is not None:
            if self._session_t0 is None:
                self._session_t0 = float(UTCDateTime(session.started_at).timestamp)
            elapsed = time.time() - self._session_t0
            statuses = engine.device_status()
            written = 0
            for name in session.devices:
                status = statuses.get(name)
                if status is not None:
                    # Baseline on FIRST SIGHT of a member, synchronously
                    # (rule 9 — counters sourced where membership is
                    # observed): DeviceStatus.archive_bytes_written
                    # accumulates across sessions, and waiting for the
                    # queued sessionChanged would let one render show
                    # the lifetime counter. Writes can't precede this
                    # read — packets reach the writer only via later
                    # drain ticks on this same thread.
                    baseline = self._bytes_baseline.setdefault(
                        name, status.archive_bytes_written
                    )
                    written += max(0, status.archive_bytes_written - baseline)
            self._status_label.setText(
                f"⏺ {session.project_name} · "
                f"{_format_elapsed(elapsed)} · {_format_bytes(written)}"
            )
        elif any_active:
            n = sum(1 for s in states if s is not AcquisitionState.IDLE)
            self._status_label.setText(f"Monitoring ({n})")
        else:
            self._status_label.setText("Idle")
