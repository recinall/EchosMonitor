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
from PySide6.QtCore import Qt, QThread

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


def test_restart_after_join_timeout_rebuilds_worker(qtbot, monkeypatch) -> None:
    """Auditor F2: a stop whose bounded join times out leaves the thread
    finishing an uninterruptible compute with a quit() pending; once that
    slot returns, exec() exits DISCARDING queued events (the recorded
    postmortem race) and nothing restarts the thread — a new measurement
    dispatched into it hangs in COMPUTING forever. _boot_worker must
    detect the poisoned thread and rebuild, so the new run's compute
    actually lands."""
    import echosmonitor.core.hvsr_array as hvsr_array_mod

    calls: list[str] = []

    def _compute(self: hvsr_mod.HvsrAccumulator) -> HvsrResult:
        calls.append(self._device)
        if len(calls) == 1:
            time.sleep(2.0)  # the uninterruptible first compute
        return _dummy_result(self._device)

    monkeypatch.setattr(hvsr_mod.HvsrAccumulator, "compute", _compute)
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    try:
        first = hv.start_measurement({_DEV_A: _group("STA")}, _settings(), _geometry())
        _seed_windows(hv, _DEV_A, 3)
        m = hv._measurement
        assert m is not None
        hv._request_recompute(m, force=True)
        qtbot.waitUntil(lambda: bool(calls), timeout=2000)  # compute in flight
        # Force the join to time out while the 2 s compute is still running.
        monkeypatch.setattr(hvsr_array_mod, "_THREAD_JOIN_MS", 50)
        hv.stop_measurement()
        assert hv._join_timed_out
        monkeypatch.setattr(hvsr_array_mod, "_THREAD_JOIN_MS", 8000)
        # Immediate restart: must rebuild (the old thread is still busy)
        # and the new run's forced compute must land, not hang.
        second = hv.start_measurement({_DEV_A: _group("STA")}, _settings(), _geometry())
        assert second != first
        assert hv._abandoned, "expected the poisoned worker/thread to be abandoned"
        _seed_windows(hv, _DEV_A, 3)
        m2 = hv._measurement
        assert m2 is not None
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000) as blocker:
            hv._request_recompute(m2, force=True)
        result = blocker.args[0]
        assert isinstance(result, ArrayHvsrResult)
        assert result.measurement_id == second
    finally:
        hv.shutdown()  # drains the abandoned thread (bounded)
    assert not hv._abandoned


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


def test_worker_drops_superseded_cycle(qtbot, monkeypatch) -> None:
    """Latest-wins token (skill §2): a stale queued compute from a stopped
    run dies at the first check instead of burning a full N-device cycle
    ahead of the new run's first honest result (auditor F3).

    The worker slot is called directly (deterministic — the real path is a
    posted event surviving quit() and dispatching on the next thread start).
    """
    from echosmonitor.core.hvsr_array import _ArrayComputeRequest

    calls: list[str] = []

    def _compute(self: hvsr_mod.HvsrAccumulator) -> HvsrResult:
        calls.append(self._device)
        return _dummy_result(self._device)

    monkeypatch.setattr(hvsr_mod.HvsrAccumulator, "compute", _compute)
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    mid = hv.start_measurement(_DEVICES, _settings(), _geometry())
    try:
        _seed_windows(hv, _DEV_A, 3)
        m = hv._measurement
        assert m is not None
        emitted: list[object] = []
        # Direct: the slot must run inside the compute() call itself, not on
        # a later event-loop turn (the assertions are immediate).
        hv._worker.computed.connect(emitted.append, Qt.ConnectionType.DirectConnection)
        snapshot = m.stations[_DEV_A].accumulator.snapshot()
        # A request from a DEAD measurement: dropped before any compute runs.
        hv._worker.compute(_ArrayComputeRequest("hvsr-array-999", ((_DEV_A, snapshot),)))
        assert calls == [] and emitted == []
        # The live measurement's request still computes and announces.
        hv._worker.compute(_ArrayComputeRequest(mid, ((_DEV_A, snapshot),)))
        assert calls == [_DEV_A] and len(emitted) == 1
        # After stop the token is cleared: even the old live id is dead.
        hv.stop_measurement()
        hv._worker.compute(_ArrayComputeRequest(mid, ((_DEV_A, snapshot),)))
        assert calls == [_DEV_A] and len(emitted) == 1
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


