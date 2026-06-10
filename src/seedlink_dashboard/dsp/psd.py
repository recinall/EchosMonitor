"""Welch's-method PSD estimator for the M6 PSD widget.

Thin wrapper around :func:`scipy.signal.welch` so:

1. The parameter set is deterministic (one source of truth).
2. The signature is type-checked and easy to mock in tests.
3. Callers do not import scipy themselves and the project can
   migrate to a different backend in one place if needed.

This is a pure DSP function — no Qt, no I/O, no global state.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from scipy.signal import welch

# Default Welch parameters tuned for seismic broadband data per the
# seismic-dsp skill recipe. Callers can override ``segment_seconds``
# for tighter / looser frequency resolution trade-offs.
_DEFAULT_SEGMENT_SECONDS = 8.0
_DEFAULT_OVERLAP = 0.5


def welch_psd(
    samples: np.ndarray,
    fs: float,
    *,
    segment_seconds: float = _DEFAULT_SEGMENT_SECONDS,
    overlap: float = _DEFAULT_OVERLAP,
    window: str = "hann",
    detrend: Literal["linear", "constant"] | None = "linear",
    scaling: Literal["density", "spectrum"] = "density",
) -> tuple[np.ndarray, np.ndarray]:
    """Compute the Welch PSD of ``samples``.

    Args:
        samples: 1-D array (any float dtype). Empty input is valid
            and returns ``(empty, empty)``.
        fs: Sample rate in Hz. Must be positive.
        segment_seconds: Per-segment window length in seconds. The
            ``nperseg`` parameter passed to scipy is
            ``int(round(segment_seconds * fs))``, clamped to at most
            the input length.
        overlap: Fractional overlap between segments in ``[0, 1)``.
        window: Window function name (passed to scipy).
        detrend: scipy ``detrend`` argument; ``None`` keeps the data
            untouched.
        scaling: "density" returns PSD in ``Unit^2/Hz``; "spectrum"
            returns power spectrum. The PSD widget uses "density".

    Returns:
        ``(frequencies_hz, psd)`` — both 1-D float64 arrays of the
        same length. ``frequencies_hz[0] == 0`` and
        ``frequencies_hz[-1] == fs / 2``.
    """
    if fs <= 0:
        raise ValueError(f"fs must be > 0, got {fs}")
    if not 0.0 <= overlap < 1.0:
        raise ValueError(f"overlap must be in [0, 1), got {overlap}")
    if samples.ndim != 1:
        raise ValueError(f"samples must be 1-D, got shape {samples.shape}")
    if samples.size == 0:
        empty = np.empty(0, dtype=np.float64)
        return empty, empty

    nperseg = max(1, round(segment_seconds * fs))
    nperseg = min(nperseg, int(samples.size))
    noverlap = min(round(overlap * nperseg), nperseg - 1)
    if noverlap < 0:
        noverlap = 0

    detrend_arg: Literal["linear", "constant"] | bool = False if detrend is None else detrend
    freqs, power = welch(
        samples.astype(np.float64, copy=False),
        fs=fs,
        window=window,
        nperseg=nperseg,
        noverlap=noverlap,
        detrend=detrend_arg,
        scaling=scaling,
        return_onesided=True,
    )
    return freqs, power


def power_to_db(power: np.ndarray, *, floor: float = 1e-30) -> np.ndarray:
    """Convert PSD power values to dB. ``floor`` clamps non-positive
    inputs so ``log10(0)`` does not produce ``-inf`` / ``NaN``."""
    safe = np.maximum(power, floor)
    out: np.ndarray = 10.0 * np.log10(safe)
    return out
