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

import numpy as np
import pytest
from obspy import Stream, read

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
from echosmonitor.core.streaming_engine import (
    _REPLAY_MIN_OVERLAP_S,
    StreamingEngine,
    _replay_action,
    _windows_match,
)
from tests.core.fakes import FakeSeedLinkServer, FakeSeedLinkServerConfig
from tests.core.test_seedlink_worker import fake_server, loop_thread  # noqa: F401
from tests.core.test_streaming_engine_multi import (
    make_fake_server,  # noqa: F401  pytest fixture re-export
)


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
    # M2-A/B: writers exist only in the RECORDING state (rule 13) and
    # recording happens inside a named session (rule 14). One call
    # covers session + Idle→Recording.
    engine.start_session("proj", ["fake"])
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


def test_replay_burst_loses_no_recorded_samples(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    """M6.5-A regression: a FETCH replay burst far larger than
    ``archive.queue_max`` reaches disk complete and gap-free.

    The first field run (2026-06-12, real echos.local, 500 Hz x 3 ch)
    lost 33 s of RECORDED data: a reconnect replay burst arrived faster
    than the engine's flush tick and the old bounded archive inbox
    applied drop-oldest to the science sink. The fake server sends
    ``burst_records`` back-to-back records on connect — many times
    ``queue_max`` — so the pre-fix engine drops most of them, while the
    fixed engine (direct per-packet post to the storage thread, no
    engine-side drop point) must archive every sample of the burst with
    no gaps.
    """
    burst = 400
    n = 100
    server_cfg = FakeSeedLinkServerConfig(
        network="XX",
        station="BURST",
        location="00",
        channel="HHZ",
        sampling_rate=500.0,
        samples_per_record=n,
        packet_interval_s=0.1,
        burst_records=burst,
    )
    server = make_fake_server(server_cfg)
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="dev",
                host=server.host,
                port=server.port,
                reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
                selectors=[
                    StreamSelectorConfig(
                        network="XX", station="BURST", location="00", channel="HHZ"
                    )
                ],
                archive=ArchiveConfig(
                    enabled=True,
                    fsync_interval_s=0.5,
                    # Schema minimum — the burst is 25x this, so the old
                    # drop-oldest inbox cannot pass this test.
                    queue_max=16,
                ),
            ),
        ],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start_session("burst", ["dev"])
    try:
        target = burst * n

        def _samples_on_disk() -> int:
            total = 0
            for f in archive_root.rglob("*.D.*"):
                if not f.is_file():
                    continue
                # Safe to read mid-recording: appends are whole
                # 512-byte records via a single os.write.
                for tr in read(str(f)):
                    total += tr.stats.npts
            return total

        # Generous bound: the dev machine runs a production instance
        # and the suite is timing-flaky under load.
        assert _wait_until(lambda: _samples_on_disk() >= target, timeout_s=30.0, qtbot=qtbot), (
            f"only {_samples_on_disk()}/{target} burst samples reached the "
            f"archive — recorded samples were dropped"
        )

        # Completeness is not enough: drop-oldest punched HOLES in the
        # field archive. The fake stream is perfectly contiguous, so
        # everything on disk must merge into one unmasked trace.
        merged = Stream()
        for f in archive_root.rglob("*.D.*"):
            if f.is_file():
                merged += read(str(f))
        merged.merge(method=0)
        assert len(merged) == 1, (
            f"archive is fragmented into {len(merged)} segments: gaps "
            f"were introduced into a contiguous recorded stream"
        )
        assert not np.ma.isMaskedArray(merged[0].data) or not np.ma.is_masked(merged[0].data), (
            "merged archive stream contains masked gap samples"
        )
    finally:
        engine.stop()


