"""Tests for :class:`SpectrogramDock` (M6 stage 1)."""

from __future__ import annotations

import numpy as np

from echosmonitor.gui.widgets.spectrogram_dock import SpectrogramDock


def _column_and_freqs(n: int = 65) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(0)
    return (
        rng.exponential(1.0, size=n).astype(np.float32),
        np.linspace(0.0, 50.0, n, dtype=np.float32),
    )


def test_empty_dock_renders_placeholder(qtbot) -> None:
    dock = SpectrogramDock()
    qtbot.addWidget(dock)
    assert dock._tabs.count() == 0
    assert dock._empty_label.isVisible() or not dock._tabs.isVisible()


def test_add_stream_creates_tab(qtbot) -> None:
    dock = SpectrogramDock()
    qtbot.addWidget(dock)
    dock.add_stream("dev", "N.S.L.HHZ", fs=100.0)
    assert dock._tabs.count() == 1
    # Idempotent: second call with same key is a no-op.
    dock.add_stream("dev", "N.S.L.HHZ", fs=100.0)
    assert dock._tabs.count() == 1


def test_add_stream_handles_multiple_streams(qtbot) -> None:
    dock = SpectrogramDock()
    qtbot.addWidget(dock)
    dock.add_stream("dev-a", "N.S.L.HHZ", fs=100.0)
    dock.add_stream("dev-b", "N.S.L.HHN", fs=200.0)
    assert dock._tabs.count() == 2


def test_remove_stream_drops_tab(qtbot) -> None:
    dock = SpectrogramDock()
    qtbot.addWidget(dock)
    dock.add_stream("dev", "N.S.L.HHZ", fs=100.0)
    dock.remove_stream("dev", "N.S.L.HHZ")
    assert dock._tabs.count() == 0
    assert ("dev", "N.S.L.HHZ") not in dock._views


def test_remove_device_drops_all_its_streams(qtbot) -> None:
    dock = SpectrogramDock()
    qtbot.addWidget(dock)
    dock.add_stream("dev", "N.S.L.HHZ", fs=100.0)
    dock.add_stream("dev", "N.S.L.HHN", fs=100.0)
    dock.add_stream("other", "N.S.L.HHZ", fs=100.0)
    dock.remove_device("dev")
    assert dock._tabs.count() == 1
    assert ("dev", "N.S.L.HHZ") not in dock._views
    assert ("other", "N.S.L.HHZ") in dock._views


def test_on_column_routes_to_matching_tab(qtbot) -> None:
    dock = SpectrogramDock()
    qtbot.addWidget(dock)
    dock.add_stream("dev", "N.S.L.HHZ", fs=100.0)
    col, freqs = _column_and_freqs()
    dock.on_column("dev", "N.S.L.HHZ", col, freqs, None)
    view = dock._views[("dev", "N.S.L.HHZ")]
    assert view._column_count == 1


def test_on_column_for_unknown_stream_is_silent(qtbot) -> None:
    dock = SpectrogramDock()
    qtbot.addWidget(dock)
    col, freqs = _column_and_freqs()
    # Should not raise, should not allocate a tab.
    dock.on_column("dev", "N.S.L.HHZ", col, freqs, None)
    assert dock._tabs.count() == 0


def test_update_meta_forwards_to_view(qtbot) -> None:
    dock = SpectrogramDock()
    qtbot.addWidget(dock)
    dock.add_stream("dev", "N.S.L.HHZ", fs=100.0)
    dock.update_meta("dev", "N.S.L.HHZ", fs=50.0)
    view = dock._views[("dev", "N.S.L.HHZ")]
    assert view._fs == 50.0
