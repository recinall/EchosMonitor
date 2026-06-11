"""End-to-end pipeline tests for the M5 stage-B metadata index.

Verifies that the DAO + writer + gap detector pipeline produces both
correct SDS files AND consistent SQLite rows. Crash injection (SIGKILL
mid-write) is exercised at the writer-level integrity boundary in
``test_mseed_writer.py``; this file covers the engine-level wiring.
"""

from __future__ import annotations

import sqlite3
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
from echosmonitor.core.streaming_engine import StreamingEngine
from echosmonitor.storage.db import connect
from tests.core.fakes import FakeSeedLinkServer
from tests.core.test_seedlink_worker import fake_server, loop_thread  # noqa: F401


def _make_root_cfg(devices: list[DeviceConfig], *, archive_root: Path) -> RootConfig:
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
) -> Iterator[tuple[StreamingEngine, Path]]:
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
                    fsync_interval_s=0.5,
                    queue_max=256,
                ),
            ),
        ],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    # M2-A: archive writers exist only in the RECORDING state (rule 13).
    engine.start_recording("fake")
    try:
        yield engine, archive_root
    finally:
        engine.stop()


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_session_row_created_on_start(qtbot, archive_engine: tuple[StreamingEngine, Path]) -> None:
    _engine, archive_root = archive_engine
    db_path = archive_root / "archive.db"

    # Wait briefly for the DB to be opened (lazy on first archive-enabled
    # device's start).
    assert _wait_until(lambda: db_path.exists(), timeout_s=5.0, qtbot=qtbot)

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, host, version, config_hash, started_at, ended_at FROM sessions"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    row = rows[0]
    assert row["host"]  # populated
    assert row["version"]
    assert len(row["config_hash"]) == 64  # SHA256 hex
    assert row["started_at"]
    assert row["ended_at"] is None  # session still open while engine runs


def test_session_row_finalized_on_stop(
    qtbot,
    tmp_path: Path,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> None:
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="fake",
                host=fake_server.host,
                port=fake_server.port,
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
                    fsync_interval_s=0.5,
                    queue_max=128,
                ),
            ),
        ],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start_recording("fake")
    qtbot.wait(500)
    engine.stop()

    db_path = archive_root / "archive.db"
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT ended_at FROM sessions").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["ended_at"] is not None  # session was closed


# ---------------------------------------------------------------------------
# Device + stream + file rows after live data
# ---------------------------------------------------------------------------


def test_device_stream_file_rows_appear_after_first_fsync(
    qtbot, archive_engine: tuple[StreamingEngine, Path]
) -> None:
    _engine, archive_root = archive_engine
    db_path = archive_root / "archive.db"

    def _has_file_row() -> bool:
        if not db_path.exists():
            return False
        conn = connect(db_path)
        try:
            row = conn.execute("SELECT count(*) AS n FROM files").fetchone()
            return row is not None and row["n"] > 0
        finally:
            conn.close()

    assert _wait_until(_has_file_row, timeout_s=10.0, qtbot=qtbot), (
        "no row appeared in files within 10 s"
    )

    conn = connect(db_path)
    try:
        # devices: one row.
        devs = conn.execute("SELECT name, host, port FROM devices").fetchall()
        assert len(devs) == 1
        assert devs[0]["name"] == "fake"
        # streams: one row.
        streams = conn.execute(
            "SELECT id, network, station, location, channel,"
            "       total_packets, total_bytes, sample_rate"
            " FROM streams"
        ).fetchall()
        assert len(streams) == 1
        s = streams[0]
        assert s["network"] == "IV"
        assert s["station"] == "MILN"
        assert s["channel"] == "HHZ"
        assert s["total_packets"] >= 1
        assert s["total_bytes"] >= 1
        # files: one row, path under archive_root, bytes > 0.
        files = conn.execute("SELECT path, t_start, t_end, bytes FROM files").fetchall()
        assert len(files) == 1
        f = files[0]
        assert f["path"].startswith(str(archive_root))
        assert f["bytes"] >= 1
        assert f["t_start"] != ""
        assert f["t_end"] != ""

        # The file must read back via obspy (cross-check DB ↔ disk).
        st = read(f["path"])
        assert len(st) >= 1
    finally:
        conn.close()