def test_engine_rectifies_jittered_stamps_before_archiving(
    qtbot,
    tmp_path: Path,
    capture_structlog,
) -> None:
    """M6.5-B wiring: ``_on_packet`` → ``_observe_gap`` applies the
    detector's grid snap to the trace before it reaches the writer, so
    field-like stamp jitter (±2 samples @ 500 Hz) produces ONE
    contiguous on-disk segment and zero gap events/logs."""
    from obspy import Trace, UTCDateTime

    fs = 500.0
    npts = 100
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="dev",
                host="192.0.2.1",  # unroutable: only injected packets flow
                port=18000,
                reconnect=ReconnectConfig(
                    initial_delay_s=3600.0, max_delay_s=3600.0, connect_timeout_s=0.5
                ),
                selectors=[
                    StreamSelectorConfig(
                        network="XX", station="JIT", location="00", channel="HHZ"
                    )
                ],
                archive=ArchiveConfig(
                    enabled=True, fsync_interval_s=0.5, jitter_tolerance_ms=10.0
                ),
            )
        ],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start_session("jit", ["dev"])
    try:
        t0 = UTCDateTime("2026-06-12T12:00:00")
        for i, j in enumerate([0, 2, 0, -1, 0, 1, -2, 0]):
            tr = Trace(
                data=(np.arange(npts, dtype=np.int32) % 100),
                header={
                    "network": "XX",
                    "station": "JIT",
                    "location": "00",
                    "channel": "HHZ",
                    "sampling_rate": fs,
                    "starttime": t0 + i * npts / fs + j / fs,
                },
            )
            engine._on_packet("dev", tr)

        def _on_disk() -> int:
            total = 0
            for f in archive_root.rglob("*JIT*"):
                if f.is_file():
                    total += sum(tr.stats.npts for tr in read(str(f)))
            return total

        assert _wait_until(lambda: _on_disk() >= 8 * npts, timeout_s=15.0, qtbot=qtbot)
        # Segment count straight after read: ``read`` groups contiguous
        # records into one trace; jittered stamps would split it (and a
        # merge() would mask, not heal).
        segments = Stream()
        for f in archive_root.rglob("*JIT*"):
            if f.is_file():
                segments += read(str(f))
        assert len(segments) == 1, f"jittered stamps fragmented the archive: {segments}"
        assert not np.ma.isMaskedArray(segments[0].data)
        gap_logs = [
            r
            for r in capture_structlog
            if r.get("event") == "streaming_engine_archive_gap_detected"
        ]
        assert gap_logs == [], "in-tolerance jitter must not log gap chatter"
    finally:
        engine.stop()


def test_archive_inflight_gauge_warns_without_dropping(
    qtbot,
    tmp_path: Path,
    capture_structlog,
) -> None:
    """The in-flight gauge replaces the inbox drop counter (M6.5-A):
    when sent-minus-acked exceeds ``queue_max`` the engine warn-logs and
    emits ``archiveBackpressure`` (throttled), but every trace is still
    posted to the storage seam — nothing is dropped.

    Driven directly against ``_enqueue_for_archive`` with a sender
    whose signal has no receiver, so the writer never acks and the
    gauge climbs deterministically.
    """
    from obspy import Trace, UTCDateTime

    from echosmonitor.core.streaming_engine import _ArchiveSender

    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="dev",
                host="192.0.2.1",
                port=18000,
                reconnect=ReconnectConfig(
                    initial_delay_s=3600.0, max_delay_s=3600.0, connect_timeout_s=0.5
                ),
            )
        ],
        archive_root=tmp_path / "archive",
    )
    engine = StreamingEngine(cfg)
    fired: list[tuple[str, int]] = []
    engine.archiveBackpressure.connect(lambda d, n_: fired.append((d, n_)))
    try:
        sender = _ArchiveSender(engine)
        engine._archive_senders["dev"] = sender
        engine._archive_sent["dev"] = 0
        engine._archive_acked["dev"] = 0
        engine._archive_inflight_warn["dev"] = 16
        tr = Trace(
            data=np.arange(10, dtype=np.int32),
            header={
                "network": "XX",
                "station": "GAUGE",
                "location": "00",
                "channel": "HHZ",
                "sampling_rate": 100.0,
                "starttime": UTCDateTime(),
            },
        )
        for _ in range(20):
            engine._enqueue_for_archive("dev", "XX.GAUGE.00.HHZ", tr)
        hits = [
            r
            for r in capture_structlog
            if r.get("event") == "streaming_engine_archive_backpressure"
        ]
        assert len(hits) == 1, hits  # throttled: one line despite 4 over-threshold posts
        assert hits[0]["inflight"] == 17
        assert "dropped" not in hits[0]
        assert fired == [("dev", 17)]
        # Acks bring the gauge back down; clamped so over-acking can
        # never underflow below zero.
        for _ in range(25):
            engine._ack_archive_trace("dev")
        assert engine._archive_acked["dev"] == engine._archive_sent["dev"] == 20
    finally:
        engine.stop()


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
    """When both per-device and top-level are None, the platformdirs
    fallback is used — asserted via delegation to the single base-root
    definition (``resolve_base_archive_root``), because the suite-wide
    conftest redirect points platformdirs at the per-test tmp dir (the
    M2-C suite-isolation blocker) so the real org/app path never
    appears in tests."""
    from echosmonitor.core.session import resolve_base_archive_root

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
    assert resolved == resolve_base_archive_root(cfg)
    assert resolved.name == "archive"


