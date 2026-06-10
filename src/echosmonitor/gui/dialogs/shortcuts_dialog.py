"""Keyboard-shortcuts reference dialog (M7 Stage C3).

A read-only modal listing every window shortcut in readable groups. The
shortcut catalogue is a module-level data structure
(:data:`SHORTCUT_GROUPS`) so the dialog renders from the *same* source
the C4 test asserts against — no duplicated list between UI and test.

The four dock names + their Alt+N / Ctrl+Shift+N indices mirror the
canonical dock order documented on
:class:`~echosmonitor.gui.main_window.MainWindow`. Reordering that
contract should update :data:`_DOCK_NAMES_IN_ORDER` here too.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Canonical dock order — mirrors MainWindow's Alt+1..4 contract.
_DOCK_NAMES_IN_ORDER: tuple[str, ...] = (
    "Devices",
    "Stations",
    "Spectrogram",
    "Log",
)


@dataclass(frozen=True)
class ShortcutEntry:
    """One ``(keys, description)`` row in a shortcut group."""

    keys: str
    description: str


@dataclass(frozen=True)
class ShortcutGroup:
    """A named group of related shortcuts."""

    title: str
    entries: list[ShortcutEntry] = field(default_factory=list)


def _dock_list() -> str:
    """Human-readable ``Devices, Stations, Spectrogram, Log`` dock-name list."""
    return ", ".join(_DOCK_NAMES_IN_ORDER)


# The single source of truth the dialog renders and the C4 test scrapes.
SHORTCUT_GROUPS: tuple[ShortcutGroup, ...] = (
    ShortcutGroup(
        title="Focus mode",
        entries=[
            ShortcutEntry("F11", "Toggle focus mode on the active dock"),
            ShortcutEntry("Esc", "Exit focus mode"),
        ],
    ),
    ShortcutGroup(
        title="Docks",
        entries=[
            ShortcutEntry("Alt+1 .. Alt+4", f"Toggle each dock ({_dock_list()})"),
            ShortcutEntry("Ctrl+Shift+1 .. Ctrl+Shift+4", "Detach / redock each dock"),
        ],
    ),
    ShortcutGroup(
        title="Devices",
        entries=[
            ShortcutEntry("Ctrl+N", "New device"),
            ShortcutEntry("Ctrl+E", "Edit selected device"),
            ShortcutEntry("Del", "Remove selected device"),
        ],
    ),
    ShortcutGroup(
        title="Live view",
        entries=[
            ShortcutEntry(
                "Click a tab",
                "Switch between the All overview and per-device tabs "
                "(only the visible tab renders at full rate)",
            ),
        ],
    ),
    ShortcutGroup(
        title="Application",
        entries=[
            ShortcutEntry("Ctrl+Q", "Quit the application"),
        ],
    ),
)


def all_shortcut_entries() -> list[ShortcutEntry]:
    """Flatten :data:`SHORTCUT_GROUPS` into a single list of entries."""
    return [entry for group in SHORTCUT_GROUPS for entry in group.entries]


class ShortcutsDialog(QDialog):
    """Read-only modal listing every window keyboard shortcut.

    Renders :data:`SHORTCUT_GROUPS` into a two-column, non-editable
    :class:`QTableWidget`. Group headers are rendered as spanned, bold
    rows so the table reads as labelled sections.

    Args:
        parent: Owning widget (the MainWindow).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.setModal(True)
        self.setObjectName("ShortcutsDialog")

        intro = QLabel(
            "All keyboard shortcuts are listed below. They work window-wide "
            "(focus and dock shortcuts) or when the relevant dock has focus.",
            self,
        )
        intro.setWordWrap(True)

        self._table = QTableWidget(self)
        self._table.setColumnCount(2)
        self._table.setHorizontalHeaderLabels(["Shortcut", "Action"])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        self._populate()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self._table, stretch=1)
        layout.addWidget(buttons)

    def _populate(self) -> None:
        """Fill the table from :data:`SHORTCUT_GROUPS`."""
        total_rows = sum(1 + len(group.entries) for group in SHORTCUT_GROUPS)
        self._table.setRowCount(total_rows)
        row = 0
        for group in SHORTCUT_GROUPS:
            header_item = QTableWidgetItem(group.title)
            font = header_item.font()
            font.setBold(True)
            header_item.setFont(font)
            header_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._table.setItem(row, 0, header_item)
            self._table.setSpan(row, 0, 1, 2)
            row += 1
            for entry in group.entries:
                keys_item = QTableWidgetItem(entry.keys)
                keys_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                desc_item = QTableWidgetItem(entry.description)
                desc_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self._table.setItem(row, 0, keys_item)
                self._table.setItem(row, 1, desc_item)
                row += 1
        self._table.resizeColumnToContents(0)
