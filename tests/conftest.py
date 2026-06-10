"""Test-suite-wide fixtures and headless Qt setup."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

# Force the offscreen Qt platform before PySide6 is imported anywhere.
# This is required for CI and any environment without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp_args() -> list[str]:
    """pytest-qt hook: command-line arguments for the test QApplication."""
    return ["echosmonitor-tests"]


@pytest.fixture(autouse=True)
def _redirect_qsettings(tmp_path: Path) -> None:
    """Route QSettings to a per-test directory so tests never touch the user's
    real settings store.

    BOTH formats must be redirected: ``setDefaultFormat`` only affects the
    no-argument ``QSettings()`` constructor, while ``QSettings(org, app)``
    (e.g. ``MainWindow._settings()``) always uses ``NativeFormat`` — without
    the NativeFormat redirect those reads/writes hit the user's real
    ``~/.config/<org>/<app>.conf`` and test outcomes depend on the machine.
    """
    from PySide6.QtCore import QSettings

    for fmt in (QSettings.Format.IniFormat, QSettings.Format.NativeFormat):
        QSettings.setPath(
            fmt,
            QSettings.Scope.UserScope,
            str(tmp_path / "qsettings"),
        )
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)


@pytest.fixture
def capture_structlog() -> Iterator[list[dict[str, Any]]]:
    """Yield a list that gets appended to with every structlog event.

    Wraps `structlog.testing.capture_logs()`. The yielded list contains one
    dict per emitted event with keys like `event`, `log_level`, plus any
    bound key/value pairs.
    """
    with structlog.testing.capture_logs() as records:
        yield records
