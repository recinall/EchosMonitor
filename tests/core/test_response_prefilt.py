"""Instrument-aware deconvolution pre_filt (H5 fix).

The instrument-response deconvolution used a hard-coded broadband low corner
(0.005/0.01 Hz). For a 4.5 Hz geophone that sits ~2.7 decades below the
sensor corner, so ``remove_response`` inverted the geophone's omega**2
roll-off below f0 and amplified sub-corner noise into a dominant spurious
low-frequency lobe. The fix derives the LOW pre_filt corners from the
instrument's own corner frequency (lowest PAZ pole / 2π), keeping the
fs-based anti-alias high corners, with an optional per-device override.

These tests assert the OBSERVABLE the user expects (rule 10): for a
velocimeter the VEL output is proportional to counts across the passband and
carries NO spurious sub-corner LF lobe — the property that FAILS with the old
broadband pre_filt and PASSES with the derived one.
"""

from __future__ import annotations

import numpy as np
import obspy
import pytest
from obspy.core.inventory import Channel, Inventory, Network, Site, Station
from obspy.core.inventory.response import Response

from echosmonitor.config.schema import ResponseMetadataConfig
from echosmonitor.core.response import (
    ResponseRemover,
    default_pre_filt,
    instrument_pre_filt,
)

_FS = 200.0
_F0 = 4.5  # geophone corner (EG-4.5-II)
_T0 = obspy.UTCDateTime("2026-06-05T20:45:00")


def _geophone_response(f0: float = _F0) -> Response:
    """A velocimeter velocity response: 2 zeros at origin, conjugate poles at f0."""
    w0 = 2.0 * np.pi * f0
    h = 0.6  # damping
    poles = [
        complex(-h * w0, w0 * np.sqrt(1.0 - h * h)),
        complex(-h * w0, -w0 * np.sqrt(1.0 - h * h)),
    ]
    resp = Response.from_paz(
        zeros=[0j, 0j],
        poles=poles,
        stage_gain=1.0,
        stage_gain_frequency=5.0,
        input_units="M/S",
        output_units="COUNTS",
        normalization_frequency=5.0,
        pz_transfer_function_type="LAPLACE (RADIANS/SECOND)",
    )
    resp.instrument_sensitivity.value = 4.9e9
    resp.instrument_sensitivity.frequency = 5.0
    resp.instrument_sensitivity.input_units = "M/S"
    resp.instrument_sensitivity.output_units = "COUNTS"
    return resp


def _geophone_inventory(f0: float = _F0) -> Inventory:
    cha = Channel("HHZ", "00", 0.0, 0.0, 0.0, 0.0, sample_rate=_FS, response=_geophone_response(f0))
    sta = Station("ECHOS", 0.0, 0.0, 0.0, site=Site("test"), channels=[cha])
    return Inventory(networks=[Network("XX", stations=[sta])])


def _counts_trace(seconds: float = 60.0) -> obspy.Trace:
    """Counts with BOTH a sub-corner component and an above-f0 wavelet.

    Mimics what a geophone records plus low-frequency noise: a 0.3 Hz
    sinusoid (below the 4.5 Hz corner) and an 8 Hz damped wavelet (in the
    passband). Deconvolving amplifies the sub-corner part unless the
    pre_filt excludes it.
    """
    n = int(seconds * _FS)
    t = np.arange(n) / _FS
    rng = np.random.default_rng(7)
    sub_corner = 300.0 * np.sin(2.0 * np.pi * 0.3 * t)
    rel = t - seconds / 2.0
    ev = (rel >= 0.0) & (rel < 2.0)
    wavelet = np.zeros(n)
    wavelet[ev] = 300.0 * np.sin(2.0 * np.pi * 8.0 * rel[ev]) * np.exp(-rel[ev] * 1.5)
    y = sub_corner + wavelet + rng.normal(0.0, 5.0, n)
    return obspy.Trace(
        data=y.astype(np.float64),
        header={
            "network": "XX",
            "station": "ECHOS",
            "location": "00",
            "channel": "HHZ",
            "sampling_rate": _FS,
            "starttime": _T0,
        },
    )


def _band_energy(x: np.ndarray, lo: float, hi: float) -> float:
    spec = np.fft.rfft(x * np.hanning(len(x)))
    freqs = np.fft.rfftfreq(len(x), 1.0 / _FS)
    mask = (freqs >= lo) & (freqs < hi)
    return float(np.sqrt(np.sum(np.abs(spec[mask]) ** 2)))


