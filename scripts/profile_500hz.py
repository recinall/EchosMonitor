"""M6.5-C profiling harness — synthetic 500 Hz x 3 ch Monitor+Record load.

Drives a real StreamingEngine (recording session, real MseedWriter on the
storage thread, real DSP/spectrogram routers) with synthetic packets shaped
like the real echos.local stream: 500 Hz, 3 channels, ~108-sample packets
(the field-run record size), device-realistic packet pacing. The SeedLink
worker connects to an unroutable TEST-NET-1 host with a huge backoff, so
the ONLY packets are the injected ones — exactly the
``tests/core/test_high_rate_load.py`` pattern, but wrapped in cProfile and
with N configurable devices to measure second-device headroom.

Usage:
    uv run python scripts/profile_500hz.py [--devices 1] [--seconds 20]
        [--profile]

Reports:
    * wall/CPU seconds consumed by the whole process
    * per-tick flush latency (max / p99) on the engine thread
    * archive in-flight gauge high-water mark (storage drain headroom)
    * top-30 cumulative cProfile entries (with --profile)

NOT part of the test gate (it is a load harness, timing depends on the
machine); run it on an otherwise idle box for meaningful numbers.
"""

from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import resource
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from obspy import Trace, UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from echosmonitor.config.schema import (
    AppConfig,
    ArchiveConfig,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.streaming_engine import StreamingEngine

_FS = 500.0
_NPTS = 108  # field-run record fill
_CHANNELS = ("HHZ", "HHN", "HHE")
_DEAD_HOST = "192.0.2.1"


class _Feeder(QObject):
    """Emits one packet per channel per interval from its own thread."""

    packet = Signal(str, object)  # device, Trace

    def __init__(self, device: str, station: str) -> None:
        super().__init__()
        self._device = device
        self._station = station
        self._idx = 0
        self._t0 = UTCDateTime()
        self._rng = np.random.default_rng(42)
        self._timer: QTimer | None = None

    @Slot()
    def start(self) -> None:
        timer = QTimer()
        # 108 samples @ 500 Hz = 216 ms per channel-packet; the device
        # interleaves 3 channels, so a packet leaves every ~72 ms.
        timer.setInterval(72)
        timer.timeout.connect(self._tick)
        timer.start()
        self._timer = timer

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _tick(self) -> None:
        ch = _CHANNELS[self._idx % 3]
        chunk = self._idx // 3
        data = (self._rng.standard_normal(_NPTS) * 1000.0).astype(np.int32)
        tr = Trace(
            data=data,
            header={
                "network": "XX",
                "station": self._station,
                "location": "00",
                "channel": ch,
                "sampling_rate": _FS,
                "starttime": self._t0 + chunk * _NPTS / _FS,
            },
        )
        self.packet.emit(self._device, tr)
        self._idx += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", type=int, default=1)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--profile", action="store_true")
    args = ap.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication([])

    tmp = tempfile.mkdtemp(prefix="echos_profile_")
    devices = []
    for i in range(args.devices):
        devices.append(
            DeviceConfig(
                name=f"dev{i}",
                host=_DEAD_HOST,
                port=18000,
                reconnect=ReconnectConfig(
                    initial_delay_s=3600.0, max_delay_s=3600.0, connect_timeout_s=0.5
                ),
                selectors=[
                    StreamSelectorConfig(network="XX", station=f"S{i}", location="00", channel=c)
                    for c in _CHANNELS
                ],
                archive=ArchiveConfig(enabled=True, fsync_interval_s=5.0),
            )
        )
    cfg = RootConfig(
        app=AppConfig(archive_root=Path(tmp)),
        ui=UiConfig(refresh_hz=20, default_window_seconds=60, max_display_rate_hz=250),
        devices=devices,
    )
    engine = StreamingEngine(cfg)
    engine.start_session("profile", [d.name for d in devices])

    # Feeders on their own threads, packets over the same queued path the
    # real worker bridge uses.
    feeders: list[tuple[QThread, _Feeder]] = []
    for i, dev in enumerate(devices):
        worker = _Feeder(dev.name, f"S{i}")
        thread = QThread()
        thread.setObjectName(f"feeder-{dev.name}")
        worker.moveToThread(thread)
        worker.packet.connect(engine._on_packet, type=Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.start)
        thread.start()
        feeders.append((thread, worker))

    # Instrument the flush tick.
    tick_latencies: list[float] = []
    orig_flush = engine._flush_all

    def timed_flush() -> None:
        t0 = time.perf_counter()
        orig_flush()
        tick_latencies.append(time.perf_counter() - t0)

    engine._flush_timer.timeout.disconnect(engine._flush_all)
    engine._flush_timer.timeout.connect(timed_flush)

    inflight_high = {"v": 0}

    def poll_inflight() -> None:
        for d in devices:
            v = engine._archive_sent.get(d.name, 0) - engine._archive_acked.get(d.name, 0)
            inflight_high["v"] = max(inflight_high["v"], v)

    gauge_timer = QTimer()
    gauge_timer.setInterval(50)
    gauge_timer.timeout.connect(poll_inflight)
    gauge_timer.start()

    stop_timer = QTimer()
    stop_timer.setSingleShot(True)
    stop_timer.setInterval(int(args.seconds * 1000))
    stop_timer.timeout.connect(app.quit)
    stop_timer.start()

    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    wall0 = time.perf_counter()
    prof = cProfile.Profile() if args.profile else None
    if prof:
        prof.enable()
    app.exec()
    if prof:
        prof.disable()
    wall = time.perf_counter() - wall0
    ru1 = resource.getrusage(resource.RUSAGE_SELF)

    for thread, worker in feeders:
        worker.packet.disconnect()
        thread.quit()
        thread.wait(2000)
    engine.stop()

    cpu = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    lat = sorted(tick_latencies)
    p99 = lat[int(len(lat) * 0.99)] if lat else 0.0
    print(f"\n=== profile_500hz: {args.devices} device(s) x 3ch @ {_FS:.0f} Hz, {wall:.1f}s wall")
    print(f"process CPU: {cpu:.2f}s ({100.0 * cpu / wall:.1f}% of one core)")
    if lat:
        print(
            f"flush tick: n={len(lat)} max={lat[-1] * 1000:.1f}ms "
            f"p99={p99 * 1000:.1f}ms median={lat[len(lat) // 2] * 1000:.2f}ms"
        )
    print(f"archive in-flight high-water: {inflight_high['v']}")
    files = list(Path(tmp).rglob("*.D.*"))
    total = sum(f.stat().st_size for f in files)
    print(f"archive: {len(files)} files, {total / 1024:.0f} kB")
    if prof:
        stats = pstats.Stats(prof)
        stats.sort_stats("cumulative")
        print("\n--- top 30 by cumulative time (engine thread only) ---")
        stats.print_stats(30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
