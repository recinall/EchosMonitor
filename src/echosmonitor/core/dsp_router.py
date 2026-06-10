"""DSP router — runs `DspChain` instances on a dedicated QThread.

The router is a `QObject` that lives on its own `QThread`, owned by
:class:`echosmonitor.core.streaming_engine.StreamingEngine`. The
engine pushes packets into per-stream bounded deques on the engine
thread (drop-oldest backpressure per CLAUDE.md rule 5), and the engine's
flush timer dispatches a `_drain` call to the router via
`QueuedConnection`. The router runs `chain.process()` for each pending
stream and emits the result via signals — those signals are re-published
on the engine's own thread.

Streams are keyed by ``(device_name, nslc)`` so the same NSLC arriving
from two different SeedLink servers (rare but legal) holds two
independent chain states. Internally, the router stores chains under a
composite string key (``f"{device}/{nslc}"``) for cheap dict lookup;
the slot/signal API exposes the pair as separate ``str`` arguments so
consumers never need to parse the composite.

Keeping every chain on a single dedicated thread has two benefits over
spawning a worker per stream: the GIL serialization is trivial to reason
about, and the GUI thread is never blocked by DSP work even when many
streams are configured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import structlog
from PySide6.QtCore import QObject, Signal, Slot

from echosmonitor.core.models import device_stream_key

if TYPE_CHECKING:
    from obspy.core.utcdatetime import UTCDateTime

    from echosmonitor.dsp.chain import DspChain

_log = structlog.get_logger(__name__)


class _DspRouter(QObject):
    """Per-engine DSP dispatcher. Lives on a dedicated QThread."""

    # Signals carry ``(device_name, nslc, payload)`` — every nslc-bearing
    # signal in the engine subsystem is now device-keyed (M3 part 1).
    processedTraceReady = Signal(str, str, object)  # device, nslc, ndarray (float32)  # noqa: N815
    triggerFired = Signal(object, str, str)  # Trigger, device, nslc  # noqa: N815

    def __init__(self) -> None:
        super().__init__()  # parentless — must be moveable to a different thread
        # Keyed by ``device_stream_key(device, nslc)`` so two devices that
        # publish the same NSLC keep their chain state independent.
        self._chains: dict[str, DspChain] = {}

    # ------------------------------------------------------------------
    # Slots — invoked from the engine via QueuedConnection / invokeMethod
    # ------------------------------------------------------------------
    @Slot(str, str, object)
    def install_chain(self, device_name: str, nslc: str, chain: object) -> None:
        """Install or replace the chain for ``(device_name, nslc)``. Idempotent."""
        from echosmonitor.dsp.chain import DspChain

        if not isinstance(chain, DspChain):  # defensive — type-erased through Signal
            _log.warning(
                "dsp_router_invalid_chain",
                device=device_name,
                nslc=nslc,
                type=type(chain).__name__,
            )
            return
        self._chains[device_stream_key(device_name, nslc)] = chain

    @Slot(str, str)
    def remove_chain(self, device_name: str, nslc: str) -> None:
        self._chains.pop(device_stream_key(device_name, nslc), None)

    @Slot()
    def clear_chains(self) -> None:
        # Log on entry so the wait the engine performs on this slot
        # (BlockingQueuedConnection — see streaming_engine.py
        # ``_clearChainsRequested`` wiring) is observable per
        # CLAUDE.md rule 7. The body itself is O(N) chain entries
        # and completes in microseconds; never blocks on I/O.
        _log.info("dsp_router_clearing_chains", n_chains=len(self._chains))
        self._chains.clear()

    @Slot(str, str, object)
    def drain(self, device_name: str, nslc: str, items: object) -> None:
        """Run the chain on a snapshot of pending packets for one stream.

        ``items`` is a list of ``(samples_ndarray, t_start_UTCDateTime)``
        tuples — the engine's per-stream deque content captured under
        lock and cleared before this call.
        """
        chain = self._chains.get(device_stream_key(device_name, nslc))
        if chain is None:
            return
        items_list = self._coerce_items(items)
        if not items_list:
            return

        try:
            samples = np.concatenate([np.asarray(s, dtype=np.float64) for s, _ in items_list])
            t_start = items_list[0][1]
            result = chain.process(samples, t_start)
        except Exception as exc:
            # Never let a single bad chain crash the router thread.
            _log.error(
                "dsp_chain_process_failed",
                device=device_name,
                nslc=nslc,
                error=str(exc),
            )
            return

        # Cast to float32 for the UI plot path — float64 doubles the wire
        # weight crossing thread boundaries for no perceptual gain.
        self.processedTraceReady.emit(device_name, nslc, result.samples.astype(np.float32))
        for trigger in result.triggers:
            self.triggerFired.emit(trigger, device_name, nslc)

    @staticmethod
    def _coerce_items(items: object) -> list[tuple[np.ndarray, UTCDateTime]]:
        """The Qt signal layer types `items` as `object`. Validate it back
        to the expected shape so downstream code can be strict-typed."""
        if not isinstance(items, list):
            return []
        result: list[tuple[np.ndarray, UTCDateTime]] = []
        for item in items:
            if isinstance(item, tuple) and len(item) == 2:
                samples, t_start = item
                result.append((samples, t_start))
        return result
