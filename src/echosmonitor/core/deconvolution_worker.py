"""One-shot off-thread instrument-response deconvolution worker (M11 B).

The detection-detail pane lets the user view a *fixed* (already-rendered)
window in physical units. Deconvolution is a DISPLAY computation: it must
run off the GUI thread, but — unlike the Welch :class:`~echosmonitor.
core.psd_worker.PsdWorker`, which shares the engine's science DSP thread —
it MUST NOT share the live DSP/detection/storage thread (rule 11). The
:class:`MainWindow` hosts this worker on a *dedicated* QThread so a slow
deconvolution can never back-pressure the science path.

Contract:

* A request arrives via :meth:`compute` (a queued ``@Slot`` across the
  thread boundary). It carries a monotonic ``token`` so the GUI side can
  drop stale results (latest-wins).
* On success the float64 physical samples come back via ``deconvolved``;
  on any failure a human-readable message comes back via ``failed``. The
  worker thread never dies — every failure is caught and reported.

The deconvolution itself is the pure
:meth:`~echosmonitor.core.response.ResponseRemover.to_physical`
(it logs its own start/done wait lines per rule 7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import numpy as np
import obspy
import structlog
from obspy import UTCDateTime
from PySide6.QtCore import QObject, Signal, Slot

from echosmonitor.core.exceptions import ResponseError

if TYPE_CHECKING:
    from echosmonitor.core.response import ResponseProvider

_log = structlog.get_logger(__name__)

# Output-code → human axis label. The output codes match
# ``ResponseRemover.to_physical``'s ``output`` literal.
_UNIT_LABELS: dict[str, str] = {
    "VEL": "Velocity (m/s)",
    "ACC": "Acceleration (m/s²)",
    "DISP": "Displacement (m)",
}


class DeconvolutionWorker(QObject):
    """Per-window deconvolution worker. Lives on a dedicated QThread.

    NOT the engine's science DSP thread — see the module docstring and
    CLAUDE.md rule 11.
    """

    # ``deconvolved(token, unit_label, physical_samples)`` — ``token``
    # echoes the request so the GUI drops stale results; ``unit_label`` is
    # the axis label (e.g. "Velocity (m/s)"); ``physical_samples`` is a
    # float64 1-D ndarray the same length as the input window.
    deconvolved = Signal(int, str, object)
    # ``failed(token, human_message)`` — a configured-but-unreadable
    # response, a no-match, a gappy window, or any unexpected error.
    failed = Signal(int, str)

    def __init__(self, provider: ResponseProvider) -> None:
        super().__init__()  # parentless — moveToThread requires no parent
        self._provider = provider

    @Slot(int, str, str, str, object, float, float)
    def compute(
        self,
        token: int,
        device: str,
        nslc: str,
        output: str,
        samples: object,
        fs: float,
        start_epoch: float,
    ) -> None:
        """Deconvolve ``samples`` to physical ``output`` units, off-thread.

        Builds a one-trace ObsPy Stream from the raw count samples and the
        stream identity, then runs the pure deconvolution core. The result
        (or a human-readable failure) is emitted back to the GUI thread.

        Args:
            token: Monotonic request id; echoed on the result so the GUI
                drops stale (superseded) results.
            device: Configured device name (selects the response metadata).
            nslc: ``"NET.STA.LOC.CHA"`` stream identifier.
            output: One of ``"VEL"``, ``"ACC"``, ``"DISP"``.
            samples: The displayed COUNTS window (1-D array-like).
            fs: Sampling rate in Hz.
            start_epoch: POSIX epoch of the window's first sample.
        """
        try:
            data = np.asarray(samples, dtype=np.float64)
            parts = nslc.split(".")
            if len(parts) != 4:
                self.failed.emit(token, f"malformed stream id {nslc!r}")
                return
            net, sta, loc, cha = parts
            trace = obspy.Trace(
                data=data,
                header={
                    "network": net,
                    "station": sta,
                    "location": loc,
                    "channel": cha,
                    "sampling_rate": fs,
                    "starttime": UTCDateTime(start_epoch),
                },
            )
            stream = obspy.Stream(traces=[trace])

            remover = self._provider.remover_for(device)
            if remover is None:
                self.failed.emit(token, "No response metadata for this device")
                return

            out_code: Literal["VEL", "ACC", "DISP"] = output  # type: ignore[assignment]
            result = remover.to_physical(stream, out_code)
            label = _UNIT_LABELS.get(output, output)
            physical = np.asarray(result[0].data, dtype=np.float64)
            self.deconvolved.emit(token, label, physical)
        except ResponseError as exc:
            self.failed.emit(token, str(exc))
        except Exception as exc:  # defence-in-depth — bad input must not kill the thread
            _log.error(
                "deconvolution_worker_failed",
                device=device,
                nslc=nslc,
                output=output,
                error=str(exc),
            )
            self.failed.emit(token, f"deconvolution failed: {exc}")
