"""Waveform window exports (M3-C) — atomicity + fidelity.

The contract under test: MiniSEED exports carry exactly the archived
samples with gaps preserved as separate records; CSV exports put the
components on one shared grid with gaps as EMPTY cells; every failure
or cancellation path leaves NO file (neither destination nor temp) —
the atomic-write shape from skill ``miniseed-sds``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import obspy
import pytest
from obspy import Stream, Trace, UTCDateTime

from echosmonitor.storage.exports import (
    ArchiveExportError,
    ExportCancelledError,
    write_window_csv,
    write_window_mseed,
)

_FS = 100.0
_T0 = UTCDateTime("2026-05-10T12:00:00")


def _trace(start: UTCDateTime, npts: int, *, channel: str = "HHZ") -> Trace:
    rng = np.random.default_rng(npts)
    return Trace(
        data=(rng.standard_normal(npts) * 1000.0).astype(np.int32),
        header={
            "network": "XX",
            "station": "STA",
            "location": "00",
            "channel": channel,
            "starttime": start,
            "sampling_rate": _FS,
        },
    )


def _gappy_window() -> Stream:
    """What ArchiveReader.read_window returns: merged, gap left explicit."""
    st = Stream([_trace(_T0, 500), _trace(_T0 + 10, 500)])
    st.merge(method=0)
    return st


def test_mseed_roundtrip_preserves_samples_and_gap(tmp_path: Path) -> None:
    original = Stream([_trace(_T0, 500), _trace(_T0 + 10, 500)])
    window = _gappy_window()
    out = tmp_path / "win.mseed"

    n_bytes = write_window_mseed(window, out)

    assert out.is_file() and out.stat().st_size == n_bytes
    assert not (tmp_path / "win.mseed.tmp").exists()
    back = obspy.read(str(out))
    # The gap survives as two records, samples bit-identical.
    assert len(back) == 2
    for orig, rt in zip(original, back.sort(), strict=True):
        assert rt.stats.starttime == orig.stats.starttime
        np.testing.assert_array_equal(rt.data, orig.data)


def test_mseed_empty_stream_raises_and_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "win.mseed"
    with pytest.raises(ArchiveExportError):
        write_window_mseed(Stream(), out)
    assert not out.exists()
    assert not (tmp_path / "win.mseed.tmp").exists()


def test_mseed_unwritable_destination_leaves_no_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "no_such_dir" / "win.mseed"
    with pytest.raises(ArchiveExportError):
        write_window_mseed(_gappy_window(), out)
    assert not out.exists()


def test_csv_writes_grid_with_empty_gap_cells(tmp_path: Path) -> None:
    n = 10
    epochs = float(_T0.timestamp) + np.arange(n) / _FS
    z = np.arange(n, dtype=np.float64)
    z[4:6] = np.nan  # the gap
    out = tmp_path / "win.csv"

    n_bytes = write_window_csv(
        out, epochs, {"XX.STA.00.HHZ": z}, header_meta={"device": "dev"}
    )

    assert out.is_file() and out.stat().st_size == n_bytes
    assert not (tmp_path / "win.csv.tmp").exists()
    lines = out.read_text().splitlines()
    assert lines[0] == "# device: dev"
    assert lines[1] == "time_iso,epoch,XX.STA.00.HHZ"
    rows = [line.split(",") for line in lines[2:]]
    assert len(rows) == n
    # Gap rows have EMPTY cells, never interpolated values.
    assert rows[4][2] == "" and rows[5][2] == ""
    # Non-gap samples round-trip exactly (repr of float64).
    assert float(rows[0][2]) == 0.0
    assert float(rows[9][2]) == 9.0
    assert float(rows[0][1]) == float(_T0.timestamp)


def test_csv_cancel_removes_temp_and_writes_nothing(tmp_path: Path) -> None:
    n = 200_000  # > one stop-poll stride
    epochs = float(_T0.timestamp) + np.arange(n) / _FS
    out = tmp_path / "win.csv"
    calls = {"n": 0}

    def _stop_soon() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # first poll passes, second cancels

    with pytest.raises(ExportCancelledError):
        write_window_csv(out, epochs, {"C": np.zeros(n)}, should_stop=_stop_soon)
    assert not out.exists()
    assert not (tmp_path / "win.csv.tmp").exists()


def test_csv_length_mismatch_raises(tmp_path: Path) -> None:
    with pytest.raises(ArchiveExportError):
        write_window_csv(
            tmp_path / "win.csv", np.arange(5, dtype=np.float64), {"C": np.zeros(4)}
        )
    assert not (tmp_path / "win.csv").exists()