def _archive_windows(n: int) -> list[tuple]:
    rng = np.random.default_rng(4)
    return [
        (
            rng.standard_normal(50),
            rng.standard_normal(50),
            rng.standard_normal(50),
            UTCDateTime(i * _WL),
            _FS,
        )
        for i in range(n)
    ]


class _RootedReader:
    """Duck-typed reader carrying only the ``root`` the engine surfaces."""

    def __init__(self, root: str) -> None:
        self.root = root


def test_archive_run_one_shot_and_per_device_independent(qtbot, monkeypatch) -> None:
    """M5-D: devA has archived windows, devB none — one slice+compute cycle
    runs (on the worker — M6), devA computes (provenance archive), devB
    stays selected with no result, the measurement ends IDLE and the live
    timer never starts."""
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    monkeypatch.setattr(
        hvsr_mod,
        "slice_archive_windows",
        lambda reader, device, *a, **k: _archive_windows(3) if device == _DEV_A else [],
    )
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    try:
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000) as blocker:
            mid = hv.start_archive_measurement(
                _DEVICES,
                UTCDateTime(0),
                UTCDateTime(10),
                _settings(),
                _geometry(),
                {_DEV_A: object(), _DEV_B: object()},  # type: ignore[dict-item]
            )
        assert mid
        result = blocker.args[0]
        assert isinstance(result, ArrayHvsrResult)
        assert result.measurement_id == mid
        assert result.provenance == "archive"
        assert set(result.results) == {_DEV_A}
        assert result.devices == (_DEV_A, _DEV_B)
        m = hv._measurement
        assert m is not None
        assert m.state is HvsrState.IDLE  # one-shot: cycle done, idle
        assert not hv._timer.isActive()  # archive runs never tick
        # The accumulators are archive-provenance.
        assert m.stations[_DEV_A].accumulator._provenance == "archive"
        # Engine-side totals reflect the worker's slice (devB honestly 0).
        summary = hv.active_measurement()
        assert summary is not None
        assert summary.window_counts[_DEV_A] == (3, 3)
        assert summary.window_counts[_DEV_B] == (0, 0)
    finally:
        hv.shutdown()


def test_archive_no_data_is_async_and_names_searched_roots(qtbot, monkeypatch) -> None:
    """M6: a range with no gap-free 3C window on any device is announced
    asynchronously via arrayArchiveNoData with the deduped searched roots;
    the measurement is discarded WITHOUT an arrayMeasurementStopped."""
    monkeypatch.setattr(hvsr_mod, "slice_archive_windows", lambda *a, **k: [])
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    stopped: list[str] = []
    hv.arrayMeasurementStopped.connect(stopped.append)
    try:
        with qtbot.waitSignal(hv.arrayArchiveNoData, timeout=5000) as blocker:
            mid = hv.start_archive_measurement(
                _DEVICES,
                UTCDateTime(0),
                UTCDateTime(10),
                _settings(),
                _geometry(),
                {_DEV_A: _RootedReader("/arch/a"), _DEV_B: _RootedReader("/arch/a")},  # type: ignore[dict-item]
            )
        assert mid
        assert blocker.args[0] == mid
        assert blocker.args[1] == ("/arch/a",)  # shared root, deduped
        assert hv.active_measurement() is None
        assert stopped == []  # no-data is terminal without a stopped emit
    finally:
        hv.shutdown()


