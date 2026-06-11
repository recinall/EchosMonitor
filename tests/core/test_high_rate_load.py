"""High-sample-rate load harness — regression guard for render coupling.

Reproduces the throughput bug deterministically WITHOUT a real device and
asserts CLAUDE.md rule 11: data acquisition, DSP, detection and storage
are FULL-RATE consumers and must NOT be throttled by GUI rendering.

Two complementary guards:

* :func:`test_flush_tick_not_gated_by_render_latency` — the *deterministic
  gate*. A deliberately slow slot is connected to ``traceReady`` (the
  best-effort render path). The engine's flush tick ``_flush_all`` — which
  drains the per-stream DSP queue and the archive inbox toward the
  full-rate consumers — must return promptly regardless of how slow that
  render slot is. On the PRE-FIX engine the coalescer re-emits
  ``traceReady`` via a ``DirectConnection`` *inside* ``_flush_all``, so a
  slow render blocks the whole tick (and therefore the DSP/archive
  dispatch and the next tick): the measured tick latency tracks the render
  latency. This is exactly the M7 blind spot — the old proxy counted
  ``setData`` CALLS but never its LATENCY under load.

* :func:`test_high_rate_no_science_loss_under_slow_render` — a ``perf``
  stress test (load-sensitive timing, per the project convention). A fake
  feeder QThread pushes synthetic packets over the SAME cross-thread
  ``QueuedConnection`` path the real :class:`SeedLinkWorker` bridge uses,
  while a slow render runs on the GUI thread. Every fed sample must reach
  the DSP chain (so detection sees it); no ``chainDropped`` may fire.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pytest
from obspy import Trace, UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from echosmonitor.config.schema import (
    AppConfig,
    ArchiveConfig,
    DeviceConfig,
    HighpassStage,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.streaming_engine import StreamingEngine

# Stream identity used throughout the harness.
_NET, _STA, _LOC, _CHA = "XX", "LOAD", "00", "HHZ"
_NSLC = f"{_NET}.{_STA}.{_LOC}.{_CHA}"
_DEVICE = "loadgen"
# An unroutable TEST-NET-1 host (RFC 5737): the real worker spun up by
# engine.start() can never connect, so the ONLY packets the engine sees
# are the synthetic ones the harness injects.
_DEAD_HOST = "192.0.2.1"
_DEAD_PORT = 18000


def _make_trace(t0: UTCDateTime, n: int, fs: float, rng: np.random.Generator) -> Trace:
    """One synthetic packet of ``n`` samples starting at ``t0``."""
    data = (rng.standard_normal(n) * 1000.0).astype(np.int32)
    tr = Trace(data=data)
    tr.stats.network = _NET
    tr.stats.station = _STA
    tr.stats.location = _LOC
    tr.stats.channel = _CHA
    tr.stats.sampling_rate = fs
    tr.stats.starttime = t0
    return tr


def _build_cfg(fs: float, window_s: int, *, archive_dir, archive: bool) -> RootConfig:
    return RootConfig(
        app=AppConfig(archive_root=str(archive_dir)),
        ui=UiConfig(refresh_hz=20, default_window_seconds=window_s),
        devices=[
            DeviceConfig(
                name=_DEVICE,
                host=_DEAD_HOST,
                port=_DEAD_PORT,
                # Huge backoff so the dead worker tries once and sleeps —
                # it never competes with the synthetic feed.
                reconnect=ReconnectConfig(
                    initial_delay_s=3600.0,
                    max_delay_s=3600.0,
                    connect_timeout_s=0.5,
                ),
                selectors=[
                    StreamSelectorConfig(network=_NET, station=_STA, location=_LOC, channel=_CHA)
                ],
                # Length-preserving chain so DSP output length == input
                # length: summing processedTraceReady gives exactly the
                # samples detection would have seen.
                dsp_chain=[HighpassStage(type="highpass", freq=1.0)],
                archive=ArchiveConfig(enabled=archive),
            )
        ],
    )


@pytest.fixture
def load_engine(qtbot, tmp_path):
    engines: list[StreamingEngine] = []

    def _make(fs: float, window_s: int = 10, *, archive: bool = False) -> StreamingEngine:
        cfg = _build_cfg(fs, window_s, archive_dir=tmp_path / "arch", archive=archive)
        eng = StreamingEngine(cfg)
        # M2-A: archive writers exist only in the RECORDING state (rule 13).
        if archive:
            eng.start_recording(_DEVICE)
        else:
            eng.start()
        engines.append(eng)
        return eng

    yield _make
    for eng in engines:
        eng.stop()


# ----------------------------------------------------------------------
# Deterministic gate: acquisition tick must not be gated by render latency
# ----------------------------------------------------------------------
def test_flush_tick_not_gated_by_render_latency(qtbot, load_engine) -> None:
    """``_flush_all`` must not block for the render slot's latency.

    The render path (``traceReady``) is a best-effort consumer. The flush
    tick drains the DSP queue + archive inbox toward the full-rate
    consumers; it must complete promptly even when a render slot is slow,
    otherwise a slow GUI back-pressures ingestion (rule 11).

    PRE-FIX: ``coalescer.flushed`` re-emits ``traceReady`` via a
    ``DirectConnection`` inside ``_flush_all``, so the tick blocks for the
    full ``RENDER_S`` and this assertion fails. POST-FIX the render is
    delivered out-of-band (queued / decimated), so the tick returns fast.
    """
    render_s = 0.5
    engine = load_engine(500.0)

    rendered = {"n": 0}

    def _slow_render(_dev: str, _nslc: str, _samples: object) -> None:
        rendered["n"] += 1
        time.sleep(render_s)

    engine.traceReady.connect(_slow_render, type=Qt.ConnectionType.DirectConnection)

    # Seed one stream with a packet so the coalescer has data to flush
    # (and therefore would fire the render slot on the next tick).
    rng = np.random.default_rng(7)
    engine._on_packet(_DEVICE, _make_trace(UTCDateTime(0), 250, 500.0, rng))
    qtbot.wait(20)  # let the queued first-packet machinery settle

    # Push a second packet so the coalescer buffer is non-empty, then time
    # exactly one flush tick.
    engine._on_packet(_DEVICE, _make_trace(UTCDateTime(0.5), 250, 500.0, rng))
    t0 = time.perf_counter()
    engine._flush_all()
    elapsed = time.perf_counter() - t0

    # The tick must not absorb the render latency. Generous bound: well
    # under one render but comfortably above any incidental work.
    assert elapsed < render_s / 2, (
        f"_flush_all took {elapsed * 1000:.0f} ms — it is blocked by the "
        f"{render_s * 1000:.0f} ms render slot (render coupled into ingestion, "
        f"rule 11 violation)"
    )


# ----------------------------------------------------------------------
# perf stress test: full-rate DSP/detection under a slow render
# ----------------------------------------------------------------------
class _Feeder(QObject):
    """Emits synthetic packets at a target effective rate from a worker thread.

    ``packet`` is wired to ``engine._on_packet`` with a ``QueuedConnection``
    so delivery happens on the engine (GUI) thread — exactly the topology
    the real ``_DeviceBridge`` establishes. The feeder thread itself never
    blocks on the engine, mirroring the non-blocking queued hand-off.
    """

    packet = Signal(str, object)  # device_name, Trace
    finished = Signal(int)  # total samples emitted

    def __init__(self, fs: float, samples_per_packet: int, n_packets: int) -> None:
        super().__init__()
        self._fs = fs
        self._spp = samples_per_packet
        self._n_packets = n_packets
        self._stop = False

    @Slot()
    def run(self) -> None:
        rng = np.random.default_rng(1234)
        t0 = UTCDateTime(0)
        dt_packet = self._spp / self._fs
        total = 0
        for _ in range(self._n_packets):
            if self._stop:
                break
            self.packet.emit(_DEVICE, _make_trace(t0, self._spp, self._fs, rng))
            total += self._spp
            t0 = t0 + dt_packet
            QThread.msleep(max(1, int(dt_packet * 1000.0)))
        self.finished.emit(total)

    def stop(self) -> None:
        self._stop = True


@dataclass
class _LoadResult:
    fed_samples: int = 0
    dsp_samples: int = 0
    chain_dropped: int = 0
    feed_done: bool = False
    drop_events: list[tuple[str, str, int]] = field(default_factory=list)


@pytest.mark.perf
@pytest.mark.parametrize("fs", [500.0, 1000.0, 4000.0])
def test_high_rate_no_science_loss_under_slow_render(qtbot, load_engine, fs: float) -> None:
    """High fs + a slow render must not starve DSP/detection (rule 11).

    The feeder paces at the instrument's real effective rate; the render
    slot is slow enough that, on the PRE-FIX engine, the serialized flush
    tick cannot drain the DSP queue in time and ``chainDropped`` fires
    (detection loses samples). POST-FIX the drain is decoupled from render
    so every fed sample reaches the chain.
    """
    engine = load_engine(fs)
    result = _LoadResult()

    def _slow_render(_dev: str, _nslc: str, _samples: object) -> None:
        time.sleep(0.3)

    def _on_processed(_dev: str, _nslc: str, samples: object) -> None:
        result.dsp_samples += len(samples)  # type: ignore[arg-type]

    def _on_chain_dropped(dev: str, nslc: str, count: int) -> None:
        result.chain_dropped += int(count)
        result.drop_events.append((dev, nslc, int(count)))

    engine.traceReady.connect(_slow_render, type=Qt.ConnectionType.DirectConnection)
    engine.processedTraceReady.connect(_on_processed, type=Qt.ConnectionType.DirectConnection)
    engine.chainDropped.connect(_on_chain_dropped, type=Qt.ConnectionType.DirectConnection)

    # Small packets => high packet rate, so a stalled flush tick accumulates
    # a real backlog that overruns the per-stream DSP queue on the pre-fix
    # engine.
    samples_per_packet = 10
    n_packets = 300

    feeder = _Feeder(fs, samples_per_packet, n_packets)
    thread = QThread()
    feeder.moveToThread(thread)
    feeder.packet.connect(engine._on_packet, type=Qt.ConnectionType.QueuedConnection)
    feeder.finished.connect(lambda total: setattr(result, "fed_samples", total))
    feeder.finished.connect(lambda _t: setattr(result, "feed_done", True))
    thread.started.connect(feeder.run)
    thread.start()
    try:
        qtbot.waitUntil(lambda: result.feed_done, timeout=30_000)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            qtbot.wait(50)
            if result.chain_dropped > 0:
                break
            if result.dsp_samples >= result.fed_samples:
                break
    finally:
        feeder.stop()
        thread.quit()
        thread.wait(2000)

    assert result.fed_samples > 0
    assert result.chain_dropped == 0, (
        f"DSP queue dropped {result.chain_dropped} packets at {fs} Hz under a slow "
        f"render — detection lost samples (rule 11 violation)"
    )
    assert result.dsp_samples == result.fed_samples, (
        f"DSP saw {result.dsp_samples}/{result.fed_samples} samples at {fs} Hz"
    )


def test_filtered_path_delivers_full_rate_at_high_fs(qtbot, load_engine) -> None:
    """The processed (filtered) stream carries every sample at high fs.

    Engine-side guarantee: with a waveform-producing chain at 500 Hz and a
    slow render connected, ``processedTraceReady`` delivers every fed
    sample (no starvation, no ``chainDropped``) — so the filtered pipeline
    always has data to show.

    NB: the user-visible "filtered plot is empty" bug had two *widget*-side
    causes — the stacked plot being shown for detector-only chains, and the
    processed X axis not being anchored to wall-clock — fixed and guarded
    in ``tests/gui/test_live_stack.py`` and
    ``tests/gui/test_trace_plot_decimation.py``. This test covers only the
    engine-side delivery.
    """
    fs = 500.0
    engine = load_engine(fs)
    result = _LoadResult()

    def _slow_render(_dev: str, _nslc: str, _samples: object) -> None:
        time.sleep(0.1)

    def _on_processed(_dev: str, _nslc: str, samples: object) -> None:
        result.dsp_samples += len(samples)  # type: ignore[arg-type]

    def _on_chain_dropped(dev: str, nslc: str, count: int) -> None:
        result.chain_dropped += int(count)

    engine.traceReady.connect(_slow_render, type=Qt.ConnectionType.DirectConnection)
    engine.processedTraceReady.connect(_on_processed, type=Qt.ConnectionType.DirectConnection)
    engine.chainDropped.connect(_on_chain_dropped, type=Qt.ConnectionType.DirectConnection)

    feeder = _Feeder(fs, samples_per_packet=50, n_packets=40)  # ~4 s of data
    thread = QThread()
    feeder.moveToThread(thread)
    feeder.packet.connect(engine._on_packet, type=Qt.ConnectionType.QueuedConnection)
    feeder.finished.connect(lambda total: setattr(result, "fed_samples", total))
    feeder.finished.connect(lambda _t: setattr(result, "feed_done", True))
    thread.started.connect(feeder.run)
    thread.start()
    try:
        qtbot.waitUntil(lambda: result.feed_done, timeout=30_000)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            qtbot.wait(50)
            if result.dsp_samples >= result.fed_samples:
                break
    finally:
        feeder.stop()
        thread.quit()
        thread.wait(2000)

    # The filtered plot is fed from this stream — it must be non-empty,
    # and (decoupled from render) it must carry every sample.
    assert result.fed_samples > 0
    assert result.dsp_samples > 0, "filtered/processed path is empty — V3 regression"
    assert result.chain_dropped == 0
    assert result.dsp_samples == result.fed_samples
