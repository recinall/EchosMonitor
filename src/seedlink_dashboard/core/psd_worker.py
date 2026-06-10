"""On-demand Welch PSD computation, dispatched off the GUI thread.

The PSD widget asks for a PSD curve via ``psdRequested``; the worker
fetches the relevant samples from the :class:`StreamingEngine` (via
its lock-protected ``read_recent`` API) and runs :func:`welch_psd` on
them. The result is returned via ``psdReady`` on the engine thread,
so the widget's slot runs on the GUI thread without any blocking
across thread boundaries.

The worker shares the engine's existing ``_dsp_thread`` — Welch is a
short-running, one-shot computation and adding a third QThread would
only complicate the lifecycle. A request received while another is in
flight does not block; the in-flight request runs to completion and
the next one runs after. Stale results are dropped at the widget side
(latest-result-wins).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import structlog
from PySide6.QtCore import QObject, Signal, Slot

from seedlink_dashboard.dsp.psd import power_to_db, welch_psd

if TYPE_CHECKING:
    from seedlink_dashboard.core.streaming_engine import StreamingEngine

_log = structlog.get_logger(__name__)


class PsdWorker(QObject):
    """Per-engine PSD compute worker. Lives on the DSP QThread."""

    # ``psdReady(device, nslc, seconds, freqs, db)`` — ``seconds`` echoes
    # the request payload so consumers can drop stale results that no
    # longer match the current selection. ``freqs`` and ``db`` are both
    # float64 1-D arrays of the same length (``freqs[0] == 0``,
    # ``freqs[-1] == fs / 2``).
    psdReady = Signal(str, str, float, object, object)  # noqa: N815

    def __init__(self, engine: StreamingEngine) -> None:
        super().__init__()  # parentless — moveToThread requires no parent
        self._engine = engine

    @Slot(str, str, float)
    def compute(self, device_name: str, nslc: str, seconds: float) -> None:
        """Read the last ``seconds`` of the stream's ring buffer and
        emit its Welch PSD. No-op on unknown stream / empty data."""
        samples, fs, _t_end = self._engine.read_recent(device_name, nslc, seconds)
        if samples.size == 0 or fs <= 0.0:
            return
        try:
            freqs, power = welch_psd(samples, fs)
        except Exception as exc:  # defence-in-depth — bad input must not kill the thread
            _log.error(
                "psd_worker_failed",
                device=device_name,
                nslc=nslc,
                seconds=seconds,
                error=str(exc),
            )
            return
        db = power_to_db(power).astype(np.float64, copy=False)
        self.psdReady.emit(device_name, nslc, float(seconds), freqs, db)
