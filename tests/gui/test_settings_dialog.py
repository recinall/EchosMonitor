"""SettingsDialog (M6) — round-trip, cancel, validation, theme module."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.gui.dialogs.settings_dialog import SettingsDialog


def _make(
    qtbot: Any, tmp_path: Path, root: RootConfig | None = None
) -> tuple[SettingsDialog, ConfigStore]:
    from PySide6.QtWidgets import QWidget

    store = ConfigStore(
        root or RootConfig(app=AppConfig(), ui=UiConfig(), devices=[]),
        tmp_path / "config.yaml",
    )
    parent = QWidget()
    qtbot.addWidget(parent)
    dialog = SettingsDialog(parent, store)
    dialog._test_parent_keepalive = parent  # type: ignore[attr-defined]
    qtbot.addWidget(dialog)
    return dialog, store


def test_round_trip_writes_app_and_ui(qtbot, tmp_path: Path) -> None:
    dialog, store = _make(qtbot, tmp_path)
    dialog._archive_edit.setText(str(tmp_path / "field-archive"))
    dialog._theme_combo.setCurrentIndex(dialog._theme_combo.findData("light"))
    dialog._rate_spin.setValue(500)
    dialog._plots_spin.setValue(12)
    dialog.accept()
    assert store.root.app.archive_root == tmp_path / "field-archive"
    assert store.root.ui.theme == "light"
    assert store.root.ui.max_display_rate_hz == 500
    assert store.root.ui.max_visible_plots == 12
    assert (tmp_path / "config.yaml").exists()  # atomic write happened


def test_empty_archive_root_means_platformdirs_default(qtbot, tmp_path: Path) -> None:
    """Empty field → archive_root None (the resolver's default); the
    placeholder names that exact default path so the UI can't lie."""
    root = RootConfig(
        app=AppConfig(archive_root=tmp_path / "old"), ui=UiConfig(), devices=[]
    )
    dialog, store = _make(qtbot, tmp_path, root)
    assert dialog._archive_edit.text() == str(tmp_path / "old")
    # The suite redirects platformdirs; what's pinned is that the
    # placeholder IS the resolver's default (…/archive), not a guess.
    assert Path(dialog._archive_edit.placeholderText()).name == "archive"
    dialog._archive_edit.clear()
    dialog.accept()
    assert store.root.app.archive_root is None


def test_cancel_changes_nothing(qtbot, tmp_path: Path) -> None:
    dialog, store = _make(qtbot, tmp_path)
    before = store.root.model_dump(mode="json")
    dialog._rate_spin.setValue(999)
    dialog.reject()
    assert store.root.model_dump(mode="json") == before
    assert not (tmp_path / "config.yaml").exists()  # nothing written


def test_fields_prefill_from_current_config(qtbot, tmp_path: Path) -> None:
    root = RootConfig(
        app=AppConfig(),
        ui=UiConfig(theme="light", refresh_hz=30, max_display_rate_hz=1000),
        devices=[],
    )
    dialog, _store = _make(qtbot, tmp_path, root)
    assert dialog._theme_combo.currentData() == "light"
    assert dialog._refresh_spin.value() == 30
    assert dialog._rate_spin.value() == 1000


def test_apply_theme_sets_pyqtgraph_options() -> None:
    """gui/theme.py: ui.theme drives pyqtgraph's global plot colors."""
    import pyqtgraph as pg

    from echosmonitor.gui.theme import apply_theme

    try:
        apply_theme("light")
        assert pg.getConfigOption("background") == "w"
        assert pg.getConfigOption("foreground") == "k"
        apply_theme("dark")
        assert pg.getConfigOption("background") == "k"
        assert pg.getConfigOption("foreground") == "d"
    finally:
        apply_theme("dark")  # restore the suite-wide default