# ---------------------------------------------------------------------------
# Reconnect-replay dedup (verified 2026-07-01: overlaps >= 3 s are
# byte-identical re-sends of the device ring buffer after a reconnect; the
# append-only writer would otherwise persist them as backward-timestamped
# overlaps). See ``_replay_action`` / ``_REPLAY_MIN_OVERLAP_S``.
# ---------------------------------------------------------------------------

_FS = 500.0


def test_replay_action_passes_forward_and_jitter() -> None:
    fs = _FS
    wm = 1000.0  # frontier at t=1000 s
    dt = 1.0 / fs
    # Strictly forward (next sample after frontier).
    assert _replay_action(wm, wm + dt, wm + dt + 0.2, fs) == ("pass", None)
    # A legitimate forward gap is never touched.
    assert _replay_action(wm, wm + 5.0, wm + 5.2, fs) == ("pass", None)
    # Device stamp jitter of -2 samples (M6.5-B territory) stays a pass.
    assert _replay_action(wm, wm - 2 * dt, wm + 0.2, fs) == ("pass", None)
    # A sub-threshold (chronic ~1 s) overlap is left alone — those samples
    # are NOT clean duplicates, so dedup would drop real data.
    assert _replay_action(wm, wm - 1.0, wm + 0.2, fs) == ("pass", None)
    assert _replay_action(wm, wm - (_REPLAY_MIN_OVERLAP_S - 0.01), wm + 0.2, fs) == ("pass", None)


def test_replay_action_drops_full_replay() -> None:
    fs = _FS
    wm = 1000.0
    # A block that starts 9.6 s behind the frontier and ends before it:
    # a full reconnect replay of data we already hold.
    action, arg = _replay_action(wm, wm - 9.6, wm - 6.0, fs)
    assert action == "drop"
    assert arg is None


def test_replay_action_trims_partial_replay() -> None:
    fs = _FS
    wm = 1000.0
    # Starts 4 s behind the frontier but its tail runs 1 s past it: keep only
    # the tail, trimming at the frontier + half a sample.
    action, trim_start = _replay_action(wm, wm - 4.0, wm + 1.0, fs)
    assert action == "trim"
    assert trim_start == pytest.approx(wm + 0.5 / fs)


def _feed_forward(engine: StreamingEngine, t0, n_packets: int, npts: int):
    """Inject ``n_packets`` contiguous forward packets; return the next start."""
    from obspy import Trace

    for i in range(n_packets):
        tr = Trace(
            data=(np.arange(npts, dtype=np.int32) % 100),
            header={
                "network": "XX",
                "station": "RPL",
                "location": "00",
                "channel": "HHZ",
                "sampling_rate": _FS,
                "starttime": t0 + i * npts / _FS,
            },
        )
        engine._on_packet("dev", tr)
    return t0 + n_packets * npts / _FS


@pytest.fixture
def _replay_engine(tmp_path: Path) -> Iterator[tuple[StreamingEngine, Path]]:
    archive_root = tmp_path / "archive"
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="dev",
                host="192.0.2.1",  # unroutable: only injected packets flow
                port=18000,
                reconnect=ReconnectConfig(
                    initial_delay_s=3600.0, max_delay_s=3600.0, connect_timeout_s=0.5
                ),
                selectors=[
                    StreamSelectorConfig(
                        network="XX", station="RPL", location="00", channel="HHZ"
                    )
                ],
                archive=ArchiveConfig(enabled=True, fsync_interval_s=0.5),
            )
        ],
        archive_root=archive_root,
    )
    engine = StreamingEngine(cfg)
    engine.start_session("rpl", ["dev"])
    try:
        yield engine, archive_root
    finally:
        engine.stop()


def _on_disk(archive_root: Path) -> Stream:
    merged = Stream()
    for f in archive_root.rglob("*RPL*"):
        if f.is_file():
            merged += read(str(f))
    return merged