def test_archive_run_without_any_reader_returns_empty() -> None:
    """The degenerate synchronous "" path: no checked device has a reader."""
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    try:
        mid = hv.start_archive_measurement(
            _DEVICES, UTCDateTime(0), UTCDateTime(10), _settings(), _geometry(), {}
        )
        assert mid == ""
        assert hv.active_measurement() is None
    finally:
        hv.shutdown()


def test_archive_slicing_runs_off_the_gui_thread(qtbot, monkeypatch) -> None:
    """M6 (auditor F1): the N-device archive read runs on the array worker
    thread, never on the calling/GUI thread."""
    threads: list[QThread] = []

    def _slice(reader, device, *a, **k):
        threads.append(QThread.currentThread())
        return _archive_windows(2)

    monkeypatch.setattr(hvsr_mod, "slice_archive_windows", _slice)
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    try:
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000):
            hv.start_archive_measurement(
                _DEVICES,
                UTCDateTime(0),
                UTCDateTime(10),
                _settings(),
                _geometry(),
                {_DEV_A: object(), _DEV_B: object()},  # type: ignore[dict-item]
            )
        gui_thread = QThread.currentThread()
        assert threads
        assert all(t is hv._array_thread for t in threads)
        assert all(t is not gui_thread for t in threads)
    finally:
        hv.shutdown()


def test_archive_all_slices_failed_reports_errors_not_no_data(qtbot, monkeypatch) -> None:
    """M6 review: when NOTHING sliced because every READ failed, the cycle
    announces the per-device errors (empty-results arrayUpdated), never the
    misleading 'no data' outcome."""

    def _boom(reader, device, *a, **k):
        raise OSError(f"disk gone for {device}")

    monkeypatch.setattr(hvsr_mod, "slice_archive_windows", _boom)
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    no_data: list[object] = []
    hv.arrayArchiveNoData.connect(lambda *a: no_data.append(a))
    try:
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000) as blocker:
            mid = hv.start_archive_measurement(
                _DEVICES,
                UTCDateTime(0),
                UTCDateTime(10),
                _settings(),
                _geometry(),
                {_DEV_A: object(), _DEV_B: object()},  # type: ignore[dict-item]
            )
        result = blocker.args[0]
        assert isinstance(result, ArrayHvsrResult)
        assert result.measurement_id == mid
        assert dict(result.results) == {}
        assert set(result.errors) == {_DEV_A, _DEV_B}
        assert "disk gone" in result.errors[_DEV_A]
        assert no_data == []
    finally:
        hv.shutdown()


def test_window_override_ignored_while_archive_cycle_inflight(qtbot, monkeypatch) -> None:
    """M6 auditor: the archive cycle owns the accumulators; a GUI-thread
    override during the in-flight slice must not touch them (and works
    again once the cycle lands)."""
    override_calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator,
        "set_window_override",
        lambda self, wid, acc: override_calls.append((wid, acc)),
    )
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    started = []

    def _slow_slice(reader, device, *a, **k):
        started.append(device)
        time.sleep(1.0)
        return _archive_windows(2)

    monkeypatch.setattr(hvsr_mod, "slice_archive_windows", _slow_slice)
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    try:
        with qtbot.waitSignal(hv.arrayUpdated, timeout=8000):
            mid = hv.start_archive_measurement(
                {_DEV_A: _group("STA")},
                UTCDateTime(0),
                UTCDateTime(10),
                _settings(),
                _geometry(),
                {_DEV_A: object()},  # type: ignore[dict-item]
            )
            qtbot.waitUntil(lambda: bool(started), timeout=2000)  # slice in flight
            hv.set_window_override(mid, _DEV_A, 0, False)
            assert override_calls == []  # worker owns the accumulator
        # Cycle landed: ownership is back, overrides apply again.
        with qtbot.waitSignal(hv.arrayUpdated, timeout=5000):
            hv.set_window_override(mid, _DEV_A, 0, False)
        assert override_calls == [(0, False)]
    finally:
        hv.shutdown()


