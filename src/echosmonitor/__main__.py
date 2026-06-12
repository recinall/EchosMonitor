"""Application entry point.

Wires CLI parsing, structured logging, config loading, and the qasync-driven
Qt event loop. Per CLAUDE.md, networking and DSP code are forbidden in this
module — it only bootstraps.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

import qasync
import structlog
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from echosmonitor import __version__
from echosmonitor.config import load_config
from echosmonitor.config.loader import user_config_path
from echosmonitor.core.firstrun import is_first_run
from echosmonitor.gui.dialogs.first_run_wizard import FirstRunWizard
from echosmonitor.gui.main_window import MainWindow
from echosmonitor.utils.logging import configure_logging

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="echosmonitor",
        description="Realtime seismic monitoring for Echos devices (SeedLink).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to a YAML config file (overrides XDG and bundled defaults).",
    )
    parser.add_argument(
        "--log-level",
        choices=_LOG_LEVELS,
        default=None,
        help="Override log level from config.",
    )
    parser.add_argument(
        "--log-json",
        action="store_true",
        default=None,
        help="Emit JSON log lines instead of the colored console renderer.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the application. Returns the Qt event loop exit code."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    cfg, cfg_path = load_config(args.config)

    log_level = args.log_level or cfg.app.log_level
    log_json = cfg.app.log_json if args.log_json is None else args.log_json
    configure_logging(level=log_level, json_output=log_json)

    log = structlog.get_logger(__name__)
    log.info("config_loaded", path=str(cfg_path), devices=len(cfg.devices))

    app = QApplication.instance() or QApplication(sys.argv)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    # M6: plot theme from ui.theme — must run BEFORE MainWindow constructs
    # any pyqtgraph item (the colors are read at item creation).
    from echosmonitor.gui.theme import apply_theme

    apply_theme(cfg.ui.theme)

    # Qt's event loop swallows SIGINT by default; route it to a clean app quit
    # so Ctrl-C in a terminal exits the GUI instead of being ignored. A noop
    # QTimer wakes the Python interpreter often enough for the signal to be
    # delivered while the C++ event loop is running.
    signal.signal(signal.SIGINT, lambda *_args: QApplication.quit())
    sigint_keepalive = QTimer()
    sigint_keepalive.start(200)
    sigint_keepalive.timeout.connect(lambda: None)

    window = MainWindow(cfg, cfg_path)

    # M4 stage C — first-run wizard. Detection is "no user config file
    # on disk AND zero devices in the loaded config" (see
    # ``core.firstrun``). Constructing MainWindow first means the
    # wizard runs against the same StreamingEngine + ConfigStore the
    # main window will use; the engine picks up the wizard's writes
    # via the regular configChanged hot-reload path. The
    # ``args.config`` branch suppresses the wizard so users explicitly
    # passing a config (typically tests, or an explicit YAML override)
    # never get interrupted.
    if args.config is None and is_first_run(user_config_path(), cfg):
        wizard = FirstRunWizard(
            store=window._store,
            parent=window,
        )
        wizard.exec()
        wizard.deleteLater()

    window.show()

    with loop:
        exit_code = loop.run_forever()
    log.info("event_loop_exited", code=exit_code)
    return int(exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
