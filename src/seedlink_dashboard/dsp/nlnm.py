"""Peterson (1993) New Low / High Noise Models, canonical values.

The reference tables come from
:func:`obspy.signal.spectral_estimation.get_nlnm` /
:func:`obspy.signal.spectral_estimation.get_nhnm`. ObsPy is the
canonical distribution of these curves used across the seismology
community and is already a locked project dependency (CLAUDE.md tech
stack). We import once at module load time, cache the period/dB
arrays as immutable numpy views, and provide a tiny
log-period-linear interpolation helper so the PSD widget can render
the overlay at any user-chosen frequency grid.

Both models are in **dB rel. (m/s^2)^2 / Hz** — acceleration PSD. They
are only physically meaningful when overlaid on a station's PSD that
has been corrected to acceleration units (instrument response
removed). Plotting them against raw-count PSDs is a unit error; the
caller is responsible for surfacing that constraint to the user.

We refuse to transcribe the Peterson break-point tables by hand: an
earlier draft hand-typed values that turned out to be off by 30 to
170 dB at several break points (PR review caught it). Sourcing from
ObsPy makes the values reviewable against a test (see
``tests/dsp/test_nlnm.py``) and immune to transcription drift.
"""

from __future__ import annotations

import numpy as np
from obspy.signal.spectral_estimation import get_nhnm, get_nlnm

_NLNM_PERIODS, _NLNM_DB = get_nlnm()
_NHNM_PERIODS, _NHNM_DB = get_nhnm()


def _ascending(periods: np.ndarray, levels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(periods_asc, levels_asc)``. ObsPy currently returns
    NLNM/NHNM in DESCENDING period order; ``numpy.interp`` requires
    its ``xp`` argument to be ascending or the result is silently
    wrong, so we sort eagerly at module load."""
    p = np.asarray(periods, dtype=np.float64)
    d = np.asarray(levels, dtype=np.float64)
    if p.size > 1 and p[0] > p[-1]:
        return p[::-1].copy(), d[::-1].copy()
    return p.copy(), d.copy()


# Public, read-only views. ``setflags(write=False)`` prevents a downstream
# consumer from accidentally mutating the canonical reference in place.
NLNM_PERIODS_S, NLNM_DB = _ascending(_NLNM_PERIODS, _NLNM_DB)
NHNM_PERIODS_S, NHNM_DB = _ascending(_NHNM_PERIODS, _NHNM_DB)
for _arr in (NLNM_PERIODS_S, NLNM_DB, NHNM_PERIODS_S, NHNM_DB):
    _arr.setflags(write=False)

# Floor used when consumers ask for a frequency that is exactly zero —
# log10(0) is undefined and the model is only meaningful for finite
# positive periods.
_MIN_HZ = 1e-12


def _interp_loglog(
    freqs_hz: np.ndarray,
    periods_s: np.ndarray,
    levels_db: np.ndarray,
) -> np.ndarray:
    """Linear interpolation in ``log10(period)`` vs dB space.

    Peterson 1993 expresses both models as piecewise-linear in
    ``log10(period)``; ObsPy's helpers sample that piecewise model at
    a dense grid. Re-interpolating in the same coordinate system
    preserves the model.
    """
    safe_freqs = np.maximum(freqs_hz.astype(np.float64), _MIN_HZ)
    log_query = np.log10(1.0 / safe_freqs)  # log10(period)
    log_table_p = np.log10(periods_s)
    out: np.ndarray = np.interp(log_query, log_table_p, levels_db).astype(np.float64)
    return out


def interpolate_to(freqs_hz: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(nlnm_db, nhnm_db)`` interpolated at ``freqs_hz``.

    Out-of-table frequencies extrapolate flat (the table's first /
    last dB level), never NaN. This matches ObsPy's behaviour.

    Args:
        freqs_hz: 1-D array of frequencies in Hz.

    Returns:
        Two 1-D float64 arrays of the same length as ``freqs_hz``,
        in dB rel ``(m/s^2)^2 / Hz``.
    """
    nlnm = _interp_loglog(freqs_hz, NLNM_PERIODS_S, NLNM_DB)
    nhnm = _interp_loglog(freqs_hz, NHNM_PERIODS_S, NHNM_DB)
    return nlnm, nhnm
