"""Tests for ``storage/gap_detector.py`` — pure logic, synthetic packets."""

from __future__ import annotations

import numpy as np
import pytest
from obspy import Trace, UTCDateTime

from echosmonitor.storage.gap_detector import GapDetector, GapEvent


def _make_trace(starttime: UTCDateTime, npts: int, sampling_rate: float = 100.0) -> Trace:
    return Trace(
        data=np.zeros(npts, dtype=np.int32),
        header={
            "network": "IU",
            "station": "ANMO",
            "location": "00",
            "channel": "BHZ",
            "starttime": starttime,
            "sampling_rate": sampling_rate,
        },
    )


def test_first_packet_returns_none_and_primes_state() -> None:
    d = GapDetector(stream_id=1, sample_rate=100.0)
    t = UTCDateTime("2026-05-09T12:00:00")
    tr = _make_trace(t, npts=100)
    assert d.observe(tr) is None
    assert d.last_end == tr.stats.endtime


def test_contiguous_packet_returns_none() -> None:
    d = GapDetector(stream_id=1, sample_rate=100.0)
    t0 = UTCDateTime("2026-05-09T12:00:00")
    tr1 = _make_trace(t0, npts=100)
    d.observe(tr1)
    # Next packet starts 0.01 s after tr1.endtime — exactly one delta.
    tr2 = _make_trace(tr1.stats.endtime + 0.01, npts=100)
    assert d.observe(tr2) is None
    assert d.last_end == tr2.stats.endtime


def test_sub_sample_drift_does_not_trigger() -> None:
    """A drift smaller than half a sample must not register a gap."""
    d = GapDetector(stream_id=1, sample_rate=100.0)
    t0 = UTCDateTime("2026-05-09T12:00:00")
    tr1 = _make_trace(t0, npts=100)
    d.observe(tr1)
    # Drift = 0.001 s < half_sample (0.005 s @ 100 Hz)
    tr2 = _make_trace(tr1.stats.endtime + 0.01 + 0.001, npts=100)
    assert d.observe(tr2) is None
    # M6.5-B: even sub-half misalignment reports a grid snap so the
    # archive stamps stay exactly contiguous on disk.
    assert d.last_snap_s == pytest.approx(-0.001, abs=1e-6)
    assert d.last_end == tr1.stats.endtime + 0.01 + 99 * 0.01


# ---------------------------------------------------------------------------
# M6.5-B — jitter tolerance + grid rectification (field-run chatter)
# ---------------------------------------------------------------------------


def test_jitter_within_tolerance_is_contiguous_and_snapped() -> None:
    """A stamp 2 samples late at 500 Hz (4 ms — beyond half a sample,
    within the 10 ms tolerance) is contiguous: no event, and the snap
    puts it back on the expected grid."""
    fs = 500.0
    d = GapDetector(stream_id=1, sample_rate=fs, jitter_tolerance_s=0.010)
    t0 = UTCDateTime("2026-06-12T17:42:35")
    tr1 = _make_trace(t0, npts=100, sampling_rate=fs)
    d.observe(tr1)
    expected_start = tr1.stats.endtime + 1.0 / fs
    tr2 = _make_trace(expected_start + 0.004, npts=100, sampling_rate=fs)
    assert d.observe(tr2) is None
    assert d.last_snap_s == pytest.approx(-0.004, abs=1e-7)
    # last_end follows the RECONSTRUCTED grid, not the jittered stamp.
    assert float(d.last_end - (expected_start + 99 / fs)) == pytest.approx(0.0, abs=1e-7)


def test_field_jitter_pairs_no_events_with_tolerance_two_without() -> None:
    """The field-run signature: one packet stamped late, the next back
    on grid. With the tolerance this is silent; with tolerance 0 the
    old behavior produced a gap/overlap PAIR (the log-chatter bug)."""
    fs = 500.0
    t0 = UTCDateTime("2026-06-12T17:42:35")

    def _run(tol_s: float) -> list[GapEvent]:
        d = GapDetector(stream_id=1, sample_rate=fs, jitter_tolerance_s=tol_s)
        events = []
        # Perfect 100-sample grid; packet 1 stamped +2 samples late.
        for i, jitter in enumerate([0.0, 2.0 / fs, 0.0, 0.0]):
            tr = _make_trace(t0 + i * 100 / fs + jitter, npts=100, sampling_rate=fs)
            ev = d.observe(tr)
            if ev is not None:
                events.append(ev)
        return events

    assert _run(0.010) == []
    legacy = _run(0.0)
    assert [e.kind for e in legacy] == ["gap", "overlap"]
    assert [e.samples_missing for e in legacy] == [2, -2]


def test_real_gap_beyond_tolerance_still_detected_and_reanchors() -> None:
    fs = 500.0
    d = GapDetector(stream_id=1, sample_rate=fs, jitter_tolerance_s=0.010)
    t0 = UTCDateTime("2026-06-12T17:42:35")
    tr1 = _make_trace(t0, npts=100, sampling_rate=fs)
    d.observe(tr1)
    # 8 samples = 16 ms — a real gap, beyond the 10 ms tolerance.
    expected_start = tr1.stats.endtime + 1.0 / fs
    tr2 = _make_trace(expected_start + 8.0 / fs, npts=100, sampling_rate=fs)
    ev = d.observe(tr2)
    assert ev is not None
    assert ev.kind == "gap"
    assert ev.samples_missing == 8
    # No snap on a real discontinuity; the grid re-anchors to the
    # device stamps.
    assert d.last_snap_s == 0.0
    assert d.last_end == tr2.stats.endtime


