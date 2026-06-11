"""Integration tests for the engine's hot-reload path (M4 stage B).

Boots a real :class:`StreamingEngine` with a real :class:`ConfigStore`
and a fake SeedLink server, then exercises the four diff buckets via
store mutations:

* added       -> worker spawned
* removed     -> worker stopped
* chain_only  -> worker NOT recycled (per-device-id check)
* restart     -> worker recycled (id changes)

Reuses the ``make_fake_server`` fixture pattern from
``test_streaming_engine_multi.py``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from echosmonitor.config.schema import (
    AppConfig,
    BandpassStage,
    DetrendStage,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.core.models import AcquisitionState, ConnState
from echosmonitor.core.streaming_engine import StreamingEngine
from tests.core.fakes import FakeSeedLinkServer, FakeSeedLinkServerConfig
from tests.core.test_seedlink_worker import _LoopThread, loop_thread  # noqa: F401
from tests.core.test_streaming_engine_multi import (
    _device_from_server,
    make_fake_server,  # noqa: F401  pytest fixture re-export
)


def _wait_until(predicate: Callable[[], bool], timeout_s: float, qtbot) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qtbot.wait(50)
        if predicate():
            return True
    return False


def _make_root_cfg(devices: list[DeviceConfig]) -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=devices,
    )


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"


def test_add_device_via_store_registers_idle_until_user_starts(
    qtbot,
    make_fake_server,  # noqa: F811
    store_path,
) -> None:
    """``store.add_device`` on a running engine registers the device IDLE
    — no worker, no connection — until the user explicitly starts it
    (CLAUDE.md rule 13: nothing starts without the user). M2-A
    consciously rewrote the original autostart expectation of this test.

    Still demonstrates the ``added`` diff bucket reaching the engine via
    the queued ``configChanged`` -> ``_on_config_changed`` path: the
    device becomes startable by name, and ``start_monitoring`` then
    brings it to CONNECTED.
    """
    server = make_fake_server(
        FakeSeedLinkServerConfig(network="IU", station="ANMO", location="00", channel="BHZ")
    )
    cfg = _make_root_cfg(devices=[])
    store = ConfigStore(cfg, store_path)
    engine = StreamingEngine(cfg, store=store)
    engine.start()
    try:
        device_cfg = _device_from_server("dev-a", server)
        store.add_device(device_cfg)

        # Wait until the queued diff lands (the engine's device view
        # advances), then assert the device did NOT start.
        assert _wait_until(
            lambda: any(d.name == "dev-a" for d in engine.devices()),
            timeout_s=2.0,
            qtbot=qtbot,
        ), "added device never reached the engine's device view"
        assert engine.acquisition_state("dev-a") is AcquisitionState.IDLE
        assert "dev-a" not in engine._workers, "added device autostarted (rule 13 violation)"

        # The user starts it: now it connects.
        engine.start_monitoring("dev-a")

        def reached_connected() -> bool:
            status = engine.device_status().get("dev-a")
            return status is not None and status.state == ConnState.CONNECTED

        assert _wait_until(reached_connected, timeout_s=5.0, qtbot=qtbot), (
            f"dev-a never reached CONNECTED after start_monitoring: {engine.device_status()}"
        )
    finally:
        engine.stop()


def test_remove_device_via_store_stops_worker(qtbot, make_fake_server, store_path) -> None:  # noqa: F811
    """Calling ``store.remove_device`` tears down the corresponding worker."""
    server = make_fake_server(
        FakeSeedLinkServerConfig(network="IU", station="ANMO", location="00", channel="BHZ")
    )
    cfg = _make_root_cfg(devices=[_device_from_server("dev-a", server)])
    store = ConfigStore(cfg, store_path)
    engine = StreamingEngine(cfg, store=store)
    engine.start()
    try:
        # Wait for CONNECTED before removing — otherwise the test could
        # pass by virtue of the worker never having started, not because
        # the diff path actually tore it down.
        assert _wait_until(
            lambda: (
                engine.device_status().get("dev-a") is not None
                and engine.device_status()["dev-a"].state == ConnState.CONNECTED
            ),
            timeout_s=5.0,
            qtbot=qtbot,
        )
        store.remove_device("dev-a")

        # The diff path runs on a queued connection, so we have to wait
        # for the engine event loop to dispatch it. Up to 2 s is plenty
        # for a local fake-server stop(). We assert against
        # ``engine._workers`` (the lifecycle dict the diff actually
        # mutates) rather than ``device_status()`` — the latter retains
        # the last-known status snapshot for telemetry purposes even
        # after the worker has been torn down, so it is not a reliable
        # signal that the worker stopped.
        assert _wait_until(
            lambda: "dev-a" not in engine._workers,
            timeout_s=2.0,
            qtbot=qtbot,
        ), f"dev-a worker still alive after remove: {list(engine._workers)}"
    finally:
        engine.stop()


def test_chain_only_change_does_not_restart_socket(
    qtbot,
    make_fake_server,  # noqa: F811
    store_path,
) -> None:
    """Changing only the dsp_chain reuses the existing worker AND swaps the chain.

    Two assertions matter, NOT just one:

    1. The worker object's identity is preserved (chain_only branch
       was taken, not restart).
    2. The router's installed chain instance was actually replaced
       with a fresh one matching the new config — and a packet
       processed by it reaches the engine via
       ``processedTraceReady``.

    Earlier draft asserted only (1). Code-reviewer caught that under
    that assertion alone, ``_reinstall_chain`` could silently leave
    DSP off (CRITICAL #1 in the M4 stage B review): the old chain
    state was cleared but the new one was never installed because
    ``_maybe_install_chain`` only fires from the "first packet for
    this stream" branch in ``_on_packet`` — which never re-fires for
    a stream whose buffer already exists.
    """
    from echosmonitor.core.models import device_stream_key
    from echosmonitor.dsp.stages import Bandpass

    server = make_fake_server(
        FakeSeedLinkServerConfig(network="IU", station="ANMO", location="00", channel="BHZ")
    )
    initial_chain = [DetrendStage(type="detrend")]
    new_chain = [
        DetrendStage(type="detrend"),
        BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0),
    ]
    base = _device_from_server("dev-a", server)
    initial_device = base.model_copy(update={"dsp_chain": initial_chain})
    cfg = _make_root_cfg(devices=[initial_device])
    store = ConfigStore(cfg, store_path)
    engine = StreamingEngine(cfg, store=store)
    engine.start()
    try:
        assert _wait_until(
            lambda: (
                engine.device_status().get("dev-a") is not None
                and engine.device_status()["dev-a"].state == ConnState.CONNECTED
            ),
            timeout_s=5.0,
            qtbot=qtbot,
        )
        # Wait until the FIRST packet has built the initial chain so
        # the assertion below ("after update, chain instance is fresh")
        # has a meaningful before-snapshot.
        nslc = "IU.ANMO.00.BHZ"
        composite_key = device_stream_key("dev-a", nslc)
        assert _wait_until(
            lambda: composite_key in engine._dsp_router._chains,
            timeout_s=3.0,
            qtbot=qtbot,
        ), "initial chain never installed — first packet for the stream did not arrive"
        chain_id_before = id(engine._dsp_router._chains[composite_key])
        worker_id_before = id(engine._workers["dev-a"])

        # Same host/port/selectors, only the chain changes.
        store.update_device("dev-a", initial_device.model_copy(update={"dsp_chain": new_chain}))
        # Give the queued diff a chance to run AND ``_reinstall_chain``
        # to rebuild the chain synchronously.
        qtbot.wait(300)

        # (1) Worker identity preserved — chain_only branch taken.
        assert "dev-a" in engine._workers
        assert id(engine._workers["dev-a"]) == worker_id_before, (
            "chain-only update unexpectedly recycled the worker"
        )
        # (2) Chain instance replaced — the router holds a *different*
        # DspChain object now, and that chain has the new Bandpass stage
        # the test just installed.
        assert composite_key in engine._dsp_router._chains, (
            "chain dropped without reinstall — DSP would be silent"
        )
        assert id(engine._dsp_router._chains[composite_key]) != chain_id_before, (
            "chain-only update did not actually swap the DspChain instance"
        )
        new_stages = engine._dsp_router._chains[composite_key].stages
        assert any(isinstance(s, Bandpass) for s in new_stages), (
            f"new chain missing Bandpass stage; got {[type(s).__name__ for s in new_stages]}"
        )
        # (3) The new chain processes packets — proves the chain is
        # actually wired into the data path, not just sitting in a
        # dict. Wait for at least one ``processedTraceReady`` after
        # the swap so the assertion isn't satisfied by a stale emit.
        with qtbot.waitSignal(engine.processedTraceReady, timeout=5000) as blocker:
            pass
        device_name, emitted_nslc, _samples = blocker.args
        assert device_name == "dev-a"
        assert emitted_nslc == nslc
    finally:
        engine.stop()


def test_host_change_triggers_restart(qtbot, make_fake_server, store_path) -> None:  # noqa: F811
    """Mutating the device's host through the store recycles the worker."""
    # Two distinct fake servers; we'll switch the device from server A's
    # host:port to server B's host:port.
    cfg_a = FakeSeedLinkServerConfig(network="IU", station="ANMO", location="00", channel="BHZ")
    cfg_b = FakeSeedLinkServerConfig(network="IU", station="ANMO", location="00", channel="BHZ")
    server_a: FakeSeedLinkServer = make_fake_server(cfg_a)
    server_b: FakeSeedLinkServer = make_fake_server(cfg_b)
    initial_device = DeviceConfig(
        name="dev-a",
        host=server_a.host,
        port=server_a.port,
        reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
        selectors=[
            StreamSelectorConfig(
                network=cfg_a.network,
                station=cfg_a.station,
                location=cfg_a.location,
                channel=cfg_a.channel,
            )
        ],
    )
    cfg = _make_root_cfg(devices=[initial_device])
    store = ConfigStore(cfg, store_path)
    engine = StreamingEngine(cfg, store=store)
    engine.start()
    try:
        assert _wait_until(
            lambda: (
                engine.device_status().get("dev-a") is not None
                and engine.device_status()["dev-a"].state == ConnState.CONNECTED
            ),
            timeout_s=5.0,
            qtbot=qtbot,
        )
        worker_id_before = id(engine._workers["dev-a"])
        # Repoint the device at server B.
        store.update_device(
            "dev-a",
            initial_device.model_copy(update={"host": server_b.host, "port": server_b.port}),
        )

        def worker_recycled() -> bool:
            new_worker = engine._workers.get("dev-a")
            return new_worker is not None and id(new_worker) != worker_id_before

        assert _wait_until(worker_recycled, timeout_s=3.0, qtbot=qtbot), (
            "host change did not recycle the worker — restart bucket was not applied"
        )
    finally:
        engine.stop()


def test_host_change_on_idle_device_does_not_start_it(
    qtbot,
    make_fake_server,  # noqa: F811
    store_path,
) -> None:
    """The ``restart`` diff bucket only restarts what the user has
    running. A device the user stopped absorbs the new config silently
    and connects to the NEW endpoint when monitoring resumes (M2-A,
    rule 13)."""
    cfg_a = FakeSeedLinkServerConfig(network="IU", station="ANMO", location="00", channel="BHZ")
    cfg_b = FakeSeedLinkServerConfig(network="IU", station="ANMO", location="00", channel="BHZ")
    server_a: FakeSeedLinkServer = make_fake_server(cfg_a)
    server_b: FakeSeedLinkServer = make_fake_server(cfg_b)
    initial_device = _device_from_server("dev-a", server_a)
    cfg = _make_root_cfg(devices=[initial_device])
    store = ConfigStore(cfg, store_path)
    engine = StreamingEngine(cfg, store=store)
    engine.start()
    try:
        assert _wait_until(
            lambda: (
                engine.device_status().get("dev-a") is not None
                and engine.device_status()["dev-a"].state == ConnState.CONNECTED
            ),
            timeout_s=5.0,
            qtbot=qtbot,
        )
        engine.stop("dev-a")  # user stops it → Idle
        assert "dev-a" not in engine._workers

        store.update_device(
            "dev-a",
            initial_device.model_copy(update={"host": server_b.host, "port": server_b.port}),
        )
        # Wait for the queued diff to land (engine view shows the new port),
        # then assert no worker was spawned.
        assert _wait_until(
            lambda: any(d.name == "dev-a" and d.port == server_b.port for d in engine.devices()),
            timeout_s=2.0,
            qtbot=qtbot,
        )
        assert "dev-a" not in engine._workers, "restart bucket started an idle device"
        assert engine.acquisition_state("dev-a") is AcquisitionState.IDLE

        # Resuming monitoring connects to the NEW endpoint — the idle
        # device really absorbed the config change.
        engine.start_monitoring("dev-a")
        assert _wait_until(
            lambda: (
                engine.device_status().get("dev-a") is not None
                and engine.device_status()["dev-a"].state == ConnState.CONNECTED
            ),
            timeout_s=5.0,
            qtbot=qtbot,
        )
        assert engine._workers["dev-a"]._port == server_b.port
    finally:
        engine.stop()


def test_config_diff_posted_before_stop_does_not_apply_mid_stop(
    qtbot,
    make_fake_server,  # noqa: F811
    store_path,
    capture_structlog,
) -> None:
    """A queued configChanged posted just before engine.stop() must NOT
    apply its diff inside stop()'s processEvents barrier — that would
    spawn a fresh worker thread the remaining teardown then orphans
    (qt-concurrency-auditor F5 on the M2-C diff; the early
    ``_started = False`` flip closes the window)."""
    cfg_a = FakeSeedLinkServerConfig(network="IU", station="ANMO", location="00", channel="BHZ")
    server_a: FakeSeedLinkServer = make_fake_server(cfg_a)
    initial_device = _device_from_server("dev-a", server_a)
    cfg = _make_root_cfg(devices=[initial_device])
    store = ConfigStore(cfg, store_path)
    engine = StreamingEngine(cfg, store=store)
    engine.start()
    try:
        assert _wait_until(
            lambda: (
                engine.device_status().get("dev-a") is not None
                and engine.device_status()["dev-a"].state == ConnState.CONNECTED
            ),
            timeout_s=5.0,
            qtbot=qtbot,
        )
        # Post the queued configChanged, then stop in the SAME event-loop
        # turn so the diff can only ever dispatch inside stop()'s
        # processEvents barrier.
        store.update_device(
            "dev-a",
            initial_device.model_copy(update={"port": initial_device.port + 1}),
        )
        engine.stop()
        diff_events = [
            r for r in capture_structlog if r.get("event") == "streaming_engine_config_diff"
        ]
        assert diff_events == [], "config diff applied mid-stop (orphaned-worker window)"
        assert engine._workers == {}
        assert engine._threads == {}
    finally:
        engine.stop()
