"""HVSR core — scientific correctness + the override / boundary invariants.

These exercise :mod:`echosmonitor.core.hvsr` directly (no Qt). The
heavy ``hvsrpy`` workflow runs for real (hvsrpy is a core dependency), so
the f0-recovery test is a genuine end-to-end scientific check, not a mock.
"""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest
from obspy.core.utcdatetime import UTCDateTime
from scipy import signal

from echosmonitor.core.exceptions import HvsrError
from echosmonitor.core.hvsr import (
    HvsrAccumulator,
    HvsrSettings,
    responses_identical,
)

_FS = 100.0
_WL = 20.0
_NPER = int(_WL * _FS)
_F0 = 5.0  # injected resonance (Hz)


def _resonant(n: int, f0: float, rng: np.random.Generator) -> np.ndarray:
    """White noise shaped by a sharp resonator at ``f0`` (a site resonance)."""
    white = rng.standard_normal(n)
    b, a = signal.iirpeak(f0 / (_FS / 2.0), 25.0)
    return white * 0.3 + signal.lfilter(b, a, white) * 4.0


def _accumulator(
    n_windows: int,
    *,
    f0: float = _F0,
    seed: int = 0,
    settings: HvsrSettings | None = None,
) -> HvsrAccumulator:
    """An accumulator filled with ``n_windows`` synthetic 3C resonance windows.

    Z is white; N/E carry the resonance, so the H/V ratio peaks at ``f0``.
    """
    rng = np.random.default_rng(seed)
    s = settings or HvsrSettings(
        window_length_s=_WL, freqmin_hz=0.5, freqmax_hz=40.0, resample_n=128
    )
    acc = HvsrAccumulator(
        s,
        same_response=True,
        same_response_detail="test",
        device="dev",
        station_key="XX.STA",
        provenance="live",
    )
    t0 = UTCDateTime("2026-01-01")
    for i in range(n_windows):
        z = rng.standard_normal(_NPER)
        n = _resonant(_NPER, f0, rng)
        e = _resonant(_NPER, f0, rng)
        acc.add_window(z, n, e, t0 + i * _WL, _FS)
    return acc


# ----------------------------------------------------------------------
# Accumulation / validation
# ----------------------------------------------------------------------
def test_add_window_rejects_length_mismatch() -> None:
    acc = _accumulator(0)
    with pytest.raises(HvsrError, match="length mismatch"):
        acc.add_window(np.zeros(100), np.zeros(100), np.zeros(99), UTCDateTime(0), _FS)


def test_add_window_rejects_bad_fs() -> None:
    acc = _accumulator(0)
    with pytest.raises(HvsrError, match="fs must be"):
        acc.add_window(np.zeros(100), np.zeros(100), np.zeros(100), UTCDateTime(0), 0.0)


def test_add_window_rejects_inconsistent_fs() -> None:
    acc = _accumulator(0)
    acc.add_window(np.zeros(100), np.zeros(100), np.zeros(100), UTCDateTime(0), 100.0)
    with pytest.raises(HvsrError, match="inconsistent"):
        acc.add_window(np.zeros(100), np.zeros(100), np.zeros(100), UTCDateTime(0), 50.0)


def test_window_id_is_monotonic_and_stable() -> None:
    acc = _accumulator(0)
    ids = [
        acc.add_window(np.ones(10), np.ones(10), np.ones(10), UTCDateTime(0), _FS) for _ in range(5)
    ]
    assert ids == [0, 1, 2, 3, 4]
    assert acc.window_ids() == (0, 1, 2, 3, 4)


# ----------------------------------------------------------------------
# Scientific correctness
# ----------------------------------------------------------------------
def test_f0_recovery_on_known_resonance() -> None:
    """The computed f0 matches the injected resonance within tolerance."""
    acc = _accumulator(20, f0=_F0, seed=1)
    res = acc.compute()
    assert abs(res.f0_hz - _F0) / _F0 < 0.15, f"recovered f0={res.f0_hz}"
    assert res.a0 > 2.0  # a clear resonance peak
    assert res.n_windows_valid >= 1
    # SESAME ran and produced the full 3 + 6 criteria.
    assert len(res.reliability) == 3
    assert len(res.clarity) == 6


