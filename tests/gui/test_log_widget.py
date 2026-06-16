"""Tests for the M6.6-D Log tab widget (:class:`LogWidget`).

Assert observable behaviour (rule 10): a record logged from a WORKER
thread reaches the view on the GUI thread (the QueuedConnection marshal),
the level filter actually removes lines from the view, pause buffers then
flushes, prefill replays the sink snapshot, and clear empties the view.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from echosmonitor.gui.widgets import log_widget as log_widget_mod
from echosmonitor.gui.widgets.log_widget import LogWidget
from echosmonitor.storage.log_export import LogExportError
from echosmonitor.utils.logging import LogRecordView, QtLogSink


def _view(levelno: int, line: str) -> LogRecordView:
    return LogRecordView(
        levelno=levelno, level=logging.getLevelName(levelno), logger="test", line=line
    )


def test_record_from_worker_thread_reaches_view(qtbot: QtBot) -> None:
    """The cross-thread marshal: a worker-thread log lands on the GUI view."""
    sink = QtLogSink(max_lines=100)
    widget = LogWidget(sink)
    qtbot.addWidget(widget)

    logger = logging.getLogger("test.m66d.worker")
    logger.handlers = [sink]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    marker = "from_worker_thread_marker"
    thread = threading.Thread(target=lambda: logger.warning(marker))
    with qtbot.waitSignal(sink.bridge.recordReady, timeout=2000):
        thread.start()
    thread.join()
    # Delivery to the widget slot is queued — pump the loop until it lands.
    qtbot.waitUntil(lambda: marker in widget._view.toPlainText(), timeout=2000)


def test_level_filter_removes_lines_from_view(qtbot: QtBot) -> None:
    sink = QtLogSink(max_lines=100)
    widget = LogWidget(sink)
    qtbot.addWidget(widget)

    widget.on_record(_view(logging.DEBUG, "dbg-line"))
    widget.on_record(_view(logging.WARNING, "warn-line"))
    assert "dbg-line" in widget._view.toPlainText()
    assert "warn-line" in widget._view.toPlainText()

    widget._level_combo.setCurrentText("WARNING")
    text = widget._view.toPlainText()
    assert "warn-line" in text
    assert "dbg-line" not in text

    # Relaxing the filter brings the DEBUG line back (mirror is retained).
    widget._level_combo.setCurrentText("ALL")
    assert "dbg-line" in widget._view.toPlainText()


def test_pause_buffers_then_flushes(qtbot: QtBot) -> None:
    sink = QtLogSink(max_lines=100)
    widget = LogWidget(sink)
    qtbot.addWidget(widget)

    widget._pause.setChecked(True)
    widget.on_record(_view(logging.INFO, "while-paused"))
    assert "while-paused" not in widget._view.toPlainText()

    widget._pause.setChecked(False)
    assert "while-paused" in widget._view.toPlainText()


def test_prefill_replays_snapshot(qtbot: QtBot) -> None:
    sink = QtLogSink(max_lines=100)
    logger = logging.getLogger("test.m66d.prefill")
    logger.handlers = [sink]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.info("logged_before_tab_existed")

    widget = LogWidget(sink)
    qtbot.addWidget(widget)
    assert "logged_before_tab_existed" in widget._view.toPlainText()


def test_clear_empties_view(qtbot: QtBot) -> None:
    sink = QtLogSink(max_lines=100)
    widget = LogWidget(sink)
    qtbot.addWidget(widget)

    widget.on_record(_view(logging.INFO, "to-be-cleared"))
    assert "to-be-cleared" in widget._view.toPlainText()
    widget._on_clear()
    assert widget._view.toPlainText() == ""


def test_view_block_count_matches_sink_cap(qtbot: QtBot) -> None:
    """The view's drop-oldest block cap is driven by the sink's max_lines
    (which __main__ sets from app.log_max_lines)."""
    sink = QtLogSink(max_lines=42)
    widget = LogWidget(sink)
    qtbot.addWidget(widget)
    assert widget._view.maximumBlockCount() == 42


def test_export_writes_view_text(qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = QtLogSink(max_lines=100)
    widget = LogWidget(sink)
    qtbot.addWidget(widget)
    widget.on_record(_view(logging.INFO, "exported-line-1"))
    widget.on_record(_view(logging.WARNING, "exported-line-2"))

    target = tmp_path / "out.log"
    monkeypatch.setattr(
        log_widget_mod.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: (str(target), "")),
    )
    widget._on_export()

    text = target.read_text(encoding="utf-8")
    assert "exported-line-1" in text
    assert "exported-line-2" in text


def test_export_failure_warns_user(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    sink = QtLogSink(max_lines=100)
    widget = LogWidget(sink)
    qtbot.addWidget(widget)
    widget.on_record(_view(logging.INFO, "doomed"))

    monkeypatch.setattr(
        log_widget_mod.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: ("/no/such/dir/out.log", "")),
    )

    def _boom(text: str, path: Path) -> int:
        raise LogExportError("permission denied")

    monkeypatch.setattr(log_widget_mod, "write_log_text", _boom)
    warned: list[str] = []
    monkeypatch.setattr(
        log_widget_mod.QMessageBox,
        "warning",
        staticmethod(lambda *a, **k: warned.append(a[2] if len(a) > 2 else "")),
    )
    widget._on_export()
    assert warned and "permission denied" in warned[0]
