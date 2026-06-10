"""HVSR over an ARCHIVED window (Stage C) — f0 recovery from the archive path.

Writes a synthetic 3-component SDS archive with a known injected resonance,
then recovers f0 through both the pure slice helper
(:func:`slice_archive_windows`) and the engine's
:meth:`HvsrEngine.start_archive_measurement` (which slices, feeds an
accumulator with ``provenance="archive"``, and runs one off-thread compute).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from obspy import Trace, UTCDateTime
from PySide6.QtCore import QObject, Signal
from scipy import signal

from echosmonitor.config.schema import AppConfig, RootConfig, UiConfig
from echosmonitor.core.hvsr import HvsrAccumulator, HvsrSettings, slice_archive_windows
from echosmonitor.core.hvsr_engine import HvsrEngine
from echosmonitor.core.models import StreamID
from echosmonitor.core.streaming_engine import StreamingEngine
from echosmonitor.storage.archive_reader import ArchiveReader
from echosmonitor.storage.sds import device_sds_root, sds_path

_FS = 100.0
_F0 = 6.0
_NET, _STA, _LOC = "XX", "ARC", "00"
_GROUP = {
    "Z": f"{_NET}.{_STA}.{_LOC}.HHZ",
    "N": f"{_NET}.{_STA}.{_LOC}.HHN",
    "E": f"{_NET}.{_STA}.{_LOC}.HHE",
}
_T0 = UTCDateTime("2026-05-10T12:00:00")
_DURATION_S = 600.0


def _resonant(n: int, rng: np.random.Generator) -> np.ndarray:
    b, a = signal.iirpeak(_F0 / (_FS / 2.0), 25.0)
    w = rng.standard_normal(n)
    return w * 0.3 + signal.lfilter(b, a, w) * 4.0


def _write_archive(root: Path) -> None:
    """Write Z (white) + N/E (resonant) MiniSEED day-files to an SDS root."""
    n = int(_DURATION_S * _FS)
    rng = np.random.default_rng(5)
    comp_data = {
        "HHZ": rng.standard_normal(n),
        "HHN": _resonant(n, rng),
        "HHE": _resonant(n, rng),
    }
    for cha, data in comp_data.items():
        sid = StreamID(_NET, _STA, _LOC, cha)
        # Scale all channels by the SAME factor so the H/V ratio is preserved,
        # then cast to int32 for STEIM/MSEED.
        tr = Trace(data=(data * 1000.0).astype(np.int32))
        tr.stats.network, tr.stats.station = sid.network, sid.station
        tr.stats.location, tr.stats.channel = sid.location, sid.channel
        tr.stats.sampling_rate = _FS
        tr.stats.starttime = _T0
        # Namespace under the same device ("dev") the slice/measurement
        # helpers pass to the reader, matching the device-namespaced SDS
        # layout the live writer produces.
        path = sds_path(device_sds_root(root, "dev"), _T0, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        tr.write(str(path), format="MSEED")


def test_slice_archive_windows_recovers_f0(tmp_path) -> None:
    """The pure slice helper + accumulator recover the injected f0."""
    root = tmp_path / "sds"
    _write_archive(root)
    reader = ArchiveReader(root)  # no DAO → SDS-scan fallback
    settings = HvsrSettings(window_length_s=30.0, freqmin_hz=0.5, freqmax_hz=40.0, resample_n=128)
    windows = slice_archive_windows(reader, "dev", _GROUP, _T0, _T0 + _DURATION_S, settings)
    assert len(windows) >= 15  # ~20 non-overlapping 30 s windows in 600 s

    acc = HvsrAccumulator(
        settings,
        same_response=True,
        same_response_detail="test",
        device="dev",
        station_key=f"{_NET}.{_STA}",
        provenance="archive",
    )
    for z, n, e, ws, fs in windows:
        acc.add_window(z, n, e, ws, fs)
    res = acc.compute()
    assert res.provenance == "archive"
    assert abs(res.f0_hz - _F0) / _F0 < 0.15, f"recovered f0={res.f0_hz}"


def test_slice_archive_windows_empty_range(tmp_path) -> None:
    """A range with no archived data yields no windows (honest, no fabrication)."""
    root = tmp_path / "sds"
    _write_archive(root)
    reader = ArchiveReader(root)
    settings = HvsrSettings(window_length_s=30.0)
    # A day with nothing written.
    far = UTCDateTime("2020-01-01T00:00:00")
    windows = slice_archive_windows(reader, "dev", _GROUP, far, far + 300, settings)
    assert windows == []


def _bare_engine() -> StreamingEngine:
    return StreamingEngine(RootConfig(app=AppConfig(), ui=UiConfig(), devices=[]))


class _Sink(QObject):
    got = Signal(object)


def test_engine_archive_measurement_recovers_f0(qtbot, tmp_path) -> None:
    """start_archive_measurement emits an archive-provenance result with f0."""
    root = tmp_path / "sds"
    _write_archive(root)
    reader = ArchiveReader(root)
    engine = _bare_engine()
    hv = HvsrEngine(engine, None)
    results: list[object] = []
    hv.hvsrUpdated.connect(results.append)

    settings = HvsrSettings(window_length_s=30.0, freqmin_hz=0.5, freqmax_hz=40.0, resample_n=128)
    mid = hv.start_archive_measurement("dev", _GROUP, _T0, _T0 + _DURATION_S, settings, reader)
    assert mid != ""
    try:
        qtbot.waitUntil(lambda: len(results) >= 1, timeout=30_000)
    finally:
        hv.shutdown()
        engine.stop()
    res = results[0]
    from echosmonitor.core.hvsr import HvsrResult

    assert isinstance(res, HvsrResult)
    assert res.provenance == "archive"
    assert abs(res.f0_hz - _F0) / _F0 < 0.15


def test_engine_archive_empty_returns_blank(qtbot, tmp_path) -> None:
    """An empty range returns "" (no measurement started)."""
    root = tmp_path / "sds"
    _write_archive(root)
    reader = ArchiveReader(root)
    engine = _bare_engine()
    hv = HvsrEngine(engine, None)
    far = UTCDateTime("2020-01-01T00:00:00")
    settings = HvsrSettings(window_length_s=30.0)
    try:
        mid = hv.start_archive_measurement("dev", _GROUP, far, far + 300, settings, reader)
        assert mid == ""
        assert hv.active_measurement() is None
    finally:
        hv.shutdown()
        engine.stop()
