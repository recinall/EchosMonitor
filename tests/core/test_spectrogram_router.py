"""Unit tests for :class:`_SpectrogramRouter`.

The router is exercised here in isolation (no QThread, no engine) —
slot calls run on the test thread synchronously. The integration with
the engine is exercised in
:mod:`tests.core.test_streaming_engine_spectrogram`.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from PySide6.QtCore import QObject, Qt, Slot
from PySide6.QtWidgets import QApplication

from echosmonitor.core.models import device_stream_key
from echosmonitor.core.spectrogram_router import (
    _DROP_LOG_INTERVAL_S,
    _SPECTROGRAM_MAX_COLUMNS,
    _SpectrogramRouter,
)

_FS = 100.0
_DEV = "fake"


@pytest.fixture(autouse=True)
def _qapp() -> QApplication:
    """Ensure a QApplication exists so later GUI tests in the same
    session don't trip over a QCoreApplication-only state.

    A bare QCoreApplication (created by an earlier test) cannot be
    upgraded to a QApplication: pytest-qt and PySide6 widget tests in
    the same session will then fail to construct QWidgets and abort
    the interpreter at shutdown.
    """
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_install_creates_spectrogram_with_expected_fs() -> None:
    router = _SpectrogramRouter()
    router.install_for(_DEV, "NET.STA.LOC.HHZ", _FS)
    key = device_stream_key(_DEV, "NET.STA.LOC.HHZ")
    spec = router._spectrograms.get(key)
    assert spec is not None
    assert spec.fs == _FS


def test_install_with_invalid_fs_logs_and_skips() -> None:
    router = _SpectrogramRouter()
    router.install_for(_DEV, "NET.STA.LOC.HHZ", 0.0)
    assert router._spectrograms == {}


def test_remove_for_drops_state() -> None:
    router = _SpectrogramRouter()
    router.install_for(_DEV, "NET.STA.LOC.HHZ", _FS)
    router.remove_for(_DEV, "NET.STA.LOC.HHZ")
    assert router._spectrograms == {}
    assert router._pending == {}


def test_clear_for_device_drops_only_matching_keys() -> None:
    router = _SpectrogramRouter()
    router.install_for("dev-a", "N.S.L.HHZ", _FS)
    router.install_for("dev-b", "N.S.L.HHZ", _FS)
    router.clear_for_device("dev-a")
    assert device_stream_key("dev-a", "N.S.L.HHZ") not in router._spectrograms
    assert device_stream_key("dev-b", "N.S.L.HHZ") in router._spectrograms


def test_clear_all_empties_state() -> None:
    router = _SpectrogramRouter()
    router.install_for("dev-a", "N.S.L.HHZ", _FS)
    router.install_for("dev-b", "N.S.L.HHN", _FS)
    router.clear_all()
    assert router._spectrograms == {}
    assert router._pending == {}


class _ColumnSpy(QObject):
    def __init__(self, router: _SpectrogramRouter) -> None:
        super().__init__()
        self.columns: list[tuple[str, str, np.ndarray]] = []
        self.drops: list[tuple[str, str, int]] = []
        router.columnReady.connect(self._on_col, type=Qt.ConnectionType.DirectConnection)
        router.spectrogramDropped.connect(self._on_drop, type=Qt.ConnectionType.DirectConnection)

    @Slot(str, str, object, object, object)
    def _on_col(self, dev: str, nslc: str, col: object, _f: object, _t: object) -> None:
        if isinstance(col, np.ndarray):
            self.columns.append((dev, nslc, col))

    @Slot(str, str, int)
    def _on_drop(self, dev: str, nslc: str, count: int) -> None:
        self.drops.append((dev, nslc, count))


def test_feed_emits_columns_for_known_stream() -> None:
    router = _SpectrogramRouter()
    spy = _ColumnSpy(router)
    nslc = "NET.STA.LOC.HHZ"
    router.install_for(_DEV, nslc, _FS)
    # 1000 samples on 100 Hz with default 200-sample window + 50%
    # overlap → expect ~9 columns from the streamer.
    n = 1000
    t = np.arange(n, dtype=np.float64) / _FS
    sine = np.sin(2 * np.pi * 5.0 * t).astype(np.float32)
    router.feed(_DEV, nslc, sine, None)
    assert spy.columns, "router emitted no columns"
    assert all(d == _DEV and ns == nslc for d, ns, _ in spy.columns)
    # Each column is float32 and has nperseg/2+1 = 101 bins (defaults).
    assert all(c.dtype == np.float32 and c.shape == (101,) for _, _, c in spy.columns)


def test_feed_stamps_per_column_t_end() -> None:
    """Each emitted column carries its own end time, derived from the
    chunk's ``t_end`` and the spectrogram's ``column_dt``, so a GUI
    consumer can place columns on a wall-clock axis. The last column
    ends at the chunk's ``t_end``; earlier columns one step earlier."""
    from obspy.core.utcdatetime import UTCDateTime

    stamps: list[object] = []

    class _Spy(QObject):
        def __init__(self, router: _SpectrogramRouter) -> None:
            super().__init__()
            router.columnReady.connect(self._on, type=Qt.ConnectionType.DirectConnection)

        @Slot(str, str, object, object, object)
        def _on(self, dev: str, nslc: str, col: object, f: object, t: object) -> None:
            stamps.append(t)

    router = _SpectrogramRouter()
    spy = _Spy(router)  # keep a reference so the connection survives GC
    assert spy is not None
    nslc = "NET.STA.LOC.HHZ"
    router.install_for(_DEV, nslc, _FS)
    chunk_end = UTCDateTime(2026, 5, 31, 12, 0, 0)
    n = 1000
    t = np.arange(n, dtype=np.float64) / _FS
    router.feed(_DEV, nslc, np.sin(2 * np.pi * 5.0 * t).astype(np.float32), chunk_end)

    assert stamps, "no columns emitted"
    assert all(isinstance(s, UTCDateTime) for s in stamps)
    dt = router._spectrograms[device_stream_key(_DEV, nslc)].column_dt
    # Last column lands exactly on the chunk end; spacing is one column.
    assert float(stamps[-1]) == pytest.approx(float(chunk_end))
    assert float(stamps[-1]) - float(stamps[-2]) == pytest.approx(dt)