def test_compute_handles_variable_length_windows() -> None:
    """Windows shorter OR longer than window_length_s still compute 1:1.

    The live 0.9-fill gate permits a window a hair short of window_length_s,
    and the archive path can hand a longer slice. Each accumulated window must
    map to exactly one H/V curve (no ValueError, no split) — the regression
    guard for the row↔window-id alignment the override system rests on.
    """
    rng = np.random.default_rng(11)
    s = HvsrSettings(window_length_s=_WL, freqmin_hz=0.5, freqmax_hz=40.0, resample_n=128)
    acc = HvsrAccumulator(
        s,
        same_response=True,
        same_response_detail="test",
        device="dev",
        station_key="XX.STA",
        provenance="live",
    )
    t0 = UTCDateTime("2026-01-01")
    # Lengths: 0.9·wl (live under-fill), exactly wl, 1.5·wl (archive over-fill).
    for i, frac in enumerate([0.9, 1.0, 1.0, 1.5, 0.9, 1.0]):
        n = int(_WL * frac * _FS)
        acc.add_window(
            rng.standard_normal(n),
            _resonant(n, _F0, rng),
            _resonant(n, _F0, rng),
            t0 + i * _WL,
            _FS,
        )
    res = acc.compute()
    # Exactly one curve per accumulated window — no ValueError, no split.
    assert res.window_curves.shape[0] == acc.n_windows == 6
    assert res.window_ids == (0, 1, 2, 3, 4, 5)
    assert abs(res.f0_hz - _F0) / _F0 < 0.2


def test_psd_smoothing_is_smoother_than_raw() -> None:
    """FEATURE 5: the K-O smoothed PSD has lower point-to-point variance than raw."""
    acc_raw = _accumulator(
        12,
        seed=9,
        settings=HvsrSettings(
            window_length_s=_WL,
            freqmin_hz=0.5,
            freqmax_hz=40.0,
            resample_n=128,
            psd_smoothing=False,
        ),
    )
    acc_sm = _accumulator(
        12,
        seed=9,
        settings=HvsrSettings(
            window_length_s=_WL, freqmin_hz=0.5, freqmax_hz=40.0, resample_n=128, psd_smoothing=True
        ),
    )
    _, db_raw = acc_raw.compute().psd_z
    _, db_sm = acc_sm.compute().psd_z
    var_raw = float(np.var(np.diff(db_raw)))
    var_sm = float(np.var(np.diff(db_sm)))
    assert var_sm < var_raw, f"smoothed ({var_sm}) not smoother than raw ({var_raw})"
    # The raw channel PSD helper (early-display path) is the un-smoothed one.
    raw = acc_sm.raw_channel_psds()
    assert set(raw) == {"Z", "N", "E"}


