"""Application settings dialog (M6) — archive root, theme, display caps.

Edits the config's ``app``/``ui`` sections through
:meth:`ConfigStore.update_settings` (rule 3: validate → rotate backups →
atomic write → ``configChanged``). Devices are untouched, so the engine
diff is a no-op; every field here is read at construction/launch time,
which is why the dialog says so instead of pretending to hot-apply.

Pure GUI-thread work: a handful of input widgets and one ConfigStore
call — no workers, no I/O beyond the store's own atomic write (the
store IS the sanctioned config writer).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.exceptions import ConfigError
from echosmonitor.core.session import resolve_base_archive_root

if TYPE_CHECKING:
    from echosmonitor.core.config_store import ConfigStore

_log = structlog.get_logger(__name__)


class SettingsDialog(QDialog):
    """Modal editor for ``app.archive_root`` + the ``ui`` section."""

    def __init__(self, parent: QWidget, store: ConfigStore) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("Settings")
        self.setModal(True)

        root = store.root
        layout = QVBoxLayout(self)
        form = QFormLayout()

        # --- archive root --------------------------------------------
        # Empty field = the platformdirs default (shown as placeholder);
        # the resolver (core/session.resolve_base_archive_root) is the
        # single shared definition, so the placeholder can never lie.
        default_root = resolve_base_archive_root(
            root.model_copy(update={"app": root.app.model_copy(update={"archive_root": None})})
        )
        self._archive_edit = QLineEdit(self)
        self._archive_edit.setPlaceholderText(str(default_root))
        if root.app.archive_root is not None:
            self._archive_edit.setText(str(root.app.archive_root))
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self._on_browse)
        archive_row = QHBoxLayout()
        archive_row.addWidget(self._archive_edit, 1)
        archive_row.addWidget(browse)
        form.addRow("Archive root:", archive_row)
        archive_hint = QLabel(
            "Recording sessions are created under this directory "
            "(one folder per project). Leave empty for the default.",
            self,
        )
        archive_hint.setWordWrap(True)
        archive_hint.setStyleSheet("color: palette(mid);")
        form.addRow("", archive_hint)

        # --- theme ----------------------------------------------------
        self._theme_combo = QComboBox(self)
        self._theme_combo.addItem("Dark plots", "dark")
        self._theme_combo.addItem("Light plots", "light")
        index = self._theme_combo.findData(root.ui.theme)
        self._theme_combo.setCurrentIndex(max(0, index))
        form.addRow("Plot theme:", self._theme_combo)

        # --- display caps ----------------------------------------------
        self._refresh_spin = self._spin(1, 120, root.ui.refresh_hz, " Hz")
        form.addRow("Display refresh:", self._refresh_spin)
        self._window_spin = self._spin(1, 3600, root.ui.default_window_seconds, " s")
        form.addRow("Default trace window:", self._window_spin)
        self._plots_spin = self._spin(1, 64, root.ui.max_visible_plots, "")
        form.addRow("Max visible plots:", self._plots_spin)
        self._rate_spin = self._spin(1, 20000, root.ui.max_display_rate_hz, " Hz")
        form.addRow("Max display rate:", self._rate_spin)
        rate_hint = QLabel(
            "Streams faster than this are peak-decimated FOR DISPLAY only "
            "— recording, DSP and detection always keep the full rate.",
            self,
        )
        rate_hint.setWordWrap(True)
        rate_hint.setStyleSheet("color: palette(mid);")
        form.addRow("", rate_hint)
        # ui.recent_detections_limit is deliberately NOT exposed: nothing
        # consumes it since the rule-13 autostart removal (the cross-
        # session detection prefill is an open M3 item) — a dead setting
        # in a dialog promising "takes effect at next launch" would lie.

        layout.addLayout(form)

        note = QLabel(
            "Settings are saved to the config file and take effect at the "
            "next launch. The archive root applies to new recording "
            "sessions.",
            self,
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def _spin(self, lo: int, hi: int, value: int, suffix: str) -> QSpinBox:
        spin = QSpinBox(self)
        spin.setRange(lo, hi)
        spin.setValue(value)
        if suffix:
            spin.setSuffix(suffix)
        return spin

    @Slot()
    def _on_browse(self) -> None:
        start = self._archive_edit.text().strip() or self._archive_edit.placeholderText()
        chosen = QFileDialog.getExistingDirectory(self, "Choose archive root", start)
        if chosen:
            self._archive_edit.setText(chosen)

    def accept(self) -> None:
        root = self._store.root
        archive_text = self._archive_edit.text().strip()
        new_app = root.app.model_copy(
            update={"archive_root": Path(archive_text) if archive_text else None}
        )
        new_ui = root.ui.model_copy(
            update={
                "theme": self._theme_combo.currentData(),
                "refresh_hz": int(self._refresh_spin.value()),
                "default_window_seconds": int(self._window_spin.value()),
                "max_visible_plots": int(self._plots_spin.value()),
                "max_display_rate_hz": int(self._rate_spin.value()),
            }
        )
        try:
            self._store.update_settings(new_app, new_ui)
        except ConfigError as exc:
            _log.warning("settings_dialog_commit_failed", error=str(exc))
            QMessageBox.warning(
                self,
                "Could not save settings",
                f"The settings could not be saved:\n\n{exc}",
            )
            return  # keep the dialog open for correction
        super().accept()


__all__ = ["SettingsDialog"]
