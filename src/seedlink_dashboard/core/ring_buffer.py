"""Lock-protected ring buffer for one-writer / many-readers streaming.

Designed for the StreamingEngine: the network worker thread pushes incoming
samples; GUI / DSP / archive consumers read snapshots.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from obspy.core.utcdatetime import UTCDateTime


class RingBuffer:
    """Fixed-capacity float32 ring buffer.

    Args:
        capacity_samples: Total ring size in samples. Must be > 0.
        sampling_rate: Sample rate in Hz, kept for downstream consumers.

    The buffer is single-writer / many-reader by contract. All public
    methods take an internal `threading.Lock` so concurrent access is
    safe; for performance, keep critical sections minimal and prefer
    `read_last(n)` over `read_all()` when `n` is much smaller than the
    capacity.
    """

    __slots__ = (
        "_buf",
        "_capacity",
        "_latest_t",
        "_lock",
        "_sampling_rate",
        "_stored",
        "_total_pushed",
        "_write_idx",
    )

    def __init__(self, capacity_samples: int, sampling_rate: float) -> None:
        if capacity_samples <= 0:
            raise ValueError(f"capacity_samples must be > 0, got {capacity_samples}")
        if sampling_rate <= 0.0:
            raise ValueError(f"sampling_rate must be > 0, got {sampling_rate}")
        self._buf: np.ndarray = np.zeros(capacity_samples, dtype=np.float32)
        self._capacity: int = capacity_samples
        self._sampling_rate: float = float(sampling_rate)
        self._total_pushed: int = 0
        self._stored: int = 0
        self._write_idx: int = 0
        self._latest_t: UTCDateTime | None = None
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def sampling_rate(self) -> float:
        return self._sampling_rate

    @property
    def latest_t(self) -> UTCDateTime | None:
        with self._lock:
            return self._latest_t

    def push(
        self,
        samples: np.ndarray,
        end_time: UTCDateTime | None = None,
    ) -> int:
        """Append `samples` to the ring; return number of samples dropped.

        "Dropped" counts both samples evicted from existing contents and
        samples from the input that don't fit (when `len(samples)` >
        capacity, only the last `capacity` are retained). The
        `total_pushed` counter returned by `read_all` is incremented by
        the offered count (i.e. `len(samples)` before truncation), so
        callers can detect throughput / loss separately.
        """
        if samples.ndim != 1:
            raise ValueError(f"samples must be 1-D, got shape {samples.shape}")
        if samples.dtype != np.float32:
            samples = samples.astype(np.float32, copy=False)

        with self._lock:
            n_input = int(samples.shape[0])
            if n_input == 0:
                return 0

            capacity = self._capacity
            if n_input > capacity:
                dropped_in_input = n_input - capacity
                samples = samples[-capacity:]
                n_to_write = capacity
            else:
                dropped_in_input = 0
                n_to_write = n_input

            free = capacity - self._stored
            evicted = max(0, n_to_write - free)

            end = self._write_idx + n_to_write
            if end <= capacity:
                self._buf[self._write_idx : end] = samples
            else:
                first_chunk = capacity - self._write_idx
                self._buf[self._write_idx :] = samples[:first_chunk]
                self._buf[: end - capacity] = samples[first_chunk:]

            self._write_idx = end % capacity
            self._stored = min(self._stored + n_to_write, capacity)
            self._total_pushed += n_input
            if end_time is not None:
                self._latest_t = end_time
            return dropped_in_input + evicted

    def read_last(self, n: int) -> np.ndarray:
        """Return a copy of the last `n` stored samples (or fewer if not yet filled)."""
        if n <= 0:
            return np.empty(0, dtype=np.float32)
        with self._lock:
            capacity = self._capacity
            n = min(n, self._stored)
            if n == 0:
                return np.empty(0, dtype=np.float32)
            start = (self._write_idx - n) % capacity
            end = self._write_idx
            if end > start:
                return self._buf[start:end].copy()
            # n == capacity or wraps the seam
            out = np.empty(n, dtype=np.float32)
            first_chunk = capacity - start
            out[:first_chunk] = self._buf[start:]
            out[first_chunk:] = self._buf[:end]
            return out

    def read_all(self) -> tuple[np.ndarray, int]:
        """Return (in-order copy of currently stored samples, total offered)."""
        with self._lock:
            capacity = self._capacity
            stored = self._stored
            if stored == 0:
                return np.empty(0, dtype=np.float32), self._total_pushed
            if stored < capacity:
                # Buffer hasn't wrapped: write_idx == stored.
                return self._buf[:stored].copy(), self._total_pushed
            # Full ring — read from write_idx (oldest) wrapping back.
            out = np.empty(capacity, dtype=np.float32)
            first_chunk = capacity - self._write_idx
            out[:first_chunk] = self._buf[self._write_idx :]
            out[first_chunk:] = self._buf[: self._write_idx]
            return out, self._total_pushed
