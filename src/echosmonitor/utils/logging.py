"""Structured logging setup.

Routes both ``structlog`` and stdlib ``logging`` records through the same
processor pipeline so libraries like ``obspy`` and ``urllib3`` end up with the
same shape (timestamp, level, app, version, pid, ...).
"""

from __future__ import annotations

import logging
import os
import sys

import structlog
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

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

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
