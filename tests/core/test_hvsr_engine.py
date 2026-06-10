"""HvsrEngine best-effort guarantee — rule 11, plus disjoint-window + stop.

The HVSR subsystem is a best-effort consumer: it *pulls* recent 3C windows
from the ring buffers on its own timer and runs the (slow, JIT-bearing)
hvsrpy re-compute off-thread. It must NEVER throttle acquisition / DSP /
detection / storage — if a compute can't keep up it SKIPS a recompute, it
never blocks the data path.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field

import numpy as np
from obspy import Trace, UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    HighpassStage,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core import hvsr as hvsr_mod
from echosmonitor.core.hvsr import HvsrResult, HvsrSettings, SesameCriterion
from echosmonitor.core.hvsr_engine import HvsrEngine
from echosmonitor.core.streaming_engine import StreamingEngine

_NET, _STA, _LOC = "XX", "HVLOAD", "00"
_DEVICE = "hvloadgen"
_CHANS = ("HHZ", "HHN", "HHE")
_GROUP = {
    "Z": f"{_NET}.{_STA}.{_LOC}.HHZ",
    "N": f"{_NET}.{_STA}.{_LOC}.HHN",
    "E": f"{_NET}.{_STA}.{_LOC}.HHE",
}


def _make_trace(cha: str, t0: UTCDateTime, n: int, fs: float, rng: np.random.Generator) -> Trace:
    tr = Trace(data=(rng.standard_normal(n) * 1000.0).astype(np.int32))
    tr.stats.network, tr.stats.station = _NET, _STA
    tr.stats.location, tr.stats.channel = _LOC, cha
    tr.stats.sampling_rate = fs
    tr.stats.starttime = t0
    return tr


def _cfg(fs: float, archive_dir) -> RootConfig:
    return RootConfig(
        app=AppConfig(archive_root=str(archive_dir)),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name=_DEVICE,
                host="192.0.2.1",
                port=18000,
                reconnect=ReconnectConfig(
                    initial_delay_s=3600.0, max_delay_s=3600.0, connect_timeout_s=0.5
                ),
                selectors=[
                    StreamSelectorConfig(network=_NET, station=_STA, location=_LOC, channel="HH?")
                ],
                dsp_chain=[HighpassStage(type="highpass", freq=1.0)],
            )
        ],
    )


class _Feeder(QObject):
    """Feeds 3 components (Z/N/E) per tick over the real cross-thread path."""

    packet = Signal(str, object)
    finished = Signal(int)

    def __init__(self, fs: float, spp: int, n_packets: int) -> None:
        super().__init__()
        self._fs, self._spp, self._n = fs, spp, n_packets
        self._stop = False

    @Slot()
    def run(self) -> None:
        rng = np.random.default_rng(99)
        t0 = UTCDateTime(0)
        dt = self._spp / self._fs
        total = 0
        for _ in range(self._n):
            if self._stop:
                break
            for cha in _CHANS:
                self.packet.emit(_DEVICE, _make_trace(cha, t0, self._spp, self._fs, rng))
                total += self._spp
            t0 = t0 + dt
            QThread.msleep(max(1, int(dt * 1000)))
        self.finished.emit(total)

    def stop(self) -> None:
        self._stop = True


@dataclass
class _Result:
    fed: int = 0
    dsp: int = 0
    dropped_chain: int = 0
    feed_done: bool = False
    backpressure: list[int] = field(default_factory=list)


def _dummy_result() -> HvsrResult:
    """A minimal valid HvsrResult so the patched compute need not run hvsrpy."""
    freq = np.linspace(1.0, 10.0, 16)
    curves = np.ones((3, 16))
    crit3 = tuple(SesameCriterion(f"r{i}", True, "") for i in range(3))
    crit6 = tuple(SesameCriterion(f"c{i}", True, "") for i in range(6))
    empty = (np.empty(0), np.empty(0))
    return HvsrResult(
        frequency=freq,
        window_curves=curves,
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
        device=_DEVICE,
        station_key="XX.HVLOAD",
        t_start=UTCDateTime(0),
        t_end=UTCDateTime(10),
    )


def test_slow_compute_does_not_starve_dsp_or_storage(qtbot, tmp_path, monkeypatch) -> None:
    """A saturated HVSR compute skips recomputes; the data path is intact.

    With ``compute`` patched slower than the window cadence, the engine can
    never keep up, so it skips recomputes (``hvsrBackpressure``). Meanwhile
    every fed sample must still reach the DSP chain (``processedTraceReady``
    == fed) with zero ``chainDropped`` — the HVSR consumer shares no queue
    or lock with ingestion/DSP, so it structurally cannot back-pressure it.
    """
    sample = _dummy_result()

    def _slow_compute(self) -> HvsrResult:
        time.sleep(0.8)
        return sample

    monkeypatch.setattr(hvsr_mod.HvsrAccumulator, "compute", _slow_compute)

    fs = 200.0
    engine = StreamingEngine(_cfg(fs, tmp_path / "arch"))
    engine.start()
    hv = HvsrEngine(engine, None)
    result = _Result()

    engine.processedTraceReady.connect(
        lambda _d, _n, s: setattr(result, "dsp", result.dsp + len(s)),
        type=Qt.ConnectionType.DirectConnection,
    )
    engine.chainDropped.connect(
        lambda _d, _n, c: setattr(result, "dropped_chain", result.dropped_chain + int(c)),
        type=Qt.ConnectionType.DirectConnection,
    )
    hv.hvsrBackpressure.connect(lambda _id, n: result.backpressure.append(n))

    # Short windows so several accumulate during the feed, forcing the slow
    # compute to fall behind and skip.
    hv.start_measurement(_DEVICE, _GROUP, HvsrSettings(window_length_s=0.5, freqmin_hz=1.0))

    feeder = _Feeder(fs, spp=20, n_packets=50)  # ~5 s of data
    thread = QThread()
    feeder.moveToThread(thread)
    feeder.packet.connect(engine._on_packet, type=Qt.ConnectionType.QueuedConnection)
    feeder.finished.connect(lambda total: setattr(result, "fed", total))
    feeder.finished.connect(lambda _t: setattr(result, "feed_done", True))
    thread.started.connect(feeder.run)
    thread.start()
    try:
        qtbot.waitUntil(lambda: result.feed_done, timeout=30_000)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            qtbot.wait(50)
            if result.dsp >= result.fed:
                break
    finally:
        feeder.stop()
        thread.quit()
        thread.wait(2000)
        hv.shutdown()
        engine.stop()

    assert result.fed > 0
    assert result.dropped_chain == 0, "DSP dropped packets while HVSR was computing"
    assert result.dsp == result.fed, f"DSP saw {result.dsp}/{result.fed} samples"
    # The slow compute fell behind, so at least one recompute was skipped.
    assert result.backpressure, "expected recompute skips under a slow compute"


def test_overloaded_recompute_skips_with_bounded_inflight(qtbot, tmp_path, monkeypatch) -> None:
    """Back-to-back recompute requests never queue unboundedly — they skip."""
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: (time.sleep(0.5), _dummy_result())[1]
    )
    engine = StreamingEngine(_cfg(200.0, tmp_path / "arch"))
    hv = HvsrEngine(engine, None)
    skips: list[int] = []
    hv.hvsrBackpressure.connect(lambda _id, n: skips.append(n))
    hv.start_measurement(_DEVICE, _GROUP, HvsrSettings(window_length_s=0.5))
    m = hv._measurement
    assert m is not None
    # Seed enough windows that a recompute is allowed.
    rng = np.random.default_rng(0)
    for _ in range(5):
        m.accumulator.add_window(
            rng.standard_normal(50),
            rng.standard_normal(50),
            rng.standard_normal(50),
            UTCDateTime(0),
            100.0,
        )
    try:
        hv._request_recompute(m, force=True)  # dispatches (pending -> 1)
        hv._request_recompute(m, force=True)  # in flight -> skip
        hv._request_recompute(m, force=True)  # still in flight -> skip
        assert m.pending == 1, "in-flight slot must stay bounded at 1"
        assert skips, "expected backpressure skips"
    finally:
        hv.shutdown()
        engine.stop()


def test_windows_accumulate_and_are_disjoint(qtbot, tmp_path) -> None:
    """Captured live windows are non-overlapping (the disjoint invariant)."""
    fs = 200.0
    engine = StreamingEngine(_cfg(fs, tmp_path / "arch"))
    engine.start()
    hv = HvsrEngine(engine, None)
    hv.start_measurement(_DEVICE, _GROUP, HvsrSettings(window_length_s=0.5, freqmin_hz=1.0))

    feeder = _Feeder(fs, spp=20, n_packets=50)  # ~5 s
    thread = QThread()
    feeder.moveToThread(thread)
    feeder.packet.connect(engine._on_packet, type=Qt.ConnectionType.QueuedConnection)
    done = {"v": False}
    feeder.finished.connect(lambda _t: done.__setitem__("v", True))
    thread.started.connect(feeder.run)
    thread.start()
    try:
        qtbot.waitUntil(lambda: done["v"], timeout=30_000)
        qtbot.wait(300)  # let the last few ticks capture
        m = hv._measurement
        assert m is not None
        windows = m.accumulator._windows
        assert len(windows) >= 3, f"expected several windows, got {len(windows)}"
        tol = 2.0 / fs
        for a, b in itertools.pairwise(windows):
            assert float(b.t_start - a.t_end) >= -tol, "captured windows overlap"
    finally:
        feeder.stop()
        thread.quit()
        thread.wait(2000)
        hv.shutdown()
        engine.stop()


def test_stop_joins_within_bound_during_slow_compute(qtbot, tmp_path, monkeypatch) -> None:
    """Stop interrupts an in-flight slow compute and returns promptly (rule 7)."""
    monkeypatch.setattr(
        hvsr_mod.HvsrAccumulator, "compute", lambda self: (time.sleep(3.0), _dummy_result())[1]
    )
    engine = StreamingEngine(_cfg(200.0, tmp_path / "arch"))
    hv = HvsrEngine(engine, None)
    hv.start_measurement(_DEVICE, _GROUP, HvsrSettings(window_length_s=0.5))
    m = hv._measurement
    assert m is not None
    rng = np.random.default_rng(0)
    for _ in range(5):
        m.accumulator.add_window(
            rng.standard_normal(50),
            rng.standard_normal(50),
            rng.standard_normal(50),
            UTCDateTime(0),
            100.0,
        )
    hv._request_recompute(m, force=True)  # slow compute now in flight
    qtbot.wait(100)
    t0 = time.monotonic()
    try:
        hv.stop_measurement()
    finally:
        engine.stop()
    elapsed = time.monotonic() - t0
    assert elapsed < 8.5, f"stop took {elapsed:.1f}s (should join within the bound)"
    assert hv.active_measurement() is None