def test_psd_smoothing_has_no_nonphysical_downward_spikes() -> None:
    """REGRESSION: the K-O smoothed PSD must not emit -300 dB sentinel spikes.

    Near the low edge of the band the log-spaced K-O centre frequencies are
    denser than the linearly-spaced Welch grid (df = fs/nperseg), so some
    centre frequencies land in a gap with NO input bin inside the operator's
    narrow ±18% support. hvsrpy returns exactly 0 there, which power_to_db
    would floor to a non-physical -300 dB downward spike. The fix drops those
    unsupported centre frequencies; assert the OBSERVABLE (no spike across the
    band, incl. the edges), not just that smoothing ran (rule 10).
    """
    from echosmonitor.core.hvsr import _maybe_smooth_psd
    from echosmonitor.dsp.psd import welch_psd

    rng = np.random.default_rng(0)
    data = np.cumsum(rng.standard_normal(int(_FS * 600)))
    data -= data.mean()
    # Defaults reproduce the bug: freqmin 0.2 Hz, b=40, 512 log-spaced centre
    # frequencies against a df=0.125 Hz (8 s segment) Welch grid.
    settings = HvsrSettings(
        freqmin_hz=0.2,
        freqmax_hz=20.0,
        resample_n=512,
        psd_konno_ohmachi_b=40.0,
        psd_smoothing=True,
    )
    freqs, power = welch_psd(data, _FS)
    # Sanity: the raw Welch input itself has no zeros / non-finite bins, so any
    # -300 dB spike could only come from the smoothing (rules out H3).
    assert np.all(np.isfinite(power)) and np.all(power[freqs > 0] > 0.0)

    fcs, db = _maybe_smooth_psd(freqs, power, settings)
    assert fcs.size > 0
    assert np.all(np.isfinite(db))
    # No non-physical downward spike anywhere — the -300 dB sentinel is ~-300;
    # a real microtremor PSD floor sits far above -200 dB rel. counts²/Hz.
    assert db.min() > -200.0, f"non-physical PSD spike survived: min={db.min():.1f} dB"
    # Clipped frequency axis stays monotonic and inside the requested band.
    assert np.all(np.diff(fcs) > 0)
    assert fcs[0] >= settings.freqmin_hz
    assert fcs[-1] <= settings.freqmax_hz + 1e-9


def test_psd_smoothing_runs_in_linear_power_not_db() -> None:
    """The K-O smoothing must run on LINEAR power, THEN convert to dB.

    Welch(power) -> K-O(power) -> 10·log10 is the only correct order. Smoothing
    dB values directly (Welch -> 10·log10 -> K-O) is a different, wrong result.
    Pin the domain so a refactor can't silently move the smoothing into dB.
    """
    import hvsrpy.smoothing as ko

    from echosmonitor.core.hvsr import _maybe_smooth_psd
    from echosmonitor.dsp.psd import power_to_db, welch_psd

    rng = np.random.default_rng(1)
    data = np.cumsum(rng.standard_normal(int(_FS * 600)))
    data -= data.mean()
    settings = HvsrSettings(
        freqmin_hz=0.5,
        freqmax_hz=20.0,
        resample_n=256,
        psd_konno_ohmachi_b=40.0,
        psd_smoothing=True,
    )
    freqs, power = welch_psd(data, _FS)
    _, db = _maybe_smooth_psd(freqs, power, settings)

    pos = freqs > 0
    f = freqs[pos]
    fcs = settings.center_frequencies_hz()
    # Correct path: smooth LINEAR power, drop unsupported fcs, then dB.
    lin_smoothed = np.asarray(
        ko.konno_and_ohmachi(f, power[pos].reshape(1, -1), fcs, bandwidth=40.0)
    )[0]
    supported = lin_smoothed > 0.0
    expected_db = power_to_db(lin_smoothed[supported])
    np.testing.assert_allclose(db, expected_db, rtol=1e-9)

    # Wrong path: smoothing dB values directly. It must genuinely differ, so
    # the equality above has teeth (otherwise the two domains would coincide).
    wrong = np.asarray(
        ko.konno_and_ohmachi(f, power_to_db(power[pos]).reshape(1, -1), fcs, bandwidth=40.0)
    )[0][supported]
    assert not np.allclose(db, wrong, rtol=1e-3)


def test_horizontal_method_changes_the_hv_curve() -> None:
    """FEATURE 6: a meaningful param (horizontal combine) changes the result."""
    geo = _accumulator(
        12,
        seed=10,
        settings=HvsrSettings(
            window_length_s=_WL,
            freqmin_hz=0.5,
            freqmax_hz=40.0,
            resample_n=128,
            horizontal_method="geometric_mean",
        ),
    ).compute()
    tot = _accumulator(
        12,
        seed=10,
        settings=HvsrSettings(
            window_length_s=_WL,
            freqmin_hz=0.5,
            freqmax_hz=40.0,
            resample_n=128,
            horizontal_method="total_horizontal_energy",
        ),
    ).compute()
    assert not np.allclose(geo.mean_curve, tot.mean_curve)


