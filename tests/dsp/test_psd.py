"""Tests for :func:`welch_psd` and :func:`power_to_db`.

Numerical assertions allow some headroom for Welch's noise floor on
synthetic inputs.
"""

from __future__ import annotations

import numpy as np
import pytest

from echosmonitor.dsp.psd import power_to_db, welch_psd

_FS = 100.0


def _sine(fs: float, f0: float, n: int, *, amp: float = 1.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / fs
    return amp * np.sin(2 * np.pi * f0 * t)


def test_welch_rejects_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        welch_psd(np.array([1.0]), fs=0.0)
    with pytest.raises(ValueError):
        welch_psd(np.array([1.0]), fs=_FS, overlap=1.0)
    with pytest.raises(ValueError):
        welch_psd(np.zeros((4, 4)), fs=_FS)


def test_welch_returns_empty_on_empty_input() -> None:
    freqs, power = welch_psd(np.empty(0, dtype=np.float64), fs=_FS)
    assert freqs.size == 0
    assert power.size == 0


def test_welch_peak_close_to_input_frequency() -> None:
    """A 5 Hz pure tone must produce its largest PSD peak near 5 Hz."""
    n = 30 * int(_FS)  # 30 s of signal
    samples = _sine(_FS, 5.0, n, amp=1.0)
    freqs, power = welch_psd(samples, _FS, segment_seconds=8.0)
    peak_idx = int(np.argmax(power))
    assert abs(freqs[peak_idx] - 5.0) < 0.5


def test_welch_power_scales_with_amplitude_squared() -> None:
    """Doubling input amplitude must roughly 4x the peak PSD value
    (power = amplitude^2 in PSD space)."""
    n = 30 * int(_FS)
    s1 = _sine(_FS, 5.0, n, amp=1.0)
    s2 = _sine(_FS, 5.0, n, amp=2.0)
    _, p1 = welch_psd(s1, _FS)
    _, p2 = welch_psd(s2, _FS)
    ratio = float(p2.max() / max(p1.max(), 1e-30))
    # Welch's noise floor makes the assertion non-trivial; allow ±20 %.
    assert 3.2 < ratio < 4.8


def test_welch_frequencies_span_zero_to_nyquist() -> None:
    n = 4_000
    samples = _sine(_FS, 10.0, n)
    freqs, _ = welch_psd(samples, _FS, segment_seconds=4.0)
    assert freqs[0] == pytest.approx(0.0)
    assert freqs[-1] == pytest.approx(_FS / 2.0)


def test_welch_freq_resolution_matches_nperseg() -> None:
    """With nperseg = segment_seconds * fs samples, the bin spacing is
    fs / nperseg. Verify on a known input length."""
    n = 4_000
    samples = _sine(_FS, 10.0, n)
    freqs, _ = welch_psd(samples, _FS, segment_seconds=4.0)
    expected_df = _FS / round(4.0 * _FS)  # 100 / 400 = 0.25 Hz
    actual_df = float(freqs[1] - freqs[0])
    assert actual_df == pytest.approx(expected_df, rel=1e-6)


def test_welch_handles_short_input_by_clamping_nperseg() -> None:
    # 200 samples is shorter than 8 s x 100 Hz = 800; nperseg is clamped.
    samples = _sine(_FS, 5.0, 200)
    freqs, power = welch_psd(samples, _FS, segment_seconds=8.0)
    assert freqs.size > 0
    assert power.size == freqs.size


def test_power_to_db_clamps_zero() -> None:
    power = np.array([0.0, 1e-40, 1.0, 100.0], dtype=np.float64)
    db = power_to_db(power)
    assert np.all(np.isfinite(db))
    # 1.0 → 0 dB, 100 → 20 dB
    assert db[2] == pytest.approx(0.0, abs=1e-3)
    assert db[3] == pytest.approx(20.0, abs=1e-3)
