"""Tests for :class:`PsdWidget`.

The widget never computes PSD itself — it talks to the engine via
``psdRequested`` / ``psdReady`` signals. The tests substitute a tiny
:class:`_FakeEngine` that mimics the public engine surface the widget
touches (``newStreamSeen``, ``devicesChanged``, ``psdRequested``,
``psdReady``, and the private ``_buffers`` map the widget reads to
enumerate streams).
"""

from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QObject, Qt, Signal, Slot

from echosmonitor.core.models import device_stream_key
from echosmonitor.gui.widgets.psd_widget import PsdWidget


class _FakeEngine(QObject):
    newStreamSeen = Signal(str, str)  # noqa: N815
    devicesChanged = Signal()  # noqa: N815
    psdRequested = Signal(str, str, float)  # noqa: N815
    psdReady = Signal(str, str, float, object, object)  # noqa: N815

    def __init__(self) -> None:
        super().__init__()
        # Map keyed by ``device_stream_key`` so the widget's enumerator
        # finds entries via the production helper.
        self._buffers: dict[str, object] = {}
        self.requests: list[tuple[str, str, float]] = []
        self.psdRequested.connect(self._record_request, type=Qt.ConnectionType.DirectConnection)

    @Slot(str, str, float)
    def _record_request(self, device: str, nslc: str, seconds: float) -> None:
        self.requests.append((device, nslc, seconds))

    def add_stream(self, device: str, nslc: str) -> None:
        self._buffers[device_stream_key(device, nslc)] = object()
        self.devicesChanged.emit()
        self.newStreamSeen.emit(device, nslc)


def _make_widget(qtbot) -> tuple[PsdWidget, _FakeEngine]:
    engine = _FakeEngine()
    widget = PsdWidget(engine=engine)  # type: ignore[arg-type]
    qtbot.addWidget(widget)
    return widget, engine


def _curve_or_db(freqs_n: int = 33, fs: float = 100.0) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.linspace(0.0, fs / 2.0, freqs_n, dtype=np.float64)
    db = np.linspace(-180.0, -120.0, freqs_n, dtype=np.float64)
    return freqs, db


def test_widget_construction_yields_default_window(qtbot) -> None:
    widget, _engine = _make_widget(qtbot)
    assert widget.window_seconds() == 60.0  # _DEFAULT_WINDOW_INDEX = 1 → 60 s
    assert widget.selected_stream() is None
    assert widget.overlays() == ()


def test_new_stream_populates_combo_and_fires_request(qtbot) -> None:
    widget, engine = _make_widget(qtbot)
    engine.add_stream("dev", "NET.STA.LOC.HHZ")
    qtbot.wait(20)
    sel = widget.selected_stream()
    assert sel is not None
    assert sel.device == "dev" and sel.nslc == "NET.STA.LOC.HHZ"
    assert ("dev", "NET.STA.LOC.HHZ", 60.0) in engine.requests


def test_window_combo_change_fires_fresh_request(qtbot) -> None:
    widget, engine = _make_widget(qtbot)
    engine.add_stream("dev", "NET.STA.LOC.HHZ")
    qtbot.wait(20)
    engine.requests.clear()
    widget._window_combo.setCurrentIndex(2)  # 5 min
    qtbot.wait(20)
    assert ("dev", "NET.STA.LOC.HHZ", 300.0) in engine.requests


def test_psd_ready_draws_curve_for_active_stream(qtbot) -> None:
    widget, engine = _make_widget(qtbot)
    engine.add_stream("dev", "NET.STA.LOC.HHZ")
    qtbot.wait(20)
    freqs, db = _curve_or_db()
    engine.psdReady.emit("dev", "NET.STA.LOC.HHZ", 60.0, freqs, db)
    qtbot.wait(20)
    assert ("dev", "NET.STA.LOC.HHZ") in widget._curves


def test_stale_psd_ready_is_dropped(qtbot) -> None:
    widget, engine = _make_widget(qtbot)
    engine.add_stream("dev", "NET.STA.LOC.HHZ")
    qtbot.wait(20)
    freqs, db = _curve_or_db()
    # Wrong seconds — the user moved on to a different window length.
    engine.psdReady.emit("dev", "NET.STA.LOC.HHZ", 30.0, freqs, db)
    qtbot.wait(20)
    assert ("dev", "NET.STA.LOC.HHZ") not in widget._curves
    # Unknown stream — silently dropped.
    engine.psdReady.emit("other", "N.S.L.HHZ", 60.0, freqs, db)
    qtbot.wait(20)
    assert ("other", "N.S.L.HHZ") not in widget._curves


