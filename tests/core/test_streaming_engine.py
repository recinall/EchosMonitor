"""Integration tests for `StreamingEngine`."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QObject, Qt, Slot

from echosmonitor.config.schema import (
    AppConfig,
    BandpassStage,
    DetrendStage,
    DeviceConfig,
    HighpassStage,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import ConnState, device_stream_key
from echosmonitor.core.streaming_engine import StreamingEngine
from echosmonitor.dsp.stages import Bandpass, Highpass
from tests.core.fakes import FakeSeedLinkServer
from tests.core.test_seedlink_worker import _LoopThread, fake_server, loop_thread  # noqa: F401


def _make_root_cfg(devices: list[DeviceConfig]) -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=devices,
    )


class _EngineSpy(QObject):
    """Captures the post-M3 (device_name, nslc, ...) signal shapes.

    Pre-M3, every per-stream signal carried only ``nslc``. Multi-device
    isolation makes ``(device_name, nslc)`` the stream identity — this
    spy records both halves so tests can assert which device produced
    each event.
    """

    def __init__(self, engine: StreamingEngine) -> None:
        super().__init__()
        self.streams_seen: list[tuple[str, str]] = []
        self.device_states: list[tuple[str, int]] = []
        self.coalesced_emissions: list[tuple[str, str, int]] = []  # (device, nslc, len)
        engine.newStreamSeen.connect(self._on_stream, type=Qt.ConnectionType.DirectConnection)
        engine.deviceStateChanged.connect(self._on_state, type=Qt.ConnectionType.DirectConnection)
        engine.traceReady.connect(self._on_trace, type=Qt.ConnectionType.DirectConnection)

    @Slot(str, str)
    def _on_stream(self, device_name: str, nslc: str) -> None:
        self.streams_seen.append((device_name, nslc))

    @Slot(str, int)
    def _on_state(self, name: str, state: int) -> None:
        self.device_states.append((name, state))

    @Slot(str, str, object)
    def _on_trace(self, device_name: str, nslc: str, samples: object) -> None:
        n = len(samples) if hasattr(samples, "__len__") else 0
        self.coalesced_emissions.append((device_name, nslc, n))


def _wait_until(predicate, timeout_s: float, qtbot) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qtbot.wait(50)
        if predicate():
            return True
    return False


def test_empty_devices_starts_and_stops_cleanly(qtbot) -> None:
    cfg = _make_root_cfg(devices=[])
    engine = StreamingEngine(cfg)
    engine.start()
    qtbot.wait(50)
    engine.stop()
    # No assertion error = pass.


def test_idempotent_start_and_stop(qtbot) -> None:
    cfg = _make_root_cfg(devices=[])
    engine = StreamingEngine(cfg)
    engine.start()
    engine.start()  # no-op
    engine.stop()
    engine.stop()  # no-op
    qtbot.wait(20)


def _seed_connected_silent(
    engine: StreamingEngine, name: str, *, expected_interval_s: float, silent_s: float
) -> None:
    """Put a device into CONNECTED + silent-for-``silent_s`` watchdog state."""
    from echosmonitor.core.models import DeviceStatus

    engine._status[name] = DeviceStatus(name=name, state=ConnState.CONNECTED)
    engine._expected_packet_interval_s[name] = expected_interval_s
    engine._last_packet_monotonic[name] = time.monotonic() - silent_s
    engine._last_stall_scan_s = 0.0  # bypass the ~1 Hz scan throttle


def test_stall_watchdog_flags_silent_connected_stream(qtbot) -> None:
    """A CONNECTED stream silent past its expected cadence is flagged (Bug 2).

    expected interval 1 s → threshold = clamp(12x1, 5, 60) = 12 s; silent 30 s.
    """
    engine = StreamingEngine(_make_root_cfg([]))
    _seed_connected_silent(engine, "dev", expected_interval_s=1.0, silent_s=30.0)
    with qtbot.waitSignal(engine.streamStalled, timeout=1000) as blocker:
        engine._scan_stalls()
    assert blocker.args == ["dev", True]
    assert "dev" in engine._stalled
    # Idempotent: a second scan does not re-emit for an already-flagged device.
    engine._last_stall_scan_s = 0.0
    with qtbot.assertNotEmitted(engine.streamStalled):
        engine._scan_stalls()


def test_stall_threshold_scales_with_sampling_rate(qtbot) -> None:
    """The threshold adapts to the stream's cadence ('watchdog intelligente'):
    the SAME silence flags a fast stream but not a slow one (Bug 2)."""
    # Slow stream: interval 5 s → threshold clamp(60, 5, 60) = 60 s; silent 40 s → OK.
    slow = StreamingEngine(_make_root_cfg([]))
    _seed_connected_silent(slow, "slow", expected_interval_s=5.0, silent_s=40.0)
    slow._scan_stalls()
    assert "slow" not in slow._stalled

    # Fast stream: interval 0.5 s → threshold clamp(6, 5, 60) = 6 s; silent 40 s → stalled.
    fast = StreamingEngine(_make_root_cfg([]))
    _seed_connected_silent(fast, "fast", expected_interval_s=0.5, silent_s=40.0)
    fast._scan_stalls()
    assert "fast" in fast._stalled


def test_stall_watchdog_ignores_non_connected(qtbot) -> None:
    """A device that is not CONNECTED is never flagged (no stream to stall)."""
    engine = StreamingEngine(_make_root_cfg([]))
    _seed_connected_silent(engine, "dev", expected_interval_s=1.0, silent_s=999.0)
    engine._status["dev"].state = ConnState.WAITING_RETRY
    with qtbot.assertNotEmitted(engine.streamStalled):
        engine._scan_stalls()
    assert "dev" not in engine._stalled


@pytest.fixture
def engine_with_one_fake_device(
    qtbot,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> Iterator[tuple[StreamingEngine, _EngineSpy, str]]:
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
            ),
        ]
    )
    engine = StreamingEngine(cfg)
    spy = _EngineSpy(engine)
    engine.start()
    try:
        yield engine, spy, nslc
    finally:
        engine.stop()


def test_engine_observes_new_stream_and_fills_ring_buffer(
    qtbot,
    engine_with_one_fake_device,
) -> None:
    engine, spy, nslc = engine_with_one_fake_device

    assert _wait_until(
        lambda: any(state == int(ConnState.CONNECTED) for _, state in spy.device_states),
        timeout_s=3.0,
        qtbot=qtbot,
    ), f"never CONNECTED; states={spy.device_states}"

    assert _wait_until(
        lambda: ("fake", nslc) in spy.streams_seen,
        timeout_s=3.0,
        qtbot=qtbot,
    ), f"newStreamSeen never fired for fake/{nslc}; got {spy.streams_seen}"

    # Ring buffer has data
    rb = engine._buffer_for_test("fake", nslc)
    assert rb is not None
    samples_in_rb, total = rb.read_all()
    assert total > 0
    assert samples_in_rb.dtype.name == "float32"


def test_engine_coalesces_trace_ready_at_refresh_hz(
    qtbot,
    engine_with_one_fake_device,
) -> None:
    _engine, spy, nslc = engine_with_one_fake_device

    assert _wait_until(
        lambda: any(e[0] == "fake" and e[1] == nslc for e in spy.coalesced_emissions),
        timeout_s=3.0,
        qtbot=qtbot,
    )

    # Sample over a 1-second window.
    spy.coalesced_emissions.clear()
    qtbot.wait(1000)
    n = sum(1 for e in spy.coalesced_emissions if e[0] == "fake" and e[1] == nslc)
    # refresh_hz=20 → 20 emissions per second nominal; allow ±50% slop for CI.
    assert 5 <= n <= 35, f"got {n} coalesced emissions in 1s, expected ~20"


def test_engine_uses_single_shared_flush_timer(
    qtbot,
    engine_with_one_fake_device,
) -> None:
    """Regression: per-stream QTimers were collapsed into one engine-owned
    timer. Verify the timer is wired to `_flush_all` and that coalescers
    don't carry their own timers any more."""
    engine, _spy, nslc = engine_with_one_fake_device
    key = device_stream_key("fake", nslc)

    assert _wait_until(
        lambda: key in engine._coalescers,
        timeout_s=3.0,
        qtbot=qtbot,
    )

    assert engine._flush_timer.isActive(), "engine flush timer should be running while started"

    for coalescer in engine._coalescers.values():
        # Coalescers must not own their own QTimers any more.
        assert not hasattr(coalescer, "_timer"), (
            "coalescer should no longer own a per-stream QTimer"
        )


