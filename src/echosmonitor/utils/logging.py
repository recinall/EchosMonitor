"""Structured logging setup.

Routes both ``structlog`` and stdlib ``logging`` records through the same
processor pipeline so libraries like ``obspy`` and ``urllib3`` end up with the
same shape (timestamp, level, app, version, pid, ...).
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
from collections import deque
from dataclasses import dataclass

import structlog
from PySide6.QtCore import QObject, Signal
from structlog.types import Processor

from echosmonitor import __version__

# Substrings emitted by obspy's seedlink connection at WARNING/ERROR/INFO
# whenever a connection is torn down — by us, by the server, or because the
# socket goes away. None of them are actionable; they're verbose teardown
# chatter that the user doesn't need at INFO level. We downgrade to DEBUG so
# they remain inspectable when the user explicitly asks for DEBUG output.
_OBSPY_SEEDLINK_NOISE_SUBSTRINGS = (
    "socket read error",
    "network socket closed",
    "terminating collect loop",
)
_OBSPY_SEEDLINK_LOGGER = "obspy.clients.seedlink"


def _shared_processors() -> list[Processor]:
    """The processor chain shared by every renderer (stderr + GUI sink).

    A fresh list each call: the processors are stateless callables, so
    rebuilding is cheap and avoids cross-handler aliasing.
    """
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Configure structlog + stdlib logging.

    Args:
        level: Minimum level for the root logger (e.g. ``"INFO"``, ``"DEBUG"``).
        json_output: If true, emit JSON lines (production); otherwise a colored
            console renderer for dev.
    """
    level_no = logging.getLevelName(level.upper())
    if not isinstance(level_no, int):
        raise ValueError(f"invalid log level: {level!r}")

    shared_processors = _shared_processors()

    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    # structlog -> stdlib bridge: structlog emits a ProcessorFormatter-friendly
    # event dict; the stdlib handler renders it.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    # The handler must enforce the configured level itself: our seedlink-noise
    # filter downgrades records to DEBUG, and downgraded records would still
    # reach the handler unless it gates on `record.levelno >= handler.level`.
    handler.setLevel(level_no)

    root = logging.getLogger()
    # Replace any prior handlers (re-configuration is supported).
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level_no)

    _install_obspy_seedlink_filter()

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        app="echosmonitor",
        version=__version__,
        pid=os.getpid(),
    )


class _ObsPySeedLinkNoiseFilter(logging.Filter):
    """Downgrade obspy seedlink shutdown chatter to DEBUG.

    Three messages are emitted by ``obspy.clients.seedlink.client.seedlinkconnection``
    every time a SeedLink connection is torn down. They never indicate a real
    problem in this app's flow — we drive reconnects ourselves and treat
    socket loss as expected. Keep them at DEBUG so they're available for
    troubleshooting without polluting normal output.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # Bad format args shouldn't drop the record — let it through unchanged.
            return True
        for substring in _OBSPY_SEEDLINK_NOISE_SUBSTRINGS:
            if substring in message:
                record.levelno = logging.DEBUG
                record.levelname = "DEBUG"
                return True
        return True


def _install_obspy_seedlink_filter() -> None:
    """Attach the noise filter to the obspy seedlink logger. Idempotent."""
    target = logging.getLogger(_OBSPY_SEEDLINK_LOGGER)
    for existing in target.filters:
        if isinstance(existing, _ObsPySeedLinkNoiseFilter):
            return
    target.addFilter(_ObsPySeedLinkNoiseFilter())


# ----------------------------------------------------------------------
# M6.6-D: in-app Log tab sink.
#
# A stdlib ``logging.Handler`` whose ``emit`` is called SYNCHRONOUSLY from
# whatever thread logged (any worker, or the GUI thread). It does two
# thread-safe things and nothing Qt-widget-touching:
#   1. append a rendered, frozen view to a bounded deque under a lock
#      (rule 5: cap + drop-oldest, no unbounded growth), and
#   2. emit a ``Signal(object)`` on a GUI-affinity QObject bridge.
# The bridge → widget connection is QueuedConnection, so the widget (which
# only the GUI thread may touch, rule 1) always handles records on the GUI
# thread regardless of the emitting thread (skill qt-worker-threading §§1,4).
# The deque doubles as a prefill snapshot for logs emitted before the tab
# existed.
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogRecordView:
    """A frozen, GUI-facing snapshot of one log record (rule 4 payload).

    ``line`` is the fully rendered text (timestamp + level + logger + event
    + kv context, no ANSI); ``levelno``/``level``/``logger`` are split out
    so the Log tab can filter by level without re-parsing the line.
    """

    levelno: int
    level: str
    logger: str
    line: str


class _QtLogBridge(QObject):
    """QObject carrying the cross-thread record signal. GUI-thread affinity."""

    recordReady = Signal(object)  # noqa: N815 — Qt signal naming


def _make_gui_formatter() -> logging.Formatter:
    """A console renderer with colors OFF — ANSI codes would litter the GUI."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )


class QtLogSink(logging.Handler):
    """Bounded, thread-safe log handler that feeds the in-app Log tab.

    ``emit`` runs on the logging thread; it never touches a widget and
    never raises across the logging boundary (a failure routes to
    :meth:`logging.Handler.handleError`). The widget connects
    ``bridge.recordReady`` with ``QueuedConnection`` and prefills from
    :meth:`snapshot`.

    Lifetime: the sink stays attached to the root logger for the whole
    process so logs emitted during shutdown (e.g. worker joins) still flow.
    The bridge QObject is owned by the sink (not parented to any widget),
    so it intentionally outlives the Log dock and every worker ``emit``;
    the ``suppress(RuntimeError)`` covers the interpreter-teardown race
    where the C++ bridge is gone but Python still calls ``emit``.
    """

    def __init__(self, max_lines: int = 1000) -> None:
        super().__init__()
        self.max_lines = max(1, max_lines)
        self._buffer: deque[LogRecordView] = deque(maxlen=self.max_lines)
        # Dedicated lock guarding the deque: ``snapshot`` is called from the
        # GUI thread during widget construction, concurrently with worker
        # ``emit`` calls (rule 5).
        self._buf_lock = threading.Lock()
        self.bridge = _QtLogBridge()
        self.setFormatter(_make_gui_formatter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            view = LogRecordView(
                levelno=record.levelno,
                level=record.levelname,
                logger=record.name,
                line=self.format(record),
            )
        except Exception:
            # Logging boundary: never propagate (rule mirrors stdlib emit).
            self.handleError(record)
            return
        with self._buf_lock:
            self._buffer.append(view)
        # Emit OUTSIDE the lock (Qt posts an event; no widget work here).
        # The bridge may be torn down during interpreter shutdown.
        with contextlib.suppress(RuntimeError):
            self.bridge.recordReady.emit(view)

    def snapshot(self) -> tuple[LogRecordView, ...]:
        """Records buffered so far, oldest → newest (for tab prefill)."""
        with self._buf_lock:
            return tuple(self._buffer)


def install_gui_log_sink(max_lines: int = 1000) -> QtLogSink:
    """Attach a fresh :class:`QtLogSink` to the root logger and return it.

    Call AFTER :func:`configure_logging` (so the root level is set) and
    hand the returned sink to ``MainWindow``. Any prior sink is removed so
    re-invocation (tests, reconfigure) does not stack handlers. The sink
    inherits the root logger's level filtering, so the Log tab shows the
    same record set as stderr.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, QtLogSink):
            root.removeHandler(existing)
    sink = QtLogSink(max_lines=max_lines)
    root.addHandler(sink)
    return sink
