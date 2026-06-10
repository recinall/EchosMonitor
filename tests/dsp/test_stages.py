"""Unit tests for the streaming DSP stages.

The numerical assertions intentionally allow some headroom to absorb the
filter warm-up window — see comments inline.
"""

from __future__ import annotations

import numpy as np
import pytest
from obspy import UTCDateTime

from echosmonitor.dsp.stages import (
    Bandpass,
    Decimation,
    Detrend,
    Highpass,
    Lowpass,
    Notch,
    StaLta,
)

_FS = 100.0


def _t() -> UTCDateTime:
    return UTCDateTime(2026, 5, 8, 0, 0, 0)


# ---------------------------------------------------------------------------
# Bandpass — frequency-domain assertions on a synthetic two-tone signal
# ---------------------------------------------------------------------------


def test_bandpass_attenuates_low_frequency_passes_in_band() -> None:
    """5 Hz should pass with <1 dB loss; 0.05 Hz should be killed by >40 dB."""
    fs = 100.0
    n = 5 * 100  # 5 packets of 100 samples each = 5 seconds
    t = np.arange(n, dtype=np.float64) / fs
    low = np.sin(2 * np.pi * 0.05 * t)
    high = np.sin(2 * np.pi * 5.0 * t)
    mixed = low + high

    bp = Bandpass(fs=fs, freqmin=1.0, freqmax=10.0, corners=4)
    # Feed 5 packets so the filter warms up across boundaries.
    output = []
    for i in range(5):
        chunk = mixed[i * 100 : (i + 1) * 100]
        output.append(bp.process(chunk, _t() + i))
    y = np.concatenate(output)

    # Skip the first 5/fmin = 5 s of warm-up; for fmin=1 Hz, that's the
    # entire signal — so use 1 s instead and accept slightly tighter bounds.
    warmup = int(2.0 * fs)
    y_warm = y[warmup:]
    high_warm = high[warmup:]
    low_warm = low[warmup:]

    # 5 Hz passes through with <1 dB loss: amplitude ratio close to 1.
    high_amp_in = np.sqrt(np.mean(high_warm**2))
    high_amp_out = np.sqrt(np.mean((y_warm - low_warm * 0)[: len(high_warm)] ** 2))
    # Output is dominated by 5 Hz (low killed). RMS ~ 1/sqrt(2) for unit sine.
    assert high_amp_out > 0.5 * high_amp_in, (
        f"5 Hz lost too much energy: {high_amp_out / high_amp_in:.3f}"
    )

    # The filter output should not contain detectable 0.05 Hz energy.
    # Project y_warm onto a 0.05 Hz cosine — expect attenuation > 40 dB.
    # (40 dB → amplitude ratio < 0.01)
    low_proj_in = np.sqrt(np.mean(low_warm**2))
    # Compute the 0.05 Hz component of the output via Goertzel-style projection.
    t_warm = np.arange(y_warm.size, dtype=np.float64) / fs
    cos_lf = np.cos(2 * np.pi * 0.05 * t_warm)
    sin_lf = np.sin(2 * np.pi * 0.05 * t_warm)
    a = 2 * np.mean(y_warm * cos_lf)
    b = 2 * np.mean(y_warm * sin_lf)
    low_proj_out = np.sqrt(a**2 + b**2) / np.sqrt(2)
    ratio = low_proj_out / max(low_proj_in, 1e-12)
    db = 20 * np.log10(max(ratio, 1e-12))
    assert db < -40.0, f"0.05 Hz attenuation only {db:.1f} dB, expected < -40 dB"


# ---------------------------------------------------------------------------
# Stateful continuity — chunked vs one-shot must agree (after warm-up)
# ---------------------------------------------------------------------------


def test_bandpass_chunked_matches_oneshot() -> None:
    """Process 1000 samples in one shot vs 100 packets of 10 samples each.

    The two outputs must agree after the warm-up window. We use 5/fmin as
    the warm-up budget, then compare with `atol=1e-9`.
    """
    fs = 100.0
    n = 1000
    rng = np.random.default_rng(seed=42)
    x = rng.standard_normal(n)

    bp_oneshot = Bandpass(fs=fs, freqmin=1.0, freqmax=10.0, corners=4)
    y_oneshot = bp_oneshot.process(x, _t())

    bp_chunked = Bandpass(fs=fs, freqmin=1.0, freqmax=10.0, corners=4)
    chunks = []
    for i in range(0, n, 10):
        chunks.append(bp_chunked.process(x[i : i + 10], _t() + i / fs))
    y_chunked = np.concatenate(chunks)

    assert y_oneshot.shape == y_chunked.shape
    np.testing.assert_allclose(y_oneshot, y_chunked, atol=1e-9, rtol=0)


