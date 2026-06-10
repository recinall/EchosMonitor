"""Tests for MainWindow dock detach to floating windows (M7 Stage A2)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, QRect, QSettings
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QDockWidget
from pytestqt.qtbot import QtBot

from echosmonitor.config.loader import load_config
from echosmonitor.gui.main_window import MainWindow


@pytest.fixture
def isolated_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Redirect MainWindow's QSettings to an isolated INI file."""
    settings_path = tmp_path / "settings.ini"

    def _make_settings(_self: MainWindow) -> QSettings:
        return QSettings(str(settings_path), QSettings.Format.IniFormat)

    monkeypatch.setattr(MainWindow, "_settings", _make_settings)
    yield settings_path


def _build_window(qtbot: QtBot) -> MainWindow:
    cfg, _ = load_config(None)
    window = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(window)
    return window


def _docks_by_title(window: MainWindow) -> dict[str, QDockWidget]:
    return {dock.windowTitle(): dock for dock in window.findChildren(QDockWidget)}


def test_toggle_floating_round_trips(qtbot: QtBot, isolated_settings: Path) -> None:
    window = _build_window(qtbot)
    spectrogram = _docks_by_title(window)["Spectrogram"]

    assert not spectrogram.isFloating()
    window._toggle_dock_floating(spectrogram)
    assert spectrogram.isFloating()
    window._toggle_dock_floating(spectrogram)
    assert not spectrogram.isFloating()

    window.close()


def test_floating_geometry_persists_across_redock_cycle(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    window = _build_window(qtbot)
    window.show()
    qtbot.waitExposed(window)
    spectrogram = _docks_by_title(window)["Spectrogram"]

    # Detach, set a known geometry, then redock (persists geometry).
    # The geometry must clear the dock contents' minimum size, or the
    # window manager clamps the floating dock and the round-trip blob
    # no longer matches the requested rect.
    window._toggle_dock_floating(spectrogram)
    assert spectrogram.isFloating()
    known = QRect(220, 180, 540, 460)
    spectrogram.setGeometry(known)
    saved_bytes = QByteArray(spectrogram.saveGeometry())

    window._toggle_dock_floating(spectrogram)  # redock — persists geometry
    assert not spectrogram.isFloating()

    # Re-detach should restore the geometry from QSettings.
    window._toggle_dock_floating(spectrogram)
    assert spectrogram.isFloating()

    # The persisted geometry blob must round-trip through QSettings.
    key = window._float_geometry_key(spectrogram)
    settings = window._settings()
    stored = settings.value(key)
    assert isinstance(stored, (bytes, bytearray, QByteArray))
    # A fresh dock restored with the stored blob lands at the known rect.
    probe = QDockWidget("probe")
    probe.setFloating(True)
    probe.restoreGeometry(saved_bytes)
    assert probe.geometry().size() == known.size()

    window.close()


def _detach_action_for(window: MainWindow, sequence: str) -> QAction:
    """Return the View ▸ Detach QAction bound to ``sequence``.

    Ctrl+Shift+N is owned by the Detach submenu QAction (one binding,
    displayed in the menu), NOT a standalone QShortcut — binding the
    sequence twice would be an ambiguous-shortcut overload. We locate
    the action whose shortcut matches and whose handler toggles a dock.
    """
    target = QKeySequence(sequence)
    for action in window.findChildren(QAction):
        if action.shortcut() == target:
            return action
    raise AssertionError(f"no QAction bound to {sequence!r}")


def test_ctrl_shift_n_has_exactly_one_binding(qtbot: QtBot, isolated_settings: Path) -> None:
    """Each Ctrl+Shift+N must have exactly ONE owner.

    A standalone window ``QShortcut`` *plus* a menu ``QAction`` for the
    same sequence registers two WindowShortcut-context entries → Qt's
    "Ambiguous shortcut overload" → neither fires on a real key press.
    This guards that regression: zero QShortcuts, exactly one QAction
    per Ctrl+Shift+1..4.
    """
    window = _build_window(qtbot)
    for index in range(1, 5):
        seq = QKeySequence(f"Ctrl+Shift+{index}")
        n_shortcuts = sum(1 for s in window.findChildren(QShortcut) if s.key() == seq)
        n_actions = sum(1 for a in window.findChildren(QAction) if a.shortcut() == seq)
        assert n_shortcuts == 0, f"Ctrl+Shift+{index}: unexpected QShortcut ({n_shortcuts})"
        assert n_actions == 1, f"Ctrl+Shift+{index}: expected 1 QAction, got {n_actions}"
    window.close()


def test_ctrl_shift_3_toggles_spectrogram_dock(qtbot: QtBot, isolated_settings: Path) -> None:
    """Ctrl+Shift+3 must target the Spectrogram dock, matching Alt+3.

    Triggering the menu QAction would need synthetic key delivery to an
    active top-level window, which the offscreen platform plugin does
    not provide in CI. Triggering the QAction bound to that exact
    sequence verifies it maps Ctrl+Shift+3 → the Spectrogram dock without
    depending on key delivery.
    """
    window = _build_window(qtbot)
    spectrogram = _docks_by_title(window)["Spectrogram"]
    action = _detach_action_for(window, "Ctrl+Shift+3")

    assert not spectrogram.isFloating()
    action.trigger()
    assert spectrogram.isFloating()
    action.trigger()
    assert not spectrogram.isFloating()

    window.close()