def test_measurement_refines_as_windows_accumulate() -> None:
    """With more windows the mean H/V curve stabilises and f0 stays on target.

    The "refines over time" property: comparing the mean curve at N and 2N
    windows, the curves are close (the estimate has converged) and f0 stays
    within tolerance of the injected resonance.
    """
    res_small = _accumulator(8, seed=2).compute()
    res_large = _accumulator(24, seed=2).compute()
    assert abs(res_small.f0_hz - _F0) / _F0 < 0.2
    assert abs(res_large.f0_hz - _F0) / _F0 < 0.15
    # Mean curves close in log space (the curve has stabilised).
    a = np.log(np.maximum(res_small.mean_curve, 1e-9))
    b = np.log(np.maximum(res_large.mean_curve, 1e-9))
    rel = float(np.linalg.norm(a - b) / np.linalg.norm(b))
    assert rel < 0.25, f"mean curve not stabilising (rel={rel:.3f})"


# ----------------------------------------------------------------------
# Manual override layer
# ----------------------------------------------------------------------
def test_manual_override_changes_result() -> None:
    """Excluding an accepted window drops n_valid and changes the mean curve."""
    acc = _accumulator(12, seed=3)
    base = acc.compute()
    accepted = [wid for wid, ok in zip(base.window_ids, base.effective_mask, strict=True) if ok]
    assert accepted, "expected some accepted windows"
    acc.set_window_override(accepted[0], False)
    after = acc.compute()
    assert after.n_windows_valid == base.n_windows_valid - 1
    assert not np.allclose(after.mean_curve, base.mean_curve)
    idx = after.window_ids.index(accepted[0])
    assert not after.effective_mask[idx]
    assert after.manual_override_mask[idx]


def test_manual_override_survives_recompute() -> None:
    """An override keyed on window id survives later windows arriving.

    Override window id 2, then add many more windows (shifting every later
    positional index) and recompute: the override must still apply to the
    SAME window, proving identity-by-id not by-position.
    """
    acc = _accumulator(10, seed=4)
    target = acc.window_ids()[2]
    acc.set_window_override(target, False)
    # Twenty more windows arrive — positional indices all shift.
    rng = np.random.default_rng(40)
    t0 = UTCDateTime("2026-02-01")
    for i in range(20):
        acc.add_window(
            rng.standard_normal(_NPER),
            _resonant(_NPER, _F0, rng),
            _resonant(_NPER, _F0, rng),
            t0 + i * _WL,
            _FS,
        )
    res = acc.compute()
    idx = res.window_ids.index(target)
    assert idx == 2  # insertion order preserved
    assert not res.effective_mask[idx], "override lost across recompute"
    assert res.manual_override_mask[idx]


def test_override_composes_on_top_of_auto_mask() -> None:
    """effective == auto except where the user overrode."""
    acc = _accumulator(12, seed=5)
    base = acc.compute()
    # Force-reject an auto-accepted window and force-accept an auto-rejected
    # one if any exists; otherwise just flip one accepted window.
    auto = base.auto_accept_mask
    ids = base.window_ids
    flip_id = ids[int(np.flatnonzero(auto)[0])]
    acc.set_window_override(flip_id, False)
    res = acc.compute()
    for i, wid in enumerate(res.window_ids):
        if wid == flip_id:
            assert res.effective_mask[i] is np.False_ or not res.effective_mask[i]
        else:
            assert bool(res.effective_mask[i]) == bool(res.auto_accept_mask[i])


def test_override_excluding_everything_is_honest() -> None:
    """Rejecting all windows yields an empty, non-NaN-crashing result."""
    acc = _accumulator(5, seed=6)
    for wid in acc.window_ids():
        acc.set_window_override(wid, False)
    res = acc.compute()
    assert res.n_windows_valid == 0
    assert not res.reliability_passed and not res.clarity_passed
    assert np.isnan(res.f0_hz)


