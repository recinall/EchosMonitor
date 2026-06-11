"""Integration tests for the M5 archive wiring on the StreamingEngine.

The fake SeedLink server feeds real ObsPy traces into the engine; with
the device in the RECORDING state (M2-A — writers are created by
``start_recording``, never by config), files appear under the
configured archive root and ``DeviceStatus.archive_*`` fields update.
Shutdown must remain clean (≤ engine's existing ~5 s budget).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from obspy import read

from echosmonitor.config.schema import (
    AppConfig,
    ArchiveConfig,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import StreamID
from echosmonitor.core.streaming_engine import StreamingEngine
from tests.core.fakes import FakeSeedLinkServer
from tests.core.test_seedlink_worker import fake_server, loop_thread  # noqa: F401


def _make_root_cfg(devices: list[DeviceConfig], *, archive_root: Path | None = None) -> RootConfig:
    return RootConfig(
        app=AppConfig(archive_root=archive_root),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=devices,
    )


def _wait_until(predicate, timeout_s: float, qtbot) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qtbot.wait(50)
        if predicate():
            return True
    return False


@pytest.fixture
def archive_engine(
    qtbot,
    tmp_path: Path,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> Iterator[tuple[StreamingEngine, Path, str]]:
    nslc = (
        f"{fake_server.config.network}.{fake_server.config.station}."
        f"{fake_server.config.location}.{fake_server.config.channel}"
    )
    archive_root = tmp_path / "archive"
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
                archive=ArchiveConfig(
                    enabled=True,
                    encoding="STEIM2",
                    record_length=512,
                    fsync_interval_s=0.5,  # tight so tests don't have to wait long
                    queue_max=256,
                ),
            ),
        ],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    # M2-A: writers are created by the RECORDING path only, never by
    # ``archive.enabled`` config (rule 13). One call covers Idle→Recording.
    engine.start_recording("fake")
    try:
        yield engine, archive_root, nslc
    finally:
        engine.stop()


def test_config_time_nslc_collision_warns_without_blocking(
    capture_structlog,
) -> None:
    """Two devices emitting the same concrete NSLC log ONE collision warning
    at start-up, and start-up is not blocked (informational only)."""
    sel = StreamSelectorConfig(network="XX", station="ECHOS", location="00", channel="HHZ")
    rc = ReconnectConfig(initial_delay_s=3600.0, max_delay_s=3600.0, connect_timeout_s=0.5)
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(name="Echos", host="192.0.2.1", port=18000, reconnect=rc, selectors=[sel]),
            DeviceConfig(
                name="Echos_WK", host="192.0.2.2", port=18000, reconnect=rc, selectors=[sel]
            ),
        ],
    )
    engine = StreamingEngine(cfg)
    engine.start()  # must not block / raise
    try:
        hits = [r for r in capture_structlog if r.get("event") == "streaming_engine_nslc_collision"]
        assert len(hits) == 1, hits
        assert hits[0]["nslc"] == "XX.ECHOS.00.HHZ"
        assert sorted(hits[0]["devices"]) == ["Echos", "Echos_WK"]
        assert engine._started  # started anyway
    finally:
        engine.stop()


def test_archive_writes_appear_under_sds_layout(
    qtbot,
    archive_engine,
) -> None:
    _engine, archive_root, nslc = archive_engine

    sid = StreamID.from_trace_id(nslc)

    def _file_exists() -> bool:
        # Compare paths by SDS day-of-year of "now" — the fake server
        # uses ``UTCDateTime()`` (now) as starttime so the trace lands
        # in today's SDS path.
        return any(p.is_file() and p.stat().st_size > 0 for p in archive_root.rglob("*.D.*"))

    assert _wait_until(_file_exists, timeout_s=10.0, qtbot=qtbot), (
        f"no archive file appeared under {archive_root}; layout: "
        f"{[str(p) for p in archive_root.rglob('*')]}"
    )

    # The file must be at a path matching the SDS scheme for the trace's
    # NSLC. We don't assert on the exact ``year/doy`` (depends on test
    # runtime) but we DO require the layout shape.
    files = [p for p in archive_root.rglob("*.D.*") if p.is_file()]
    assert files, "archive root has no files yet"
    f = files[0]
    parts = f.parts
    # Walk back from the leaf: filename, channel.D dir, station, network, year, root...
    assert parts[-2] == f"{sid.channel}.D"
    assert parts[-3] == sid.station
    assert parts[-4] == sid.network

    # The file must be readable via obspy.
    st = read(str(f))
    assert len(st) >= 1
    rt = st[0]
    assert rt.stats.network == sid.network
    assert rt.stats.station == sid.station
    assert rt.stats.channel == sid.channel


def test_archive_updates_device_status_counters(
    qtbot,
    archive_engine,
) -> None:
    engine, _archive_root, _nslc = archive_engine

    def _has_bytes() -> bool:
        status = engine.device_status().get("fake")
        if status is None:
            return False
        return status.archive_enabled and status.archive_bytes_written > 0

    assert _wait_until(_has_bytes, timeout_s=10.0, qtbot=qtbot), (
        "archive_bytes_written never advanced past zero"
    )

    status = engine.device_status()["fake"]
    assert status.archive_enabled is True
    assert status.archive_bytes_written > 0
    assert status.archive_files_open >= 1
    assert status.archive_last_write_at is not None
    assert status.archive_last_error is None


def test_monitoring_writes_no_files(
    qtbot,
    tmp_path: Path,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> None:
    """A device that is only MONITORING (here via the start() monitor-all
    convenience) produces zero archive files — writers exist only in the
    RECORDING state (M2-A, rule 13)."""
    archive_root = tmp_path / "archive"
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
                # Default archive config; irrelevant either way — only
                # start_recording() creates a writer.
            ),
        ],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start()
    try:
        # Let some packets arrive.
        qtbot.wait(2000)
        status = engine.device_status().get("fake")
        # Archive must remain disabled and produce no files.
        assert status is not None
        assert status.archive_enabled is False
        assert status.archive_bytes_written == 0
        # The archive root may not even exist if no writes were attempted.
        if archive_root.exists():
            assert list(archive_root.rglob("*.D.*")) == []
    finally:
        engine.stop()


def test_archive_engine_stop_within_budget(
    qtbot,
    archive_engine,
) -> None:
    """``engine.stop()`` must close writers + storage thread inside the
    existing ~5 s budget. We measure wall time and assert ≤ 5 s."""
    engine, _root, _nslc = archive_engine

    # Wait for at least one packet so a writer has open files to close.
    def _bytes_written() -> bool:
        status = engine.device_status().get("fake")
        return status is not None and status.archive_bytes_written > 0

    assert _wait_until(_bytes_written, timeout_s=10.0, qtbot=qtbot)

    t0 = time.monotonic()
    engine.stop()
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"engine.stop() took {elapsed:.2f}s, budget is 5s"


def test_resolve_archive_root_falls_back_to_app(
    tmp_path: Path,
) -> None:
    """When ``DeviceConfig.archive.root_dir`` is None, the engine uses
    ``AppConfig.archive_root``; both None → platformdirs default."""
    app_root = tmp_path / "app_archive"
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="dev",
                host="localhost",
                port=18000,
                archive=ArchiveConfig(enabled=True),  # root_dir=None
            )
        ],
        archive_root=app_root,
    )
    engine = StreamingEngine(cfg)
    resolved = engine._resolve_archive_root(cfg.devices[0])
    assert resolved == app_root


def test_resolve_archive_root_per_device_override(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app_archive"
    dev_root = tmp_path / "dev_archive"
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="dev",
                host="localhost",
                port=18000,
                archive=ArchiveConfig(enabled=True, root_dir=dev_root),
            )
        ],
        archive_root=app_root,
    )
    engine = StreamingEngine(cfg)
    resolved = engine._resolve_archive_root(cfg.devices[0])
    assert resolved == dev_root


def test_resolve_archive_root_platformdirs_fallback() -> None:
    """When both per-device and top-level are None, platformdirs is used."""
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="dev",
                host="localhost",
                port=18000,
                archive=ArchiveConfig(enabled=True),
            )
        ],
        archive_root=None,
    )
    engine = StreamingEngine(cfg)
    resolved = engine._resolve_archive_root(cfg.devices[0])
    # platformdirs path includes the org/app components we passed.
    assert "echosmonitor" in str(resolved) or "EchosMonitor" in str(resolved)
    assert resolved.name == "archive"
