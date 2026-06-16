"""In-app Log tab (M6.6-D).

A read-only viewer for the application's structured logs. Records arrive
from :class:`~echosmonitor.utils.logging.QtLogSink` via a queued
``Signal(object)`` — so every record is handled on the GUI thread no
matter which worker logged it (rule 1/4, skill qt-worker-threading §§1,4).

The widget keeps its OWN bounded mirror of the records (``deque(maxlen)``)
so the level filter can re-render without re-querying the sink; the
``QPlainTextEdit`` itself is also block-capped as a second guard. Nothing
here touches a worker thread.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.storage.log_export import LogExportError, write_log_text
from echosmonitor.utils.logging import LogRecordView, QtLogSink

# Level filter options: label → minimum levelno admitted. "ALL" admits
# everything the sink already passed (the root level still gates upstream).
_LEVEL_FILTERS: tuple[tuple[str, int], ...] = (
    ("ALL", 0),
    ("DEBUG", 10),
    ("INFO", 20),
    ("WARNING", 30),
    ("ERROR", 40),
)


class LogWidget(QWidget):
    """Read-only log view with level filter, autoscroll, pause and export."""

    def __init__(self, sink: QtLogSink, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sink = sink
        # Mirror of the rendered records, same cap as the sink. The view's
        # text is derived from this on every filter/pause re-render.
        self._records: deque[LogRecordView] = deque(maxlen=sink.max_lines)
        self._min_level = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Level:"))
        self._level_combo = QComboBox()
        for label, _levelno in _LEVEL_FILTERS:
            self._level_combo.addItem(label)
        self._level_combo.setCurrentText("ALL")
        self._level_combo.currentIndexChanged.connect(self._on_level_changed)
        controls.addWidget(self._level_combo)

        self._autoscroll = QCheckBox("Autoscroll")
        self._autoscroll.setChecked(True)
        controls.addWidget(self._autoscroll)

        self._pause = QCheckBox("Pause")
        self._pause.setChecked(False)
        self._pause.toggled.connect(self._on_pause_toggled)
        controls.addWidget(self._pause)

        controls.addStretch(1)

        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self._on_clear)
        controls.addWidget(self._clear_btn)

        self._copy_btn = QPushButton("Copy")
        self._copy_btn.clicked.connect(self._on_copy)
        controls.addWidget(self._copy_btn)

        self._export_btn = QPushButton("Export…")
        self._export_btn.clicked.connect(self._on_export)
        controls.addWidget(self._export_btn)

        root.addLayout(controls)

        self._view = QPlainTextEdit()
        self._view.setObjectName("LogView")
        self._view.setReadOnly(True)
        self._view.setUndoRedoEnabled(False)
        # Monospace + capped block count: a second drop-oldest guard on top
        # of the deque so the widget can never grow unbounded (rule 5).
        self._view.setMaximumBlockCount(sink.max_lines)
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = self._view.font()
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFamily("monospace")
        self._view.setFont(font)
        root.addWidget(self._view, 1)

        # Prefill from records emitted before this tab existed, THEN go live.
        # Connecting after prefill avoids double-rendering a record: anything
        # in the snapshot was emitted before we connected, so its queued
        # signal never reaches on_record. A record logged in the microsecond
        # gap between snapshot() and connect() is missed by the LIVE view
        # (it still lives in the sink buffer and replays on the next launch's
        # prefill) — an acceptable, self-correcting cosmetic gap.
        for view in sink.snapshot():
            self._records.append(view)
        self._rerender()
        sink.bridge.recordReady.connect(
            self.on_record, Qt.ConnectionType.QueuedConnection
        )

    # ------------------------------------------------------------------
    # Live ingest (GUI thread, via QueuedConnection)
    # ------------------------------------------------------------------
    def on_record(self, view: object) -> None:
        """Handle one record from the sink. Guarded per rule 4 (Signal(object))."""
        if not isinstance(view, LogRecordView):
            return
        self._records.append(view)
        if self._pause.isChecked():
            return
        if view.levelno >= self._min_level:
            self._append_line(view)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _append_line(self, view: LogRecordView) -> None:
        self._view.appendPlainText(view.line)
        if self._autoscroll.isChecked():
            bar = self._view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _rerender(self) -> None:
        """Rebuild the view from the record mirror under the active filter."""
        self._view.clear()
        lines = [v.line for v in self._records if v.levelno >= self._min_level]
        if lines:
            self._view.setPlainText("\n".join(lines))
            if self._autoscroll.isChecked():
                bar = self._view.verticalScrollBar()
                bar.setValue(bar.maximum())

    def _on_level_changed(self, index: int) -> None:
        self._min_level = _LEVEL_FILTERS[index][1]
        self._rerender()

    def _on_pause_toggled(self, paused: bool) -> None:
        # On resume, flush everything buffered while paused.
        if not paused:
            self._rerender()

    def _on_clear(self) -> None:
        self._records.clear()
        self._view.clear()

    def _on_copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._view.toPlainText())

    def _on_export(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export log", "echosmonitor.log", "Log files (*.log *.txt);;All files (*)"
        )
        if not path:
            return
        # Rule 8: the actual write lives in storage/ and is atomic.
        try:
            write_log_text(self._view.toPlainText(), Path(path))
        except LogExportError as exc:
            QMessageBox.warning(self, "Export log", str(exc))


__all__ = ["LogWidget"]
