"""Integration tests for :class:`PsdWorker` end-to-end through the engine.

Live SeedLink samples flow into the engine's ring buffer; the test
fires ``psdRequested`` and asserts ``psdReady`` lands with the
expected payload shape on the engine thread.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import numpy as np
import pytest
from PySide6.QtCore import QObject, Qt, Slot

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.streaming_engine import StreamingEngine
from echosmonitor.dsp.psd import welch_psd
from tests.core.fakes import FakeSeedLinkServer
from tests.core.test_seedlink_worker import fake_server, loop_thread  # noqa: F401


def _make_root_cfg(devices: list[DeviceConfig]) -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=devices,
    )


class _PsdSpy(QObject):
    def __init__(self, engine: StreamingEngine) -> None:
        super().__init__()
        self.results: list[tuple[str, str, float, int]] = []
        engine.psdReady.connect(self._on_ready, type=Qt.ConnectionType.DirectConnection)

    @Slot(str, str, float, object, object)
    def _on_ready(
        self,
        device: str,
        nslc: str,
        seconds: float,
        freqs: object,
        db: object,
    ) -> None:
        f = freqs if isinstance(freqs, np.ndarray) else np.empty(0)
        self.results.append((device, nslc, seconds, int(f.size)))


def _wait_until(predicate, timeout_s: float, qtbot) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qtbot.wait(50)
        if predicate():
            return True
    return False


@pytest.fixture
def engine_with_device(
    qtbot,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> Iterator[tuple[StreamingEngine, _PsdSpy, str]]:
    nslc = (
        f"{fake_server.config.network}.{fake_server.config.station}."
        f"{fake_server.config.location}.{fake_server.config.channel}"
    )
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="fake",
                host=fake_server.host,
                port=fake_server.port,
                reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
                selectors=[
                    StreamSelectorConfig(
                        network=fake_server.config.network,
                        station=fake_server.config.station,
                        location=fake_server.config.location,
                        channel=fake_server.config.channel,
                    )
                ],
            )
        ]
    )
    engine = StreamingEngine(cfg)
    spy = _PsdSpy(engine)
    engine.start()
    try:
        yield engine, spy, nslc
    finally:
        engine.stop()


def test_psd_request_yields_a_curve(qtbot, engine_with_device) -> None:
    engine, spy, nslc = engine_with_device
    # Wait until the ring buffer has some data.
    assert _wait_until(
        lambda: engine._buffer_for_test("fake", nslc) is not None,
        timeout_s=5.0,
        qtbot=qtbot,
    )
    qtbot.wait(800)  # let a couple of packets accumulate

    engine.psdRequested.emit("fake", nslc, 1.0)

    assert _wait_until(
        lambda: any(r[0] == "fake" and r[1] == nslc for r in spy.results),
        timeout_s=5.0,
        qtbot=qtbot,
    ), f"psdReady never fired; results={spy.results}"
    dev, ns, seconds, nfreq = spy.results[0]
    assert dev == "fake" and ns == nslc
    assert seconds == 1.0
    assert nfreq > 0


def test_psd_request_unknown_stream_yields_no_result(qtbot, engine_with_device) -> None:
    engine, spy, _ = engine_with_device
    qtbot.wait(200)
    engine.psdRequested.emit("nope", "NET.STA.LOC.HHZ", 1.0)
    # Wait long enough for the queued slot to fire and confirm nothing emerged.
    qtbot.wait(500)
    assert not any(r[0] == "nope" for r in spy.results)


@pytest.mark.perf
def test_psd_compute_completes_within_an_order_of_magnitude_of_budget() -> None:
    """Order-of-magnitude regression bound for the Welch compute path.

    Tagged ``perf`` (excluded from the default gate): an absolute
    wall-time compute budget. Run with ``uv run pytest -m perf``.

    The PSD widget's auto-refresh interval is ``max(5 s, window/4)``,
    so any compute under a second is invisible to the user even on
    the longest 1-hour window. We assert a 10x headroom over that
    budget so the test stays green under suite load while still
    catching a real regression (e.g. accidentally re-running an
    O(N²) algorithm) — under suite load Welch on 1 h of 100 Hz data
    is typically ~500 ms; the 10 s assertion is a 20x cushion.
    """
    fs = 100.0
    rng = np.random.default_rng(0)
    long = rng.normal(0.0, 1.0, int(3600 * fs)).astype(np.float64)

    t0 = time.perf_counter()
    freqs, power = welch_psd(long, fs, segment_seconds=8.0)
    elapsed = time.perf_counter() - t0

    assert freqs.size > 0 and power.size > 0
    assert elapsed < 10.0, f"welch on 1 h of 100 Hz data took {elapsed:.2f} s"
