"""DspChain — orchestrate a list of stateful stages on streaming packets.

Pure module: no Qt, no I/O. Intended to be driven by `_DspRouter` on the
engine's DSP thread; tests instantiate it directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import structlog

from seedlink_dashboard.dsp.stages import Decimation, Stage, StaLta

if TYPE_CHECKING:
    from obspy.core.utcdatetime import UTCDateTime

    from seedlink_dashboard.core.models import StreamID, Trigger

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ChainResult:
    """Output of a single `DspChain.process()` call."""

    samples: np.ndarray
    fs_out: float
    triggers: list[Trigger]


class DspChain:
    """Ordered pipeline of streaming DSP stages.

    Constructed by :func:`seedlink_dashboard.dsp.factory.build_chain`. The
    factory has already validated stage compatibility against the input
    sample rate, so the chain just runs them in order.
    """

    def __init__(
        self,
        stages: list[Stage],
        fs_in: float,
        stream_id: StreamID,
    ) -> None:
        self._stages = list(stages)
        self._fs_in = float(fs_in)
        self._fs_out = float(fs_in)
        self._stream_id = stream_id
        # Compute `fs_out` by walking decimations.
        for stage in self._stages:
            self._fs_out = stage.fs_out
        # Surface a friendly hint when STA/LTA precedes any filtering — the
        # user is likely going to get noisy triggers if so.
        first_filter = next(
            (i for i, s in enumerate(self._stages) if not isinstance(s, StaLta)),
            None,
        )
        first_sta_lta = next(
            (i for i, s in enumerate(self._stages) if isinstance(s, StaLta)),
            None,
        )
        if first_filter is not None and first_sta_lta is not None and first_sta_lta < first_filter:
            _log.info(
                "dsp_chain_sta_lta_before_filters",
                stream=stream_id.nslc,
                hint="STA/LTA placed before any filter — consider filtering first",
            )

    @property
    def fs_in(self) -> float:
        return self._fs_in

    @property
    def fs_out(self) -> float:
        return self._fs_out

    @property
    def stages(self) -> tuple[Stage, ...]:
        """Read-only view of the configured stages, in chain order.

        Useful for tests asserting that a chain was built with the
        expected stage types without reaching into private attributes.
        """
        return tuple(self._stages)

    def reset(self) -> None:
        for stage in self._stages:
            stage.reset()

    def process(self, samples: np.ndarray, t_start: UTCDateTime) -> ChainResult:
        """Run every stage in order. Returns final samples + collected triggers.

        `t_start` is the wall-clock time of the first sample of `samples`.
        Triggers from STA/LTA stages are collected across all stages in
        chain order.
        """
        if samples.ndim != 1:
            raise ValueError(f"samples must be 1-D, got shape {samples.shape}")

        out = np.asarray(samples, dtype=np.float64)
        triggers: list[Trigger] = []
        for stage in self._stages:
            out = stage.process(out, t_start)
            # Stages reset their own trigger buffer at the start of each
            # process() call, so a snapshot here is safe — the next call
            # won't double-report.
            triggers.extend(stage.triggers)
        return ChainResult(samples=out, fs_out=self._fs_out, triggers=triggers)


def has_decimation(stages: list[Stage]) -> bool:
    """Return True iff any stage in `stages` reduces the sample rate."""
    return any(isinstance(s, Decimation) for s in stages)