def _lf_hf_ratio(x: np.ndarray) -> float:
    return _band_energy(x, 0.05, 1.0) / _band_energy(x, 4.5, 40.0)


# ----------------------------------------------------------------------
# 1. Derivation: low corners anchored to the instrument corner frequency
# ----------------------------------------------------------------------
def test_instrument_pre_filt_anchors_low_corners_to_corner_freq() -> None:
    pf = instrument_pre_filt(_geophone_response(_F0), _FS)
    assert pf is not None
    low_stop, low_pass, high_pass, high_stop = pf
    # Low corners near f0, NOT the old broadband 0.005/0.01 Hz.
    assert low_pass == pytest.approx(_F0, rel=0.02)
    assert low_stop == pytest.approx(_F0 / 2.0, rel=0.02)
    assert low_pass > 1.0  # decisively above the broadband default
    # High corners keep the fs-based anti-alias values.
    assert high_pass == pytest.approx(0.45 * _FS)
    assert high_stop == pytest.approx(0.5 * _FS)


def test_instrument_pre_filt_none_without_paz_pole() -> None:
    # A response with no PAZ poles (sensitivity-only) -> None -> caller falls back.
    resp = Response()
    assert instrument_pre_filt(resp, _FS) is None


def test_instrument_pre_filt_none_when_f0_above_antialias_band() -> None:
    # A 200 Hz "corner" against fs=200 (high_pass=90) collapses the band -> None.
    assert instrument_pre_filt(_geophone_response(f0=200.0), _FS) is None


# ----------------------------------------------------------------------
# 2. THE observable: VEL ∝ counts in passband, no spurious LF lobe;
#    and it FAILS with the old broadband pre_filt.
# ----------------------------------------------------------------------
def test_derived_prefilt_kills_subcorner_lobe_but_broadband_amplifies_it() -> None:
    inv = _geophone_inventory()
    st = obspy.Stream([_counts_trace()])
    counts = st[0].data.copy()
    counts_ratio = _lf_hf_ratio(counts)

    # NEW default path: no explicit pre_filt -> instrument-aware derived.
    vel_new = ResponseRemover(inv).to_physical(st.copy(), "VEL")[0].data
    # OLD behaviour: the hard-coded broadband pre_filt, passed explicitly.
    old_pf = (0.005, 0.01, 0.45 * _FS, 0.5 * _FS)
    vel_old = ResponseRemover(inv).to_physical(st.copy(), "VEL", pre_filt=old_pf)[0].data

    new_ratio = _lf_hf_ratio(vel_new)
    old_ratio = _lf_hf_ratio(vel_old)

    # FAIL-ON-OLD: the broadband pre_filt amplifies sub-corner energy into a
    # dominant LF lobe far above what counts carry.
    assert old_ratio > 5.0 * counts_ratio
    # PASS-WITH-FIX: the derived pre_filt keeps LF/HF comparable to counts.
    assert new_ratio < 2.0 * counts_ratio + 1e-6
    # And the fix is decisively better than the old behaviour.
    assert new_ratio < old_ratio / 5.0


def test_vel_proportional_to_counts_above_corner() -> None:
    inv = _geophone_inventory()
    st = obspy.Stream([_counts_trace()])
    counts = st[0].data.copy()
    vel_new = ResponseRemover(inv).to_physical(st.copy(), "VEL")[0].data

    # "Velocity looks like rescaled counts above f0" is a flat-SCALING
    # (magnitude) property — phase near the corner makes time-domain
    # correlation pessimistic, so assert it in the frequency domain: the
    # |VEL|/|counts| ratio is ~constant across the genuinely flat band
    # (well above the corner — ~10 Hz up for this damped geophone).
    cspec = np.abs(np.fft.rfft(counts * np.hanning(len(counts))))
    vspec = np.abs(np.fft.rfft(vel_new * np.hanning(len(vel_new))))
    freqs = np.fft.rfftfreq(len(counts), 1.0 / _FS)
    flat = (freqs >= 10.0) & (freqs <= 40.0)
    ratio = vspec[flat] / (cspec[flat] + 1e-30)
    # Use a robust spread: the ratio must be near-constant across the band.
    med = float(np.median(ratio))
    spread = float(np.percentile(ratio, 90) / np.percentile(ratio, 10))
    assert med > 0.0
    assert spread < 1.3  # < 30% variation -> proportional (flat scaling)


