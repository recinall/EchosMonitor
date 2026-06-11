"""Off-thread Archive-tab window loader — correctness + the rule-11 invariant.

The loader reads an arbitrary ``device`` + 3-component ``group`` over an
arbitrary interval from the SDS archive on a dedicated worker thread (mirroring
``ArchiveDetailLoader``/``HvsrEngine``), AND builds the primary component's
spectrogram off that thread, handing the GUI prepared arrays + a raw-power
image. Two properties matter:

* **Correctness**: it reads the right window per present component, keeps gaps
  as NaN (never interpolated), builds a non-degenerate spectrogram (rule 10:
  the image varies — not merely the right shape), emits ``empty`` when nothing
  is archived, ``failed`` (without crashing the thread) on a bad read, and is
  latest-wins.
* **The rule-11 invariant** (the one the reverted Archive Replay got wrong): a
  slow archive load MUST NOT stall the live drain. The test asserts the
  engine's live drain KEEPS ADVANCING while a deliberately-slow load runs.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from obspy import Stream, Trace, UTCDateTime
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    HighpassStage,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.archive_window_loader import (
    ArchiveWindowLoader,
    ArchiveWindowResult,
)
from echosmonitor.core.models import StreamID
from echosmonitor.core.streaming_engine import StreamingEngine
from echosmonitor.storage import archive_reader as archive_reader_mod
from echosmonitor.storage.sds import device_sds_root, sds_path

_NET, _STA, _LOC = "IU", "ANMO", "00"
_DEVICE = "anmo"
_FS = 100.0
_PREFIX = "BH"


def _nslc(comp: str) -> str:
    return f"{_NET}.{_STA}.{_LOC}.{_PREFIX}{comp}"


def _group() -> dict[str, str]:
    return {c: _nslc(c) for c in ("Z", "N", "E")}


def _write_trace(root: Path, comp: str, t0: UTCDateTime, npts: int, device: str = _DEVICE) -> None:
    sid = StreamID(_NET, _STA, _LOC, f"{_PREFIX}{comp}")
    path = sds_path(device_sds_root(root, device), t0, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(abs(hash(comp)) % (2**32))
    tr = Trace(
        data=(rng.standard_normal(npts) * 1000.0).astype(np.int32),
        header={
            "network": _NET,
            "station": _STA,
            "location": _LOC,
            "channel": f"{_PREFIX}{comp}",
            "starttime": t0,
            "sampling_rate": _FS,
        },
    )
    tr.write(str(path), format="MSEED")


def _write_gapped_trace(root: Path, comp: str, t0: UTCDateTime, npts: int, gap_at: int) -> None:
    sid = StreamID(_NET, _STA, _LOC, f"{_PREFIX}{comp}")
    path = sds_path(device_sds_root(root, _DEVICE), t0, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    hdr = {
        "network": _NET,
        "station": _STA,
        "location": _LOC,
        "channel": f"{_PREFIX}{comp}",
        "sampling_rate": _FS,
    }
    first = Trace(
        data=(rng.standard_normal(gap_at) * 1000.0).astype(np.int32),
        header={**hdr, "starttime": t0},
    )
    second = Trace(
        data=(rng.standard_normal(npts - gap_at) * 1000.0).astype(np.int32),
        header={**hdr, "starttime": t0 + (gap_at + gap_at) / _FS},
    )
    Stream(traces=[first, second]).write(str(path), format="MSEED")


class _Collector(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.loaded: list[ArchiveWindowResult] = []
        self.empty: list[int] = []
        self.failed: list[tuple[int, str]] = []

    def bind(self, loader: ArchiveWindowLoader) -> None:
        loader.loaded.connect(self._on_loaded)
        loader.empty.connect(self.empty.append)
        loader.failed.connect(lambda tok, msg: self.failed.append((tok, msg)))

    @Slot(object)
    def _on_loaded(self, payload: object) -> None:
        if isinstance(payload, ArchiveWindowResult):
            self.loaded.append(payload)


def _t0() -> UTCDateTime:
    return UTCDateTime("2026-06-01T00:00:00")


def test_loads_three_components_and_spectrogram_off_thread(qtbot, tmp_path) -> None:
    root = tmp_path / "sds"
    t0 = _t0()
    for comp in ("Z", "N", "E"):
        _write_trace(root, comp, t0, npts=int(_FS * 30))  # 30 s

    loader = ArchiveWindowLoader(None)
    col = _Collector()
    col.bind(loader)
    try:
        loader.request(_DEVICE, _group(), float(t0), float(t0 + 30.0), str(root))
        qtbot.waitUntil(lambda: bool(col.loaded) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        res = col.loaded[-1]
        assert {t.comp for t in res.traces} == {"Z", "N", "E"}
        assert res.traces[0].comp == "Z"  # Z first

        # Spectrogram built off-thread: right shape AND non-degenerate (rule 10).
        assert res.spec_power is not None and res.spec_freqs is not None
        n_freq, n_cols = res.spec_power.shape
        assert n_freq == res.spec_freqs.shape[0]
        assert n_cols > 1
        assert float(np.var(res.spec_power)) > 0.0
        # The image's X extent maps to real wall-clock epochs INSIDE the window
        # (rule-10 time-axis corollary — assert the mapping, not just ordering).
        # column_dt = step/fs = (200-100)/100 = 1.0 s at fs=100; the extent is
        # exactly n_cols hops and never overruns the 30 s window.
        from echosmonitor.dsp.spectrogram import RollingSpectrogram

        column_dt = RollingSpectrogram(_FS).column_dt
        assert res.spec_t_start == float(t0)
        assert res.spec_t_end == pytest.approx(float(t0) + n_cols * column_dt, abs=1e-6)
        assert float(t0) < res.spec_t_end <= float(t0) + 30.0 + column_dt
    finally:
        loader.shutdown()


def test_gap_renders_as_nan_break(qtbot, tmp_path) -> None:
    root = tmp_path / "sds"
    t0 = _t0()
    _write_gapped_trace(root, "Z", t0, npts=int(_FS * 30), gap_at=int(_FS * 10))
    _write_trace(root, "N", t0, npts=int(_FS * 30))
    _write_trace(root, "E", t0, npts=int(_FS * 30))

    loader = ArchiveWindowLoader(None)
    col = _Collector()
    col.bind(loader)
    try:
        loader.request(_DEVICE, _group(), float(t0), float(t0 + 30.0), str(root))
        qtbot.waitUntil(lambda: bool(col.loaded) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        z = next(t for t in col.loaded[-1].traces if t.comp == "Z")
        assert np.isnan(z.y).any(), "gap must remain a NaN break, never interpolated"
    finally:
        loader.shutdown()


def test_short_window_has_traces_but_no_spectrogram(qtbot, tmp_path) -> None:
    """A window shorter than one STFT segment yields traces but no image."""
    root = tmp_path / "sds"
    t0 = _t0()
    for comp in ("Z", "N", "E"):
        _write_trace(root, comp, t0, npts=int(_FS * 10))
    loader = ArchiveWindowLoader(None)
    col = _Collector()
    col.bind(loader)
    try:
        # 1 s window << nperseg (2 s) → no full STFT segment.
        loader.request(_DEVICE, _group(), float(t0), float(t0 + 1.0), str(root))
        qtbot.waitUntil(lambda: bool(col.loaded) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        res = col.loaded[-1]
        assert res.traces  # waveform still rendered
        assert res.spec_power is None
    finally:
        loader.shutdown()


def test_no_data_emits_empty(qtbot, tmp_path) -> None:
    loader = ArchiveWindowLoader(None)
    col = _Collector()
    col.bind(loader)
    try:
        loader.request(_DEVICE, _group(), float(_t0()), float(_t0() + 30.0), str(tmp_path / "sds"))
        qtbot.waitUntil(lambda: bool(col.empty) or bool(col.failed), timeout=10_000)
        assert col.empty and not col.failed
    finally:
        loader.shutdown()


def test_corrupt_path_emits_failed_without_crashing_thread(qtbot, tmp_path, monkeypatch) -> None:
    root = tmp_path / "sds"
    t0 = _t0()
    _write_trace(root, "Z", t0, npts=int(_FS * 30))

    def _boom(self, *a, **k):  # type: ignore[no-untyped-def]
        raise OSError("simulated archive corruption")

    monkeypatch.setattr(archive_reader_mod.ArchiveReader, "read_window", _boom)

    loader = ArchiveWindowLoader(None)
    col = _Collector()
    col.bind(loader)
    try:
        loader.request(_DEVICE, _group(), float(t0), float(t0 + 30.0), str(root))
        qtbot.waitUntil(lambda: bool(col.failed), timeout=10_000)
        assert col.failed
        # The worker thread survives — a second request still completes.
        monkeypatch.undo()
        loader.request(_DEVICE, {"Z": _nslc("Z")}, float(t0), float(t0 + 30.0), str(root))
        qtbot.waitUntil(lambda: bool(col.loaded), timeout=10_000)
        assert col.loaded
    finally:
        loader.shutdown()


def test_per_request_db_path_enables_index_backed_discovery(qtbot, tmp_path) -> None:
    """M3-A stale-DAO fix: the request's ``db_path`` is opened read-only per
    load. Pinned by a file at a NON-canonical path that only the ``files``
    index knows: with ``db_path`` it loads, without it the scan misses it."""
    from echosmonitor.storage.dao import ArchiveDao

    root = tmp_path / "session_root"
    t0 = _t0()
    # Write at the canonical path, then move it where only the index points.
    _write_trace(root, "Z", t0, npts=int(_FS * 30))
    sid = StreamID(_NET, _STA, _LOC, f"{_PREFIX}Z")
    canonical = sds_path(device_sds_root(root, _DEVICE), t0, sid)
    hidden = root / "off-grammar.mseed"
    canonical.rename(hidden)

    db = root / "archive.db"
    dao = ArchiveDao(db)
    dev_id = dao.upsert_device(_DEVICE, "h", 18000, {})
    stream_id = dao.upsert_stream(dev_id, (_NET, _STA, _LOC, f"{_PREFIX}Z"), _FS)
    dao.record_file(stream_id, hidden, t0, t0 + 30, hidden.stat().st_size)
    dao.close()

    loader = ArchiveWindowLoader(None)
    col = _Collector()
    col.bind(loader)
    try:
        # Without the index: the canonical scan finds nothing.
        loader.request(_DEVICE, {"Z": _nslc("Z")}, float(t0), float(t0 + 30.0), str(root))
        qtbot.waitUntil(lambda: bool(col.empty) or bool(col.failed), timeout=10_000)
        assert col.empty and not col.failed

        # With the request-scoped index: the hidden file is discovered.
        loader.request(
            _DEVICE,
            {"Z": _nslc("Z")},
            float(t0),
            float(t0 + 30.0),
            str(root),
            db_path=str(db),
        )
        qtbot.waitUntil(lambda: bool(col.loaded) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        assert {t.comp for t in col.loaded[-1].traces} == {"Z"}
    finally:
        loader.shutdown()


def test_unreadable_db_path_degrades_to_scan_not_failure(qtbot, tmp_path) -> None:
    """A corrupt/missing index is an accelerator loss, never a load failure."""
    root = tmp_path / "session_root"
    t0 = _t0()
    for comp in ("Z", "N", "E"):
        _write_trace(root, comp, t0, npts=int(_FS * 30))
    bad_db = root / "archive.db"
    bad_db.write_bytes(b"not sqlite")

    loader = ArchiveWindowLoader(None)
    col = _Collector()
    col.bind(loader)
    try:
        loader.request(
            _DEVICE, _group(), float(t0), float(t0 + 30.0), str(root), db_path=str(bad_db)
        )
        qtbot.waitUntil(lambda: bool(col.loaded) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        assert {t.comp for t in col.loaded[-1].traces} == {"Z", "N", "E"}
    finally:
        loader.shutdown()


def test_latest_wins_supersedes_stale_load(qtbot, tmp_path) -> None:
    root = tmp_path / "sds"
    t0 = _t0()
    for comp in ("Z", "N", "E"):
        _write_trace(root, comp, t0, npts=int(_FS * 30))
    loader = ArchiveWindowLoader(None)
    col = _Collector()
    col.bind(loader)
    try:
        tok1 = loader.request(_DEVICE, _group(), float(t0), float(t0 + 30.0), str(root))
        tok2 = loader.request(_DEVICE, _group(), float(t0), float(t0 + 20.0), str(root))
        assert tok2 > tok1
        qtbot.waitUntil(lambda: bool(col.loaded) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        # Only the latest token's result should be delivered to the GUI.
        assert all(r.token == tok2 for r in col.loaded)
    finally:
        loader.shutdown()


# ---------------------------------------------------------------------------
# THE rule-11 invariant: a slow archive load must not stall the live drain.
# ---------------------------------------------------------------------------

_LIVE_NET, _LIVE_STA, _LIVE_LOC = "XX", "DRAIN", "00"
_LIVE_DEVICE = "draingen"
_LIVE_CHANS = ("HHZ", "HHN", "HHE")


def _live_cfg(archive_dir: Path) -> RootConfig:
    return RootConfig(
        app=AppConfig(archive_root=str(archive_dir)),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name=_LIVE_DEVICE,
                host="192.0.2.1",
                port=18000,
                reconnect=ReconnectConfig(
                    initial_delay_s=3600.0, max_delay_s=3600.0, connect_timeout_s=0.5
                ),
                selectors=[
                    StreamSelectorConfig(
                        network=_LIVE_NET, station=_LIVE_STA, location=_LIVE_LOC, channel="HH?"
                    )
                ],
                dsp_chain=[HighpassStage(type="highpass", freq=1.0)],
            )
        ],
    )


def _live_trace(cha: str, t0: UTCDateTime, n: int, rng: np.random.Generator) -> Trace:
    tr = Trace(data=(rng.standard_normal(n) * 1000.0).astype(np.int32))
    tr.stats.network, tr.stats.station = _LIVE_NET, _LIVE_STA
    tr.stats.location, tr.stats.channel = _LIVE_LOC, cha
    tr.stats.sampling_rate = _FS
    tr.stats.starttime = t0
    return tr


class _LiveFeeder(QObject):
    packet = Signal(str, object)
    finished = Signal(int)

    def __init__(self, spp: int, n_packets: int) -> None:
        super().__init__()
        self._spp, self._n = spp, n_packets
        self._stop = False

    @Slot()
    def run(self) -> None:
        rng = np.random.default_rng(123)
        t0 = UTCDateTime(0)
        dt = self._spp / _FS
        total = 0
        for _ in range(self._n):
            if self._stop:
                break
            for cha in _LIVE_CHANS:
                self.packet.emit(_LIVE_DEVICE, _live_trace(cha, t0, self._spp, rng))
                total += self._spp
            t0 = t0 + dt
            QThread.msleep(max(1, int(dt * 1000)))
        self.finished.emit(total)

    def stop(self) -> None:
        self._stop = True


def test_slow_archive_load_does_not_stall_live_drain(qtbot, tmp_path, monkeypatch) -> None:
    """A deliberately-slow archive window load must not stall the live drain.

    The invariant the reverted Archive Replay got wrong: the read (and now the
    spectrogram build) run on the loader's worker thread, so while a slow load
    is in flight the engine's live drain keeps advancing and the DSP path drops
    nothing. We assert the drain ADVANCES across the load.
    """
    arch_root = tmp_path / "sds"
    a0 = UTCDateTime("2026-06-01T00:00:00")
    for comp in ("Z", "N", "E"):
        # Seed under the SAME device the loader request uses below
        # (_LIVE_DEVICE), so the device-namespaced reader finds the files.
        _write_trace(arch_root, comp, a0, npts=int(_FS * 30), device=_LIVE_DEVICE)

    real_read = archive_reader_mod.ArchiveReader.read_window

    def _slow_read(self, *a, **k):  # type: ignore[no-untyped-def]
        time.sleep(0.8)  # sleep releases the GIL — the GUI loop keeps draining
        return real_read(self, *a, **k)

    monkeypatch.setattr(archive_reader_mod.ArchiveReader, "read_window", _slow_read)

    engine = StreamingEngine(_live_cfg(tmp_path / "live"))
    processed = {"n": 0, "dropped": 0, "fed": 0, "done": False}
    engine.processedTraceReady.connect(
        lambda _d, _n, s: processed.__setitem__("n", processed["n"] + len(s)),
        type=Qt.ConnectionType.DirectConnection,
    )
    engine.chainDropped.connect(
        lambda _d, _n, c: processed.__setitem__("dropped", processed["dropped"] + int(c)),
        type=Qt.ConnectionType.DirectConnection,
    )
    engine.start()

    loader = ArchiveWindowLoader(None)
    col = _Collector()
    col.bind(loader)

    feeder = _LiveFeeder(spp=20, n_packets=25)  # ~5 s of feed (overlaps the slow load)
    feeder.finished.connect(lambda total: processed.__setitem__("fed", total))
    feeder.finished.connect(lambda _t: processed.__setitem__("done", True))
    fthread = QThread()
    feeder.moveToThread(fthread)
    feeder.packet.connect(engine._on_packet, type=Qt.ConnectionType.QueuedConnection)
    fthread.started.connect(feeder.run)
    fthread.start()
    try:
        qtbot.waitUntil(lambda: bool(engine._latest_raw_endtime), timeout=10_000)

        def _max_endtime() -> float:
            ends = list(engine._latest_raw_endtime.values())
            return max(float(e) for e in ends) if ends else 0.0

        t_before = _max_endtime()
        loader.request(_LIVE_DEVICE, _group(), float(a0), float(a0 + 20.0), str(arch_root))
        qtbot.waitUntil(lambda: bool(col.loaded) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        t_after = _max_endtime()

        assert t_after > t_before, (
            f"live drain did not advance during the archive load "
            f"({t_before} -> {t_after}) — the load stalled the GUI thread"
        )

        qtbot.waitUntil(lambda: processed["done"], timeout=15_000)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and processed["n"] < processed["fed"]:
            qtbot.wait(50)
        assert processed["fed"] > 0
        assert processed["dropped"] == 0, "DSP dropped packets during the archive load"
        assert processed["n"] == processed["fed"], (
            f"DSP saw {processed['n']}/{processed['fed']} samples"
        )
    finally:
        feeder.stop()
        fthread.quit()
        fthread.wait(2000)
        loader.shutdown()
        engine.stop()
