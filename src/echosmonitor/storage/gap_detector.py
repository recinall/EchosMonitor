"""Pure per-stream gap / overlap / rate-change detection (M5 stage B).

Each :class:`GapDetector` holds the last seen end time and sample rate
for one stream. ``observe(trace)`` returns a :class:`GapEvent` when
the trace's starttime departs from the expected continuation by more
than half a sample period; otherwise returns ``None``.

The detector is pure (no I/O, no Qt, no logging side-effects beyond a
single WARN on a clock jump). It runs on the storage QThread, called
from the engine before the writer's ``write_trace`` so the next
writer call can carry a freshly-computed gap to the DAO via the
post-fsync ``flushedFile`` pipeline.
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
    """

    __slots__ = ("_fs", "_last_end", "_stream_id")

    def __init__(self, stream_id: int, sample_rate: float) -> None:
        if sample_rate <= 0.0:
            raise ValueError(f"sample_rate must be positive, got {sample_rate}")
        self._stream_id = stream_id
        self._fs = float(sample_rate)
        self._last_end: UTCDateTime | None = None

    @property
    def stream_id(self) -> int:
        return self._stream_id

    @property
    def sample_rate(self) -> float:
        return self._fs

    @property
    def last_end(self) -> UTCDateTime | None:
        return self._last_end

    def observe(self, trace: Trace) -> GapEvent | None:
        """Inspect the next packet for this stream.

        Returns:
            ``None`` for a contiguous packet (within half a sample of
            the expected continuation); otherwise a :class:`GapEvent`
            describing the discontinuity. The detector's internal
            ``last_end`` always advances to the trace's endtime, even
            on a discontinuity, so the next ``observe`` is judged
            against the freshly-seen state.
        """
        fs_packet = float(trace.stats.sampling_rate)

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
        half_sample = 0.5 * delta

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

        if abs(gap_seconds) < half_sample:
            # Contiguous (within half a sample of the expected
            # continuation) — advance state, no event.
            self._last_end = trace.stats.endtime
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