def test_nlnm_toggle_defaults_off_to_avoid_unit_mismatch(qtbot) -> None:
    """The NLNM/NHNM toggle defaults OFF: the trace PSD is in counts²/Hz
    while the noise models are in (m/s²)²/Hz, so the overlay is only
    meaningful on response-corrected channels (M8). Users with an
    externally converted feed can opt in via the toggle."""
    widget, _engine = _make_widget(qtbot)
    assert widget._nlnm_toggle.isChecked() is False
    # No PSD result yet — curves are lazily constructed by the
    # _update_nlnm_overlay path, gated by the toggle.
    assert widget._nlnm_curve is None
    assert widget._nhnm_curve is None


def test_nlnm_toggle_on_adds_curves_then_off_hides_them(qtbot) -> None:
    widget, engine = _make_widget(qtbot)
    engine.add_stream("dev", "NET.STA.LOC.HHZ")
    qtbot.wait(20)
    # User opts into the overlay.
    widget._nlnm_toggle.setChecked(True)
    freqs, db = _curve_or_db()
    engine.psdReady.emit("dev", "NET.STA.LOC.HHZ", 60.0, freqs, db)
    qtbot.wait(20)
    assert widget._nlnm_curve is not None
    assert widget._nhnm_curve is not None
    assert widget._nlnm_curve.isVisible()
    assert widget._nhnm_curve.isVisible()
    # And back off.
    widget._nlnm_toggle.setChecked(False)
    assert not widget._nlnm_curve.isVisible()
    assert not widget._nhnm_curve.isVisible()


def test_overlay_button_adds_independent_curve(qtbot) -> None:
    widget, engine = _make_widget(qtbot)
    engine.add_stream("dev-a", "N.S.L.HHZ")
    qtbot.wait(20)
    widget._on_add_overlay()
    qtbot.wait(20)
    # Overlay tracks the same stream that was selected when the button
    # was pressed; we now switch the primary to a different stream so
    # both render side by side.
    engine.add_stream("dev-b", "N.S.L.HHZ")
    qtbot.wait(20)
    # The combo refresh re-pinned the primary to dev-a (selection
    # preservation); explicitly pick dev-b.
    widget._stream_combo.setCurrentIndex(1)
    qtbot.wait(20)
    freqs, db = _curve_or_db()
    engine.psdReady.emit("dev-a", "N.S.L.HHZ", 60.0, freqs, db)  # overlay
    engine.psdReady.emit("dev-b", "N.S.L.HHZ", 60.0, freqs, db)  # primary
    qtbot.wait(20)
    assert ("dev-a", "N.S.L.HHZ") in widget._curves
    assert ("dev-b", "N.S.L.HHZ") in widget._curves


def test_overlay_dedupes_against_primary(qtbot) -> None:
    """Adding an overlay for the currently-selected primary stream is
    a no-op — only the overlay-list bookkeeping changes, not the
    underlying curve count."""
    widget, engine = _make_widget(qtbot)
    engine.add_stream("dev", "N.S.L.HHZ")
    qtbot.wait(20)
    widget._on_add_overlay()
    widget._on_add_overlay()  # second call — already in overlays
    assert len(widget.overlays()) == 1


def test_auto_refresh_disabled_stops_periodic_requests(qtbot) -> None:
    widget, engine = _make_widget(qtbot)
    widget._auto_refresh.setChecked(False)
    engine.add_stream("dev", "N.S.L.HHZ")
    qtbot.wait(20)
    # We don't wait an entire refresh cycle here; just verify the
    # timer is inactive when auto-refresh is off.
    assert not widget._auto_timer.isActive()


def test_window_change_updates_auto_timer_interval(qtbot) -> None:
    widget, _engine = _make_widget(qtbot)
    widget._window_combo.setCurrentIndex(4)  # 1 hour
    qtbot.wait(20)
    # 1 h / 4 = 900 s → 900 000 ms
    assert widget._auto_timer.interval() == 900_000


@pytest.mark.parametrize(
    ("idx", "expected_seconds"),
    [(0, 30.0), (1, 60.0), (2, 300.0), (3, 900.0), (4, 3600.0)],
)
def test_window_preset_mapping(qtbot, idx: int, expected_seconds: float) -> None:
    widget, _engine = _make_widget(qtbot)
    widget._window_combo.setCurrentIndex(idx)
    assert widget.window_seconds() == expected_seconds
