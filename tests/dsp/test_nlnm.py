"""Tests for the NLNM / NHNM tables and the log-period interpolation.

The whole point of this module is to surface the canonical Peterson
1993 noise models — so the tests anchor on the ObsPy reference, not
on the module's own internal data. (An earlier draft of ``nlnm.py``
hand-typed values that were off by 30-170 dB at several break points;
the tests passed because they only compared the module's tables
against themselves. The reference-comparison assertions below would
have caught that immediately.)
"""

from __future__ import annotations

import numpy as np
import pytest
from obspy.signal.spectral_estimation import get_nhnm, get_nlnm

from echosmonitor.dsp.nlnm import (
    NHNM_DB,
    NHNM_PERIODS_S,
    NLNM_DB,
    NLNM_PERIODS_S,
    interpolate_to,
)

# Spot-check periods (seconds) the seismology community references most
# often when discussing station noise: ~0.5 s (local-quake high-end),
# 5 s and 10 s (regional), 12 s (marine microseism), 30 s (long
# period), 100 s (very long period).
_SPOT_CHECK_PERIODS = np.array([0.5, 5.0, 10.0, 12.0, 30.0, 100.0], dtype=np.float64)
_SPOT_CHECK_FREQS = 1.0 / _SPOT_CHECK_PERIODS


def test_module_tables_are_a_resorted_view_of_obspy() -> None:
    """The exported tables are the ObsPy reference reversed to
    ascending-period order so ``numpy.interp`` works correctly.
    Sorting on either side must produce identical results
    element-wise."""
    obspy_nl_p, obspy_nl_db = get_nlnm()
    obspy_nh_p, obspy_nh_db = get_nhnm()
    nl_order = np.argsort(obspy_nl_p)
    nh_order = np.argsort(obspy_nh_p)
    np.testing.assert_array_equal(NLNM_PERIODS_S, obspy_nl_p[nl_order])
    np.testing.assert_array_equal(NLNM_DB, obspy_nl_db[nl_order])
    np.testing.assert_array_equal(NHNM_PERIODS_S, obspy_nh_p[nh_order])
    np.testing.assert_array_equal(NHNM_DB, obspy_nh_db[nh_order])


def test_tables_are_monotonic_in_period_after_module_normalisation() -> None:
    """After the module's ascending-period normalisation, np.interp
    can safely treat the period grid as its ``xp`` argument."""
    assert np.all(np.diff(NLNM_PERIODS_S) > 0)
    assert np.all(np.diff(NHNM_PERIODS_S) > 0)


def test_interpolation_matches_obspy_within_1db_at_spot_check_periods() -> None:
    """The PSD widget renders NLNM/NHNM by interpolating at the user's
    frequency grid. The interpolation MUST stay within 1 dB of the
    ObsPy reference values at canonical spot-check periods — a tighter
    tolerance than the 5 dB casual reader would accept, but well
    within the resolution of the canonical Peterson dataset."""
    obspy_nl_p, obspy_nl_db = get_nlnm()
    obspy_nh_p, obspy_nh_db = get_nhnm()

    nlnm_ours, nhnm_ours = interpolate_to(_SPOT_CHECK_FREQS)
    # ObsPy returns periods in DESCENDING order; numpy.interp needs
    # ascending ``xp`` — sort the reference path the same way the
    # module does internally so the cross-check is honest.
    nl_order = np.argsort(obspy_nl_p)
    nh_order = np.argsort(obspy_nh_p)
    nlnm_ref = np.interp(
        np.log10(_SPOT_CHECK_PERIODS),
        np.log10(obspy_nl_p[nl_order]),
        obspy_nl_db[nl_order],
    )
    nhnm_ref = np.interp(
        np.log10(_SPOT_CHECK_PERIODS),
        np.log10(obspy_nh_p[nh_order]),
        obspy_nh_db[nh_order],
    )
    np.testing.assert_allclose(nlnm_ours, nlnm_ref, atol=1.0)
    np.testing.assert_allclose(nhnm_ours, nhnm_ref, atol=1.0)


@pytest.mark.parametrize(
    ("period_s", "nlnm_expected_db_approx"),
    [
        # ObsPy-confirmed values at canonical seismology spot-check
        # periods (sampled via ``get_nlnm()`` and log-period
        # interpolated). Tolerance below absorbs interpolation drift.
        (0.5, -167.5),
        (10.0, -163.7),
        (100.0, -185.1),
    ],
)
def test_nlnm_known_period_values_match_reference(
    period_s: float, nlnm_expected_db_approx: float
) -> None:
    nlnm, _ = interpolate_to(np.array([1.0 / period_s], dtype=np.float64))
    assert abs(float(nlnm[0]) - nlnm_expected_db_approx) < 2.0, (
        f"NLNM at P={period_s} s: got {float(nlnm[0]):.1f} dB, "
        f"expected ~{nlnm_expected_db_approx} dB"
    )


def test_interpolation_extrapolates_flat_outside_table() -> None:
    """``np.interp`` clamps to endpoints — out-of-table queries yield
    the boundary dB rather than NaN."""
    very_low = np.array([1e-9], dtype=np.float64)  # period ~10^9 s
    very_high = np.array([1e9], dtype=np.float64)  # period ~10^-9 s
    nl_low, _ = interpolate_to(very_low)
    nl_high, _ = interpolate_to(very_high)
    assert float(nl_low[0]) == pytest.approx(float(NLNM_DB[-1]))
    assert float(nl_high[0]) == pytest.approx(float(NLNM_DB[0]))


def test_interpolation_zero_frequency_is_safe() -> None:
    nlnm, nhnm = interpolate_to(np.array([0.0], dtype=np.float64))
    assert np.all(np.isfinite(nlnm))
    assert np.all(np.isfinite(nhnm))


def test_interpolation_returns_float64_arrays_with_input_shape() -> None:
    freqs = np.linspace(0.01, 25.0, 257, dtype=np.float64)
    nlnm, nhnm = interpolate_to(freqs)
    assert nlnm.dtype == np.float64
    assert nhnm.dtype == np.float64
    assert nlnm.shape == freqs.shape
    assert nhnm.shape == freqs.shape


def test_module_tables_are_immutable() -> None:
    with pytest.raises(ValueError):
        NLNM_DB[0] = 0.0
    with pytest.raises(ValueError):
        NHNM_DB[0] = 0.0
