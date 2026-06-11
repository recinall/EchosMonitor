"""M2-A — per-device acquisition lifecycle on ``StreamingEngine``.

Rule 13 end to end at the engine level: nothing starts without the
user; three explicit per-device states Idle → Monitoring → Recording;
archive writers exist ONLY in the Recording state (never created from
``archive.enabled`` config); stop is always immediate within the
rule-7 bounds.

The fake SeedLink server feeds real ObsPy traces so the
zero-disk-writes assertions run against the genuine data path.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Qt, Slot

from echosmonitor.config.schema import (
    AppConfig,
    ArchiveConfig,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import AcquisitionState, ConnState
from echosmonitor.core.streaming_engine import StreamingEngine
from tests.core.fakes import FakeSeedLinkServer, FakeSeedLinkServerConfig
from tests.core.test_seedlink_worker import _LoopThread, loop_thread  # noqa: F401
from tests.core.test_streaming_engine_multi import (
    make_fake_server,  # noqa: F401  pytest fixture re-export
)


def _make_root_cfg(devices: list[DeviceConfig], *, archive_root: Path | None = None) -> RootConfig:
    return RootConfig(
        app=AppConfig(archive_root=archive_root),
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


def _device_from_server(
    name: str,
    server: FakeSeedLinkServer,
    *,
    archive: ArchiveConfig | None = None,
) -> DeviceConfig:
    cfg = server.config
    kwargs: dict[str, object] = {}
    if archive is not None:
        kwargs["archive"] = archive
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
        **kwargs,  # type: ignore[arg-type]
    )


_SERVER_CFG = FakeSeedLinkServerConfig(
    network="IU",
    station="ANMO",
    location="00",
    channel="BHZ",
    sampling_rate=20.0,
    samples_per_record=20,
    packet_interval_s=0.1,
)

# Tight fsync (schema minimum) so recording tests don't wait long;
# enabled=True on purpose in the monitoring tests — it must NOT matter
# (rule 13).
_TIGHT_ARCHIVE = ArchiveConfig(enabled=True, fsync_interval_s=0.5, queue_max=256)


class _StateSpy(QObject):
    """Collects ``acquisitionStateChanged`` emissions in order."""

    def __init__(self, engine: StreamingEngine) -> None:
        super().__init__()
        self.transitions: list[tuple[str, int]] = []
        engine.acquisitionStateChanged.connect(
            self._on_state, type=Qt.ConnectionType.DirectConnection
        )

    @Slot(str, int)
    def _on_state(self, name: str, state: int) -> None:
        self.transitions.append((name, state))


def _connected(engine: StreamingEngine, name: str) -> bool:
    status = engine.device_status().get(name)
    return status is not None and status.state == ConnState.CONNECTED


def _has_archive_file(root: Path) -> bool:
    return root.exists() and any(p.is_file() for p in root.rglob("*.D.*"))


# ---------------------------------------------------------------------------
# Launch: nothing connects
# ---------------------------------------------------------------------------


def test_construct_does_not_connect_or_start_anything(qtbot, tmp_path: Path) -> None:
    """Constructing the engine with configured devices starts NOTHING:
    no workers, no DSP thread, no flush timer, no disk writes (rule 13)."""
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="dev",
                host="127.0.0.1",
                port=1,
                reconnect=ReconnectConfig(initial_delay_s=3600.0, max_delay_s=3600.0),
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
                archive=_TIGHT_ARCHIVE,
            )
        ],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    qtbot.wait(300)
    assert not engine._started
    assert engine._workers == {}
    assert engine._archive_writers == {}
    assert not engine._dsp_thread.isRunning()
    assert not engine._flush_timer.isActive()
    assert engine.acquisition_state("dev") is AcquisitionState.IDLE
    assert not archive_root.exists()
    # Global stop on a never-started engine is a safe no-op.
    engine.stop()


# ---------------------------------------------------------------------------
# Monitoring: live traces, zero disk writes
# ---------------------------------------------------------------------------


def test_start_monitoring_streams_with_zero_disk_writes(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    """Monitoring shows live data and writes NOTHING to disk, even with
    ``archive.enabled=True`` in the config — the writer is a Recording-
    state artifact only."""
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[_device_from_server("dev", server, archive=_TIGHT_ARCHIVE)],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    spy = _StateSpy(engine)
    engine.start_monitoring("dev")
    try:
        assert engine.acquisition_state("dev") is AcquisitionState.MONITORING
        assert spy.transitions == [("dev", int(AcquisitionState.MONITORING))]
        with qtbot.waitSignal(engine.traceReady, timeout=10_000):
            pass
        # Live samples reached the ring buffer...
        samples, fs, _t = engine.read_recent("dev", "IU.ANMO.00.BHZ", 5.0)
        assert fs > 0 and samples.size > 0
        # ...and several fsync intervals later there is still no archive
        # tree and no writer.
        qtbot.wait(1000)
        assert engine._archive_writers == {}
        assert not archive_root.exists(), (
            f"monitoring created files: {[str(p) for p in archive_root.rglob('*')]}"
        )
    finally:
        engine.stop()


def test_global_start_is_monitor_all_and_ignores_archive_config(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    """``start()`` (test/headless convenience) monitors every device and
    creates no writers regardless of ``archive.enabled``."""
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[_device_from_server("dev", server, archive=_TIGHT_ARCHIVE)],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start()
    try:
        assert engine.acquisition_state("dev") is AcquisitionState.MONITORING
        with qtbot.waitSignal(engine.traceReady, timeout=10_000):
            pass
        qtbot.wait(700)
        assert engine._archive_writers == {}
        assert not archive_root.exists()
    finally:
        engine.stop()


def test_start_monitoring_is_idempotent(qtbot, tmp_path: Path, make_fake_server) -> None:  # noqa: F811
    server = make_fake_server(_SERVER_CFG)
    cfg = _make_root_cfg(devices=[_device_from_server("dev", server)])
    engine = StreamingEngine(cfg)
    engine.start_monitoring("dev")
    try:
        assert _wait_until(lambda: _connected(engine, "dev"), timeout_s=5.0, qtbot=qtbot)
        worker = engine._workers["dev"]
        engine.start_monitoring("dev")  # no-op
        assert engine._workers["dev"] is worker
        assert engine.acquisition_state("dev") is AcquisitionState.MONITORING
    finally:
        engine.stop()


def test_lifecycle_unknown_device_raises(tmp_path: Path) -> None:
    engine = StreamingEngine(_make_root_cfg(devices=[]))
    try:
        with pytest.raises(KeyError):
            engine.start_monitoring("nope")
        with pytest.raises(KeyError):
            engine.start_recording("nope")
    finally:
        engine.stop()


# ---------------------------------------------------------------------------
# Recording: writers appear; upgrade/downgrade without socket churn
# ---------------------------------------------------------------------------


def test_start_recording_from_idle_creates_sds_tree(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[_device_from_server("dev", server, archive=_TIGHT_ARCHIVE)],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start_recording("dev")
    try:
        assert engine.acquisition_state("dev") is AcquisitionState.RECORDING
        assert "dev" in engine._archive_writers
        assert _wait_until(
            lambda: _has_archive_file(archive_root), timeout_s=10.0, qtbot=qtbot
        ), f"no archive file under {archive_root}"
    finally:
        engine.stop()


def test_monitor_to_record_attaches_writer_without_socket_churn(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[_device_from_server("dev", server, archive=_TIGHT_ARCHIVE)],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start_monitoring("dev")
    try:
        assert _wait_until(lambda: _connected(engine, "dev"), timeout_s=5.0, qtbot=qtbot)
        worker_before = engine._workers["dev"]
        engine.start_recording("dev")
        # Same worker object: the live socket was not recycled.
        assert engine._workers["dev"] is worker_before
        assert engine.acquisition_state("dev") is AcquisitionState.RECORDING
        assert _wait_until(
            lambda: _has_archive_file(archive_root), timeout_s=10.0, qtbot=qtbot
        )
        engine.start_recording("dev")  # idempotent — writer not recreated
        assert engine._workers["dev"] is worker_before
    finally:
        engine.stop()


def test_record_to_monitor_tears_down_writer_keeps_streaming(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[_device_from_server("dev", server, archive=_TIGHT_ARCHIVE)],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start_recording("dev")
    try:
        assert _wait_until(
            lambda: _has_archive_file(archive_root), timeout_s=10.0, qtbot=qtbot
        )
        worker_before = engine._workers["dev"]
        engine.start_monitoring("dev")  # downgrade
        assert engine.acquisition_state("dev") is AcquisitionState.MONITORING
        assert engine._archive_writers == {}
        assert engine._workers["dev"] is worker_before
        # The archive stops growing (teardown flushed synchronously);
        # streaming continues.
        size_after = sum(p.stat().st_size for p in archive_root.rglob("*.D.*"))
        with qtbot.waitSignal(engine.traceReady, timeout=5000):
            pass
        qtbot.wait(1000)  # several would-be fsync intervals
        assert sum(p.stat().st_size for p in archive_root.rglob("*.D.*")) == size_after
    finally:
        engine.stop()


# ---------------------------------------------------------------------------
# Stop: bounded, isolated, signalled
# ---------------------------------------------------------------------------


def test_stop_one_device_is_bounded_and_leaves_other_streaming(
    qtbot,
    make_fake_server,  # noqa: F811
) -> None:
    cfg_b = FakeSeedLinkServerConfig(
        network="IV",
        station="MILN",
        location="",
        channel="HHZ",
        sampling_rate=100.0,
        samples_per_record=50,
        packet_interval_s=0.05,
    )
    server_a = make_fake_server(_SERVER_CFG)
    server_b = make_fake_server(cfg_b)
    cfg = _make_root_cfg(
        devices=[
            _device_from_server("dev-a", server_a),
            _device_from_server("dev-b", server_b),
        ]
    )
    engine = StreamingEngine(cfg)
    engine.start_monitoring("dev-a")
    engine.start_monitoring("dev-b")
    try:
        assert _wait_until(
            lambda: _connected(engine, "dev-a") and _connected(engine, "dev-b"),
            timeout_s=5.0,
            qtbot=qtbot,
        )
        t0 = time.monotonic()
        engine.stop("dev-a")
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"per-device stop took {elapsed:.2f}s"
        assert engine.acquisition_state("dev-a") is AcquisitionState.IDLE
        assert "dev-a" not in engine._workers
        # dev-b is untouched and still produces coalesced traces.
        assert engine.acquisition_state("dev-b") is AcquisitionState.MONITORING
        with qtbot.waitSignal(engine.traceReady, timeout=5000) as blocker:
            pass
        assert blocker.args[0] == "dev-b"
        # Stopping again is a no-op.
        engine.stop("dev-a")
    finally:
        engine.stop()


def test_state_signal_full_cycle(qtbot, tmp_path: Path, make_fake_server) -> None:  # noqa: F811
    """Idle → Monitoring → Recording → Monitoring → Idle, each transition
    announced exactly once on ``acquisitionStateChanged``."""
    server = make_fake_server(_SERVER_CFG)
    cfg = _make_root_cfg(
        devices=[_device_from_server("dev", server, archive=_TIGHT_ARCHIVE)],
        archive_root=tmp_path / "archive",
    )
    engine = StreamingEngine(cfg)
    spy = _StateSpy(engine)
    try:
        engine.start_monitoring("dev")
        engine.start_recording("dev")
        engine.start_monitoring("dev")
        engine.stop("dev")
        assert spy.transitions == [
            ("dev", int(AcquisitionState.MONITORING)),
            ("dev", int(AcquisitionState.RECORDING)),
            ("dev", int(AcquisitionState.MONITORING)),
            ("dev", int(AcquisitionState.IDLE)),
        ]
    finally:
        engine.stop()


def test_reconnect_ignores_idle_device(qtbot, make_fake_server) -> None:  # noqa: F811
    """``reconnect_device`` restarts what's running; it never starts an
    idle device (that would be acquisition by side effect)."""
    server = make_fake_server(_SERVER_CFG)
    cfg = _make_root_cfg(devices=[_device_from_server("dev", server)])
    engine = StreamingEngine(cfg)
    try:
        engine.reconnect_device("dev")
        qtbot.wait(200)
        assert "dev" not in engine._workers
        assert engine.acquisition_state("dev") is AcquisitionState.IDLE
    finally:
        engine.stop()


def test_downgrade_flushes_inflight_archive_inbox(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    """Packets still sitting in the bounded archive inbox when the user
    downgrades Recording → Monitoring must reach disk, not vanish — the
    teardown drains the inbox to the writer before the blocking close
    (qt-concurrency-auditor F1 on the M2-A diff; rule 8).

    A distinctive NSLC is injected straight into the inbox so the
    assertion can't be satisfied by the live stream's own packets.
    """
    import numpy as np
    from obspy import Trace, UTCDateTime

    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[_device_from_server("dev", server, archive=_TIGHT_ARCHIVE)],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start_recording("dev")
    try:
        # Writer warm: at least one live file on disk.
        assert _wait_until(
            lambda: _has_archive_file(archive_root), timeout_s=10.0, qtbot=qtbot
        )
        marker = Trace(
            data=np.arange(100, dtype=np.int32),
            header={
                "network": "XX",
                "station": "FLUSH",
                "location": "00",
                "channel": "HHZ",
                "sampling_rate": 100.0,
                "starttime": UTCDateTime(),
            },
        )
        engine._enqueue_for_archive("dev", "XX.FLUSH.00.HHZ", marker)
        # Downgrade tears the writer down synchronously; the in-flight
        # marker must have been drained + fsynced by the time it returns.
        engine.start_monitoring("dev")
        flushed = [p for p in archive_root.rglob("*FLUSH*") if p.is_file()]
        assert flushed, (
            "in-flight inbox packet was dropped by the Recording→Monitoring "
            f"downgrade; archive contents: {[str(p) for p in archive_root.rglob('*')]}"
        )
    finally:
        engine.stop()