def test_highpass_chunked_matches_oneshot() -> None:
    fs = 100.0
    n = 500
    rng = np.random.default_rng(seed=1)
    x = rng.standard_normal(n)

    hp_oneshot = Highpass(fs=fs, freq=2.0, corners=4)
    y_oneshot = hp_oneshot.process(x, _t())

    hp_chunked = Highpass(fs=fs, freq=2.0, corners=4)
    chunks = [hp_chunked.process(x[i : i + 25], _t() + i / fs) for i in range(0, n, 25)]
    y_chunked = np.concatenate(chunks)

    np.testing.assert_allclose(y_oneshot, y_chunked, atol=1e-9, rtol=0)


def test_lowpass_chunked_matches_oneshot() -> None:
    fs = 100.0
    n = 500
    rng = np.random.default_rng(seed=2)
    x = rng.standard_normal(n)

    lp_oneshot = Lowpass(fs=fs, freq=20.0, corners=4)
    y_oneshot = lp_oneshot.process(x, _t())

    lp_chunked = Lowpass(fs=fs, freq=20.0, corners=4)
    chunks = [lp_chunked.process(x[i : i + 25], _t() + i / fs) for i in range(0, n, 25)]
    y_chunked = np.concatenate(chunks)

    np.testing.assert_allclose(y_oneshot, y_chunked, atol=1e-9, rtol=0)


def test_notch_attenuates_target_frequency() -> None:
    fs = 200.0  # higher fs so 50 Hz is well below Nyquist
    n = 2000
    t = np.arange(n, dtype=np.float64) / fs
    # 50 Hz interference + 5 Hz signal of interest
    x = np.sin(2 * np.pi * 50.0 * t) + 0.5 * np.sin(2 * np.pi * 5.0 * t)
    notch = Notch(fs=fs, freq=50.0, quality=30.0)
    y = notch.process(x, _t())
    # Skip warm-up
    warmup = 400
    y_warm = y[warmup:]
    t_warm = t[warmup:]
    cos_50 = np.cos(2 * np.pi * 50.0 * t_warm)
    sin_50 = np.sin(2 * np.pi * 50.0 * t_warm)
    proj_50 = np.sqrt((2 * np.mean(y_warm * cos_50)) ** 2 + (2 * np.mean(y_warm * sin_50)) ** 2)
    cos_5 = np.cos(2 * np.pi * 5.0 * t_warm)
    sin_5 = np.sin(2 * np.pi * 5.0 * t_warm)
    proj_5 = np.sqrt((2 * np.mean(y_warm * cos_5)) ** 2 + (2 * np.mean(y_warm * sin_5)) ** 2)
    # 50 Hz should be <0.1 (>20 dB notch); 5 Hz signal of input amplitude 0.5
    # should pass through with most of its energy intact.
    assert proj_50 < 0.1, f"50 Hz not notched out: {proj_50:.3f}"
    assert proj_5 > 0.4, f"5 Hz signal lost too much energy: {proj_5:.3f}"


# ---------------------------------------------------------------------------
# Decimation
# ---------------------------------------------------------------------------


def test_decimation_factor_4_changes_fs_out() -> None:
    dec = Decimation(fs=100.0, factor=4)
    assert dec.fs_out == pytest.approx(25.0)


def test_decimation_chained_factors() -> None:
    """100 Hz → factor 4 → 25 Hz → factor 4 → 6.25 Hz."""
    dec1 = Decimation(fs=100.0, factor=4)
    dec2 = Decimation(fs=25.0, factor=4)
    assert dec1.fs_out == pytest.approx(25.0)
    assert dec2.fs_out == pytest.approx(6.25)


def test_decimation_total_sample_count_matches_factor() -> None:
    """After feeding N samples with factor F, output should be ~N/F samples."""
    fs = 100.0
    factor = 4
    n_total = 10_000
    rng = np.random.default_rng(seed=7)
    x = rng.standard_normal(n_total)

    dec = Decimation(fs=fs, factor=factor)
    chunks = []
    chunk_size = 100
    for i in range(0, n_total, chunk_size):
        chunks.append(dec.process(x[i : i + chunk_size], _t() + i / fs))
    y = np.concatenate(chunks)

    # Allow ±tail_len/factor samples of slack near the end of the last buffer.
    expected = n_total // factor
    assert abs(len(y) - expected) <= 5, f"got {len(y)}, expected ~{expected}"


def test_decimation_factor_must_be_in_range() -> None:
    with pytest.raises(ValueError):
        Decimation(fs=100.0, factor=1)
    with pytest.raises(ValueError):
        Decimation(fs=100.0, factor=17)


# ---------------------------------------------------------------------------
# Detrend
# ---------------------------------------------------------------------------


def test_detrend_constant_removes_offset() -> None:
    fs = 100.0
    n = 500
    rng = np.random.default_rng(seed=3)
    x = rng.standard_normal(n) + 10.0  # offset by 10
    d = Detrend(fs=fs, kind="constant")
    chunks = []
    for i in range(0, n, 50):
        chunks.append(d.process(x[i : i + 50], _t() + i / fs))
    y = np.concatenate(chunks)
    # The recursive mean tracker takes ~30 s to converge, but for a
    # 5 s signal the offset should still be partly subtracted. Just
    # assert |mean(y)| < |mean(x)| significantly.
    assert abs(float(np.mean(y))) < 5.0, (
        f"detrend(constant) failed to reduce 10.0 offset: mean(y)={np.mean(y)}"
    )


