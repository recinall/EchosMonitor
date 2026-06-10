"""Tests for the M6 stage-3 double-click handler on DevicePanel."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QTreeWidgetItem

from echosmonitor.config.loader import load_config
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.gui.widgets.device_panel import DevicePanel


def _make_panel(qtbot, monkeypatch, tmp_path: Path) -> tuple[DevicePanel, ConfigStore]:
    cfg, _ = load_config(None)
    store = ConfigStore(cfg, tmp_path / "cfg.yaml")
    panel = DevicePanel()
    qtbot.addWidget(panel)
    panel.set_config_store(store)
    # Monkeypatch DeviceDialog.edit to a no-op so the test doesn't open
    # a modal child dialog. We assert its presence in calls below.
    from echosmonitor.gui.dialogs.device_dialog import DeviceDialog

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        DeviceDialog,
        "edit",
        classmethod(
            lambda cls, parent, store, name, *, engine=None: calls.append((name, engine)) or 0
        ),
    )
    panel._edit_calls_for_test = calls  # type: ignore[attr-defined]
    return panel, store


def test_device_row_double_click_opens_edit_dialog(qtbot, monkeypatch, tmp_path: Path) -> None:
    panel, _store = _make_panel(qtbot, monkeypatch, tmp_path)
    item = QTreeWidgetItem(["my-device", "", "", ""])
    panel._tree.addTopLevelItem(item)
    panel._on_tree_double_clicked(item, 0)
    calls = panel._edit_calls_for_test  # type: ignore[attr-defined]
    assert calls and calls[0][0] == "my-device"


def test_stream_row_double_click_is_a_noop(qtbot, monkeypatch, tmp_path: Path) -> None:
    """Double-clicking a stream row must NOT open a per-stream chain
    editor. DSP chains are per-device in the schema; opening one
    scoped to a single NSLC would mislead users."""
    panel, _store = _make_panel(qtbot, monkeypatch, tmp_path)
    device = QTreeWidgetItem(["my-device", "", "", ""])
    panel._tree.addTopLevelItem(device)
    stream = QTreeWidgetItem(["NET.STA.LOC.HHZ", "", "", ""])
    device.addChild(stream)
    panel._on_tree_double_clicked(stream, 0)
    calls = panel._edit_calls_for_test  # type: ignore[attr-defined]
    assert calls == []


def test_double_click_without_store_is_a_noop(qtbot, monkeypatch, tmp_path: Path) -> None:
    """If no config store is wired, the double-click must not crash —
    the panel may be in its empty-state placeholder."""
    panel = DevicePanel()
    qtbot.addWidget(panel)
    from echosmonitor.gui.dialogs.device_dialog import DeviceDialog

    calls: list[str] = []
    monkeypatch.setattr(
        DeviceDialog,
        "edit",
        classmethod(lambda cls, *args, **kwargs: calls.append("called") or 0),
    )
    item = QTreeWidgetItem(["some-name", "", "", ""])
    panel._tree.addTopLevelItem(item)
    panel._on_tree_double_clicked(item, 0)
    assert calls == []
