"""Tests for MainWindow full-screen focus mode (M7 Stage A1)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QRect, QSettings
from PySide6.QtWidgets import QDockWidget, QLabel, QMessageBox
from pytestqt.qtbot import QtBot

from echosmonitor.config.loader import load_config
from echosmonitor.gui.main_window import MainWindow


@pytest.fixture
def isolated_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Redirect MainWindow's QSettings to an isolated INI file.

    Mirrors the fixture in ``test_menubar.py`` so QSettings never
    pollutes the real user configuration.
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


def _docks_by_title(window: MainWindow) -> dict[str, QDockWidget]:
    return {dock.windowTitle(): dock for dock in window.findChildren(QDockWidget)}


def _focus_banner(window: MainWindow) -> QLabel | None:
    return window.findChild(QLabel, "FocusModeBanner")


def test_f11_focus_hides_others_and_central_esc_restores(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    window = _build_window(qtbot)
    docks = _docks_by_title(window)
    spectrogram = docks["Spectrogram"]

    before = bytes(window.saveState())

    # Enter focus on Spectrogram: every other dock + the central widget hide.
    window._toggle_focus(spectrogram)
    assert window._focus_active
    assert not spectrogram.isHidden()
    for title, dock in docks.items():
        if title != "Spectrogram":
            assert dock.isHidden(), f"{title} should be hidden in focus mode"
    central = window.centralWidget()
    assert central is not None
    assert central.isHidden()
    assert _focus_banner(window) is not None

    # ESC exits and restores the exact pre-focus layout.
    window._on_escape_shortcut()
    assert not window._focus_active
    assert central is not None
    assert not central.isHidden()

    after = bytes(window.saveState())
    assert after == before, "focus enter/exit must round-trip saveState exactly"

    window.close()


def test_f11_focus_central_hides_all_docks_and_keeps_central(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    """F11 on the central tabs maximises them: all docks hide, the central
    widget stays shown, and ESC restores everything (round-trip)."""
    window = _build_window(qtbot)
    docks = _docks_by_title(window)

    before = bytes(window.saveState())

    window._toggle_focus(window._central_tabs)
    assert window._focus_active
    central = window.centralWidget()
    assert central is not None
    assert not central.isHidden(), "central tabs must stay shown in central focus"
    for title, dock in docks.items():
        assert dock.isHidden(), f"{title} should be hidden in central focus"
    assert _focus_banner(window) is not None

    window._on_escape_shortcut()
    assert not window._focus_active
    for title, dock in docks.items():
        assert not dock.isHidden(), f"{title} should be shown after exit"

    after = bytes(window.saveState())
    assert after == before, "central focus enter/exit must round-trip saveState exactly"

    window.close()


def test_switching_focus_target_does_not_nest(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    window = _build_window(qtbot)
    docks = _docks_by_title(window)
    stations = docks["Stations"]
    spectrogram = docks["Spectrogram"]

    before = bytes(window.saveState())

    # Focus Stations, then switch to Spectrogram (re-point, not nest).
    window._toggle_focus(stations)
    window._toggle_focus(spectrogram)
    assert window._focus_active
    assert window._focus_target is spectrogram
    assert not spectrogram.isHidden()
    assert stations.isHidden()

    # Exiting ONCE returns to the original pre-focus layout.
    window._on_escape_shortcut()
    assert not window._focus_active

    after = bytes(window.saveState())
    assert after == before, "switching target must not nest saved states"

    window.close()


def test_reset_layout_while_focused_exits_and_resets(
    qtbot: QtBot,
    isolated_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = _build_window(qtbot)
    docks = _docks_by_title(window)

    window._toggle_focus(docks["Log"])
    assert window._focus_active
    assert _focus_banner(window) is not None
    assert _focus_banner(window).isVisibleTo(window) or not _focus_banner(window).isHidden()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes),
    )

    window._on_reset_window_layout()

    assert not window._focus_active, "reset must exit focus first"
    for title, dock in docks.items():
        assert not dock.isHidden(), f"{title} should be visible after reset"
    banner = _focus_banner(window)
    assert banner is not None
    assert banner.isHidden(), "focus banner must be gone after reset"

    window.close()


def test_focus_floating_dock_round_trips(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    window = _build_window(qtbot)
    window.show()
    qtbot.waitExposed(window)
    docks = _docks_by_title(window)
    spectrogram = docks["Spectrogram"]

    # Detach Spectrogram first so the focus target starts floating, at a
    # known geometry so we can assert it survives the focus round-trip.
    spectrogram.setFloating(True)
    qtbot.waitUntil(lambda: spectrogram.isFloating(), timeout=1000)
    spectrogram.setGeometry(QRect(160, 140, 480, 320))
    geom_before = spectrogram.geometry()

    window._toggle_focus(spectrogram)
    # While focused the dock is brought back into the main window so it
    # can fill the whole window.
    assert not spectrogram.isFloating()
    assert window._focus_active

    window._on_escape_shortcut()
    # Floating state + geometry restored on exit. (We assert the
    # observable invariants rather than full saveState byte-equality:
    # the offscreen platform plugin does not faithfully replay a floating
    # window's normal-geometry flag byte through a dock/undock cycle, so a
    # whole-blob comparison is platform-fragile here. The non-floating
    # round-trip in ``test_f11_focus_...`` covers byte-equality.)
    assert spectrogram.isFloating()
    assert spectrogram.geometry().size() == geom_before.size()

    window.close()


def test_focus_on_already_hidden_dock_restores_hidden_on_exit(
    qtbot: QtBot,
    isolated_settings: Path,
) -> None:
    """Focusing a dock the user had hidden must leave it hidden on exit.

    ``saveState()`` captures the pre-focus visibility, so ``restoreState``
    on exit must put the (briefly-shown-for-focus) dock back to hidden.
    Guards the visibility half of the round-trip the byte-equality test
    in ``test_f11_focus_...`` already covers structurally.
    """
    window = _build_window(qtbot)
    docks = _docks_by_title(window)
    log = docks["Log"]
    # Hide Log before focusing it.
    log.hide()
    assert log.isHidden()

    window._toggle_focus(log)
    assert window._focus_active
    assert not log.isHidden()  # shown to fill the window while focused

    window._on_escape_shortcut()
    assert not window._focus_active
    assert log.isHidden(), "a pre-focus-hidden dock must return to hidden on exit"

    window.close()
