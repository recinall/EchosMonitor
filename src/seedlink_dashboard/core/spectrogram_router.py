"""Spectrogram router — runs :class:`RollingSpectrogram` instances on
the engine's existing DSP QThread.

The router is a sibling of :class:`_DspRouter`: same thread, separate
state. It owns one :class:`RollingSpectrogram` per stream
(``device_stream_key``) and converts incoming sample chunks to STFT
columns on the DSP thread, then re-emits the columns to GUI consumers
via :attr:`columnReady`.

Lifecycle hooks fired by the engine (via QueuedConnection unless
otherwise noted):

- ``install_for(device, nslc, fs)``         on first packet for a stream
- ``reinstall_for(device, nslc, fs_new)``   on chain hot-reload (M4)
- ``remove_for(device, nslc)``              on per-stream removal
- ``clear_for_device(device)``              on ``_stop_device`` (per-device)
- ``clear_all()``                           on ``engine.stop()`` — wired
                                            with ``BlockingQueuedConnection``
                                            so the router thread is empty
                                            before ``QThread.quit()`` lands
                                            (POSTMORTEMS 2026-05-10)

Backpressure (CLAUDE.md rule 5 + rule 7):
A bounded deque (``maxlen = _SPECTROGRAM_MAX_COLUMNS``) accumulates
column outputs that have not yet been emitted. Whenever a push would
overflow the deque, the oldest column is dropped and a per-stream
counter is incremented. The router emits :attr:`spectrogramDropped`
(throttled, at most once every ``_DROP_LOG_INTERVAL_S`` seconds per
stream) so the GUI can surface sustained pressure.

Spectrogram columns are NEVER persisted anywhere (rule 8) — the
single source of truth remains the MiniSEED archive.
"""

from __future__ import annotations

import time
from collections import deque

import numpy as np
import structlog
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Signal, Slot

from seedlink_dashboard.core.models import DEVICE_KEY_SEP, device_stream_key
from seedlink_dashboard.dsp.spectrogram import RollingSpectrogram

_log = structlog.get_logger(__name__)

# Per-stream column-output backpressure budget. 200 columns at the
# default 1 column/s is ~3 minutes of pending work — far more than the
# GUI should ever fall behind without surfacing as a drop log.
_SPECTROGRAM_MAX_COLUMNS = 200
_DROP_LOG_INTERVAL_S = 5.0