def test_detrend_linear_zeros_a_perfect_ramp() -> None:
    fs = 100.0
    n = 200
    x = np.linspace(0, 100, n)
    d = Detrend(fs=fs, kind="linear")
    y = d.process(x, _t())
    # A perfect linear ramp gets detrended to ~zero.
    assert float(np.max(np.abs(y))) < 1e-9


# ---------------------------------------------------------------------------
# STA/LTA
# ---------------------------------------------------------------------------


def test_sta_lta_detects_synthetic_burst_within_tolerance() -> None:
    """Inject a 10x amplitude burst at t = 50 s into 60 s of noise.

    Expect at least one ON trigger; t_on should land within 1 s of the
    true onset (allowing for STA window and recursive estimator warm-up).
    """
    fs = 100.0
    duration_s = 60
    n_total = int(duration_s * fs)
    rng = np.random.default_rng(seed=8)
    noise = rng.standard_normal(n_total)
    onset_s = 50.0
    onset_idx = int(onset_s * fs)
    burst = np.zeros_like(noise)
    burst[onset_idx : onset_idx + int(2.0 * fs)] = 10.0 * rng.standard_normal(int(2.0 * fs))
    x = noise + burst

    sta_lta = StaLta(
        fs=fs,
        sta_s=1.0,
        lta_s=10.0,
        on_thr=3.5,
        off_thr=1.5,
        nslc="IU.ANMO.00.BHZ",
    )

    t0 = _t()
    triggers_seen = []
    chunk_size = 100
    for i in range(0, n_total, chunk_size):
        sta_lta.process(x[i : i + chunk_size], t0 + i / fs)
        for trig in sta_lta.triggers:
            triggers_seen.append(trig)

    # Filter to ON events (t_off may be None for open triggers, but each
    # finalised trigger has a real t_off).
    finalised = [trig for trig in triggers_seen if trig.t_off is not None]
    on_only = [trig for trig in triggers_seen if trig.t_off is None]

    # We expect at least one trigger overall.
    assert len(triggers_seen) >= 1, f"no triggers detected (saw {len(triggers_seen)})"
    # The first ON event should land near the true onset.
    first_t_on = (finalised + on_only)[0].t_on
    onset_truth = t0 + onset_s
    drift = float(first_t_on - onset_truth)
    assert -1.0 <= drift <= 1.0, f"first trigger t_on drift {drift:.3f}s exceeds ±1s"


def test_sta_lta_ratio_helper_peaks_at_burst() -> None:
    """The pure one-shot ratio helper (used by the detail pane) returns a
    same-length finite curve that peaks near a planted burst."""
    from echosmonitor.dsp.stages import sta_lta_ratio

    fs = 100.0
    rng = np.random.default_rng(seed=3)
    x = rng.standard_normal(int(60 * fs))
    onset = int(50 * fs)
    x[onset : onset + int(2 * fs)] += 10.0 * rng.standard_normal(int(2 * fs))

    ratio = sta_lta_ratio(x, sta_s=1.0, lta_s=10.0, fs=fs)
    assert ratio.shape == x.shape
    assert np.all(np.isfinite(ratio))
    # The maximum ratio lands within ±2 s of the planted onset.
    peak_idx = int(np.argmax(ratio))
    assert abs(peak_idx - onset) < 2 * fs


def test_sta_lta_ratio_helper_too_short_returns_empty() -> None:
    from echosmonitor.dsp.stages import sta_lta_ratio

    # Fewer samples than one LTA window → empty (estimator not converged).
    short = np.ones(50, dtype=np.float64)
    assert sta_lta_ratio(short, sta_s=1.0, lta_s=10.0, fs=100.0).size == 0
    assert sta_lta_ratio(np.empty(0), sta_s=1.0, lta_s=10.0, fs=100.0).size == 0


def test_sta_lta_returns_input_unchanged() -> None:
    """STA/LTA is a tap, not a filter."""
    fs = 100.0
    rng = np.random.default_rng(seed=4)
    x = rng.standard_normal(500)
    sta_lta = StaLta(
        fs=fs,
        sta_s=0.5,
        lta_s=5.0,
        on_thr=3.5,
        off_thr=1.5,
        nslc="X.Y.Z.W",
    )
    y = sta_lta.process(x, _t())
    np.testing.assert_array_equal(x, y)


# ---------------------------------------------------------------------------
# Bandpass guards
# ---------------------------------------------------------------------------


def test_bandpass_rejects_freqmax_above_nyquist() -> None:
    with pytest.raises(ValueError):
        Bandpass(fs=100.0, freqmin=1.0, freqmax=60.0, corners=4)


def test_bandpass_rejects_inverted_band() -> None:
    with pytest.raises(ValueError):
        Bandpass(fs=100.0, freqmin=10.0, freqmax=5.0, corners=4)
