"""Integration: STA/LTA triggers persist as detections + announce.

Plants a transient into a synthetic stream fed straight through the
engine's packet path (no fake server needed), drives the real DSP
chain on the DSP thread, and asserts:

* ``detectionRecorded`` fires (rule 8 — after the DB commit),
* the ``detections`` row lands with ``t_on`` near the true onset,
* the row's ``t_off`` is set once the trigger closes,
* persistence works with ``archive.enabled=False`` (the start-time
  DAO-ensure path for detection-only devices).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from obspy import Trace, UTCDateTime
from PySide6.QtCore import QObject, Qt, Slot

from echosmonitor.config.schema import (
    AppConfig,
    DetrendStage,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StaLtaStage,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import Detection
from echosmonitor.core.streaming_engine import StreamingEngine

_FS = 100.0
_NSLC = "IU.ANMO.00.BHZ"
_ONSET_S = 50.0


class _DetectionSpy(QObject):
    def __init__(self, engine: StreamingEngine) -> None:
        super().__init__()
        self.recorded: list[Detection] = []
        self.updated: list[Detection] = []
        engine.detectionRecorded.connect(self._on_recorded, type=Qt.ConnectionType.DirectConnection)
        engine.detectionUpdated.connect(self._on_updated, type=Qt.ConnectionType.DirectConnection)

    @Slot(object)
    def _on_recorded(self, detection: object) -> None:
        assert isinstance(detection, Detection)
        self.recorded.append(detection)

    @Slot(object)
    def _on_updated(self, detection: object) -> None:
        assert isinstance(detection, Detection)
        self.updated.append(detection)


def _wait_until(predicate, timeout_s: float, qtbot) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qtbot.wait(50)
        if predicate():
            return True
    return predicate()


def _signal_with_burst(t0: UTCDateTime) -> np.ndarray:
    """60 s of noise with a 2 s 10x burst planted at ``_ONSET_S``."""
    n_total = int(60 * _FS)
    rng = np.random.default_rng(seed=8)
    x = rng.standard_normal(n_total)
    onset = int(_ONSET_S * _FS)
    x[onset : onset + int(2.0 * _FS)] += 10.0 * rng.standard_normal(int(2.0 * _FS))
    return x


def _packet(x: np.ndarray, i0: int, n: int, t0: UTCDateTime) -> Trace:
    seg = x[i0 : i0 + n].astype(np.float32)
    return Trace(
        data=seg,
        header={
            "network": "IU",
            "station": "ANMO",
            "location": "00",
            "channel": "BHZ",
            "sampling_rate": _FS,
            "starttime": t0 + i0 / _FS,
        },
    )


@pytest.fixture
def detection_engine(qtbot, tmp_path: Path) -> Iterator[tuple[StreamingEngine, _DetectionSpy]]:
    cfg = RootConfig(
        app=AppConfig(archive_root=tmp_path),  # DB lands here; archive stays OFF
        ui=UiConfig(refresh_hz=20, default_window_seconds=70),
        devices=[
            DeviceConfig(
                name="dev",
                host="127.0.0.1",
                port=1,  # nothing listens; the worker idles, we feed packets by hand
                reconnect=ReconnectConfig(initial_delay_s=3600.0, max_delay_s=3600.0),
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
                dsp_chain=[
                    DetrendStage(type="detrend", kind="constant"),
                    StaLtaStage(
                        type="sta_lta", sta=1.0, lta=10.0, on_threshold=3.5, off_threshold=1.5
                    ),
                ],
            ),
        ],
    )
    engine = StreamingEngine(cfg)
    spy = _DetectionSpy(engine)
    engine.start()
    try:
        yield engine, spy
    finally:
        engine.stop()


def test_planted_transient_persists_and_announces(detection_engine, qtbot) -> None:
    engine, spy = detection_engine
    t0 = UTCDateTime("2026-06-01T00:00:00")
    x = _signal_with_burst(t0)
    chunk = int(_FS)  # 1 s packets

    # Phase 1: feed up to mid-burst (still open) and let the chain run.
    split = int(51 * _FS)
    for i0 in range(0, split, chunk):
        engine._on_packet("dev", _packet(x, i0, chunk, t0))
    assert _wait_until(lambda: len(spy.recorded) >= 1, timeout_s=5.0, qtbot=qtbot), (
        f"detectionRecorded never fired; recorded={spy.recorded}"
    )

    first = spy.recorded[0]
    assert first.device == "dev"
    assert first.nslc == _NSLC
    assert first.kind == "sta_lta"
    # rule-8: the row is durable by the time the signal fires.
    assert first.id is not None
    # onset lands near the planted transient (STA window + warm-up slack).
    drift = float(first.t_on - (t0 + _ONSET_S))
    assert -1.5 <= drift <= 1.5, f"onset drift {drift:.3f}s exceeds tolerance"
    # The detection's meta carries the STA/LTA params for the detail pane.
    assert first.meta["on_thr"] == pytest.approx(3.5)

    # Phase 2: feed the rest (burst ends) so the trigger closes.
    for i0 in range(split, int(60 * _FS), chunk):
        engine._on_packet("dev", _packet(x, i0, chunk, t0))

    # Eventually the DB row carries a finalised t_off (> t_on).
    def _closed() -> bool:
        rows = engine._archive_dao.recent_detections(limit=10)  # type: ignore[union-attr]
        return any(r.t_off is not None and r.t_off > r.t_on for r in rows)

    assert _wait_until(_closed, timeout_s=5.0, qtbot=qtbot), "detection never closed in the DB"

    rows = engine._archive_dao.recent_detections(limit=10)  # type: ignore[union-attr]
    assert len(rows) >= 1
    closed = [r for r in rows if r.t_off is not None]
    assert closed, f"no closed detection in DB; rows={rows}"
    assert engine._archive_dao.count_detections() >= 1  # type: ignore[union-attr]

    # DeviceStatus reflects the detection.
    status = engine.device_status()["dev"]
    assert status.detections_total >= 1
    assert status.last_detection_at is not None


def test_detection_persists_without_archive_enabled(detection_engine, qtbot) -> None:
    """The start-time DAO-ensure created the DB even though no device
    archives — proving detection persistence is independent of MSEED
    archiving (CLAUDE.md rule 8 boundary)."""
    engine, _spy = detection_engine
    assert engine._archive_dao is not None
    # And no archive writer/thread was spun up for this detection-only device.
    assert engine._archive_writers == {}