def test_persistent_drift_crossing_tolerance_emits_one_event_and_reanchors() -> None:
    """Slow clock drift accumulates against the reconstructed grid and
    is declared ONCE when it crosses the tolerance, then the grid
    re-anchors — drift is never silently absorbed forever."""
    fs = 500.0
    tol = 0.005  # 2.5 samples
    d = GapDetector(stream_id=1, sample_rate=fs, jitter_tolerance_s=tol)
    t0 = UTCDateTime("2026-06-12T17:42:35")
    events = []
    # Each packet's stamp drifts +1 ms further from the true grid.
    for i in range(8):
        tr = _make_trace(t0 + i * (100 / fs) + i * 0.001, npts=100, sampling_rate=fs)
        ev = d.observe(tr)
        if ev is not None:
            events.append((i, ev.kind))
    # Drift crosses the 5 ms tolerance at packet 5 (snapping kept the
    # grid at the ORIGIN, so the measured offset is the cumulative
    # drift); after the re-anchor the +1 ms/packet increments are back
    # inside the tolerance.
    assert events == [(5, "gap")]


def test_negative_jitter_tolerance_rejected() -> None:
    with pytest.raises(ValueError):
        GapDetector(stream_id=1, sample_rate=100.0, jitter_tolerance_s=-0.001)


def test_gap_emits_event_with_positive_samples_missing() -> None:
    d = GapDetector(stream_id=1, sample_rate=100.0)
    t0 = UTCDateTime("2026-05-09T12:00:00")
    tr1 = _make_trace(t0, npts=100)
    d.observe(tr1)
    # 0.5 s gap = 50 missing samples @ 100 Hz
    tr2 = _make_trace(tr1.stats.endtime + 0.01 + 0.5, npts=100)
    event = d.observe(tr2)
    assert isinstance(event, GapEvent)
    assert event.kind == "gap"
    assert event.samples_missing == 50
    assert event.t_start == tr1.stats.endtime + 0.01
    assert event.t_end == tr2.stats.starttime


def test_overlap_emits_event_with_negative_samples_missing() -> None:
    d = GapDetector(stream_id=1, sample_rate=100.0)
    t0 = UTCDateTime("2026-05-09T12:00:00")
    tr1 = _make_trace(t0, npts=100)
    d.observe(tr1)
    # Sub-second negative drift = overlap.
    tr2 = _make_trace(tr1.stats.endtime - 0.20, npts=100)
    event = d.observe(tr2)
    assert event is not None
    assert event.kind == "overlap"
    assert event.samples_missing < 0
    # Around -21 samples (1 expected + ~20 of overlap @ 100 Hz)
    assert -25 < event.samples_missing < -15


def test_rate_change_emits_rate_change_event() -> None:
    d = GapDetector(stream_id=1, sample_rate=100.0)
    t0 = UTCDateTime("2026-05-09T12:00:00")
    tr1 = _make_trace(t0, npts=100, sampling_rate=100.0)
    d.observe(tr1)
    # Different sample rate triggers rate_change before gap math.
    tr2 = _make_trace(tr1.stats.endtime + 0.01, npts=100, sampling_rate=50.0)
    event = d.observe(tr2)
    assert event is not None
    assert event.kind == "rate_change"
    assert event.samples_missing == 0
    # Detector now reports the new rate.
    assert d.sample_rate == 50.0


def test_repeated_rate_changes() -> None:
    d = GapDetector(stream_id=1, sample_rate=100.0)
    t0 = UTCDateTime("2026-05-09T12:00:00")
    d.observe(_make_trace(t0, 100, 100.0))
    e1 = d.observe(_make_trace(t0 + 1.0, 100, 50.0))
    assert e1 is not None and e1.kind == "rate_change"
    e2 = d.observe(_make_trace(t0 + 3.0, 100, 100.0))
    assert e2 is not None and e2.kind == "rate_change"
    assert d.sample_rate == 100.0


def test_clock_jump_backwards_triggers_rate_change() -> None:
    """A backwards clock step >1 s is classified as a session-level reset."""
    d = GapDetector(stream_id=1, sample_rate=100.0)
    t0 = UTCDateTime("2026-05-09T12:00:00")
    d.observe(_make_trace(t0, 100))
    # Jump 5 s backwards
    e = d.observe(_make_trace(t0 - 5.0, 100))
    assert e is not None
    assert e.kind == "rate_change"
    assert e.samples_missing == 0


def test_sample_rate_validation() -> None:
    with pytest.raises(ValueError):
        GapDetector(stream_id=1, sample_rate=0.0)
    with pytest.raises(ValueError):
        GapDetector(stream_id=1, sample_rate=-1.0)


def test_observe_after_gap_treats_state_as_freshly_advanced() -> None:
    """After a gap, the next contiguous packet must NOT re-trigger a gap.

    Regression: ensure ``last_end`` is updated to the gap-trace's
    endtime, not to the pre-gap last_end + delta.
    """
    d = GapDetector(stream_id=1, sample_rate=100.0)
    t0 = UTCDateTime("2026-05-09T12:00:00")
    tr1 = _make_trace(t0, 100)
    d.observe(tr1)
    tr2 = _make_trace(tr1.stats.endtime + 0.01 + 0.5, 100)  # gap
    g1 = d.observe(tr2)
    assert g1 is not None and g1.kind == "gap"
    # Continue contiguously after the gap.
    tr3 = _make_trace(tr2.stats.endtime + 0.01, 100)
    assert d.observe(tr3) is None
