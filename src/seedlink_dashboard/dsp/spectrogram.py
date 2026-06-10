"""Stateful rolling-spectrogram helper.

Pure DSP module: no Qt, no I/O, no global state. Holds enough internal
sample buffer to keep STFT continuity across :meth:`add_samples` calls
of arbitrary chunk size.

Returned columns are linear power values (``|FFT|^2``); conversion to
dB or z-score happens in the GUI layer because colour-mapping range
is a UI concern.
"""

from __future__ import annotations

import numpy as np

# 2-second window at the input sample rate, per .claude/skills/seismic-dsp.
_WINDOW_SECONDS = 2.0
_MIN_NPERSEG = 64
_DEFAULT_OVERLAP = 0.5


def _default_nperseg(fs: float) -> int:
    return max(_MIN_NPERSEG, round(_WINDOW_SECONDS * fs))


class RollingSpectrogram:
    """STFT helper that emits one PSD column per ``nperseg - noverlap``
    new samples.

    The class is *stateful*: callers feed packets of any size via
    :meth:`add_samples`, and the helper buffers the tail (< nperseg
    samples) so the next call can pick up where this one stopped.
    Resetting the helper (e.g. on chain hot-reload) drops the tail
    and any accumulated phase context.

    Args:
        fs: Sample rate in Hz of the input signal.
        nperseg: FFT window length in samples. Defaults to ``2 * fs``
            rounded to the nearest integer (with a floor of 64) — the
            seismic-streaming recipe in ``.claude/skills/seismic-dsp``.
        overlap: Fractional overlap in ``[0, 1)``. Defaults to 0.5.

    Raises:
        ValueError: If ``fs`` is non-positive, ``nperseg`` is below 4,
            or ``overlap`` is outside ``[0, 1)``.
    """

    def __init__(
        self,
        fs: float,
        *,
        nperseg: int | None = None,
        overlap: float = _DEFAULT_OVERLAP,
    ) -> None:
        if fs <= 0:
            raise ValueError(f"fs must be > 0, got {fs}")
        if not 0.0 <= overlap < 1.0:
            raise ValueError(f"overlap must be in [0, 1), got {overlap}")

        nperseg_final = _default_nperseg(fs) if nperseg is None else int(nperseg)
        if nperseg_final < 4:
            raise ValueError(f"nperseg must be >= 4, got {nperseg_final}")

        noverlap = round(overlap * nperseg_final)
        if noverlap >= nperseg_final:
            noverlap = nperseg_final - 1

        self._fs = float(fs)
        self._nperseg = nperseg_final
        self._noverlap = noverlap
        self._step = nperseg_final - noverlap
        # Hann window precomputed once; float32 keeps the per-column
        # multiply cheap on 32-bit input data.
        self._window = np.hanning(nperseg_final).astype(np.float32)
        # Power normalisation: divide by sum(window^2) so peaks scale
        # with signal power rather than window energy.
        self._win_norm = float(np.sum(self._window**2))
        self._buffer = np.empty(0, dtype=np.float32)
        self._freqs = np.fft.rfftfreq(nperseg_final, d=1.0 / fs).astype(np.float32)

    @property
    def fs(self) -> float:
        return self._fs

    @property
    def nperseg(self) -> int:
        return self._nperseg

    @property
    def noverlap(self) -> int:
        return self._noverlap

    @property
    def n_freq_bins(self) -> int:
        return self._nperseg // 2 + 1

    @property
    def column_dt(self) -> float:
        """Wall-clock duration represented by one column."""
        return self._step / self._fs

    def freqs(self) -> np.ndarray:
        """Frequency bin centres in Hz."""
        return self._freqs

    def reset(self) -> None:
        """Drop the buffered tail. Next :meth:`add_samples` warms from scratch."""
        self._buffer = np.empty(0, dtype=np.float32)

    def add_samples(self, samples: np.ndarray) -> list[np.ndarray]:
        """Buffer ``samples`` and emit any complete STFT columns.

        Args:
            samples: 1-D array of new samples in the order they arrived.

        Returns:
            List of float32 power-spectral-density columns. Each column
            has length :attr:`n_freq_bins`. Empty list if the buffer
            does not yet contain a full ``nperseg`` window.
        """
        if samples.ndim != 1:
            raise ValueError(f"samples must be 1-D, got shape {samples.shape}")

        if samples.size == 0 and self._buffer.size < self._nperseg:
            return []

        # Concatenate without copying when possible.
        if samples.size:
            chunk = samples.astype(np.float32, copy=False)
            self._buffer = np.concatenate([self._buffer, chunk])

        columns: list[np.ndarray] = []
        cursor = 0
        while self._buffer.size - cursor >= self._nperseg:
            segment = self._buffer[cursor : cursor + self._nperseg]
            windowed = segment * self._window
            spectrum = np.fft.rfft(windowed)
            # Power: |X|^2 normalised by the window energy so amplitudes
            # are comparable across nperseg choices.
            power = (spectrum.real**2 + spectrum.imag**2) / self._win_norm
            columns.append(power.astype(np.float32))
            cursor += self._step

        # Keep the noverlap tail (plus anything beyond the last full
        # segment) as the new buffer.
        if cursor:
            self._buffer = self._buffer[cursor:].copy()

        return columns