def test_reconnect_replay_dropped_from_archive(
    qtbot, _replay_engine, capture_structlog
) -> None:
    """A full reconnect replay (>= 3 s behind the frontier) never reaches the
    SDS: the archive stays exactly the forward stream, one contiguous segment,
    and the drop is logged."""
    from obspy import Trace, UTCDateTime

    engine, archive_root = _replay_engine
    npts = 100
    t0 = UTCDateTime("2026-06-30T00:00:00")
    # 20 forward packets → 4.0 s of data; frontier at t0 + 3.998 s.
    nxt = _feed_forward(engine, t0, 20, npts)

    # Reconnect replay: a block starting 3.6 s behind the frontier, wholly
    # behind it — a byte-identical re-send of data already written.
    replay = Trace(
        data=(np.arange(npts, dtype=np.int32) % 100),
        header={
            "network": "XX",
            "station": "RPL",
            "location": "00",
            "channel": "HHZ",
            "sampling_rate": _FS,
            "starttime": t0 + 0.4,
        },
    )
    engine._on_packet("dev", replay)

    # Streaming resumes forward from where it left off.
    _feed_forward(engine, nxt, 2, npts)

    assert _wait_until(
        lambda: sum(tr.stats.npts for tr in _on_disk(archive_root)) >= 22 * npts,
        timeout_s=15.0,
        qtbot=qtbot,
    )
    merged = _on_disk(archive_root)
    merged.merge(method=0)
    assert len(merged) == 1, f"replay fragmented the archive into {len(merged)} segments"
    assert not np.ma.is_masked(merged[0].data)
    # Exactly the forward samples — the 100-sample replay was dropped, not
    # appended (which would show 23 * npts and an overlap).
    assert merged[0].stats.npts == 22 * npts
    drops = [
        r
        for r in capture_structlog
        if r.get("event") == "streaming_engine_reconnect_replay" and r.get("action") == "drop"
    ]
    assert drops, "a dropped reconnect replay must be logged (rule 5)"


def test_partial_reconnect_replay_trimmed_to_new_tail(qtbot, _replay_engine) -> None:
    """A replay that overlaps >= 3 s but whose tail extends past the frontier
    is trimmed to its new tail: the archive gains the fresh samples with no
    duplicated overlap."""
    from obspy import Trace, UTCDateTime

    engine, archive_root = _replay_engine
    npts = 100
    t0 = UTCDateTime("2026-06-30T00:00:00")
    _feed_forward(engine, t0, 20, npts)  # frontier at t0 + 3.998 s

    # 5.0 s block starting 3.6 s behind the frontier: [t0+0.4, t0+5.398].
    # Its head duplicates the forward stream; its ~1.4 s tail is new.
    tail_npts = 2500
    replay = Trace(
        data=(np.arange(tail_npts, dtype=np.int32) % 100),
        header={
            "network": "XX",
            "station": "RPL",
            "location": "00",
            "channel": "HHZ",
            "sampling_rate": _FS,
            "starttime": t0 + 0.4,
        },
    )
    engine._on_packet("dev", replay)

    # Coverage is [t0, t0+5.398] → 2700 samples, one contiguous segment.
    expected = round(5.398 * _FS) + 1
    assert _wait_until(
        lambda: sum(tr.stats.npts for tr in _on_disk(archive_root)) >= 20 * npts + 100,
        timeout_s=15.0,
        qtbot=qtbot,
    )
    merged = _on_disk(archive_root)
    merged.merge(method=0)
    assert len(merged) == 1, f"trimmed replay left {len(merged)} segments"
    assert not np.ma.is_masked(merged[0].data)
    # More than the forward stream (tail added) but far less than forward +
    # full replay (2000 + 2500) — the duplicated head was trimmed away.
    assert 20 * npts < merged[0].stats.npts < 20 * npts + tail_npts
    # Exact contiguous coverage: the trim leaves no duplicated boundary sample
    # and no gap at the seam.
    assert merged[0].stats.npts == expected


