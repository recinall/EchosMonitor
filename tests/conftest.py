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
def _redirect_platformdirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route platformdirs' user_data_dir into the per-test directory.

    Production code resolves the default archive root via
    ``platformdirs.user_data_dir("echosmonitor", "EchosMonitor")``
    (``core/session.resolve_base_archive_root``, the credentials file
    fallback). Without this redirect, any test that constructs
    ``MainWindow`` with a default config runs the M2-C launch
    crash-recovery sweep against the USER'S REAL archive — opening,
    migrating and mutating real session rows (the M0-C QSettings bug
    class, code-reviewer blocker on the M2-C diff).
    """
    import platformdirs

    fake_data_dir = tmp_path / "platformdirs-data"

    def _fake_user_data_dir(*_args: object, **_kwargs: object) -> str:
        return str(fake_data_dir)

    monkeypatch.setattr(platformdirs, "user_data_dir", _fake_user_data_dir)


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