@pytest.fixture
def engine_with_chain_device(
    qtbot,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> Iterator[tuple[StreamingEngine, _EngineSpy, str]]:
    """Same as `engine_with_one_fake_device` but the device has a DSP chain."""
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
                dsp_chain=[
                    DetrendStage(type="detrend", kind="constant"),
                    BandpassStage(
                        type="bandpass",
                        freqmin=1.0,
                        freqmax=10.0,
                        corners=4,
                        zerophase=False,
                    ),
                ],
            ),
        ]
    )
    engine = StreamingEngine(cfg)
    spy = _EngineSpy(engine)
    # processedTraceReady is engine's signal; collect emissions for assertions.
    spy.processed_emissions: list[tuple[str, str, int]] = []  # type: ignore[attr-defined]

    def on_processed(device_name: str, nslc: str, samples: object) -> None:
        n = len(samples) if hasattr(samples, "__len__") else 0
        spy.processed_emissions.append((device_name, nslc, n))  # type: ignore[attr-defined]

    engine.processedTraceReady.connect(on_processed, type=Qt.ConnectionType.DirectConnection)
    engine.start()
    try:
        yield engine, spy, nslc
    finally:
        engine.stop()


def test_engine_emits_processed_trace_ready_when_chain_configured(
    qtbot,
    engine_with_chain_device,
) -> None:
    """A device with a non-empty `dsp_chain` causes the engine to install
    a chain on first packet and emit `processedTraceReady` for that
    stream."""
    _engine, spy, nslc = engine_with_chain_device

    # Wait for at least one processed emission to land.
    assert _wait_until(
        lambda: any(
            e[0] == "fake" and e[1] == nslc and e[2] > 0
            for e in spy.processed_emissions  # type: ignore[attr-defined]
        ),
        timeout_s=5.0,
        qtbot=qtbot,
    ), (
        f"processedTraceReady never fired for fake/{nslc}; saw {spy.processed_emissions}"  # type: ignore[attr-defined]
    )


