"""Tests for the M6.6-D in-app log sink (:mod:`utils.logging`).

Assert observable behaviour (rule 10): the sink renders a clean line, the
buffer is bounded drop-oldest (rule 5), the snapshot is the prefill source,
and installation onto the root logger is idempotent.
"""

from __future__ import annotations

import logging

from echosmonitor.utils.logging import (
    LogRecordView,
    QtLogSink,
    install_gui_log_sink,
)


def _feed(sink: QtLogSink, name: str, n: int, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = [sink]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for i in range(n):
        logger.log(level, "event_%d", i)
    return logger


def test_sink_renders_clean_line(qapp: object) -> None:
    sink = QtLogSink(max_lines=10)
    _feed(sink, "test.m66d.render", 1)
    snap = sink.snapshot()
    assert len(snap) == 1
    assert isinstance(snap[0], LogRecordView)
    # The rendered line carries the event and no ANSI escape sequences.
    assert "event_0" in snap[0].line
    assert "\x1b[" not in snap[0].line


def test_sink_buffer_is_bounded_drop_oldest(qapp: object) -> None:
    """Rule 5: the deque caps at ``max_lines`` and drops the OLDEST."""
    sink = QtLogSink(max_lines=3)
    _feed(sink, "test.m66d.cap", 5)
    snap = sink.snapshot()
    assert len(snap) == 3
    lines = [v.line for v in snap]
    # Oldest two (event_0, event_1) were dropped; newest three remain in order.
    assert "event_2" in lines[0]
    assert "event_3" in lines[1]
    assert "event_4" in lines[2]
    assert all("event_0" not in line and "event_1" not in line for line in lines)


def test_sink_preserves_level(qapp: object) -> None:
    sink = QtLogSink(max_lines=10)
    _feed(sink, "test.m66d.level", 1, level=logging.WARNING)
    view = sink.snapshot()[0]
    assert view.levelno == logging.WARNING
    assert view.level == "WARNING"


def test_install_gui_log_sink_idempotent(qapp: object) -> None:
    root = logging.getLogger()
    before = [h for h in root.handlers if isinstance(h, QtLogSink)]
    try:
        first = install_gui_log_sink(max_lines=50)
        second = install_gui_log_sink(max_lines=50)
        sinks = [h for h in root.handlers if isinstance(h, QtLogSink)]
        # Re-install removes the prior sink rather than stacking handlers.
        assert sinks == [second]
        assert first not in sinks
        assert second.max_lines == 50
    finally:
        for h in [h for h in root.handlers if isinstance(h, QtLogSink)]:
            root.removeHandler(h)
        for h in before:
            root.addHandler(h)
