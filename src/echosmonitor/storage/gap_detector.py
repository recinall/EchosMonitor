"""Pure per-stream gap / overlap / rate-change detection (M5 stage B).

Each :class:`GapDetector` holds the last seen end time and sample rate
for one stream. ``observe(trace)`` returns a :class:`GapEvent` when
the trace's starttime departs from the expected continuation by more
than the jitter tolerance (floored at half a sample period — M6.5-B);
otherwise returns ``None`` and exposes the grid correction on
:attr:`GapDetector.last_snap_s` so the caller can rectify the trace's
stamp before archiving.

The detector is pure (no I/O, no Qt, no logging side-effects beyond a
single WARN on a clock jump). The engine calls it on its own thread
from ``_observe_gap`` before posting the trace to the writer, so the
event reaches the DAO via the post-fsync ``flushedFile`` pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from obspy import Trace, UTCDateTime


GapKind = Literal["gap", "overlap", "rate_change"]

# A clock step >|1 s| backwards is treated as a session-level reset
# rather than a sub-sample correction. Documented in the module
# docstring of plan stage B.
_CLOCK_JUMP_BACK_S: float = 1.0

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class GapEvent:
    """Per-stream discontinuity. Recorded by the DAO into ``gaps``."""

    t_start: UTCDateTime
    t_end: UTCDateTime
    samples_missing: int  # positive for gap, negative for overlap, 0 for rate_change
    kind: GapKind


class GapDetector:
    """Stateful detector for one (device, NSLC) pair.

    The detector decides what happens at packet boundaries based on
    arithmetic only — packet *content* is irrelevant. Sample rate is
    accepted at construction; an incoming packet whose
    ``trace.stats.sampling_rate`` differs from the cached rate
    triggers a ``rate_change`` event and the internal state is rebuilt
    around the new rate.

    Jitter tolerance (M6.5-B): real Echos devices stamp packets with
    millisecond-scale clock wobble (the first field run showed
    ±0.5…±2.5-sample excursions at 500 Hz, in gap→overlap PAIRS that
    net zero). Within ``jitter_tolerance_s`` (floored at half a sample
    period) a misaligned packet is treated as CONTIGUOUS: no event,
    and :attr:`last_snap_s` reports the correction that puts the
    packet back on the expected sample grid — the caller applies it to
    the trace before archiving so on-disk records stay exactly
    contiguous instead of fragmenting. ``last_end`` advances along the
    reconstructed grid, so zero-mean jitter never accumulates;
    persistent drift grows the measured offset until it crosses the
    tolerance, which re-anchors the grid with one honest event. The
    cost is bounded and documented: a REAL discontinuity smaller than
    the tolerance is absorbed as ≤ one tolerance of absolute timing
    bias — inside the device's own stamping noise — and that bias
    PERSISTS until the next over-tolerance event re-anchors the grid
    (the absorbed samples get no ``gaps`` row).
    """

    __slots__ = ("_fs", "_jitter_tol_s", "_last_end", "_last_snap_s", "_stream_id")

    def __init__(
        self,
        stream_id: int,
        sample_rate: float,
        jitter_tolerance_s: float = 0.0,
    ) -> None:
        if sample_rate <= 0.0:
            raise ValueError(f"sample_rate must be positive, got {sample_rate}")
        if jitter_tolerance_s < 0.0:
            raise ValueError(f"jitter_tolerance_s must be >= 0, got {jitter_tolerance_s}")
        self._stream_id = stream_id
        self._fs = float(sample_rate)
        self._jitter_tol_s = float(jitter_tolerance_s)
        self._last_end: UTCDateTime | None = None
        self._last_snap_s: float = 0.0

    @property
    def stream_id(self) -> int:
        return self._stream_id

    @property
    def sample_rate(self) -> float:
        return self._fs

    @property
    def last_end(self) -> UTCDateTime | None:
        return self._last_end

    @property
    def last_snap_s(self) -> float:
        """Grid correction for the most recently observed packet.

        Non-zero only when the packet was within the jitter tolerance
        but off the expected grid; the caller adds this to the trace's
        ``starttime`` before archiving. Reset on every ``observe``.
        """
        return self._last_snap_s

    def observe(self, trace: Trace) -> GapEvent | None:
        """Inspect the next packet for this stream.

        Returns:
            ``None`` for a contiguous packet (within the jitter
            tolerance — floored at half a sample — of the expected
            continuation; :attr:`last_snap_s` then carries the grid
            correction); otherwise a :class:`GapEvent` describing the
            discontinuity. On a discontinuity the detector re-anchors
            ``last_end`` to the trace's own endtime, so the next
            ``observe`` is judged against the freshly-seen state.
        """
        fs_packet = float(trace.stats.sampling_rate)
        self._last_snap_s = 0.0

        # First packet for this stream: prime state, no event.
        if self._last_end is None:
            self._last_end = trace.stats.endtime
            self._fs = fs_packet
            return None

        # Sample-rate change: emit a rate_change event and rebuild
        # state around the new rate. ``samples_missing`` is 0 because
        # we don't know how to count samples across a rate boundary.
        if abs(self._fs - fs_packet) / max(self._fs, fs_packet) > 0.01:
            event = GapEvent(
                t_start=self._last_end,
                t_end=trace.stats.starttime,
                samples_missing=0,
                kind="rate_change",
            )
            self._fs = fs_packet
            self._last_end = trace.stats.endtime
            return event

        delta = 1.0 / self._fs
        expected = self._last_end + delta
        gap_seconds = trace.stats.starttime - expected
        # Half a sample is the hard floor (sub-half misalignment is
        # not even expressible as a sample count); the configured
        # jitter tolerance widens the contiguous zone above it.
        tolerance = max(0.5 * delta, self._jitter_tol_s)

        # Clock-jump guard: a sub-second backwards is overlap; a full
        # second backwards (or worse) is treated as a session-level
        # reset. Loud WARN log so operators can correlate with NTP /
        # GPS event.
        if gap_seconds < -_CLOCK_JUMP_BACK_S:
            _log.warning(
                "gap_detector_clock_jump",
                stream_id=self._stream_id,
                old_last_end=str(self._last_end),
                new_starttime=str(trace.stats.starttime),
                jump_back_s=round(-gap_seconds, 3),
            )
            event = GapEvent(
                t_start=self._last_end,
                t_end=trace.stats.starttime,
                samples_missing=0,
                kind="rate_change",
            )
            self._last_end = trace.stats.endtime
            return event

        if abs(gap_seconds) < tolerance:
            # Contiguous (within tolerance of the expected
            # continuation). Snap the packet onto the reconstructed
            # grid: report the correction and advance ``last_end``
            # along the grid (NOT the jittered device stamp), so
            # zero-mean jitter never produces gap/overlap pairs and
            # never accumulates.
            if gap_seconds != 0.0:
                self._last_snap_s = -gap_seconds
            self._last_end = expected + (trace.stats.npts - 1) * delta
            return None

        if gap_seconds > 0:
            samples_missing = round(gap_seconds * self._fs)
            kind: GapKind = "gap"
        else:
            samples_missing = -round(-gap_seconds * self._fs)
            kind = "overlap"

        event = GapEvent(
            t_start=expected,
            t_end=trace.stats.starttime,
            samples_missing=samples_missing,
            kind=kind,
        )
        self._last_end = trace.stats.endtime
        return event