def test_engine_restart_with_different_chain_does_not_leak_old_stages(
    qtbot,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> None:
    """Defense-in-depth for the M3 stale-chain regression.

    Drive the same `StreamingEngine` instance through two start/stop
    cycles with different configs (bandpass first, highpass second) and
    assert the second cycle's active chain reflects the new config —
    no Bandpass stage from the previous lifecycle should leak through.
    The engine carries its router and chain dict across cycles, so this
    is the path where a missing `_clearChainsRequested` would actually
    bite. If clear-chains ever stops firing on `stop()`, this fails.
    """
    nslc = (
        f"{fake_server.config.network}.{fake_server.config.station}."
        f"{fake_server.config.location}.{fake_server.config.channel}"
    )
    key = device_stream_key("fake", nslc)

    def _device(stage: object) -> DeviceConfig:
        return DeviceConfig(
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
            dsp_chain=[stage],  # type: ignore[list-item]
        )

    cfg_first = _make_root_cfg(
        devices=[_device(BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0))]
    )
    engine = StreamingEngine(cfg_first)
    engine.start()
    try:
        # Wait on the router dict directly — ``_chain_installed`` is set on
        # the engine thread before the queued ``install_chain`` slot fires
        # on the DSP thread, so reading the dict via the intent flag is
        # racy. M6 widened the race by adding extra queued slots
        # (`install_for`, `reinstall_for`) ahead of `install_chain` in the
        # same dispatch queue.
        assert _wait_until(
            lambda: key in engine._dsp_router._chains,
            timeout_s=5.0,
            qtbot=qtbot,
        ), "first chain never got installed"
        first_chain = engine._dsp_router._chains.get(key)
        assert first_chain is not None
        assert any(isinstance(s, Bandpass) for s in first_chain.stages)
    finally:
        engine.stop()

    # After stop(), `_clearChainsRequested` must have drained the router's
    # chain dict — this is the invariant the test enforces.
    assert engine._dsp_router._chains == {}, (
        "router still holds a chain after stop() — _clearChainsRequested"
        " did not propagate before the dsp thread quit"
    )

    # TODO(M4): replace these test-only mutations with the public
    # reconfigure method that M4 will introduce; until then, poke `_cfg`
    # and `_device_dsp_cfg` directly to drive a second start/stop cycle
    # with a different chain shape.
    engine._cfg = _make_root_cfg(devices=[_device(HighpassStage(type="highpass", freq=2.0))])
    engine._device_dsp_cfg = {dev.name: list(dev.dsp_chain) for dev in engine._cfg.devices}
    engine.start()
    try:
        assert _wait_until(
            lambda: key in engine._dsp_router._chains,
            timeout_s=5.0,
            qtbot=qtbot,
        ), "second chain never got installed"
        second_chain = engine._dsp_router._chains.get(key)
        assert second_chain is not None
        second_stages = list(second_chain.stages)
        assert any(isinstance(s, Highpass) for s in second_stages)
        assert not any(isinstance(s, Bandpass) for s in second_stages), (
            "old Bandpass stage leaked into the new chain after restart"
        )
    finally:
        engine.stop()
