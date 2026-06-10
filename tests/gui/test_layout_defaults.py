"""Tests for the M7 Stage C default sizing + the shortcuts dialog."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QTableWidget
from pytestqt.qtbot import QtBot

from echosmonitor.config.loader import load_config
from echosmonitor.gui.dialogs.shortcuts_dialog import (
    SHORTCUT_GROUPS,
    ShortcutsDialog,
    all_shortcut_entries,
)
from echosmonitor.gui.main_window import (
    _DEFAULT_HEIGHT,
    _DEFAULT_WIDTH,
    _LIVE_MIN_HEIGHT_PX,
    _PSD_MIN_WIDTH_PX,
    _SIDE_DOCK_MIN_WIDTH_PX,
    _SPECTROGRAM_MIN_HEIGHT_PX,
    MainWindow,
)


@pytest.fixture
def isolated_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Redirect MainWindow's QSettings to an empty isolated INI file.

    Mirrors ``test_menubar.isolated_settings`` — points the production
    ``QSettings(_ORG_NAME, _APP_NAME)`` reads/writes at a fresh tmp file
    so nothing is restored on launch and the from-scratch defaults apply.
    """
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


def test_fresh_launch_uses_new_default_geometry(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    window = _build_window(qtbot)
    assert _DEFAULT_WIDTH == 1600
    assert _DEFAULT_HEIGHT == 1000
    assert window.size().width() == _DEFAULT_WIDTH
    assert window.size().height() == _DEFAULT_HEIGHT
    window.close()


def test_dock_contents_have_minimum_sizes(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    window = _build_window(qtbot)

    assert window._device_panel is not None
    assert window._device_panel.minimumWidth() == _SIDE_DOCK_MIN_WIDTH_PX
    assert window._station_browser is not None
    assert window._station_browser.minimumWidth() == _SIDE_DOCK_MIN_WIDTH_PX
    assert window._live_tabs is not None
    assert window._live_tabs.minimumHeight() == _LIVE_MIN_HEIGHT_PX
    assert window._spectrogram_widget.minimumHeight() == _SPECTROGRAM_MIN_HEIGHT_PX
    assert window._psd_widget.minimumWidth() == _PSD_MIN_WIDTH_PX

    window.close()


def test_minimums_do_not_break_all_hidden_fallback(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    """The dock minimums must not interfere with focus mode or fallback."""
    window = _build_window(qtbot)
    # Focus mode should still let the focused dock fill the window: the
    # focused dock stays un-hidden while all others are hidden. (Use
    # isHidden rather than isVisible — the top-level window is not shown
    # in this headless test, so isVisible would be False for every dock.)
    assert window._device_panel is not None
    window._toggle_focus(window._device_panel)
    assert window._focus_active
    assert not window._device_panel.isHidden()
    assert window._stations_dock.isHidden()
    window._exit_focus()
    assert not window._focus_active
    assert not window._device_panel.isHidden()
    window.close()


def test_shortcuts_dialog_lists_expected_keys(
    qtbot: QtBot,
) -> None:
    """The data-driven catalogue lists every expected shortcut."""
    keys = {entry.keys for entry in all_shortcut_entries()}
    flat = " ".join(keys)
    for expected in ("F11", "Esc", "Ctrl+N", "Ctrl+E", "Del", "Ctrl+Q"):
        assert expected in keys, f"{expected} missing from shortcut catalogue"
    # Range entries fold Alt+1 / Ctrl+Shift+1 into the documented range row.
    assert "Alt+1" in flat
    assert "Ctrl+Shift+1" in flat
    # Sanity: groups are non-empty.
    assert SHORTCUT_GROUPS
    assert all(group.entries for group in SHORTCUT_GROUPS)


def test_shortcuts_dialog_renders_rows(
    qtbot: QtBot,
) -> None:
    """The rendered table contains the catalogue's shortcut + group text."""
    dialog = ShortcutsDialog()
    qtbot.addWidget(dialog)

    table = dialog.findChild(QTableWidget)
    assert table is not None

    rendered: set[str] = set()
    for row in range(table.rowCount()):
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item is not None:
                rendered.add(item.text())

    # Group headers present.
    for group in SHORTCUT_GROUPS:
        assert group.title in rendered, f"group header {group.title!r} not rendered"
    # Every catalogue key string rendered verbatim.
    for entry in all_shortcut_entries():
        assert entry.keys in rendered, f"shortcut {entry.keys!r} not rendered"

    dialog.close()