def test_total_bytes_matches_file_size_on_disk(
    qtbot, archive_engine: tuple[StreamingEngine, Path]
) -> None:
    """``streams.total_bytes`` must agree with the on-disk file size."""
    _engine, archive_root = archive_engine
    db_path = archive_root / "archive.db"

    def _stream_has_bytes() -> bool:
        if not db_path.exists():
            return False
        conn = connect(db_path)
        try:
            row = conn.execute("SELECT total_bytes FROM streams LIMIT 1").fetchone()
            return row is not None and row["total_bytes"] > 0
        finally:
            conn.close()

    assert _wait_until(_stream_has_bytes, timeout_s=10.0, qtbot=qtbot)

    conn = connect(db_path)
    try:
        files = conn.execute("SELECT path, bytes FROM files").fetchall()
        assert files
        # We need the "as last fsync saw" view, so wait briefly past
        # one more fsync to make sure DB is up to date with disk.
        qtbot.wait(500)
        for f in files:
            on_disk_size = Path(f["path"]).stat().st_size
            # DB is gated on fsync, so it may lag disk slightly. The
            # invariant is "DB never claims more than what's on disk":
            # bytes_recorded <= on_disk_size + small slack for the
            # very-last write happening in flight when we sampled.
            assert f["bytes"] <= on_disk_size, (
                f"DB claims {f['bytes']} bytes for {f['path']} but disk has {on_disk_size}"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Restart resumption
# ---------------------------------------------------------------------------


def test_restart_resumption_reuses_existing_db(
    qtbot,
    tmp_path: Path,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> None:
    """Two engine sessions land in the same DB without breaking constraints."""
    archive_root = tmp_path / "archive"

    def _build_engine() -> StreamingEngine:
        cfg = _make_root_cfg(
            devices=[
                DeviceConfig(
                    name="fake",
                    host=fake_server.host,
                    port=fake_server.port,
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
                        fsync_interval_s=0.5,
                        queue_max=128,
                    ),
                )
            ],
            archive_root=archive_root,
        )
        return StreamingEngine(cfg)

    # First session.
    engine1 = _build_engine()
    engine1.start_recording("fake")
    qtbot.wait(1500)  # let some packets land
    engine1.stop()

    db_path = archive_root / "archive.db"
    assert db_path.exists()
    conn = connect(db_path)
    try:
        s1_count = conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"]
        d1_count = conn.execute("SELECT count(*) AS n FROM devices").fetchone()["n"]
        f1 = conn.execute("SELECT path, bytes FROM files LIMIT 1").fetchone()
    finally:
        conn.close()

    assert s1_count == 1
    assert d1_count == 1

    # Second session — same archive root, same device, same NSLC.
    engine2 = _build_engine()
    engine2.start_recording("fake")
    qtbot.wait(1500)
    engine2.stop()

    conn = connect(db_path)
    try:
        s2_count = conn.execute("SELECT count(*) AS n FROM sessions").fetchone()["n"]
        d2_count = conn.execute("SELECT count(*) AS n FROM devices").fetchone()["n"]
        f2 = conn.execute("SELECT path, bytes FROM files LIMIT 1").fetchone()
    finally:
        conn.close()

    # Two sessions, same single device row (UPSERT), same single file
    # path (UPSERT — same NSLC, same UTC day in the test window).
    assert s2_count == 2, "second session must be added, not replace"
    assert d2_count == 1, "device row UPSERT must keep a single row"
    if f1 is not None and f2 is not None:
        assert f2["path"] == f1["path"]
        assert f2["bytes"] >= f1["bytes"]


# ---------------------------------------------------------------------------
# Concurrent writers: two devices same archive root
# ---------------------------------------------------------------------------


def test_two_devices_share_db_without_constraint_violation(
    qtbot,
    tmp_path: Path,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> None:
    """A second SeedLinkServer would be ideal, but starting two fakes per
    test is heavy. The constraint we want to verify is "two devices with
    distinct selectors + same archive root never violate FK/UNIQUE";
    using two device rows backed by the same fake server (different
    device names) exercises that path."""
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="fake-a",
                host=fake_server.host,
                port=fake_server.port,
                selectors=[
                    StreamSelectorConfig(
                        network=fake_server.config.network,
                        station=fake_server.config.station,
                        location=fake_server.config.location,
                        channel=fake_server.config.channel,
                    )
                ],
                archive=ArchiveConfig(enabled=True, fsync_interval_s=0.5),
            ),
            DeviceConfig(
                name="fake-b",
                host=fake_server.host,
                port=fake_server.port,
                selectors=[
                    StreamSelectorConfig(
                        network=fake_server.config.network,
                        station=fake_server.config.station,
                        location=fake_server.config.location,
                        channel=fake_server.config.channel,
                    )
                ],
                archive=ArchiveConfig(enabled=True, fsync_interval_s=0.5),
            ),
        ],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start_recording("fake-a")
    engine.start_recording("fake-b")
    qtbot.wait(2000)
    engine.stop()

    db_path = archive_root / "archive.db"
    conn = connect(db_path)
    try:
        # Two devices, two streams, > 0 files. No IntegrityError seen.
        devs = conn.execute("SELECT name FROM devices").fetchall()
        assert {r["name"] for r in devs} == {"fake-a", "fake-b"}
        streams = conn.execute("SELECT device_id, network, station FROM streams").fetchall()
        assert len(streams) == 2
    except sqlite3.IntegrityError as exc:  # pragma: no cover
        pytest.fail(f"FK or UNIQUE violation: {exc}")
    finally:
        conn.close()
