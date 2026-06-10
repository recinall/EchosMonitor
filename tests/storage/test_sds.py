"""Tests for ``storage/sds.py`` — pure SDS path helpers."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from obspy import Trace, UTCDateTime

from echosmonitor.core.models import StreamID
from echosmonitor.storage.sds import (
    day_bounds,
    parse_sds_path,
    sds_path,
    split_at_midnight,
)


def _make_trace(
    *,
    starttime: UTCDateTime,
    npts: int,
    sampling_rate: float = 100.0,
    nslc: str = "IU.ANMO.00.BHZ",
) -> Trace:
    net, sta, loc, cha = nslc.split(".")
    return Trace(
        data=np.arange(npts, dtype=np.int32),
        header={
            "network": net,
            "station": sta,
            "location": loc,
            "channel": cha,
            "starttime": starttime,
            "sampling_rate": sampling_rate,
        },
    )


# ---------------------------------------------------------------------------
# sds_path
# ---------------------------------------------------------------------------


def test_sds_path_canonical_layout() -> None:
    root = Path("/archive")
    t = UTCDateTime("2026-05-09T12:34:56")
    sid = StreamID("IU", "ANMO", "00", "BHZ")
    p = sds_path(root, t, sid)
    assert p == Path("/archive/2026/IU/ANMO/BHZ.D/IU.ANMO.00.BHZ.D.2026.129")


def test_sds_path_zero_pads_doy() -> None:
    """Day-of-year 5 must render as ``005`` (3 digits zero-padded)."""
    p = sds_path(
        Path("/a"),
        UTCDateTime("2026-01-05T00:00:00"),
        StreamID("IU", "ANMO", "00", "BHZ"),
    )
    assert p.name.endswith(".2026.005")


def test_sds_path_empty_location() -> None:
    """Empty location code must render as a doubled dot — SDS canonical."""
    p = sds_path(
        Path("/a"),
        UTCDateTime("2026-05-09T00:00:00"),
        StreamID("GE", "WLF", "", "BHZ"),
    )
    assert p.name == "GE.WLF..BHZ.D.2026.129"


def test_sds_path_year_boundary() -> None:
    """A timestamp just before New Year still uses the correct year/doy."""
    p = sds_path(
        Path("/a"),
        UTCDateTime("2024-12-31T23:59:59.999"),
        StreamID("IU", "ANMO", "00", "BHZ"),
    )
    assert "/2024/" in str(p)
    assert p.name.endswith(".2024.366")  # 2024 is a leap year


# ---------------------------------------------------------------------------
# parse_sds_path
# ---------------------------------------------------------------------------


def test_parse_sds_path_round_trips() -> None:
    sid = StreamID("IU", "ANMO", "00", "BHZ")
    t = UTCDateTime("2026-05-09T00:00:00")
    p = sds_path(Path("/archive"), t, sid)
    parsed = parse_sds_path(p)
    assert parsed is not None
    assert parsed == (sid, 2026, 129)


def test_parse_sds_path_round_trips_empty_location() -> None:
    sid = StreamID("GE", "WLF", "", "BHZ")
    p = sds_path(Path("/a"), UTCDateTime("2026-05-09T00:00:00"), sid)
    parsed = parse_sds_path(p)
    assert parsed == (sid, 2026, 129)


def test_parse_sds_path_rejects_non_sds() -> None:
    assert parse_sds_path(Path("/tmp/random/file.txt")) is None
    assert parse_sds_path(Path("/a/b/c/d/e")) is None  # too few parts after root


def test_parse_sds_path_rejects_inconsistent_filename() -> None:
    """Filename net/sta/cha must match the directory components."""
    bad = Path("/archive/2026/IU/ANMO/BHZ.D/XX.YY..ZZ.D.2026.129")
    assert parse_sds_path(bad) is None


def test_parse_sds_path_rejects_bad_year() -> None:
    bad = Path("/archive/26/IU/ANMO/BHZ.D/IU.ANMO..BHZ.D.26.129")
    assert parse_sds_path(bad) is None


def test_parse_sds_path_rejects_bad_doy() -> None:
    bad = Path("/archive/2026/IU/ANMO/BHZ.D/IU.ANMO..BHZ.D.2026.12")
    assert parse_sds_path(bad) is None


def test_parse_sds_path_rejects_wrong_type_marker() -> None:
    bad = Path("/archive/2026/IU/ANMO/BHZ.D/IU.ANMO..BHZ.R.2026.129")
    assert parse_sds_path(bad) is None


# ---------------------------------------------------------------------------
# day_bounds
# ---------------------------------------------------------------------------


def test_day_bounds_basic() -> None:
    t = UTCDateTime("2026-05-09T13:00:00.000")
    start, end = day_bounds(t)
    assert start == UTCDateTime("2026-05-09T00:00:00.000")
    assert end == UTCDateTime("2026-05-10T00:00:00.000")


def test_day_bounds_year_rollover() -> None:
    t = UTCDateTime("2024-12-31T23:00:00.000")
    start, end = day_bounds(t)
    assert start == UTCDateTime("2024-12-31T00:00:00.000")
    assert end == UTCDateTime("2025-01-01T00:00:00.000")


def test_day_bounds_leap_year_feb29_to_mar1() -> None:
    """2024 is a leap year; Feb 29 → Mar 1 must be exactly 86400 s."""
    t = UTCDateTime("2024-02-29T12:00:00")
    start, end = day_bounds(t)
    assert start == UTCDateTime("2024-02-29T00:00:00")
    assert end == UTCDateTime("2024-03-01T00:00:00")


def test_day_bounds_at_exactly_midnight() -> None:
    """If t is exactly 00:00:00, the day starts at t and ends 86400s later."""
    t = UTCDateTime("2026-05-09T00:00:00")
    start, end = day_bounds(t)
    assert start == t
    assert end - start == 86_400


# ---------------------------------------------------------------------------
# split_at_midnight
# ---------------------------------------------------------------------------


def test_split_at_midnight_no_split_within_day() -> None:
    """A trace that ends well before midnight is returned unchanged (1 elem)."""
    t0 = UTCDateTime("2026-05-09T12:00:00")
    tr = _make_trace(starttime=t0, npts=1000, sampling_rate=100.0)
    result = split_at_midnight(tr)
    assert len(result) == 1
    assert result[0] is tr  # no copy when not splitting


def test_split_at_midnight_endtime_exactly_at_midnight_does_not_split() -> None:
    """Convention matches the miniseed-sds skill: ``>``, not ``>=``.

    A trace whose LAST sample falls exactly at the next midnight stays
    whole — the midnight sample is kept on the day the trace started.
    """
    # Construct: 100 samples at 100 Hz starting at 23:59:59.000, so
    # the final sample is at 23:59:59 + 99*0.01 = 23:59:59.990 —
    # comfortably within the day. Now shift so the LAST sample is at
    # exactly 00:00:00 of the next day:
    sr = 100.0
    delta = 1.0 / sr
    npts = 100
    last_sample_at = UTCDateTime("2026-05-10T00:00:00.000")
    starttime = last_sample_at - (npts - 1) * delta
    tr = _make_trace(starttime=starttime, npts=npts, sampling_rate=sr)
    assert tr.stats.endtime == last_sample_at
    result = split_at_midnight(tr)
    assert len(result) == 1
    assert result[0].stats.npts == npts


def test_split_at_midnight_strictly_past_midnight_splits() -> None:
    """One sample beyond midnight forces a split into two pieces."""
    sr = 100.0
    starttime = UTCDateTime("2026-05-09T23:59:59.500")
    npts = 200  # spans 2 s, ends at 2026-05-10T00:00:01.490
    tr = _make_trace(starttime=starttime, npts=npts, sampling_rate=sr)
    assert tr.stats.endtime > UTCDateTime("2026-05-10T00:00:00")
    result = split_at_midnight(tr)
    assert len(result) == 2
    pre, post = result
    # Pre's endtime is < midnight; post's starttime is >= midnight.
    midnight = UTCDateTime("2026-05-10T00:00:00")
    assert pre.stats.endtime < midnight
    assert post.stats.starttime >= midnight
    # Together they preserve every sample of the original.
    assert pre.stats.npts + post.stats.npts == npts
    # Sample-rate is preserved on both sides.
    assert pre.stats.sampling_rate == pytest.approx(sr)
    assert post.stats.sampling_rate == pytest.approx(sr)


def test_split_at_midnight_year_boundary_splits() -> None:
    """Splitting works across year/decade boundaries (Dec 31 → Jan 1)."""
    sr = 100.0
    starttime = UTCDateTime("2024-12-31T23:59:59.500")
    tr = _make_trace(starttime=starttime, npts=200, sampling_rate=sr)
    result = split_at_midnight(tr)
    assert len(result) == 2
    pre, post = result
    assert pre.stats.endtime < UTCDateTime("2025-01-01T00:00:00")
    assert post.stats.starttime >= UTCDateTime("2025-01-01T00:00:00")


def test_split_at_midnight_leap_year_feb29() -> None:
    """Splitting correctly handles the 2024-02-29 → 2024-03-01 boundary."""
    sr = 100.0
    starttime = UTCDateTime("2024-02-29T23:59:59.500")
    tr = _make_trace(starttime=starttime, npts=200, sampling_rate=sr)
    result = split_at_midnight(tr)
    assert len(result) == 2
    pre, post = result
    assert pre.stats.endtime < UTCDateTime("2024-03-01T00:00:00")
    assert post.stats.starttime >= UTCDateTime("2024-03-01T00:00:00")


def test_split_at_midnight_nanosecond_just_before_midnight_no_split() -> None:
    """A trace ending just before midnight is intact."""
    sr = 1000.0
    last_sample_at = UTCDateTime("2026-05-10T00:00:00") - 0.001  # 1 ms early
    starttime = last_sample_at - (50 - 1) * (1.0 / sr)
    tr = _make_trace(starttime=starttime, npts=50, sampling_rate=sr)
    result = split_at_midnight(tr)
    assert len(result) == 1
