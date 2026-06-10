"""Tests for :class:`RollingSpectrogram`.

The four properties under test are the contract Stage 1 of M6 builds
upon: stateful continuity, frequency localisation, behaviour on
short / empty input, and reset semantics.
"""

from __future__ import annotations

import numpy as np
import pytest

from echosmonitor.dsp.spectrogram import RollingSpectrogram

_FS = 100.0


def _make_sine(fs: float, f0: float, n: int, *, phase: float = 0.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / fs
    return np.sin(2.0 * np.pi * f0 * t + phase).astype(np.float32)


def test_default_nperseg_uses_two_second_window() -> None:
    spec = RollingSpectrogram(fs=_FS)
    # 2 s x 100 Hz = 200 samples.
    assert spec.nperseg == 200
    # 50 % overlap → step 100 samples → 1 column per second.
    assert spec.noverlap == 100
    assert spec.column_dt == pytest.approx(1.0)
    # rfft yields nperseg/2 + 1 bins.
    assert spec.n_freq_bins == 101


def test_invalid_arguments_raise() -> None:
    with pytest.raises(ValueError):
        RollingSpectrogram(fs=0)
    with pytest.raises(ValueError):
        RollingSpectrogram(fs=_FS, overlap=1.0)
    with pytest.raises(ValueError):
        RollingSpectrogram(fs=_FS, overlap=-0.1)
    with pytest.raises(ValueError):
        RollingSpectrogram(fs=_FS, nperseg=2)


def test_short_input_yields_no_columns() -> None:
    spec = RollingSpectrogram(fs=_FS, nperseg=128, overlap=0.5)
    # Fewer samples than one segment → no columns yet.
    out = spec.add_samples(_make_sine(_FS, 5.0, 50))
    assert out == []


def test_empty_input_with_empty_buffer_returns_empty() -> None:
    spec = RollingSpectrogram(fs=_FS)
    assert spec.add_samples(np.empty(0, dtype=np.float32)) == []


def test_stateful_continuity_one_shot_vs_chunked() -> None:
    """The boundary contract: feeding the same signal in any chunk
    sequence must produce the SAME column sequence as one-shot."""
    fs = _FS
    n = 10_000
    signal = _make_sine(fs, 7.0, n) + 0.3 * _make_sine(fs, 17.0, n)

    one_shot = RollingSpectrogram(fs=fs, nperseg=256, overlap=0.5)
    cols_a = one_shot.add_samples(signal.copy())

    chunked = RollingSpectrogram(fs=fs, nperseg=256, overlap=0.5)
    cols_b: list[np.ndarray] = []
    sizes = [33, 100, 7, 1, 512, 1024, 2048, 50, 6225]  # sum = 10_000
    cursor = 0
    for size in sizes:
        cols_b.extend(chunked.add_samples(signal[cursor : cursor + size]))
        cursor += size
    assert cursor == n

    assert len(cols_a) == len(cols_b)
    for a, b in zip(cols_a, cols_b, strict=True):
        np.testing.assert_allclose(a, b, rtol=1e-6, atol=1e-9)


def test_frequency_localization_pure_sine() -> None:
    """A pure tone at f0 must produce a peak at the closest bin
    with > 30 dB SNR vs the median power."""
    fs = _FS
    f0 = 12.0
    n = 4_000  # plenty of full segments
    sine = _make_sine(fs, f0, n)

    spec = RollingSpectrogram(fs=fs, nperseg=512, overlap=0.5)
    cols = spec.add_samples(sine)
    assert len(cols) > 0

    avg = np.mean(np.stack(cols), axis=0)
    freqs = spec.freqs()
    peak_bin = int(np.argmax(avg))
    # The closest bin to f0 = 12 Hz at fs=100, nperseg=512 is bin 61
    # (f = 100/512 * 61 ≈ 11.91 Hz) or 62 (~12.11 Hz). Either is OK.
    closest_bin = int(np.argmin(np.abs(freqs - f0)))
    assert abs(peak_bin - closest_bin) <= 1, (
        f"expected peak near {closest_bin} (~{freqs[closest_bin]:.2f} Hz), "
        f"got bin {peak_bin} (~{freqs[peak_bin]:.2f} Hz)"
    )

    # SNR vs median: ratio should be > 1000 (≈ 30 dB).
    median = float(np.median(avg))
    assert avg[peak_bin] / max(median, 1e-30) > 1000


def test_reset_drops_buffer() -> None:
    spec = RollingSpectrogram(fs=_FS, nperseg=128, overlap=0.5)
    # Feed half a window — should produce no column but buffer the data.
    spec.add_samples(_make_sine(_FS, 5.0, 60))
    spec.reset()
    # After reset, feeding another half-window must NOT produce a
    # column (otherwise the reset didn't drop the tail).
    out = spec.add_samples(_make_sine(_FS, 5.0, 60))
    assert out == []


def test_columns_are_float32() -> None:
    spec = RollingSpectrogram(fs=_FS)
    cols = spec.add_samples(_make_sine(_FS, 5.0, 1000))
    assert all(c.dtype == np.float32 for c in cols)
    assert all(c.shape == (spec.n_freq_bins,) for c in cols)


def test_rejects_non_1d_input() -> None:
    spec = RollingSpectrogram(fs=_FS)
    with pytest.raises(ValueError):
        spec.add_samples(np.zeros((10, 10), dtype=np.float32))


def test_output_domain_is_linear_power() -> None:
    """Contract pinned for the GUI colour-map: columns are LINEAR power
    (|FFT|² / window energy) — strictly non-negative and finite, NOT
    dB or normalised. The spectrogram view relies on this to apply its
    own log / z-score transforms; if this contract ever changes the
    rendering would silently break (POSTMORTEMS 2026-05-31), so assert
    it here rather than let it slip through to a degenerate image.
    """
    fs = _FS
    # 5 Hz tone (power ≫ 1 at the peak) + noise so the dynamic range is
    # wide and we'd notice any hidden normalisation.
    sig = _make_sine(fs, 5.0, 6_000) + 0.1 * _make_sine(fs, 23.0, 6_000)
    spec = RollingSpectrogram(fs=fs)
    cols = spec.add_samples(sig)
    assert len(cols) > 0
    stacked = np.stack(cols)
    assert np.all(np.isfinite(stacked)), "power columns must be finite"
    assert np.all(stacked >= 0.0), "linear power is non-negative"
    # A unit-amplitude sine has real, large peak power — well above 1.0,
    # which a dB (≈0) or [0,1]-normalised output could never reach.
    assert stacked.max() > 10.0, "output does not look like linear power"