# ----------------------------------------------------------------------
# Same-response honesty layer
# ----------------------------------------------------------------------
_GROUP = {"Z": "XX.STA..HHZ", "N": "XX.STA..HHN", "E": "XX.STA..HHE"}


class _FakeSens:
    def __init__(self, value: float) -> None:
        self.value = value
        self.input_units = "M/S"
        self.output_units = "COUNTS"


class _FakeResp:
    def __init__(self, value: float, n_stages: int = 3) -> None:
        self.instrument_sensitivity = _FakeSens(value)
        self.response_stages = list(range(n_stages))


class _FakeInv:
    def __init__(self, by_nslc: dict[str, _FakeResp | None]) -> None:
        self._by = by_nslc

    def get_response(self, nslc: str, t: object) -> _FakeResp:
        resp = self._by.get(nslc)
        if resp is None:
            raise ValueError("no response")
        return resp


class _FakeRemover:
    def __init__(self, inv: _FakeInv) -> None:
        self._inv = inv

    def response_fingerprint(self, nslc: str, t: object) -> tuple[object, ...] | None:
        try:
            resp = self._inv.get_response(nslc, t)
        except ValueError:
            return None
        s = resp.instrument_sensitivity
        return (round(float(s.value), 6), s.input_units, s.output_units, len(resp.response_stages))


class _FakeProvider:
    def __init__(self, configured: bool, inv: _FakeInv | None) -> None:
        self._configured = configured
        self._inv = inv

    def is_configured(self, device: str) -> bool:
        return self._configured

    def remover_for(self, device: str) -> _FakeRemover | None:
        return _FakeRemover(self._inv) if self._inv is not None else None


def test_same_response_assumed_when_no_metadata() -> None:
    same, detail = responses_identical(None, "dev", _GROUP, UTCDateTime(0))
    assert same is True
    assert "assumed" in detail.lower()


def test_same_response_verified_when_identical() -> None:
    inv = _FakeInv({n: _FakeResp(1.0e9) for n in _GROUP.values()})
    provider = _FakeProvider(True, inv)
    same, detail = responses_identical(provider, "dev", _GROUP, UTCDateTime(0))
    assert same is True
    assert "verified" in detail.lower()


def test_same_response_false_when_responses_differ() -> None:
    by = {n: _FakeResp(1.0e9) for n in _GROUP.values()}
    by["XX.STA..HHE"] = _FakeResp(2.0e9)  # different sensitivity
    provider = _FakeProvider(True, _FakeInv(by))
    same, detail = responses_identical(provider, "dev", _GROUP, UTCDateTime(0))
    assert same is False
    assert "differ" in detail.lower()


def test_same_response_assumed_when_missing_one() -> None:
    by: dict[str, _FakeResp | None] = {n: _FakeResp(1.0e9) for n in _GROUP.values()}
    by["XX.STA..HHE"] = None  # no response for E
    provider = _FakeProvider(True, _FakeInv(by))
    same, detail = responses_identical(provider, "dev", _GROUP, UTCDateTime(0))
    assert same is True
    assert "assumed" in detail.lower()


# ----------------------------------------------------------------------
# Boundary cleanliness
# ----------------------------------------------------------------------
def test_result_is_frozen_and_carries_no_foreign_objects() -> None:
    """HvsrResult is frozen and holds only primitives / ndarrays / UTCDateTime."""
    res = _accumulator(6, seed=7).compute()
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.f0_hz = 1.0  # type: ignore[misc]

    allowed = (int, float, bool, str, np.ndarray, UTCDateTime, tuple, HvsrSettings)
    for f in dataclasses.fields(res):
        value = getattr(res, f.name)
        assert isinstance(value, allowed), f"{f.name} is {type(value)}"
        # Module provenance check: nothing from hvsrpy leaks through.
        assert "hvsrpy" not in type(value).__module__