# ----------------------------------------------------------------------
# 3. Per-device override wins over the derived default.
# ----------------------------------------------------------------------
def test_per_device_override_overrides_derived() -> None:
    inv = _geophone_inventory()
    st = obspy.Stream([_counts_trace()])
    broadband = (0.005, 0.01, 0.45 * _FS, 0.5 * _FS)

    derived_vel = ResponseRemover(inv).to_physical(st.copy(), "VEL")[0].data
    override_vel = (
        ResponseRemover(inv, pre_filt_override=broadband).to_physical(st.copy(), "VEL")[0].data
    )

    # With the broadband override the sub-corner lobe returns (override used,
    # derived ignored) -> much larger LF/HF than the derived default.
    assert _lf_hf_ratio(override_vel) > 5.0 * _lf_hf_ratio(derived_vel)


def test_explicit_arg_beats_override() -> None:
    inv = _geophone_inventory()
    st = obspy.Stream([_counts_trace()])
    broadband = (0.005, 0.01, 0.45 * _FS, 0.5 * _FS)
    # Override = broadband, but explicit arg = derived -> explicit should win.
    derived = instrument_pre_filt(_geophone_response(), _FS)
    assert derived is not None
    rr = ResponseRemover(inv, pre_filt_override=broadband)
    vel_explicit = rr.to_physical(st.copy(), "VEL", pre_filt=derived)[0].data
    vel_derived = ResponseRemover(inv).to_physical(st.copy(), "VEL")[0].data
    # Explicit-derived must match the plain derived path, not the override.
    assert _lf_hf_ratio(vel_explicit) == pytest.approx(_lf_hf_ratio(vel_derived), rel=1e-6)


# ----------------------------------------------------------------------
# 4. The omega-ladder still holds, anchored to an in-band velocity.
# ----------------------------------------------------------------------
def test_omega_ladder_acc_disp_relative_to_vel() -> None:
    inv = _geophone_inventory()
    rr = ResponseRemover(inv)
    st = obspy.Stream([_counts_trace()])
    acc = rr.to_physical(st.copy(), "ACC")[0].data
    vel = rr.to_physical(st.copy(), "VEL")[0].data
    disp = rr.to_physical(st.copy(), "DISP")[0].data
    # Differentiation (ACC) emphasises HF vs VEL; integration (DISP) emphasises LF.
    assert _lf_hf_ratio(acc) < _lf_hf_ratio(vel) < _lf_hf_ratio(disp)


# ----------------------------------------------------------------------
# 5. Config: the override field validates 4 strictly-increasing corners.
# ----------------------------------------------------------------------
def test_config_pre_filt_override_accepts_ordered_corners() -> None:
    cfg = ResponseMetadataConfig(pre_filt=(0.5, 1.0, 90.0, 100.0))
    assert cfg.pre_filt == (0.5, 1.0, 90.0, 100.0)


def test_config_pre_filt_override_rejects_unordered() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        ResponseMetadataConfig(pre_filt=(1.0, 0.5, 90.0, 100.0))


def test_config_pre_filt_default_is_none() -> None:
    assert ResponseMetadataConfig().pre_filt is None
    # The bundled default low corners remain the broadband fallback values.
    assert default_pre_filt(_FS)[:2] == (0.005, 0.01)


def test_provider_passes_device_override_into_remover(tmp_path) -> None:
    """The per-device config override reaches the ResponseRemover (plumbing)."""
    from pathlib import Path

    from echosmonitor.config.schema import DeviceConfig, StreamSelectorConfig
    from echosmonitor.core.response import ResponseProvider

    xml = tmp_path / "geo.xml"
    _geophone_inventory().write(str(xml), format="STATIONXML")
    override = (0.005, 0.01, 90.0, 100.0)
    dev = DeviceConfig(
        name="geo",
        host="127.0.0.1",
        selectors=[StreamSelectorConfig(network="XX", station="ECHOS")],
        response_metadata={"path": str(xml), "format": "stationxml", "pre_filt": override},
    )
    provider = ResponseProvider([dev], config_dir=Path(tmp_path))
    remover = provider.remover_for("geo")
    assert remover is not None
    assert remover._pre_filt_override == override