def test_feed_with_unknown_stream_is_a_no_op() -> None:
    router = _SpectrogramRouter()
    spy = _ColumnSpy(router)
    sine = np.zeros(500, dtype=np.float32)
    router.feed(_DEV, "NET.STA.LOC.HHZ", sine, None)
    assert spy.columns == []


def test_reinstall_drops_pending_state() -> None:
    router = _SpectrogramRouter()
    nslc = "NET.STA.LOC.HHZ"
    key = device_stream_key(_DEV, nslc)
    router.install_for(_DEV, nslc, _FS)
    # Push a half-window so the buffer holds a tail.
    router.feed(_DEV, nslc, np.zeros(50, dtype=np.float32), None)
    router.reinstall_for(_DEV, nslc, 50.0)
    assert router._spectrograms[key].fs == 50.0
    # Buffer was reset to a fresh maxlen-bounded deque.
    assert len(router._pending[key]) == 0


def test_drop_logging_throttled_to_one_per_5s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Push enough columns to overflow the bounded deque; assert at most
    one dropped-signal per stream within a 5 s window.

    The router's ``_drain`` clears the deque after each ``feed`` call,
    so to reproduce the overflow path we feed in a tight loop without
    letting the queued column emissions actually drain.
    """
    router = _SpectrogramRouter()
    spy = _ColumnSpy(router)
    nslc = "NET.STA.LOC.HHZ"
    key = device_stream_key(_DEV, nslc)
    router.install_for(_DEV, nslc, _FS)
    # Fake the deque so its maxlen is small and the drains can't keep up.
    from collections import deque

    router._pending[key] = deque(maxlen=4)

    fake_now = [1000.0]

    def _now() -> float:
        return fake_now[0]

    monkeypatch.setattr(time, "monotonic", _now)

    # Feed enough samples to produce > maxlen columns AND skip the drain
    # by directly calling _enqueue (the public path drains every call,
    # so we exercise the bounded-deque branch via the helper).
    spec = router._spectrograms[key]
    cols = spec.add_samples(np.zeros(_SPECTROGRAM_MAX_COLUMNS * spec.nperseg, dtype=np.float32))
    for col in cols[:50]:
        router._enqueue(_DEV, nslc, key, col, None)
    # First overflow window: should produce exactly one drop signal.
    n_drops_initial = len(spy.drops)
    assert n_drops_initial == 1, f"expected 1 throttled drop, got {n_drops_initial}"

    # Within the throttle window: another overflow MUST NOT produce
    # another drop signal.
    fake_now[0] += _DROP_LOG_INTERVAL_S - 0.1
    for col in cols[50:80]:
        router._enqueue(_DEV, nslc, key, col, None)
    assert len(spy.drops) == n_drops_initial

    # After the throttle window elapses: another drop is allowed.
    fake_now[0] += 1.0  # pushes past the 5 s mark
    for col in cols[80:120]:
        router._enqueue(_DEV, nslc, key, col, None)
    assert len(spy.drops) == n_drops_initial + 1


def test_feed_handles_corrupt_input_without_raising() -> None:
    router = _SpectrogramRouter()
    spy = _ColumnSpy(router)
    router.install_for(_DEV, "NET.STA.LOC.HHZ", _FS)
    # 2-D — the router rejects this and stays alive.
    router.feed(_DEV, "NET.STA.LOC.HHZ", np.zeros((10, 10), dtype=np.float32), None)
    assert spy.columns == []