def test_monitoring_stop_clears_replay_watermark(tmp_path: Path) -> None:
    """A MONITORING-only device (no writer) must have its replay frontier
    cleared on stop. Regression for the auditor finding (2026-07-01): the
    watermark is populated unconditionally in ``_on_packet``, so if cleanup
    lived only in the archive-writer teardown, a stop→restart across the
    device's nightly backward clock resync (~00:23 UTC, -8..-12 s) would
    classify the fresh live packets as a >= 3 s replay and silently drop
    them. A mere reconnect never calls ``_stop_device``, so the frontier
    still persists across reconnects (covered by the drop/trim tests)."""
    from obspy import UTCDateTime

    from echosmonitor.core.models import device_stream_key

    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="dev",
                host="192.0.2.1",
                port=18000,
                reconnect=ReconnectConfig(
                    initial_delay_s=3600.0, max_delay_s=3600.0, connect_timeout_s=0.5
                ),
                selectors=[
                    StreamSelectorConfig(
                        network="XX", station="RPL", location="00", channel="HHZ"
                    )
                ],
                archive=ArchiveConfig(enabled=False),  # monitoring only — no writer
            )
        ],
        archive_root=tmp_path / "archive",
    )
    engine = StreamingEngine(cfg)
    key = device_stream_key("dev", "XX.RPL.00.HHZ")
    npts = 100
    t0 = UTCDateTime("2026-06-30T00:00:00")
    _feed_forward(engine, t0, 20, npts)  # builds the frontier at ~t0 + 4 s
    assert key in engine._replay_watermark

    engine._stop_device("dev")
    assert key not in engine._replay_watermark, (
        "a monitoring-only stop must clear the replay frontier"
    )

    # Restart after a ~10 s backward clock resync. Without the clear these
    # would be dropped against the stale ~t0+4 s frontier; with it the
    # frontier is rebuilt from the fresh (earlier) stamps.
    _feed_forward(engine, t0 - 10.0, 3, npts)
    assert key in engine._replay_watermark
    assert engine._replay_watermark[key] < t0, (
        "fresh backward-stepped packets were dropped instead of accepted"
    )


def test_windows_match_confirms_replay_rejects_new_data() -> None:
    rng = np.random.default_rng(1)
    w = rng.standard_normal(500).astype(np.float32)
    # Identical → match; shifted by 1 sample (sub-sample-jitter proxy) → match.
    assert _windows_match(w, w.copy())
    assert _windows_match(w[1:], w[:-1])
    # A different waveform → rejected (this is the science-path guard).
    assert not _windows_match(w, rng.standard_normal(500).astype(np.float32))
    # Too few samples to confirm → rejected.
    assert not _windows_match(w[:10], w[:10].copy())
    # Constant windows: exact-equality fallback, offset-invariance must NOT
    # make two different constants match.
    c = np.full(200, 3.0, dtype=np.float32)
    assert _windows_match(c, c.copy())
    assert not _windows_match(c, np.full(200, 4.0, dtype=np.float32))


def test_unconfirmed_backward_step_is_kept_not_dropped(
    qtbot, _replay_engine, capture_structlog
) -> None:
    """A >= 3 s backward step whose samples do NOT match already-held data is a
    genuine device clock reset carrying new content — it must be KEPT (deferred
    to the gap detector), never dropped as a phantom replay. Guards the
    code-review blocker: the 3 s magnitude gate alone must not decide."""
    from obspy import Trace, UTCDateTime

    engine, archive_root = _replay_engine
    npts = 100
    t0 = UTCDateTime("2026-06-30T00:00:00")
    _feed_forward(engine, t0, 20, npts)  # ramp data; frontier at t0 + 3.998 s

    # A block 3.6 s behind the frontier but carrying DIFFERENT samples
    # (random, not the ring's ramp) — a real reset, not a re-send.
    rng = np.random.default_rng(42)
    reset = Trace(
        data=rng.integers(-1000, 1000, npts).astype(np.int32),
        header={
            "network": "XX",
            "station": "RPL",
            "location": "00",
            "channel": "HHZ",
            "sampling_rate": _FS,
            "starttime": t0 + 0.4,
        },
    )
    engine._on_packet("dev", reset)

    assert _wait_until(
        lambda: sum(tr.stats.npts for tr in _on_disk(archive_root)) >= 21 * npts,
        timeout_s=15.0,
        qtbot=qtbot,
    )
    total = sum(tr.stats.npts for tr in _on_disk(archive_root))
    assert total == 21 * npts, "genuine-reset samples must be kept, not dropped"
    dedups = [
        r
        for r in capture_structlog
        if r.get("event") == "streaming_engine_reconnect_replay"
        and r.get("action") in ("drop", "trim")
    ]
    assert not dedups, "an unconfirmed backward step must not be dedup'd"
    # The gap detector's clock-jump handling still fires — observability of the
    # real event is preserved.
    assert any(r.get("event") == "gap_detector_clock_jump" for r in capture_structlog)