class _SpectrogramRouter(QObject):
    """Per-engine spectrogram dispatcher. Lives on the DSP QThread."""

    # Outgoing — engine re-publishes via :attr:`StreamingEngine.samplesForSpec`
    # (or fans out to the GUI directly if the engine prefers, but the
    # signal-name re-use keeps the public surface tidy).
    columnReady = Signal(  # noqa: N815
        str,  # device_name
        str,  # nslc
        object,  # ndarray[float32], shape (n_freq_bins,)
        object,  # ndarray[float32] frequencies; same length as the column
        object,  # column_t_end (UTCDateTime | None)
    )
    spectrogramDropped = Signal(str, str, int)  # device, nslc, dropped count  # noqa: N815

    def __init__(self) -> None:
        super().__init__()  # parentless — must be moveable to a different thread
        self._spectrograms: dict[str, RollingSpectrogram] = {}
        # Per-stream pending column queue. The router emits columns
        # eagerly from the slot, so under normal flow the deque is
        # empty; it only fills if a slot enqueue beats the signal
        # delivery (extremely rare in practice). The deque exists so
        # we have one place to enforce drop-oldest semantics, satisfying
        # rule 5 in the worst case.
        self._pending: dict[str, deque[tuple[np.ndarray, UTCDateTime | None]]] = {}
        self._drops_pending: dict[str, int] = {}
        self._drops_last_log: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Slots — invoked from the engine via QueuedConnection / invokeMethod
    # ------------------------------------------------------------------
    @Slot(str, str, float)
    def install_for(self, device_name: str, nslc: str, fs: float) -> None:
        """Create a fresh :class:`RollingSpectrogram` for ``(device, nslc)``."""
        if fs <= 0:
            _log.warning(
                "spectrogram_router_invalid_fs",
                device=device_name,
                nslc=nslc,
                fs=fs,
            )
            return
        key = device_stream_key(device_name, nslc)
        self._spectrograms[key] = RollingSpectrogram(fs=fs)
        self._pending[key] = deque(maxlen=_SPECTROGRAM_MAX_COLUMNS)

    @Slot(str, str, float)
    def reinstall_for(self, device_name: str, nslc: str, fs_new: float) -> None:
        """Reset the spectrogram for ``(device, nslc)`` with a new fs.

        Called on ``processedStreamMeta`` after a chain hot-reload — the
        new chain may decimate to a different ``fs_out``, so the old
        ``RollingSpectrogram`` (whose nperseg / freqs / window were
        sized for the old fs) must be replaced wholesale.
        """
        key = device_stream_key(device_name, nslc)
        if fs_new <= 0:
            self.remove_for(device_name, nslc)
            return
        self._spectrograms[key] = RollingSpectrogram(fs=fs_new)
        # Drop any pending columns from the old fs — emitting them
        # under the new fs's frequency axis would be a lie.
        self._pending[key] = deque(maxlen=_SPECTROGRAM_MAX_COLUMNS)
        self._drops_pending.pop(key, None)
        self._drops_last_log.pop(key, None)

    @Slot(str, str)
    def remove_for(self, device_name: str, nslc: str) -> None:
        key = device_stream_key(device_name, nslc)
        self._spectrograms.pop(key, None)
        self._pending.pop(key, None)
        self._drops_pending.pop(key, None)
        self._drops_last_log.pop(key, None)

    @Slot(str)
    def clear_for_device(self, device_name: str) -> None:
        """Drop every spectrogram belonging to one device.

        Mirrors :meth:`StreamingEngine._stop_device`'s coalescer cleanup
        so a stopped device's flush ticks cannot race against a stale
        spectrogram entry (POSTMORTEMS 2026-05-10).
        """
        prefix = f"{device_name}{DEVICE_KEY_SEP}"
        stale = [k for k in self._spectrograms if k.startswith(prefix)]
        for k in stale:
            self._spectrograms.pop(k, None)
            self._pending.pop(k, None)
            self._drops_pending.pop(k, None)
            self._drops_last_log.pop(k, None)

    @Slot()
    def clear_all(self) -> None:
        """Drop every spectrogram. Called from ``engine.stop()`` via
        ``BlockingQueuedConnection`` — the engine waits for this slot
        to return before quitting the DSP thread."""
        # INFO so the wait the engine performs on this slot is observable
        # per CLAUDE.md rule 7 (matches _DspRouter.clear_chains).
        _log.info("spectrogram_router_clearing", n_streams=len(self._spectrograms))
        self._spectrograms.clear()
        self._pending.clear()
        self._drops_pending.clear()
        self._drops_last_log.clear()

    @Slot(str, str, object, object)
    def feed(
        self,
        device_name: str,
        nslc: str,
        samples: object,
        t_end: object,
    ) -> None:
        """Feed a chunk of (raw or processed) samples into the spectrogram.

        ``samples`` is typed ``object`` because it crosses a Qt signal —
        we coerce it back to a 1-D float32 ndarray here. ``t_end`` is
        the UTCDateTime of the last sample in the chunk (``None`` is
        accepted and forwarded — consumers that need timestamps are
        expected to fall back to wall clock).
        """
        key = device_stream_key(device_name, nslc)
        spec = self._spectrograms.get(key)
        if spec is None:
            return
        if not isinstance(samples, np.ndarray):
            return
        if samples.ndim != 1:
            return

        try:
            columns = spec.add_samples(samples)
        except Exception as exc:  # defence-in-depth — a corrupt input must not kill the thread
            _log.error(
                "spectrogram_feed_failed",
                device=device_name,
                nslc=nslc,
                error=str(exc),
            )
            return

        if not columns:
            return

        # Stamp each column with its own end time so GUI consumers can
        # place it on a wall-clock axis. ``t_end`` is the last column's
        # end (the chunk's last sample); earlier columns in this batch
        # end one column step (``column_dt``) earlier apiece.
        dt = spec.column_dt
        n = len(columns)
        base = t_end if isinstance(t_end, UTCDateTime) else None
        for j, col in enumerate(columns):
            col_t_end = (base - (n - 1 - j) * dt) if base is not None else None
            self._enqueue(device_name, nslc, key, col, col_t_end)
        self._drain(device_name, nslc, key)

    # ------------------------------------------------------------------
    # Backpressure helpers
    # ------------------------------------------------------------------
    def _enqueue(
        self,
        device_name: str,
        nslc: str,
        key: str,
        column: np.ndarray,
        t_end: UTCDateTime | None,
    ) -> None:
        queue = self._pending.get(key)
        if queue is None:
            return
        if len(queue) >= queue.maxlen:  # type: ignore[operator]
            # Drop oldest (deque enforces this on append, but we count it
            # explicitly so we can surface the rate via the dropped signal).
            self._drops_pending[key] = self._drops_pending.get(key, 0) + 1
            self._maybe_emit_drop(device_name, nslc, key)
        queue.append((column, t_end))

    def _drain(self, device_name: str, nslc: str, key: str) -> None:
        queue = self._pending.get(key)
        spec = self._spectrograms.get(key)
        if queue is None or spec is None:
            return
        freqs = spec.freqs()
        # Emit every queued column. ``columnReady`` is a queued signal
        # to the engine thread so we never block the DSP thread on GUI
        # work; the column ndarrays are small (n_freq_bins floats), so
        # emitting them one at a time is cheap.
        while queue:
            col, col_t_end = queue.popleft()
            self.columnReady.emit(device_name, nslc, col, freqs, col_t_end)

    def _maybe_emit_drop(self, device_name: str, nslc: str, key: str) -> None:
        now = time.monotonic()
        last = self._drops_last_log.get(key, 0.0)
        if now - last < _DROP_LOG_INTERVAL_S:
            return
        count = self._drops_pending.pop(key, 0)
        if count <= 0:
            return
        self._drops_last_log[key] = now
        _log.warning(
            "spectrogram_drop",
            device=device_name,
            nslc=nslc,
            dropped=count,
        )
        self.spectrogramDropped.emit(device_name, nslc, count)
