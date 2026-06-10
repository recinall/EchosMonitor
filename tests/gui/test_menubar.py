"""Tests for MainWindow's menubar, dock toggles, and recovery affordances."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QDockWidget, QMenu, QMessageBox
from pytestqt.qtbot import QtBot

from echosmonitor.config.loader import load_config
from echosmonitor.gui.main_window import MainWindow

_EXPECTED_DOCK_TITLES = (
    "Devices",
    "Stations",
    "Spectrogram",
    "Log",
)
_EXPECTED_DOCK_ORDER = (
    "Devices",
    "Stations",
    "Spectrogram",
    "Log",
)


@pytest.fixture
def isolated_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Redirect MainWindow's QSettings to an isolated INI file.

    The production code reads/writes ``QSettings(_ORG_NAME, _APP_NAME)``
    (NativeFormat). Patching ``MainWindow._settings`` to return a
    file-backed QSettings keeps the test from polluting the user's
    real configuration and makes the persisted state reproducible.
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


def _menu_by_title(window: MainWindow, title: str) -> QMenu:
    """Return the QMenu under the menubar with the given title.

    Iterating ``QMenuBar.findChildren(QMenu)`` keeps a strong PySide6
    Python wrapper alive for the returned menu. Going through
    ``QAction.menu()`` works in PyQt but the PySide6 wrapper for the
    return value is lazily constructed and disposed as soon as the
    enclosing for-loop ends, leading to a deleted-C++-object error
    on subsequent access.
    """
    for menu in window.menuBar().findChildren(QMenu):
        if menu.title() == title:
            return menu
    raise AssertionError(f"menu {title!r} not found")


def _action_by_text(menu: QMenu, text_substr: str) -> QAction:
    for action in menu.actions():
        if text_substr in action.text():
            return action
    raise AssertionError(f"action containing {text_substr!r} not found in menu {menu.title()!r}")


def _docks_by_title(window: MainWindow) -> dict[str, QDockWidget]:
    return {dock.windowTitle(): dock for dock in window.findChildren(QDockWidget)}


def test_menubar_has_three_top_level_menus(qtbot: QtBot, isolated_settings: Path) -> None:
    window = _build_window(qtbot)
    # Top-level menus are those mounted directly on the menubar via its
    # actions; ``findChildren(QMenu)`` would also pick up the View ▸ Detach
    # submenu introduced in M7, so iterate the menubar's own actions.
    titles = [a.menu().title() for a in window.menuBar().actions() if a.menu() is not None]
    assert titles == ["&File", "&View", "&Help"]
    window.close()


def test_view_menu_has_toggle_per_dock_in_workflow_order(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    window = _build_window(qtbot)
    view = _menu_by_title(window, "&View")

    # The dock-toggle actions are the checkable actions whose text is a
    # dock name. Other checkable View actions (e.g. the M8 "Show
    # detection markers" toggle) are allowed and asserted separately.
    toggle_actions = [
        a for a in view.actions() if a.isCheckable() and a.text() in _EXPECTED_DOCK_ORDER
    ]
    assert len(toggle_actions) == len(_EXPECTED_DOCK_ORDER)
    assert [a.text() for a in toggle_actions] == list(_EXPECTED_DOCK_ORDER)

    # Stable shortcuts Alt+1..4 in order.
    for index, action in enumerate(toggle_actions, start=1):
        assert action.shortcut().toString() == f"Alt+{index}"

    # M8: the global detection-markers toggle is present, checkable, on
    # by default, and carries no Alt+N shortcut (cannot shadow a dock).
    markers = [a for a in view.actions() if a.text() == "Show &detection markers"]
    assert len(markers) == 1
    assert markers[0].isCheckable() and markers[0].isChecked()
    assert markers[0].shortcut().toString() == ""

    window.close()


def test_view_detach_submenu_lists_all_docks_in_order(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    window = _build_window(qtbot)
    view = _menu_by_title(window, "&View")

    # The Detach submenu hangs off a QAction in the View menu.
    detach_menu = _menu_by_title(window, "&Detach")
    titles = [a.text() for a in detach_menu.actions()]
    assert titles == list(_EXPECTED_DOCK_ORDER)

    # Each entry carries the parallel Ctrl+Shift+N shortcut.
    for index, action in enumerate(detach_menu.actions(), start=1):
        assert action.shortcut().toString() == f"Ctrl+Shift+{index}"

    # The submenu is reachable from the View menu (compare by title to
    # avoid PySide6's lazily-constructed action.menu() wrapper identity).
    assert any(a.menu() is not None and a.menu().title() == "&Detach" for a in view.actions())

    window.close()


def test_view_detach_action_floats_dock(qtbot: QtBot, isolated_settings: Path) -> None:
    window = _build_window(qtbot)
    detach_menu = _menu_by_title(window, "&Detach")
    spectrogram_action = _action_by_text(detach_menu, "Spectrogram")

    spectrogram = _docks_by_title(window)["Spectrogram"]
    assert not spectrogram.isFloating()
    spectrogram_action.trigger()
    assert spectrogram.isFloating()
    spectrogram_action.trigger()
    assert not spectrogram.isFloating()

    window.close()


def test_view_toggle_hides_and_shows_dock(qtbot: QtBot, isolated_settings: Path) -> None:
    window = _build_window(qtbot)
    window.show()
    qtbot.waitExposed(window)
    docks = _docks_by_title(window)
    view = _menu_by_title(window, "&View")
    spectrogram_toggle = _action_by_text(view, "Spectrogram")

    assert spectrogram_toggle.isChecked()
    assert docks["Spectrogram"].isVisible()
    spectrogram_toggle.trigger()
    assert not spectrogram_toggle.isChecked()
    assert not docks["Spectrogram"].isVisible()
    spectrogram_toggle.trigger()
    assert spectrogram_toggle.isChecked()
    assert docks["Spectrogram"].isVisible()

    window.close()


def test_reset_window_layout_restores_all_docks(
    qtbot: QtBot,
    isolated_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _build_window(qtbot)
    docks = _docks_by_title(window)
    for dock in docks.values():
        dock.hide()
    assert all(dock.isHidden() for dock in docks.values())

    # Auto-confirm the reset prompt.
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes),
    )

    view = _menu_by_title(window, "&View")
    reset = _action_by_text(view, "Reset window layout")
    reset.trigger()

    for title, dock in docks.items():
        assert not dock.isHidden(), f"{title} should be visible after reset"
    assert window.size().width() == 1600
    assert window.size().height() == 1000

    window.close()


def test_reset_window_layout_cancel_does_nothing(
    qtbot: QtBot,
    isolated_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _build_window(qtbot)
    docks = _docks_by_title(window)
    docks["Spectrogram"].hide()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.No),
    )

    view = _menu_by_title(window, "&View")
    _action_by_text(view, "Reset window layout").trigger()

    # Cancel preserves the prior state.
    assert docks["Spectrogram"].isHidden()

    window.close()


def test_all_docks_hidden_on_restore_falls_back_to_devices(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    cfg, _ = load_config(None)

    # First window: hide every dock and let closeEvent persist that state.
    w1 = MainWindow(cfg, Path("/tmp/cfg.yaml"))
    qtbot.addWidget(w1)
    for dock in w1.findChildren(QDockWidget):
        dock.hide()
    w1.close()

    # Second window: restoreState should put every dock back to hidden,
    # and the fallback should re-show Devices and emit the INFO line.
    with structlog.testing.capture_logs() as logs:
        w2 = MainWindow(cfg, Path("/tmp/cfg.yaml"))
        qtbot.addWidget(w2)

    devices_dock = _docks_by_title(w2)["Devices"]
    assert not devices_dock.isHidden(), "Devices dock should be re-shown by the fallback"
    assert any(entry.get("event") == "all_docks_hidden_fallback" for entry in logs), (
        "expected fallback log line"
    )

    w2.close()


def test_create_popup_menu_lists_all_docks(qtbot: QtBot, isolated_settings: Path) -> None:
    window = _build_window(qtbot)
    popup = window.createPopupMenu()
    assert popup is not None
    titles = {a.text() for a in popup.actions() if a.text()}
    for title in _EXPECTED_DOCK_TITLES:
        assert title in titles, f"popup menu missing dock {title!r}"
    window.close()


def test_help_about_action_runs(
    qtbot: QtBot,
    isolated_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox,
        "about",
        staticmethod(lambda _parent, title, body: calls.append((title, body))),
    )

    window = _build_window(qtbot)
    help_menu = _menu_by_title(window, "&Help")
    _action_by_text(help_menu, "About").trigger()

    assert len(calls) == 1
    title, body = calls[0]
    assert "EchosMonitor" in title
    assert "EchosMonitor" in body
    window.close()


def test_help_first_run_wizard_action_runs(
    qtbot: QtBot,
    isolated_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from echosmonitor.gui.dialogs import first_run_wizard as wiz_mod

    exec_calls: list[Any] = []
    # Replace exec with a no-op so the dialog never actually shows.
    monkeypatch.setattr(
        wiz_mod.FirstRunWizard,
        "exec",
        lambda self: exec_calls.append(self) or 0,
    )

    window = _build_window(qtbot)
    help_menu = _menu_by_title(window, "&Help")
    _action_by_text(help_menu, "First-run wizard").trigger()

    assert len(exec_calls) == 1
    window.close()


def test_help_keyboard_shortcuts_action_runs(
    qtbot: QtBot,
    isolated_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from echosmonitor.gui.dialogs import shortcuts_dialog as sc_mod

    exec_calls: list[Any] = []
    # Replace exec with a no-op so the dialog never actually shows.
    monkeypatch.setattr(
        sc_mod.ShortcutsDialog,
        "exec",
        lambda self: exec_calls.append(self) or 0,
    )

    window = _build_window(qtbot)
    help_menu = _menu_by_title(window, "&Help")
    _action_by_text(help_menu, "Keyboard shortcuts").trigger()

    assert len(exec_calls) == 1
    window.close()


def test_help_manual_tests_action_opens_url(
    qtbot: QtBot,
    isolated_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[QUrl] = []
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        staticmethod(lambda url: opened.append(url) or True),
    )

    window = _build_window(qtbot)
    help_menu = _menu_by_title(window, "&Help")
    _action_by_text(help_menu, "Manual tests").trigger()

    # In dev mode docs/MANUAL_TESTS.md is reachable from the package
    # location, so openUrl should be invoked. The exact path depends on
    # the checkout but must end with MANUAL_TESTS.md.
    assert len(opened) == 1
    assert opened[0].toLocalFile().endswith("MANUAL_TESTS.md")
    window.close()


def test_file_menu_new_device_delegates_to_panel_action(
    qtbot: QtBot,
    isolated_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The File menu's New device action triggers the same QAction the
    DevicePanel toolbar uses — so handler logic stays in one place.

    The DevicePanel slot opens a modal DeviceDialog; monkeypatching
    ``DeviceDialog.add`` to a no-op keeps the test from blocking on a
    real dialog while still exercising the trigger-delegation chain.
    """
    from echosmonitor.gui.dialogs.device_dialog import DeviceDialog

    monkeypatch.setattr(
        DeviceDialog,
        "add",
        staticmethod(lambda *_args, **_kwargs: None),
    )

    window = _build_window(qtbot)
    triggered: list[bool] = []

    panel = window._device_panel
    assert panel is not None
    panel.add_action.triggered.connect(lambda *_: triggered.append(True))

    file_menu = _menu_by_title(window, "&File")
    _action_by_text(file_menu, "New device").trigger()

    assert triggered == [True]
    window.close()
