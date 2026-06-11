"""New-recording-session dialog (M2-C, rule 14).

Collects the two inputs ``StreamingEngine.start_session`` needs: a
project name and the subset of configured devices that should record.
The dialog is deliberately dumb — it validates only what it can know
locally (non-blank name, ≥1 device checked) and previews the sanitized
archive directory live; the disk-level injectivity guard and the actual
session start stay in the engine (the toolbar surfaces those errors).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from echosmonitor.core.session import sanitize_project_name

if TYPE_CHECKING:
    from collections.abc import Sequence


class NewSessionDialog(QDialog):
    """Modal "name + which devices record" prompt for a new session.

    ``device_names`` is the configured-device list (all pre-checked —
    the common case is "record everything"). After ``exec()`` returns
    ``Accepted``, read :meth:`project_name` and :meth:`checked_devices`.
    """

    def __init__(
        self,
        device_names: Sequence[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New recording session")
        self.setObjectName("NewSessionDialog")

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Project name:", self))
        self._name_edit = QLineEdit(self)
        self._name_edit.setObjectName("ProjectNameEdit")
        self._name_edit.setPlaceholderText("e.g. Survey 2026")
        layout.addWidget(self._name_edit)

        # Live preview of the sanitized directory segment so surprises
        # ("My Survey!" → "My_Survey") show before recording starts.
        self._preview = QLabel("", self)
        self._preview.setObjectName("SanitizedPreview")
        self._preview.setStyleSheet("color: palette(mid); font-style: italic;")
        layout.addWidget(self._preview)

        layout.addWidget(QLabel("Devices to record:", self))
        self._device_list = QListWidget(self)
        self._device_list.setObjectName("SessionDeviceList")
        for name in device_names:
            item = QListWidgetItem(name, self._device_list)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
        layout.addWidget(self._device_list)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._name_edit.textChanged.connect(self._on_name_changed)
        self._device_list.itemChanged.connect(lambda _item: self._update_ok_enabled())
        self._on_name_changed(self._name_edit.text())

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    def project_name(self) -> str:
        """The raw project name as typed (sanitisation is the engine's)."""
        return self._name_edit.text().strip()

    def checked_devices(self) -> tuple[str, ...]:
        """Names of the devices the user left checked, in list order."""
        out: list[str] = []
        for i in range(self._device_list.count()):
            item = self._device_list.item(i)
            if item is not None and item.checkState() is Qt.CheckState.Checked:
                out.append(item.text())
        return tuple(out)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _on_name_changed(self, text: str) -> None:
        raw = text.strip()
        if raw:
            self._preview.setText(f"archives to: {sanitize_project_name(raw)}/")
        else:
            self._preview.setText("")
        self._update_ok_enabled()

    def _update_ok_enabled(self) -> None:
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(bool(self.project_name()) and bool(self.checked_devices()))