def test_shutdown_keeps_unjoined_abandoned_thread_referenced(qtbot, monkeypatch) -> None:
    """M6 auditor: shutdown must never drop the last reference to a
    still-running abandoned QThread (destroyed-while-running aborts);
    the pair stays in _abandoned until a later drain joins it."""
    import echosmonitor.core.hvsr_array as hvsr_array_mod

    calls: list[str] = []

    def _compute(self: hvsr_mod.HvsrAccumulator) -> HvsrResult:
        calls.append(self._device)
        if len(calls) == 1:
            time.sleep(2.0)  # the uninterruptible first compute
        return _dummy_result(self._device)

    monkeypatch.setattr(hvsr_mod.HvsrAccumulator, "compute", _compute)
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    try:
        hv.start_measurement({_DEV_A: _group("STA")}, _settings(), _geometry())
        _seed_windows(hv, _DEV_A, 3)
        m = hv._measurement
        assert m is not None
        hv._request_recompute(m, force=True)
        qtbot.waitUntil(lambda: bool(calls), timeout=2000)  # compute in flight
        monkeypatch.setattr(hvsr_array_mod, "_THREAD_JOIN_MS", 50)
        hv.stop_measurement()  # join times out → poisoned
        hv.start_measurement({_DEV_A: _group("STA")}, _settings(), _geometry())
        assert hv._abandoned  # rebuilt; old pair abandoned, still busy
        hv.stop_measurement()
        hv.shutdown()  # 50 ms bound: the busy thread cannot join yet
        assert hv._abandoned, "a running abandoned thread must stay referenced"
    finally:
        monkeypatch.setattr(hvsr_array_mod, "_THREAD_JOIN_MS", 8000)
        hv.shutdown()  # full bound: drains for real
    assert not hv._abandoned


def test_stop_during_archive_slicing_aborts_at_device_boundary(qtbot, monkeypatch) -> None:
    """Rule 7: the stop flag is observed between devices in the slice phase —
    once stop lands during devA's slow read, devB is never sliced and the
    superseded cycle announces nothing."""
    sliced: list[str] = []

    def _slow_slice(reader, device, *a, **k):
        sliced.append(device)
        time.sleep(2.0)
        return _archive_windows(2)

    monkeypatch.setattr(hvsr_mod, "slice_archive_windows", _slow_slice)
    hv = HvsrArrayEngine(_FakeRingEngine(), None)  # type: ignore[arg-type]
    announced: list[object] = []
    hv.arrayUpdated.connect(announced.append)
    hv.arrayArchiveNoData.connect(lambda *a: announced.append(a))
    try:
        hv.start_archive_measurement(
            _DEVICES,
            UTCDateTime(0),
            UTCDateTime(10),
            _settings(),
            _geometry(),
            {_DEV_A: object(), _DEV_B: object()},  # type: ignore[dict-item]
        )
        qtbot.waitUntil(lambda: bool(sliced), timeout=2000)  # devA slice in flight
        t0 = time.monotonic()
        hv.stop_measurement()
        elapsed = time.monotonic() - t0
        assert elapsed < 8.5, f"stop took {elapsed:.1f}s (should join within the bound)"
        qtbot.wait(100)  # drain anything wrongly queued
        assert sliced == [_DEV_A], "stop must abort the cycle at the per-device boundary"
        assert announced == []
    finally:
        hv.shutdown()


def test_window_counts_track_totals_and_valid(qtbot, monkeypatch) -> None:
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: _dummy_result(self._device)
    )
    ring = _FakeRingEngine()
    hv = HvsrArrayEngine(ring, None)  # type: ignore[arg-type]
    counts: list[dict[str, tuple[int, int]]] = []
    hv.arrayWindowCounts.connect(lambda _id, c: counts.append(dict(c)))
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
