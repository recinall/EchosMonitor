"""Archive export worker (M3-C) — off-thread reads + atomic writes.

Skill ``qt-worker-threading`` §7 obligations for a new worker
(start→stop→start, stop-during-busy) plus the contract that makes this
worker DIFFERENT from the read loaders: exports are a serial queue —
a second request must NOT cancel an in-flight one (each is an explicit
"save this file").
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import obspy
from obspy import Stream, Trace, UTCDateTime
from PySide6.QtCore import QObject, Slot

from echosmonitor.core.archive_export_worker import (
    ArchiveExportLoader,
    ArchiveExportResult,
)
from echosmonitor.core.models import StreamID
from echosmonitor.storage import archive_reader as archive_reader_mod
from echosmonitor.storage.sds import device_sds_root, sds_path

_FS = 100.0
_T0 = UTCDateTime("2026-05-10T12:00:00")
_DEVICE = "echos-1"


def _nslc(comp: str) -> str:
    return f"XX.STA.00.HH{comp}"


def _group() -> dict[str, str]:
    return {c: _nslc(c) for c in ("Z", "N", "E")}


def _write_component(
    root: Path, comp: str, *, gap: bool = False
) -> list[Trace]:
    """30 s of data (with an optional mid-window gap) at the canonical path."""
    sid = StreamID("XX", "STA", "00", f"HH{comp}")
    path = sds_path(device_sds_root(root, _DEVICE), _T0, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    hdr = {
        "network": "XX",
        "station": "STA",
        "location": "00",
        "channel": f"HH{comp}",
        "sampling_rate": _FS,
    }
    rng = np.random.default_rng(abs(hash(comp)) % (2**32))

    def _tr(start: UTCDateTime, npts: int) -> Trace:
        return Trace(
            data=(rng.standard_normal(npts) * 1000.0).astype(np.int32),
            header={**hdr, "starttime": start},
        )

    traces = (
        [_tr(_T0, int(_FS * 10)), _tr(_T0 + 20, int(_FS * 10))]
        if gap
        else [_tr(_T0, int(_FS * 30))]
    )
    Stream(traces).write(str(path), format="MSEED")
    return traces


class _Collector(QObject):
    def __init__(self, loader: ArchiveExportLoader) -> None:
        super().__init__()
        self.done: list[ArchiveExportResult] = []
        self.failed: list[tuple[int, str]] = []
        self.empty: list[int] = []
        loader.exported.connect(self._on_done)
        loader.failed.connect(lambda tok, msg: self.failed.append((tok, msg)))
        loader.empty.connect(self.empty.append)

    @Slot(object)
    def _on_done(self, payload: object) -> None:
        if isinstance(payload, ArchiveExportResult):
            self.done.append(payload)


def test_mseed_export_roundtrips_archive_samples(qtbot, tmp_path: Path) -> None:
    root = tmp_path / "session"
    originals = {c: _write_component(root, c, gap=(c == "Z")) for c in ("Z", "N", "E")}
    out = tmp_path / "out.mseed"
    loader = ArchiveExportLoader()
    col = _Collector(loader)
    try:
        loader.request(
            "mseed", _DEVICE, _group(), float(_T0), float(_T0 + 30), str(root), None, str(out)
        )
        qtbot.waitUntil(lambda: bool(col.done) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        assert out.is_file()
        back = obspy.read(str(out))
        # Gapped Z survives as two records; N/E one each → 4 traces total.
        assert len(back) == 4
        z_back = back.select(channel="HHZ").sort()
        for orig, rt in zip(originals["Z"], z_back, strict=True):
            np.testing.assert_array_equal(rt.data, orig.data)
    finally:
        loader.shutdown()


def test_csv_export_has_grid_and_gap_cells(qtbot, tmp_path: Path) -> None:
    root = tmp_path / "session"
    for c in ("Z", "N", "E"):
        _write_component(root, c, gap=(c == "Z"))
    out = tmp_path / "out.csv"
    loader = ArchiveExportLoader()
    col = _Collector(loader)
    try:
        loader.request(
            "csv", _DEVICE, _group(), float(_T0), float(_T0 + 30), str(root), None, str(out)
        )
        qtbot.waitUntil(lambda: bool(col.done) or bool(col.failed), timeout=10_000)
        assert not col.failed, col.failed
        lines = out.read_text().splitlines()
        header_idx = next(i for i, line in enumerate(lines) if line.startswith("time_iso"))
        meta = "\n".join(lines[:header_idx])
        assert "units: counts" in meta
        header = lines[header_idx].split(",")
        assert header[:2] == ["time_iso", "epoch"]
        assert set(header[2:]) == set(_group().values())
        rows = lines[header_idx + 1 :]
        assert len(rows) == int(_FS * 30) + 1  # the shared 1/fs grid
        z_col = header.index(_nslc("Z"))
        mid = rows[int(_FS * 15)].split(",")  # inside Z's gap
        assert mid[z_col] == ""
        assert mid[header.index(_nslc("N"))] != ""
    finally:
        loader.shutdown()


def test_empty_interval_emits_empty_and_writes_nothing(qtbot, tmp_path: Path) -> None:
    out = tmp_path / "out.mseed"
    loader = ArchiveExportLoader()
    col = _Collector(loader)
    try:
        loader.request(
            "mseed",
            _DEVICE,
            _group(),
            float(_T0),
            float(_T0 + 30),
            str(tmp_path / "nothing_here"),
            None,
            str(out),
        )
        qtbot.waitUntil(lambda: bool(col.empty) or bool(col.failed), timeout=10_000)
        assert col.empty and not col.failed
        assert not out.exists()
    finally:
        loader.shutdown()


def test_failure_emits_failed_and_thread_survives(qtbot, tmp_path: Path) -> None:
    root = tmp_path / "session"
    for c in ("Z", "N", "E"):
        _write_component(root, c)
    bad = tmp_path / "no_such_dir" / "out.mseed"
    good = tmp_path / "ok.mseed"
    loader = ArchiveExportLoader()
    col = _Collector(loader)
    try:
        loader.request(
            "mseed", _DEVICE, _group(), float(_T0), float(_T0 + 30), str(root), None, str(bad)
        )
        qtbot.waitUntil(lambda: bool(col.failed), timeout=10_000)
        assert not bad.exists()
        # The worker thread survives — the next export completes.
        loader.request(
            "mseed", _DEVICE, _group(), float(_T0), float(_T0 + 30), str(root), None, str(good)
        )
        qtbot.waitUntil(lambda: bool(col.done), timeout=10_000)
        assert good.is_file()
    finally:
        loader.shutdown()


def test_exports_are_a_serial_queue_not_latest_wins(qtbot, tmp_path: Path) -> None:
    """The deliberate deviation from the read loaders: BOTH back-to-back
    exports complete — the second never cancels the first."""
    root = tmp_path / "session"
    for c in ("Z", "N", "E"):
        _write_component(root, c)
    out1 = tmp_path / "one.mseed"
    out2 = tmp_path / "two.csv"
    loader = ArchiveExportLoader()
    col = _Collector(loader)
    try:
        tok1 = loader.request(
            "mseed", _DEVICE, _group(), float(_T0), float(_T0 + 30), str(root), None, str(out1)
        )
        tok2 = loader.request(
            "csv", _DEVICE, _group(), float(_T0), float(_T0 + 30), str(root), None, str(out2)
        )
        qtbot.waitUntil(lambda: len(col.done) >= 2 or bool(col.failed), timeout=15_000)
        assert not col.failed, col.failed
        assert {r.token for r in col.done} == {tok1, tok2}
        assert out1.is_file() and out2.is_file()
    finally:
        loader.shutdown()


def test_mixed_sample_rates_fail_csv_with_clear_message(qtbot, tmp_path: Path) -> None:
    root = tmp_path / "session"
    _write_component(root, "Z")
    # N at a different rate — cannot share the CSV grid.
    sid = StreamID("XX", "STA", "00", "HHN")
    path = sds_path(device_sds_root(root, _DEVICE), _T0, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    Trace(
        data=np.zeros(int(50.0 * 30), dtype=np.int32),
        header={
            "network": "XX",
            "station": "STA",
            "location": "00",
            "channel": "HHN",
            "starttime": _T0,
            "sampling_rate": 50.0,
        },
    ).write(str(path), format="MSEED")
    out = tmp_path / "out.csv"
    loader = ArchiveExportLoader()
    col = _Collector(loader)
    try:
        loader.request(
            "csv",
            _DEVICE,
            {"Z": _nslc("Z"), "N": _nslc("N")},
            float(_T0),
            float(_T0 + 30),
            str(root),
            None,
            str(out),
        )
        qtbot.waitUntil(lambda: bool(col.failed), timeout=10_000)
        assert "sample rates" in col.failed[0][1]
        assert not out.exists()
    finally:
        loader.shutdown()


def test_shutdown_during_busy_export_is_bounded_and_leaves_no_file(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "session"
    for c in ("Z", "N", "E"):
        _write_component(root, c)
    out = tmp_path / "out.mseed"

    real_read = archive_reader_mod.ArchiveReader.read_window
    started = {"n": 0}

    def _slow_read(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        started["n"] += 1
        time.sleep(0.6)  # each component read straddles the shutdown
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(archive_reader_mod.ArchiveReader, "read_window", _slow_read)

    loader = ArchiveExportLoader()
    col = _Collector(loader)
    loader.request(
        "mseed", _DEVICE, _group(), float(_T0), float(_T0 + 30), str(root), None, str(out)
    )
    qtbot.waitUntil(lambda: started["n"] >= 1, timeout=10_000)

    t0 = time.monotonic()
    loader.shutdown()
    assert time.monotonic() - t0 < 4.5  # the join bound (rule 7)
    qtbot.wait(100)
    assert not col.done
    assert not out.exists()  # cancelled export produced nothing
    assert not Path(str(out) + ".tmp").exists()

    # start → stop → start (skill §7): the loader serves again.
    monkeypatch.undo()
    try:
        loader.request(
            "mseed", _DEVICE, _group(), float(_T0), float(_T0 + 30), str(root), None, str(out)
        )
        qtbot.waitUntil(lambda: bool(col.done), timeout=10_000)
        assert out.is_file()
    finally:
        loader.shutdown()
