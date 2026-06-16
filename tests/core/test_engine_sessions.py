"""M2-B — recording sessions on ``StreamingEngine`` (rule 14).

Sessions are the archive unit: ``start_session`` roots every archive
write at ``<archive_root>/<sanitized_project>/`` with one ``archive.db``
at that session root; ``start_recording`` requires an active session;
``end_session`` closes the row cleanly and reverts the engine to the
sessionless monitoring index. Fake SeedLink servers feed real traces so
the layout assertions run against the genuine write path.
"""

from __future__ import annotations

import sqlite3
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
    StaLtaStage,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.exceptions import SessionError
from echosmonitor.core.models import AcquisitionState
from echosmonitor.core.session import SessionInfo
from echosmonitor.core.streaming_engine import StreamingEngine
from echosmonitor.storage.dao import ArchiveDao
from echosmonitor.storage.sessions import ProjectNameCollisionError
from tests.core.fakes import FakeSeedLinkServer, FakeSeedLinkServerConfig
from tests.core.test_seedlink_worker import _LoopThread, loop_thread  # noqa: F401
from tests.core.test_streaming_engine_multi import (
    make_fake_server,  # noqa: F401  pytest fixture re-export
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

_TIGHT_ARCHIVE = ArchiveConfig(enabled=True, fsync_interval_s=0.5, queue_max=256)


def _wait_until(predicate: Callable[[], bool], timeout_s: float, qtbot) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qtbot.wait(50)
        if predicate():
            return True
    return False


def _make_cfg(
    archive_root: Path,
    server: FakeSeedLinkServer,
    name: str = "dev",
) -> RootConfig:
    cfg = server.config
    return RootConfig(
        app=AppConfig(archive_root=archive_root),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
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
                archive=_TIGHT_ARCHIVE,
            )
        ],
    )


def _session_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, project_name, started_at, ended_at, closed_dirty FROM sessions"
            " ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


class _SessionSpy(QObject):
    """Collects ``sessionChanged`` payloads in order (rule-4 guard)."""

    def __init__(self, engine: StreamingEngine) -> None:
        super().__init__()
        self.payloads: list[SessionInfo | None] = []
        engine.sessionChanged.connect(self._on_session, type=Qt.ConnectionType.DirectConnection)

    @Slot(object)
    def _on_session(self, payload: object) -> None:
        assert payload is None or isinstance(payload, SessionInfo)
        self.payloads.append(payload)


# ---------------------------------------------------------------------------
# Rule 14: no session, no archive writes
# ---------------------------------------------------------------------------


def test_start_recording_without_session_raises(qtbot, make_fake_server) -> None:  # noqa: F811
    server = make_fake_server(_SERVER_CFG)
    engine = StreamingEngine(_make_cfg(Path("/nonexistent"), server))
    try:
        with pytest.raises(SessionError, match="no active session"):
            engine.start_recording("dev")
        assert engine._archive_writers == {}
    finally:
        engine.stop()


