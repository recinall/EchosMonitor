"""HvsrArrayEngine — per-device independent windows + the worker canon (M5-A).

The array engine is N accumulators + orchestration on the HvsrEngine
skeleton. Pinned here:

* windows are PER-DEVICE INDEPENDENT (open question 5): a silent device
  contributes nothing while the others accumulate and compute;
* one bounded in-flight cycle (pending <= 1, skips emitted — rule 11);
* a per-device compute failure lands in ``errors`` and never blocks the
  other devices' results;
* stop interrupts a busy cycle within the bounded join (rule 7), and the
  engine survives a start -> stop -> start cycle (worker canon);
* geometry rides the result verbatim and unpositioned devices are
  discoverable, never guessed (rule 16).

The ring engine is a duck-typed fake: ``read_recent`` is the ONLY
StreamingEngine surface the array engine touches, and faking it keeps these
tests deterministic (ticks are driven directly) and fast (no data path).
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from obspy import UTCDateTime

from echosmonitor.core import hvsr as hvsr_mod
from echosmonitor.core.hvsr import HvsrResult, HvsrSettings, SesameCriterion
from echosmonitor.core.hvsr_array import ArrayHvsrResult, HvsrArrayEngine
from echosmonitor.core.hvsr_engine import HvsrState
from echosmonitor.core.positions import ResolvedPosition, station_geometry

_FS = 100.0
_WL = 0.5  # window length (s) -> 50 samples per window
_DEV_A, _DEV_B = "devA", "devB"


def _group(sta: str) -> dict[str, str]:
    return {c: f"XX.{sta}.00.HH{c}" for c in ("Z", "N", "E")}


_DEVICES = {_DEV_A: _group("STA"), _DEV_B: _group("STB")}

_POSITIONS = {
    _DEV_A: ResolvedPosition(_DEV_A, 45.0, 11.0, 100.0, "stationxml", 0.0),
    _DEV_B: ResolvedPosition(_DEV_B, 45.001, 11.0, 101.0, "gnss", 0.0),
}


class _FakeRingEngine:
    """Duck-typed ``read_recent`` source, advanced explicitly per device."""

    def __init__(self) -> None:
        self._latest: dict[str, UTCDateTime] = {}
        self._rng = np.random.default_rng(7)

    def advance(self, device: str, seconds: float) -> None:
        """Pretend ``seconds`` of fresh data arrived on all 3 components."""
        latest = self._latest.get(device, UTCDateTime(0))
        self._latest[device] = latest + seconds

    def read_recent(
        self, device: str, nslc: str, seconds: float
    ) -> tuple[np.ndarray, float, UTCDateTime | None]:
        del nslc
        latest = self._latest.get(device)
        if latest is None:
            return np.empty(0), 0.0, None
        n = round(seconds * _FS)
        return self._rng.standard_normal(n), _FS, latest


def _dummy_result(device: str) -> HvsrResult:
    """A minimal valid HvsrResult so a patched compute need not run hvsrpy."""
    freq = np.linspace(1.0, 10.0, 16)
    crit3 = tuple(SesameCriterion(f"r{i}", True, "") for i in range(3))
    crit6 = tuple(SesameCriterion(f"c{i}", True, "") for i in range(6))
    empty = (np.empty(0), np.empty(0))
    return HvsrResult(
        frequency=freq,
        window_curves=np.ones((3, 16)),
        mean_curve=np.ones(16),
        median_curve=np.ones(16),
        lognormal_sigma=np.full(16, 0.1),
        f0_hz=5.0,
        f0_sigma=0.1,
        a0=3.0,
        window_ids=(0, 1, 2),
        auto_accept_mask=np.ones(3, dtype=bool),
        manual_override_mask=np.zeros(3, dtype=bool),
        effective_mask=np.ones(3, dtype=bool),
        reliability=crit3,
        clarity=crit6,
        reliability_passed=True,
        clarity_passed=True,
        psd_z=empty,
        psd_n=empty,
        psd_e=empty,
        same_response=True,
        same_response_detail="test",
        provenance="live",
        settings=HvsrSettings(),
        n_windows_total=3,
        n_windows_valid=3,
        device=device,
        station_key=f"XX.{device}",
        t_start=UTCDateTime(0),
        t_end=UTCDateTime(10),
    )


def _settings() -> HvsrSettings:
    return HvsrSettings(window_length_s=_WL, freqmin_hz=1.0)


def _geometry():
    return station_geometry(_POSITIONS, _DEVICES)


def _seed_windows(engine: HvsrArrayEngine, device: str, n: int) -> None:
    m = engine._measurement
    assert m is not None
    rng = np.random.default_rng(0)
    for _ in range(n):
        m.stations[device].accumulator.add_window(
            rng.standard_normal(50),
            rng.standard_normal(50),
            rng.standard_normal(50),
            UTCDateTime(0),
            _FS,
        )


def test_start_validates_selection(qtbot) -> None:
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    try:
        with pytest.raises(ValueError, match="at least one device"):
            hv.start_measurement({}, _settings(), _geometry())
        with pytest.raises(ValueError, match="exactly Z/N/E"):
            hv.start_measurement(
                {_DEV_A: {"Z": "XX.STA.00.HHZ"}}, _settings(), _geometry()
            )
        with pytest.raises(ValueError, match="exactly Z/N/E"):
            # An extra component would make the 3C capture silently never
            # ready — rejected loudly instead.
            hv.start_measurement(
                {_DEV_A: {**_group("STA"), "H": "XX.STA.00.HNZ"}}, _settings(), _geometry()
            )
        assert hv.active_measurement() is None
    finally:
        hv.shutdown()


def test_independent_windows_silent_device_never_stalls(qtbot, monkeypatch) -> None:
    """devB streams nothing: devA still accumulates, computes, and reports.

    The per-device-independent-windows decision (open question 5): one
    not-ready device contributes nothing while the rest of the array
    proceeds. devB appears in ``devices`` (selected) but in neither
    ``results`` nor ``errors`` (no windows yet).
    """
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    ring = _FakeRingEngine()
    hv = HvsrArrayEngine(ring, None)  # type: ignore[arg-type]
    hv.start_measurement(_DEVICES, _settings(), _geometry())
    try:
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000) as blocker:
            for _ in range(4):  # 4 disjoint windows on devA only
                ring.advance(_DEV_A, _WL)
                hv._tick()
        result = blocker.args[0]
        assert isinstance(result, ArrayHvsrResult)
        assert result.devices == (_DEV_A, _DEV_B)
        assert set(result.results) == {_DEV_A}
        assert result.errors == {}
        assert result.results[_DEV_A].device == _DEV_A
        m = hv._measurement
        assert m is not None
        assert m.stations[_DEV_A].accumulator.n_windows >= 3
        assert m.stations[_DEV_B].accumulator.n_windows == 0
    finally:
        hv.shutdown()


def test_both_devices_compute_in_one_cycle(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    ring = _FakeRingEngine()
    hv = HvsrArrayEngine(ring, None)  # type: ignore[arg-type]
    hv.start_measurement(_DEVICES, _settings(), _geometry())
    try:
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000) as blocker:
            for _ in range(4):
                ring.advance(_DEV_A, _WL)
                ring.advance(_DEV_B, _WL)
                hv._tick()
        result = blocker.args[0]
        assert isinstance(result, ArrayHvsrResult)
        assert set(result.results) == {_DEV_A, _DEV_B}
        assert result.results[_DEV_B].device == _DEV_B
    finally:
        hv.shutdown()


def test_per_device_failure_isolated(qtbot, monkeypatch) -> None:
    """devB's compute raises: devA's result still lands, the error rides along."""

    def _compute(self: hvsr_mod.HvsrAccumulator) -> HvsrResult:
        if self._device == _DEV_B:
            raise RuntimeError("boom")
        return _dummy_result(self._device)

    monkeypatch.setattr(hvsr_mod.HvsrAccumulator, "compute", _compute)
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    hv.start_measurement(_DEVICES, _settings(), _geometry())
    try:
        _seed_windows(hv, _DEV_A, 3)
        _seed_windows(hv, _DEV_B, 3)
        m = hv._measurement
        assert m is not None
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000) as blocker:
            hv._request_recompute(m, force=True)
        result = blocker.args[0]
        assert isinstance(result, ArrayHvsrResult)
        assert set(result.results) == {_DEV_A}
        assert "boom" in result.errors[_DEV_B]
        # The measurement survives the partial failure and stays live.
        assert m.state is HvsrState.ACCUMULATING
        assert "boom" in m.last_error
    finally:
        hv.shutdown()


def test_inflight_cycle_bounded_and_skips(qtbot, monkeypatch) -> None:
    """Back-to-back recompute requests never queue unboundedly — they skip."""
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator,
        "compute",
        lambda self: (time.sleep(0.5), _dummy_result(self._device))[1],
    )
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    skips: list[int] = []
    hv.arrayBackpressure.connect(lambda _id, n: skips.append(n))
    hv.start_measurement(_DEVICES, _settings(), _geometry())
    try:
        _seed_windows(hv, _DEV_A, 5)
        m = hv._measurement
        assert m is not None
        hv._request_recompute(m, force=True)  # dispatches (pending -> 1)
        hv._request_recompute(m, force=True)  # in flight -> skip
        hv._request_recompute(m, force=True)  # still in flight -> skip
        assert m.pending == 1, "in-flight slot must stay bounded at 1"
        assert skips, "expected backpressure skips"
    finally:
        hv.shutdown()


def test_stop_joins_within_bound_during_slow_cycle(qtbot, monkeypatch) -> None:
    """Stop interrupts a busy serial cycle at the per-device boundary (rule 7).

    The pinned property is the BETWEEN-DEVICE stop check, not just the
    bounded join (which the single-station canon already pins): with the
    stop flag set while devA's slow compute is in flight, devB's compute
    must never run. Asserted on recorded invocations, not elapsed time —
    this machine flakes on timing under load.
    """
    calls: list[str] = []

    def _slow(self: hvsr_mod.HvsrAccumulator) -> HvsrResult:
        calls.append(self._device)
        time.sleep(3.0)
        return _dummy_result(self._device)

    monkeypatch.setattr(hvsr_mod.HvsrAccumulator, "compute", _slow)
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    hv.start_measurement(_DEVICES, _settings(), _geometry())
    _seed_windows(hv, _DEV_A, 5)
    _seed_windows(hv, _DEV_B, 5)
    m = hv._measurement
    assert m is not None
    hv._request_recompute(m, force=True)  # slow 2-device cycle now in flight
    qtbot.waitUntil(lambda: bool(calls), timeout=2000)  # devA's compute started
    t0 = time.monotonic()
    hv.stop_measurement()
    elapsed = time.monotonic() - t0
    assert elapsed < 8.5, f"stop took {elapsed:.1f}s (should join within the bound)"
    assert hv.active_measurement() is None
    assert calls == [_DEV_A], "stop must abort the cycle at the per-device boundary"


def test_start_stop_start_cycle(qtbot, monkeypatch) -> None:
    """The engine survives stop and computes again on a fresh measurement."""
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    ring = _FakeRingEngine()
    hv = HvsrArrayEngine(ring, None)  # type: ignore[arg-type]
    first = hv.start_measurement(_DEVICES, _settings(), _geometry())
    hv.stop_measurement()
    assert hv.active_measurement() is None
    second = hv.start_measurement(_DEVICES, _settings(), _geometry())
    assert second != first
    try:
        _seed_windows(hv, _DEV_A, 3)
        m = hv._measurement
        assert m is not None
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000) as blocker:
            hv._request_recompute(m, force=True)
        result = blocker.args[0]
        assert isinstance(result, ArrayHvsrResult)
        assert result.measurement_id == second
    finally:
        hv.shutdown()


def test_window_override_targets_one_device(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    mid = hv.start_measurement(_DEVICES, _settings(), _geometry())
    try:
        _seed_windows(hv, _DEV_A, 3)
        _seed_windows(hv, _DEV_B, 3)
        m = hv._measurement
        assert m is not None
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000):
            hv.set_window_override(mid, _DEV_B, 1, accepted=False)
        assert m.stations[_DEV_B].accumulator._overrides == {1: False}
        assert m.stations[_DEV_A].accumulator._overrides == {}
        # Unknown device / stale id: silently ignored, nothing dispatched.
        hv.set_window_override(mid, "ghost", 1, accepted=True)
        hv.set_window_override("hvsr-array-999", _DEV_A, 1, accepted=True)
        assert m.stations[_DEV_A].accumulator._overrides == {}
    finally:
        hv.shutdown()


def test_geometry_rides_result_and_unpositioned_is_explicit(qtbot, monkeypatch) -> None:
    """The start-time geometry snapshot is carried verbatim (rule 16)."""
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    geometry = station_geometry({_DEV_A: _POSITIONS[_DEV_A]}, _DEVICES)  # devB unpositioned
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    hv.start_measurement(_DEVICES, _settings(), geometry)
    try:
        _seed_windows(hv, _DEV_A, 3)
        m = hv._measurement
        assert m is not None
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000) as blocker:
            hv._request_recompute(m, force=True)
        result = blocker.args[0]
        assert isinstance(result, ArrayHvsrResult)
        assert result.geometry is geometry
        assert result.unpositioned() == (_DEV_B,)
        assert result.geometry.devices == (_DEV_A,)
    finally:
        hv.shutdown()


def test_window_counts_track_totals_and_valid(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    ring = _FakeRingEngine()
    hv = HvsrArrayEngine(ring, None)  # type: ignore[arg-type]
    counts: list[dict[str, tuple[int, int]]] = []
    hv.arrayWindowCounts.connect(lambda c: counts.append(dict(c)))
    hv.start_measurement(_DEVICES, _settings(), _geometry())
    try:
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000):
            for _ in range(4):
                ring.advance(_DEV_A, _WL)
                hv._tick()
        qtbot.waitUntil(
            lambda: any(c.get(_DEV_A, (0, 0))[0] == 3 for c in counts), timeout=2000
        )
        last = counts[-1]
        assert last[_DEV_A][1] >= 3  # totals grow live
        assert last[_DEV_B] == (0, 0)  # silent device honestly at zero
    finally:
        hv.shutdown()
