"""Multi-device integration tests for ``StreamingEngine`` (M3 part 1).

These exercise the engine running two fake SeedLink servers in parallel
and assert the device-isolation invariants:

  * Both devices reach CONNECTED and emit ``traceReady`` tagged with
    the right device name.
  * Two devices publishing the same NSLC keep independent ring buffers
    (data does not cross-contaminate).
  * Stopping one device leaves the other streaming; restarting the
    stopped device resumes without interfering.
  * ``engine.stop()`` with N devices completes in roughly the time of
    one device, not N x the time of one.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator

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
from echosmonitor.core.models import DEVICE_KEY_SEP, ConnState, device_stream_key
from echosmonitor.core.streaming_engine import StreamingEngine
from tests.core.fakes import FakeSeedLinkServer, FakeSeedLinkServerConfig
from tests.core.test_seedlink_worker import _LoopThread, loop_thread  # noqa: F401


def _make_root_cfg(devices: list[DeviceConfig]) -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=devices,
    )


def _wait_until(predicate: Callable[[], bool], timeout_s: float, qtbot) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qtbot.wait(50)
        if predicate():
            return True
    return False


def _device_from_server(name: str, server: FakeSeedLinkServer) -> DeviceConfig:
    cfg = server.config
    return DeviceConfig(
        name=name,
        host=server.host,
        port=server.port,
        reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
        selectors=[
            StreamSelectorConfig(
                network=cfg.network,
                station=cfg.station,
                location=cfg.location,
                channel=cfg.channel,
            )
        ],
    )


class _MultiDeviceSpy(QObject):
    """Captures the (device_name, nslc, ...) signal shapes for assertions."""

    def __init__(self, engine: StreamingEngine) -> None:
        super().__init__()
        self.streams_seen: list[tuple[str, str]] = []
        self.device_states: list[tuple[str, int]] = []
        self.coalesced: list[tuple[str, str, int]] = []  # device, nslc, len
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
        self.coalesced.append((device_name, nslc, n))


@pytest.fixture
def make_fake_server(
    loop_thread: _LoopThread,  # noqa: F811  pytest fixture parameter shadows import
) -> Iterator[Callable[..., FakeSeedLinkServer]]:
    """Factory for OS-assigned-port FakeSeedLinkServer instances.

    Multiple servers can be spun up from a single test; teardown stops
    each one regardless of order. ``port=0`` means the kernel picks
    free ports — no port-clash race when several servers run in parallel.
    """
    started: list[FakeSeedLinkServer] = []

    def _factory(cfg: FakeSeedLinkServerConfig) -> FakeSeedLinkServer:
        server = FakeSeedLinkServer(config=cfg)
        loop_thread.submit(server.start()).result(timeout=2.0)
        started.append(server)
        return server

    yield _factory

    for server in started:
        with contextlib.suppress(Exception):
            loop_thread.submit(server.stop()).result(timeout=3.0)


def test_two_devices_distinct_nslc_both_reach_connected_and_emit(qtbot, make_fake_server) -> None:
    """Two fake servers on independent ports, two devices with distinct
    NSLCs. Engine starts → both reach CONNECTED → both produce
    ``traceReady`` signals tagged with the right device name."""
    cfg_a = FakeSeedLinkServerConfig(
        network="IU",
        station="ANMO",
        location="00",
        channel="BHZ",
        sampling_rate=20.0,
        samples_per_record=20,
        packet_interval_s=0.1,
    )
    cfg_b = FakeSeedLinkServerConfig(
        network="IV",
        station="MILN",
        location="",
        channel="HHZ",
        sampling_rate=100.0,
        samples_per_record=50,
        packet_interval_s=0.05,
    )
    server_a = make_fake_server(cfg_a)
    server_b = make_fake_server(cfg_b)

    cfg = _make_root_cfg(
        devices=[
            _device_from_server("dev-a", server_a),
            _device_from_server("dev-b", server_b),
        ]
    )
    engine = StreamingEngine(cfg)
    spy = _MultiDeviceSpy(engine)
    engine.start()
    try:
        nslc_a = "IU.ANMO.00.BHZ"
        nslc_b = "IV.MILN..HHZ"

        def both_connected() -> bool:
            seen = {(d, s) for d, s in spy.device_states if s == int(ConnState.CONNECTED)}
            return ("dev-a", int(ConnState.CONNECTED)) in seen and (
                "dev-b",
                int(ConnState.CONNECTED),
            ) in seen

        assert _wait_until(both_connected, timeout_s=5.0, qtbot=qtbot), (
            f"both devices never reached CONNECTED: states={spy.device_states}"
        )

        def both_emitted() -> bool:
            from_a = any(d == "dev-a" and n == nslc_a for d, n, _ in spy.coalesced)
            from_b = any(d == "dev-b" and n == nslc_b for d, n, _ in spy.coalesced)
            return from_a and from_b

        assert _wait_until(both_emitted, timeout_s=5.0, qtbot=qtbot), (
            f"both devices never emitted: coalesced={spy.coalesced}"
        )

        # Cross-pollination guard: each device's emissions must carry only
        # its own NSLC. If we ever saw "dev-a" emit nslc_b, the bridge
        # device-name stamp leaked across workers.
        for device, nslc, _ in spy.coalesced:
            if device == "dev-a":
                assert nslc == nslc_a, f"dev-a emitted foreign nslc {nslc!r}"
            elif device == "dev-b":
                assert nslc == nslc_b, f"dev-b emitted foreign nslc {nslc!r}"
            else:  # pragma: no cover - guard against future bugs
                pytest.fail(f"unexpected device name {device!r}")
    finally:
        engine.stop()


def test_same_nslc_across_two_devices_buffers_are_independent(qtbot, make_fake_server) -> None:
    """Same NSLC across two devices — both ring buffers exist independently,
    keyed correctly; data does not cross-contaminate.

    Server A pushes a sine wave at amplitude 1000; server B pushes the
    same NSLC at amplitude 200. Each device's ring buffer must hold
    its own waveform — never the other one's.
    """
    nslc = "IU.ANMO.00.BHZ"
    cfg_a = FakeSeedLinkServerConfig(
        network="IU",
        station="ANMO",
        location="00",
        channel="BHZ",
        sine_amplitude=1000.0,
    )
    cfg_b = FakeSeedLinkServerConfig(
        network="IU",
        station="ANMO",
        location="00",
        channel="BHZ",
        sine_amplitude=200.0,
    )
    server_a = make_fake_server(cfg_a)
    server_b = make_fake_server(cfg_b)

    cfg = _make_root_cfg(
        devices=[
            _device_from_server("dev-a", server_a),
            _device_from_server("dev-b", server_b),
        ]
    )
    engine = StreamingEngine(cfg)
    _spy = _MultiDeviceSpy(engine)
    engine.start()
    try:

        def both_buffered() -> bool:
            ba = engine._buffer_for_test("dev-a", nslc)
            bb = engine._buffer_for_test("dev-b", nslc)
            if ba is None or bb is None:
                return False
            _, total_a = ba.read_all()
            _, total_b = bb.read_all()
            # Need enough samples that the sine amplitude is well-resolved.
            return total_a > 100 and total_b > 100

        assert _wait_until(both_buffered, timeout_s=8.0, qtbot=qtbot)

        ba = engine._buffer_for_test("dev-a", nslc)
        bb = engine._buffer_for_test("dev-b", nslc)
        assert ba is not None and bb is not None
        # The two buffers MUST be different objects — same key in two
        # devices would have collided to one buffer.
        assert ba is not bb, "engine collided two devices' streams onto one buffer"

        sa, _ = ba.read_all()
        sb, _ = bb.read_all()
        max_a = float(np.max(np.abs(sa)))
        max_b = float(np.max(np.abs(sb)))
        # Each buffer must hold its source amplitude — generous tolerance
        # to absorb sine sampling jitter and the int32 → float32 cast.
        assert 700.0 < max_a < 1200.0, (
            f"dev-a buffer peak={max_a}, expected ~1000 (B leaked into A?)"
        )
        assert 100.0 < max_b < 300.0, f"dev-b buffer peak={max_b}, expected ~200 (A leaked into B?)"

    finally:
        engine.stop()


def test_stop_one_device_keeps_other_streaming_then_restart(qtbot, make_fake_server) -> None:
    """Stop one device → other keeps streaming. Restart the stopped device
    → resumes without interfering with the running one.

    Locks the device-isolation invariant: a network failure or an
    operator-driven disconnect on device A must not affect device B in
    any way — neither stop B's worker nor lose its in-flight data.
    """
    cfg_a = FakeSeedLinkServerConfig(
        network="IU",
        station="ANMO",
        location="00",
        channel="BHZ",
    )
    cfg_b = FakeSeedLinkServerConfig(
        network="IV",
        station="MILN",
        location="",
        channel="HHZ",
    )
    server_a = make_fake_server(cfg_a)
    server_b = make_fake_server(cfg_b)

    cfg = _make_root_cfg(
        devices=[
            _device_from_server("dev-a", server_a),
            _device_from_server("dev-b", server_b),
        ]
    )
    engine = StreamingEngine(cfg)
    spy = _MultiDeviceSpy(engine)
    engine.start()
    try:
        nslc_a = "IU.ANMO.00.BHZ"
        nslc_b = "IV.MILN..HHZ"

        def both_streaming() -> bool:
            from_a = any(d == "dev-a" and n == nslc_a for d, n, _ in spy.coalesced)
            from_b = any(d == "dev-b" and n == nslc_b for d, n, _ in spy.coalesced)
            return from_a and from_b

        assert _wait_until(both_streaming, timeout_s=5.0, qtbot=qtbot)

        # Stop dev-a only. dev-b's worker, ring buffer, and data flow
        # MUST be untouched.
        engine._stop_device("dev-a")

        # Regression guard for POSTMORTEMS 2026-05-10 entry "Flaky
        # multi-device tests resolved". ``_stop_device`` MUST drop the
        # per-device coalescers; otherwise a flush tick after stop
        # replays buffered packets and the assertion below ("dev-a
        # should be stopped but still emitted N packets") races.
        stale = [k for k in engine._coalescers if k.startswith(f"dev-a{DEVICE_KEY_SEP}")]
        assert not stale, f"dev-a coalescer leaked after stop: {stale!r}"

        # Assert the SCIENCE path, not the render path. ``traceReady``
        # (what ``spy.coalesced`` records) is the best-effort, deferred
        # render signal: CLAUDE.md rule 11. Since b45a627 (M8.1) it is wired
        # ``coalescer.flushed -> traceReady`` via ``QueuedConnection``, so a
        # single render frame produced by the LAST flush before stop can be
        # delivered AFTER ``_stop_device`` removed the coalescer — the
        # disconnect()/deleteLater() in ``_stop_device`` does not purge a
        # meta-call event already posted to the engine's own queue. That
        # one trailing frame is harmless for display but made a
        # ``traceReady`` count an unreliable proxy for "is the device doing
        # work?" (it flaked at ~10% on a 50-iter loop — see POSTMORTEMS
        # 2026-06-01 "Flaky test resurfaced: stop-one-device" and
        # docs/diagnostics/flake-resurface-findings.md).
        #
        # ``_latest_raw_endtime[key]`` is written ONLY in ``_on_packet`` —
        # the worker->engine ingestion hand-off — so it advances iff the
        # device actually ingested a packet. A stopped device must freeze
        # it; a live device must keep advancing it. That is the invariant
        # we care about (rule 10: assert behavior, not mechanism shape).
        a_key = device_stream_key("dev-a", nslc_a)
        b_key = device_stream_key("dev-b", nslc_b)

        # Drain any in-flight ``_on_packet`` deliveries before snapshotting
        # the baseline. The worker->engine hand-off is itself a
        # ``QueuedConnection``, so a packet genuinely received from the
        # socket BEFORE stop may still sit in the engine's event queue;
        # pumping the loop here folds it into the baseline. This is the
        # legitimate, bounded tail of a stop — distinct from the
        # timer-driven REPLAY the ``assert not stale`` guard above rules
        # out. ``_stop_device`` has already joined dev-a's worker thread,
        # so no NEW dev-a packets can arrive during or after this drain.
        qtbot.wait(200)
        a_end_baseline = engine._latest_raw_endtime.get(a_key)
        b_end_baseline = engine._latest_raw_endtime.get(b_key)
        assert a_end_baseline is not None, "dev-a never ingested before stop"
        assert b_end_baseline is not None, "dev-b never ingested before stop"

        # Give dev-b about 800 ms of headroom; at refresh_hz=20 that's
        # ~16 packets worth of streaming activity.
        qtbot.wait(800)

        assert engine._latest_raw_endtime.get(a_key) == a_end_baseline, (
            "dev-a is stopped but its ingestion endtime advanced: "
            f"{a_end_baseline} -> {engine._latest_raw_endtime.get(a_key)} "
            "(a stopped device must not ingest further data)"
        )
        assert engine._latest_raw_endtime.get(b_key) > b_end_baseline, (
            "dev-b should keep streaming but its ingestion endtime did not "
            f"advance past {b_end_baseline}"
        )

        # Restart dev-a; capture each device's render-emission count *at the
        # moment of restart* so the post-restart assertion compares against
        # a fresh baseline. The render path (``spy.coalesced``) is the right
        # observable HERE because this is a POSITIVE eventual-consistency
        # wait (not a strict negative): a deferred QueuedConnection frame
        # only delays delivery, it never makes a wait-until-true flake.
        # NOTE: ``spy.coalesced`` is deliberately NOT cleared (we no longer
        # count post-stop frames), so dev-a's resumption is detected as a
        # COUNT INCREASE past the restart baseline, not as mere presence.
        a_count_at_restart = sum(1 for d, _, _ in spy.coalesced if d == "dev-a")
        b_count_at_restart = sum(1 for d, _, _ in spy.coalesced if d == "dev-b")
        engine._start_device_by_name("dev-a")

        # Wait for BOTH dev-a to have resumed AND dev-b to have produced at
        # least one new emission past the restart baseline. Checking only
        # ``dev_a_resumed`` lost a flake race: dev-a's first packet can
        # land in a flush cycle where dev-b's coalescer is momentarily
        # empty (its next packet hadn't arrived yet), so the predicate
        # would become True while dev-b's count was unchanged. Waiting
        # for both signals removes the race and still guards the
        # "devices are isolated" invariant we care about.
        def both_active_after_restart() -> bool:
            a_resumed = sum(1 for d, _, _ in spy.coalesced if d == "dev-a") > a_count_at_restart
            b_advanced = sum(1 for d, _, _ in spy.coalesced if d == "dev-b") > b_count_at_restart
            return a_resumed and b_advanced

        assert _wait_until(both_active_after_restart, timeout_s=5.0, qtbot=qtbot), (
            "dev-a never resumed OR dev-b stopped emitting after dev-a restart "
            "— devices are not isolated"
        )
    finally:
        engine.stop()


def test_engine_stop_with_two_devices_completes_within_2s(qtbot, make_fake_server) -> None:
    """``engine.stop()`` with N devices must run in roughly the time of one,
    not N x the time of one.

    The two workers' ``stop()`` calls are scheduled on helper threads so
    their internal ``_run_done.wait`` deadlines run in parallel; the
    engine then quits the QThreads concurrently. Without that
    parallelisation, two pathologically slow devices would exceed the
    2 s budget on this assertion.
    """
    cfg_a = FakeSeedLinkServerConfig(
        network="IU",
        station="ANMO",
        location="00",
        channel="BHZ",
    )
    cfg_b = FakeSeedLinkServerConfig(
        network="IV",
        station="MILN",
        location="",
        channel="HHZ",
    )
    server_a = make_fake_server(cfg_a)
    server_b = make_fake_server(cfg_b)

    cfg = _make_root_cfg(
        devices=[
            _device_from_server("dev-a", server_a),
            _device_from_server("dev-b", server_b),
        ]
    )
    engine = StreamingEngine(cfg)
    spy = _MultiDeviceSpy(engine)
    engine.start()
    try:
        # Make sure both workers are actually doing work before timing
        # the stop — otherwise the bound is meaningless.
        assert _wait_until(
            lambda: (
                any(d == "dev-a" for d, _, _ in spy.coalesced)
                and any(d == "dev-b" for d, _, _ in spy.coalesced)
            ),
            timeout_s=5.0,
            qtbot=qtbot,
        )
    finally:
        t0 = time.monotonic()
        engine.stop()
        elapsed = time.monotonic() - t0
        assert elapsed <= 2.0, (
            f"engine.stop() took {elapsed:.3f}s with two devices, "
            "exceeds 2s budget — workers are stopping sequentially"
        )