def test_start_session_creates_session_rooted_layout(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    """Acceptance (rule 14): Record creates ``<project>/`` SDS tree with
    one archive.db at the session root, the session row carries the raw
    project name, and the device is a member."""
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    engine = StreamingEngine(_make_cfg(archive_root, server))
    spy = _SessionSpy(engine)
    info = engine.start_session("Survey 2026", ["dev"])
    try:
        assert info.sanitized_name == "Survey_2026"
        assert info.devices == ("dev",)
        assert engine.active_session() == info
        assert engine.acquisition_state("dev") is AcquisitionState.RECORDING

        session_root = archive_root / "Survey_2026"
        assert (session_root / "archive.db").is_file()
        # The reading accessor agrees with the writers (one funnel).
        assert engine.archive_root("dev") == session_root

        assert _wait_until(
            lambda: any(p.is_file() for p in session_root.rglob("*.D.*")),
            timeout_s=10.0,
            qtbot=qtbot,
        ), f"no SDS file under session root; tree: {list(archive_root.rglob('*'))}"
        # Per-device segment sits directly below the session root.
        a_file = next(p for p in session_root.rglob("*.D.*") if p.is_file())
        assert a_file.relative_to(session_root).parts[0] == "dev"

        rows = _session_rows(session_root / "archive.db")
        assert len(rows) == 1
        assert rows[0]["project_name"] == "Survey 2026"
        assert rows[0]["ended_at"] is None

        # sessionChanged: start (no members yet) then membership growth.
        assert [p.devices for p in spy.payloads if p is not None] == [(), ("dev",)]
    finally:
        engine.stop()


def test_persist_session_stationxml_round_trips(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    """M6.6-B: a fetched StationXML blob persists into the active session's
    DB and reads back via the archive reader with no live device call."""
    from echosmonitor.storage.archive_reader import read_session_stationxml

    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    engine = StreamingEngine(_make_cfg(archive_root, server))
    xml = "<FDSNStationXML>persist-me</FDSNStationXML>"
    try:
        info = engine.start_session("Survey 2026", ["dev"])
        assert engine.persist_session_stationxml("dev", xml) is True
        db_path = archive_root / "Survey_2026" / "archive.db"
        assert read_session_stationxml(db_path, info.session_id, "dev") == xml
        # A device that is not a session member is not persisted.
        assert engine.persist_session_stationxml("ghost", xml) is False
    finally:
        engine.stop()


def test_persist_session_stationxml_no_session_is_noop(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    """With no recording session, persistence is a no-op (rule 14)."""
    server = make_fake_server(_SERVER_CFG)
    engine = StreamingEngine(_make_cfg(tmp_path / "archive", server))
    try:
        assert engine.persist_session_stationxml("dev", "<x/>") is False
    finally:
        engine.stop()


def test_second_session_while_active_raises(qtbot, tmp_path: Path, make_fake_server) -> None:  # noqa: F811
    server = make_fake_server(_SERVER_CFG)
    engine = StreamingEngine(_make_cfg(tmp_path / "archive", server))
    engine.start_session("one", [])
    try:
        with pytest.raises(SessionError, match="already active"):
            engine.start_session("two", [])
        assert engine.active_session() is not None
        assert engine.active_session().project_name == "one"  # type: ignore[union-attr]
    finally:
        engine.stop()


def test_start_session_unknown_device_leaves_no_session(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    engine = StreamingEngine(_make_cfg(archive_root, server))
    try:
        with pytest.raises(KeyError):
            engine.start_session("proj", ["dev", "typo"])
        assert engine.active_session() is None
        assert not (archive_root / "proj").exists()
    finally:
        engine.stop()


def test_blank_project_name_raises(qtbot, tmp_path: Path, make_fake_server) -> None:  # noqa: F811
    server = make_fake_server(_SERVER_CFG)
    engine = StreamingEngine(_make_cfg(tmp_path / "archive", server))
    try:
        with pytest.raises(SessionError, match="empty"):
            engine.start_session("   ", [])
    finally:
        engine.stop()


def test_colliding_project_name_rejected(qtbot, tmp_path: Path, make_fake_server) -> None:  # noqa: F811
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    # Pre-existing project "Survey 2026" lives at Survey_2026/.
    pre_root = archive_root / "Survey_2026"
    pre_root.mkdir(parents=True)
    dao = ArchiveDao(pre_root / "archive.db")
    dao.end_session(dao.start_session("h", "v", "c", project_name="Survey 2026"))
    dao.close()

    engine = StreamingEngine(_make_cfg(archive_root, server))
    try:
        with pytest.raises(ProjectNameCollisionError):
            engine.start_session("Survey_2026", ["dev"])
        assert engine.active_session() is None
        assert engine._archive_writers == {}
    finally:
        engine.stop()


# ---------------------------------------------------------------------------
# Ending a session
# ---------------------------------------------------------------------------


def test_end_session_downgrades_members_and_closes_row(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    engine = StreamingEngine(_make_cfg(archive_root, server))
    spy = _SessionSpy(engine)
    engine.start_session("proj", ["dev"])
    try:
        db_path = archive_root / "proj" / "archive.db"
        assert _wait_until(
            lambda: any(p.is_file() for p in (archive_root / "proj").rglob("*.D.*")),
            timeout_s=10.0,
            qtbot=qtbot,
        )
        engine.end_session()
        assert engine.active_session() is None
        assert engine.acquisition_state("dev") is AcquisitionState.MONITORING
        assert engine._archive_writers == {}
        assert spy.payloads[-1] is None
        # Reading accessor reverts to the base root between sessions.
        assert engine.archive_root("dev") == archive_root
        rows = _session_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["ended_at"] is not None
        assert rows[0]["closed_dirty"] == 0
        # Idempotent.
        engine.end_session()
    finally:
        engine.stop()


def test_engine_stop_closes_active_session_cleanly(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    engine = StreamingEngine(_make_cfg(archive_root, server))
    engine.start_session("proj", ["dev"])
    qtbot.wait(300)
    engine.stop()
    rows = _session_rows(archive_root / "proj" / "archive.db")
    assert len(rows) == 1
    assert rows[0]["ended_at"] is not None
    assert rows[0]["closed_dirty"] == 0
    assert engine.active_session() is None


def test_crashed_session_closed_dirty_on_next_open(
    qtbot,
    tmp_path: Path,
    make_fake_server,  # noqa: F811
) -> None:
    """A session row left open (crash) is closed-as-dirty when the same
    project is opened again; the new session row stays clean."""
    server = make_fake_server(_SERVER_CFG)
    archive_root = tmp_path / "archive"
    pre_root = archive_root / "proj"
    pre_root.mkdir(parents=True)
    dao = ArchiveDao(pre_root / "archive.db")
    dao.start_session("h", "v", "c", project_name="proj")  # never ended
    dao.close()

    engine = StreamingEngine(_make_cfg(archive_root, server))
    engine.start_session("proj", [])
    try:
        rows = _session_rows(pre_root / "archive.db")
        assert len(rows) == 2
        assert rows[0]["closed_dirty"] == 1
        assert rows[0]["ended_at"] is not None
        assert rows[1]["closed_dirty"] == 0
        assert rows[1]["ended_at"] is None
    finally:
        engine.stop()


# ---------------------------------------------------------------------------
# DAO context swap: sessionless monitoring index <-> session DB
# ---------------------------------------------------------------------------


def test_detection_dao_swaps_to_session_db_and_back(qtbot, tmp_path: Path) -> None:
    """A monitoring detection-capable device uses the base-root index;
    during a session the session DB takes over; after end_session the
    base index returns (open question 3 interim)."""
    archive_root = tmp_path / "archive"
    cfg = RootConfig(
        app=AppConfig(archive_root=archive_root),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name="dev",
                host="127.0.0.1",
                port=1,  # never connects; DAO creation is config-driven
                reconnect=ReconnectConfig(initial_delay_s=3600.0, max_delay_s=3600.0),
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
                dsp_chain=[
                    StaLtaStage(
                        type="sta_lta", sta=1.0, lta=10.0, on_threshold=3.5, off_threshold=1.5
                    )
                ],
            )
        ],
    )
    engine = StreamingEngine(cfg)
    engine.start_monitoring("dev")
    try:
        base_db = archive_root / "archive.db"
        assert engine._archive_db_path == base_db
        engine.start_session("proj", [])
        assert engine._archive_db_path == archive_root / "proj" / "archive.db"
        # The base index's sessionless row closed cleanly on the swap.
        assert _session_rows(base_db)[0]["ended_at"] is not None
        engine.end_session()
        # Monitoring detections persist again: base index re-opened.
        assert engine._archive_db_path == base_db
        rows = _session_rows(base_db)
        assert len(rows) == 2
        assert rows[1]["ended_at"] is None  # the re-opened sessionless row
    finally:
        engine.stop()


def test_failed_session_start_restores_detection_index(qtbot, tmp_path: Path, monkeypatch) -> None:
    """If the session DB cannot be opened/written AFTER the sessionless
    monitoring index was closed, the index is restored before the error
    surfaces — detections never silently stop persisting (code-reviewer
    major 2 on the M2-B diff; rule 8)."""
    archive_root = tmp_path / "archive"
    cfg = RootConfig(
        app=AppConfig(archive_root=archive_root),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name="dev",
                host="127.0.0.1",
                port=1,
                reconnect=ReconnectConfig(initial_delay_s=3600.0, max_delay_s=3600.0),
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
                dsp_chain=[
                    StaLtaStage(
                        type="sta_lta", sta=1.0, lta=10.0, on_threshold=3.5, off_threshold=1.5
                    )
                ],
            )
        ],
    )
    engine = StreamingEngine(cfg)
    engine.start_monitoring("dev")
    try:
        base_db = archive_root / "archive.db"
        assert engine._archive_db_path == base_db

        # Fail only the SESSION DB's row insert; the sessionless restore
        # path (no project_name) must keep working, as it would when
        # only the project directory is unwritable.
        real_start = ArchiveDao.start_session

        def _boom(self: ArchiveDao, *args: object, **kwargs: object) -> int:
            if kwargs.get("project_name") == "proj":
                raise sqlite3.OperationalError("disk I/O error (simulated)")
            return real_start(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ArchiveDao, "start_session", _boom)
        with pytest.raises(sqlite3.OperationalError):
            engine.start_session("proj", [])
        monkeypatch.undo()

        assert engine.active_session() is None
        # The sessionless index is back: detections keep persisting.
        assert engine._archive_dao is not None
        assert engine._archive_db_path == base_db
    finally:
        engine.stop()


def test_base_monitoring_index_sweeps_dirty_rows_on_open(qtbot, tmp_path: Path) -> None:
    """A crash while monitoring leaves an open sessionless row in the
    base index; the next open closes it as dirty (code-reviewer minor 4
    — the sweep is not session-DB-only)."""
    archive_root = tmp_path / "archive"
    archive_root.mkdir(parents=True)
    dao = ArchiveDao(archive_root / "archive.db")
    dao.start_session("h", "v", "c")  # sessionless row, never ended
    dao.close()

    cfg = RootConfig(
        app=AppConfig(archive_root=archive_root),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name="dev",
                host="127.0.0.1",
                port=1,
                reconnect=ReconnectConfig(initial_delay_s=3600.0, max_delay_s=3600.0),
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
                dsp_chain=[
                    StaLtaStage(
                        type="sta_lta", sta=1.0, lta=10.0, on_threshold=3.5, off_threshold=1.5
                    )
                ],
            )
        ],
    )
    engine = StreamingEngine(cfg)
    engine.start_monitoring("dev")
    try:
        rows = _session_rows(archive_root / "archive.db")
        assert len(rows) == 2
        assert rows[0]["closed_dirty"] == 1  # the crashed row, swept
        assert rows[1]["ended_at"] is None  # the live sessionless row
    finally:
        engine.stop()
