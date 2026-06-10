"""Recent-detections-on-startup (M8 C3).

A DB pre-seeded with known detections is read back by the engine on
start (a bounded DB-index read, no waveforms) and pre-populates the
table as dimmed historical rows, honouring the configured limit.
"""

from __future__ import annotations

from pathlib import Path

from obspy import UTCDateTime
from PySide6.QtCore import Qt

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StaLtaStage,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import Detection
from echosmonitor.core.streaming_engine import StreamingEngine
from echosmonitor.gui.widgets.detection_table import DetectionTable
from echosmonitor.storage.dao import ArchiveDao


def _seed_db(root: Path, n: int) -> None:
    dao = ArchiveDao(root / "archive.db")
    dao.start_session("seed-host", "0.0.0", "seed")
    dev = dao.upsert_device("dev", "h", 1, {})
    sid = dao.upsert_stream(dev, ("IU", "ANMO", "00", "BHZ"), 100.0)
    for i in range(n):
        dao.record_detection(
            sid,
            Detection(
                device="dev",
                nslc="IU.ANMO.00.BHZ",
                kind="sta_lta",
                t_on=UTCDateTime("2026-06-01T00:00:00") + i,
                t_off=UTCDateTime("2026-06-01T00:00:01") + i,
                score=float(i),
                detected_at=UTCDateTime("2026-06-01T00:00:02") + i,
                meta={"on_thr": 3.5},
            ),
        )
    dao.close()


def _cfg(root: Path, limit: int) -> RootConfig:
    return RootConfig(
        app=AppConfig(archive_root=root),
        ui=UiConfig(recent_detections_limit=limit),
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


def test_engine_start_reads_recent_detections(qtbot, tmp_path: Path) -> None:
    _seed_db(tmp_path, n=5)
    engine = StreamingEngine(_cfg(tmp_path, limit=3))
    engine.start()
    try:
        recent = engine.recent_detections(3, since=UTCDateTime("2026-05-31T00:00:00"))
        assert len(recent) == 3  # limit respected (5 seeded, 3 requested)
        # Newest-first.
        assert recent[0].t_on > recent[-1].t_on
        assert engine.count_detections() == 5  # COUNT(*) sees all seeded rows
    finally:
        engine.stop()


def test_table_prepopulated_and_flagged_historical(qtbot, tmp_path: Path) -> None:
    _seed_db(tmp_path, n=4)
    engine = StreamingEngine(_cfg(tmp_path, limit=200))
    engine.start()
    try:
        recent = engine.recent_detections(200, since=UTCDateTime("2026-05-31T00:00:00"))
    finally:
        engine.stop()

    table = DetectionTable()
    qtbot.addWidget(table)
    table.load_historical(recent)
    model = table._model_for_test()
    assert model.rowCount() == 4
    # Every pre-loaded row is flagged historical (dimmed foreground brush).
    for r in range(model.rowCount()):
        assert model.data(model.index(r, 0), Qt.ItemDataRole.ForegroundRole) is not None


def test_limit_zero_disables_preload(qtbot, tmp_path: Path) -> None:
    _seed_db(tmp_path, n=3)
    engine = StreamingEngine(_cfg(tmp_path, limit=0))
    engine.start()
    try:
        # The engine guards limit<=0 → returns nothing even though rows exist.
        assert engine.recent_detections(0) == []
    finally:
        engine.stop()
