"""HVSR (Nakamura horizontal-to-vertical spectral ratio) core — pure, no Qt.

This module is the **boundary** between the third-party ``hvsrpy`` library
(Vantassel et al.) and the rest of the application. ``hvsrpy`` owns the H/V
physics: the per-window horizontal-to-vertical ratio, Konno-Ohmachi
smoothing, the horizontal-component combination, the Cox-2020
frequency-domain window rejection, and the SESAME (2004) reliability +
clarity criteria. We never re-implement any of that.

What we own, and what lives here:

* **Accumulation** — :class:`HvsrAccumulator` holds the raw 3-component
  windows (live or archive) as plain numpy arrays and constructs the
  ``hvsrpy`` objects only at :meth:`HvsrAccumulator.compute` time, so the
  design is robust to whichever in-memory constructor the installed
  ``hvsrpy`` exposes (confirmed by ``scripts/check_hvsrpy.py``).
* **Incremental re-compute** — every accepted window contributes to a
  mean/median curve that stabilises and an f0 dispersion that shrinks as N
  grows (the "refines over time" property).
* **Manual override** — a layer ON TOP of ``hvsrpy``'s automatic rejection,
  keyed on a stable per-window id so a user's include/exclude survives every
  subsequent re-compute (see :meth:`HvsrAccumulator.compute` for the
  composition rule).

``hvsrpy`` objects MUST NOT leak past this module: :meth:`compute` returns a
frozen :class:`HvsrResult` dataclass carrying only primitives / numpy arrays
/ :class:`UTCDateTime`, exactly like the AI subsystem's ``AIAnnotation``
boundary. The GUI layer never imports ``hvsrpy``.

Counts vs physical units: H/V is a *ratio*, so when the three components
share an identical instrument response (a single 3C sensor — the common
case) the response cancels and raw counts are valid. We never assume this
silently: :func:`responses_identical` resolves the device's response
metadata and :class:`HvsrResult` carries an explicit ``same_response`` flag
plus a human-readable ``same_response_detail`` that the UI and report
surface verbatim.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal

import numpy as np
import structlog
from obspy.core.utcdatetime import UTCDateTime
from pydantic import Field, model_validator

from seedlink_dashboard.config.schema import _Base
from seedlink_dashboard.core.exceptions import HvsrError
from seedlink_dashboard.dsp.psd import power_to_db, welch_psd

if TYPE_CHECKING:
    from seedlink_dashboard.core.response import ResponseProvider
    from seedlink_dashboard.storage.archive_reader import ArchiveReader

_log = structlog.get_logger(__name__)

HorizontalMethod = Literal[
    "geometric_mean",
    "squared_average",
    "total_horizontal_energy",
    "maximum_horizontal_value",
]
RejectionMethod = Literal["frequency_domain", "none"]
Provenance = Literal["live", "archive"]

# Fractional tolerance on a window's sample rate matching the first window's.
# Live windows come from one device, so fs is identical; archive reads may
# differ by floating-point noise. Beyond this we refuse to mix (honest: a
# real fs change means a different instrument configuration).
_FS_TOL = 1e-3

# Floor for log of an H/V amplitude before aggregating (amplitudes are
# strictly positive ratios, but a degenerate window could yield ~0).
_AMP_FLOOR = 1e-12


class HvsrSettings(_Base):
    """Typed, frozen, serialisable HVSR processing settings.

    Defaults follow SESAME guidance: windows long enough to resolve a few-Hz
    f0 (window length ↔ minimum resolvable frequency f0 > 10/lw), the
    Konno-Ohmachi smoothing ``hvsrpy`` defaults to, and a broadband analysis
    range. Frozen so it can be snapshotted into :class:`HvsrResult` and
    serialised for the report without aliasing.
    """

    window_length_s: Annotated[float, Field(gt=0.0)] = 60.0
    konno_ohmachi_b: Annotated[float, Field(gt=0.0)] = 40.0
    freqmin_hz: Annotated[float, Field(gt=0.0)] = 0.2
    freqmax_hz: Annotated[float, Field(gt=0.0)] = 20.0
    horizontal_method: HorizontalMethod = "geometric_mean"
    rejection_method: RejectionMethod = "frequency_domain"
    # Cox-2020 frequency-domain rejection threshold: a window is rejected when
    # it lies more than ``rejection_n`` lognormal standard deviations from the
    # median curve. Larger = more permissive (fewer rejections).
    rejection_n: Annotated[float, Field(ge=1.0, le=5.0)] = 2.0
    # Per-window detrend before the FFT (hvsrpy preprocess): linear or mean.
    detrend: Literal["linear", "constant"] = "linear"
    # Number of log-spaced Konno-Ohmachi centre frequencies across the band
    # (the output frequency resolution).
    resample_n: Annotated[int, Field(ge=64, le=4096)] = 512
    # Konno-Ohmachi smoothing of the displayed/exported 3-channel PSD. The PSD
    # is a diagnostic (NOT the H/V science), so it has its own b; default 40
    # matches the HVSR. When off the raw Welch PSD is shown.
    psd_smoothing: bool = True
    psd_konno_ohmachi_b: Annotated[float, Field(gt=0.0)] = 40.0

    @model_validator(mode="after")
    def _band_ordered(self) -> HvsrSettings:
        if self.freqmin_hz >= self.freqmax_hz:
            raise ValueError("hvsr.freqmin_hz must be < freqmax_hz")
        return self

    def min_reliable_frequency_hz(self) -> float:
        """Lowest f0 SESAME considers reliable for this window length.

        SESAME reliability criterion (i) requires f0 > 10 / window length, so
        for a chosen window length this is the lowest frequency the
        measurement can reliably resolve. Surfaced live in the UI so the user
        knows the valid band for their window-length choice.
        """
        return 10.0 / self.window_length_s

    def center_frequencies_hz(self) -> np.ndarray:
        """Log-spaced Konno-Ohmachi centre frequencies across the band."""
        return np.geomspace(self.freqmin_hz, self.freqmax_hz, self.resample_n)


@dataclass(frozen=True, slots=True)
class SesameCriterion:
    """One SESAME (2004) sub-criterion outcome.

    ``name`` is the canonical criterion label, ``passed`` its pass/fail, and
    ``detail`` a short human string (with the relevant numbers) for tooltips
    and the report.
    """

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class HvsrResult:
    """GUI-facing HVSR result — no ``hvsrpy`` or Qt objects (the boundary).

    Masks are all length ``n_windows_total`` and indexed in window-insertion
    order (index ``i`` ↔ ``window_ids[i]``):

    * ``auto_accept_mask`` — ``hvsrpy``'s automatic accept (True = accepted)
      from the Cox-2020 frequency-domain rejection (all-True when rejection
      is disabled).
    * ``manual_override_mask`` — True where the USER explicitly overrode this
      window (regardless of the accept/reject value); lets the UI mark
      user-touched windows distinctly.
    * ``effective_mask`` — what actually fed the statistics (True = accepted):
      ``auto`` except where the user overrode, then the user's choice. A
      window ``i`` is shown accepted iff ``effective_mask[i]``; its auto
      verdict is ``auto_accept_mask[i]``; it is user-touched iff
      ``manual_override_mask[i]``.
    """

    # Frequency axis (Hz), monotonic, length F.
    frequency: np.ndarray
    # Per-window H/V curves, shape (n_windows_total, F) — ALL windows,
    # including rejected ones (the UI draws them faint behind the mean).
    window_curves: np.ndarray
    mean_curve: np.ndarray  # (F,) lognormal (geometric) mean over accepted
    median_curve: np.ndarray  # (F,) sample median over accepted
    lognormal_sigma: np.ndarray  # (F,) std of ln(amplitude) over accepted
    f0_hz: float  # peak of the mean curve (the fundamental frequency)
    f0_sigma: float  # std of the per-window peak frequencies (accepted)
    a0: float  # H/V amplitude at f0
    # Window identity + masks (see class docstring).
    window_ids: tuple[int, ...]
    auto_accept_mask: np.ndarray
    manual_override_mask: np.ndarray
    effective_mask: np.ndarray
    # SESAME (2004): 3 reliability + 6 clarity sub-criteria.
    reliability: tuple[SesameCriterion, ...]
    clarity: tuple[SesameCriterion, ...]
    reliability_passed: bool
    clarity_passed: bool
    # Welch PSD of each of the 3 channels over the accepted windows:
    # (freqs_hz, db) pairs in dB rel. counts²/Hz.
    psd_z: tuple[np.ndarray, np.ndarray]
    psd_n: tuple[np.ndarray, np.ndarray]
    psd_e: tuple[np.ndarray, np.ndarray]
    # Counts-vs-physical honesty (never silently assumed).
    same_response: bool
    same_response_detail: str
    # Provenance + reproducibility.
    provenance: Provenance
    settings: HvsrSettings
    n_windows_total: int
    n_windows_valid: int
    device: str
    station_key: str
    t_start: UTCDateTime
    t_end: UTCDateTime


@dataclass(slots=True)
class _Window:
    """One accumulated 3-component window (raw counts)."""

    window_id: int
    z: np.ndarray
    n: np.ndarray
    e: np.ndarray
    t_start: UTCDateTime
    fs: float

    @property
    def t_end(self) -> UTCDateTime:
        return self.t_start + (int(self.z.shape[0]) - 1) / self.fs


# Canonical SESAME (2004) criterion labels — the verdicts come from hvsrpy's
# sesame module; these are the human names attached to each entry.
_RELIABILITY_NAMES = (
    "i) f0 > 10 / window length",
    "ii) nc(f0) = lw · n_windows · f0 > 200",
    "iii) sigma_A(f) low around f0",
)
_CLARITY_NAMES = (
    "i) a trough below A0/2 in [f0/4, f0]",
    "ii) a trough below A0/2 in [f0, 4·f0]",
    "iii) A0 > 2",
    "iv) +/-sigma peak within +/-5% of f0",
    "v) f0 stability: sigma_f < epsilon(f0)",
    "vi) peak amplitude stability: sigma_A(f0) < theta(f0)",
)


class HvsrAccumulator:
    """Accumulates 3C windows and computes an :class:`HvsrResult` over them.

    Window identity is a monotonic ``window_id`` assigned at
    :meth:`add_window` and never reused. Manual overrides are keyed on that
    id, NOT on a positional index, so a user's include/exclude on window #12
    is untouched when window #51 later arrives — positional indices shift as
    windows accumulate, ids do not.
    """

    def __init__(
        self,
        settings: HvsrSettings,
        *,
        same_response: bool,
        same_response_detail: str,
        device: str,
        station_key: str,
        provenance: Provenance,
    ) -> None:
        self._settings = settings
        self._same_response = same_response
        self._same_response_detail = same_response_detail
        self._device = device
        self._station_key = station_key
        self._provenance: Provenance = provenance
        self._windows: list[_Window] = []
        self._overrides: dict[int, bool] = {}
        self._next_id = 0

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------
    @property
    def settings(self) -> HvsrSettings:
        return self._settings

    @property
    def n_windows(self) -> int:
        return len(self._windows)

    def window_ids(self) -> tuple[int, ...]:
        return tuple(w.window_id for w in self._windows)

    def add_window(
        self,
        z: np.ndarray,
        n: np.ndarray,
        e: np.ndarray,
        t_start: UTCDateTime,
        fs: float,
    ) -> int:
        """Append one 3C window; return its stable ``window_id``.

        Raises:
            HvsrError: if the three components differ in length, ``fs`` is
                non-positive, or ``fs`` is inconsistent with earlier windows
                (we never silently pad, truncate, or resample — a real change
                means a different instrument configuration).
        """
        za = np.asarray(z, dtype=np.float64)
        na = np.asarray(n, dtype=np.float64)
        ea = np.asarray(e, dtype=np.float64)
        if not (za.shape == na.shape == ea.shape):
            raise HvsrError(f"3C window length mismatch: Z={za.shape} N={na.shape} E={ea.shape}")
        if za.ndim != 1 or za.size == 0:
            raise HvsrError(f"window components must be non-empty 1-D, got {za.shape}")
        if fs <= 0.0:
            raise HvsrError(f"window fs must be > 0, got {fs}")
        if self._windows:
            ref = self._windows[0].fs
            if abs(fs - ref) / ref > _FS_TOL:
                raise HvsrError(
                    f"window fs {fs} inconsistent with accumulated {ref} (>{_FS_TOL:.0%})"
                )
        window_id = self._next_id
        self._next_id += 1
        self._windows.append(_Window(window_id, za, na, ea, t_start, float(fs)))
        return window_id

    def set_window_override(self, window_id: int, accepted: bool) -> None:
        """Record a manual accept/reject keyed on the stable ``window_id``.

        Survives every subsequent :meth:`compute` and :meth:`add_window`.
        """
        self._overrides[window_id] = bool(accepted)

    def clear_override(self, window_id: int) -> None:
        """Drop a manual override; the window reverts to its auto verdict."""
        self._overrides.pop(window_id, None)

    def snapshot(self) -> HvsrAccumulator:
        """A shallow copy safe to hand to a worker thread.

        Shares the immutable per-window arrays (never mutated after append)
        but copies the window list and override dict, so the live accumulator
        can keep growing on the GUI thread while the worker computes.
        """
        clone = HvsrAccumulator(
            self._settings,
            same_response=self._same_response,
            same_response_detail=self._same_response_detail,
            device=self._device,
            station_key=self._station_key,
            provenance=self._provenance,
        )
        clone._windows = list(self._windows)
        clone._overrides = dict(self._overrides)
        clone._next_id = self._next_id
        return clone

    # ------------------------------------------------------------------
    # Compute (the only place hvsrpy is touched)
    # ------------------------------------------------------------------
    def compute(self) -> HvsrResult:
        """Run the hvsrpy workflow over ALL accumulated windows.

        Composition rule (manual override on top of auto rejection):

        1. ``hvsrpy`` yields the per-window H/V curves and the auto accept
           mask (Cox-2020 frequency-domain rejection).
        2. We map the auto mask back to window-id order and overlay the user's
           overrides: ``effective[i] = override[id_i] if id_i in overrides
           else auto[i]``.
        3. We re-aggregate the mean/median/sigma and f0 ourselves from the
           per-window curves under ``effective`` — so the override decides
           which curves enter the statistics. SESAME is evaluated on the
           effective mean curve.

        Raises:
            HvsrError: no windows accumulated, or the hvsrpy workflow failed.
        """
        if not self._windows:
            raise HvsrError("no windows accumulated")
        t0 = time.monotonic()
        _log.info("hvsr_compute_start", n_windows=len(self._windows), device=self._device)
        try:
            frequency, amplitude, auto_mask = self._run_hvsrpy()
        except HvsrError:
            raise
        except Exception as exc:  # hvsrpy raises a broad family
            _log.error("hvsr_compute_failed", error=str(exc))
            raise HvsrError(f"hvsrpy workflow failed: {exc}") from exc

        ids = self.window_ids()
        effective, overridden = self._effective_mask(ids, auto_mask)
        result = self._build_result(frequency, amplitude, auto_mask, overridden, effective)
        elapsed = (time.monotonic() - t0) * 1000.0
        _log.info(
            "hvsr_compute_done",
            n_windows=len(self._windows),
            n_valid=int(np.count_nonzero(effective)),
            f0_hz=round(result.f0_hz, 4),
            elapsed_ms=round(elapsed, 1),
        )
        return result

    def _run_hvsrpy(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Build hvsrpy objects, process, and return (frequency, amplitude, auto_mask).

        Each accumulated window becomes its own one-window
        ``SeismicRecording3C`` (length == window length), so ``preprocess``
        yields exactly one hvsrpy window per accumulated window and the
        per-window ``amplitude`` rows line up 1:1 with our window-id order.
        """
        import hvsrpy
        import hvsrpy.sesame as _sesame  # noqa: F401  (ensures submodule import works)

        s = self._settings
        records = []
        for w in self._windows:
            dt = 1.0 / w.fs
            ns = hvsrpy.TimeSeries(w.n, dt)
            ew = hvsrpy.TimeSeries(w.e, dt)
            vt = hvsrpy.TimeSeries(w.z, dt)
            records.append(hvsrpy.SeismicRecording3C(ns, ew, vt))

        # Each accumulated window IS already one ~window_length_s segment, so
        # we disable hvsrpy's re-windowing (``window_length_in_seconds=None``):
        # one record in → exactly one hvsrpy window out, regardless of the
        # record's exact length. This makes the per-window ``amplitude`` rows
        # line up 1:1 with our window-id order STRUCTURALLY (not by a fragile
        # count guard), and avoids a ValueError when a live window is a hair
        # short of window_length_s (the 0.9 fill gate permits that) or a split
        # when an archive window is longer.
        pre = hvsrpy.HvsrPreProcessingSettings(
            window_length_in_seconds=None,
            detrend=s.detrend,
        )
        proc = hvsrpy.HvsrTraditionalProcessingSettings(
            method_to_combine_horizontals=s.horizontal_method,
            smoothing={
                "operator": "konno_and_ohmachi",
                "bandwidth": s.konno_ohmachi_b,
                "center_frequencies_in_hz": s.center_frequencies_hz(),
            },
        )
        windows = hvsrpy.preprocess(records, pre)
        hvsr = hvsrpy.process(windows, proc)

        frequency = np.asarray(hvsr.frequency, dtype=np.float64)
        amplitude = np.asarray(hvsr.amplitude, dtype=np.float64)
        if amplitude.ndim != 2 or amplitude.shape[0] != len(self._windows):
            raise HvsrError(
                f"hvsrpy returned {amplitude.shape} curves for {len(self._windows)} windows"
            )
        # Auto rejection (Cox 2020) — mutates valid_window_boolean_mask.
        if s.rejection_method == "frequency_domain":
            try:
                # hvsrpy's iterative std rejection divides by zero on
                # low-variance curves; the warnings are benign (we handle the
                # all-rejected outcome below), so silence them here.
                with np.errstate(divide="ignore", invalid="ignore"):
                    hvsrpy.frequency_domain_window_rejection(hvsr, n=s.rejection_n)
            except Exception as exc:
                _log.warning("hvsr_auto_rejection_failed", error=str(exc))
        auto_mask = np.asarray(hvsr.valid_window_boolean_mask, dtype=bool).copy()
        if auto_mask.shape[0] != len(self._windows):
            auto_mask = np.ones(len(self._windows), dtype=bool)
        if not auto_mask.any():
            # Auto-rejection rejected EVERY window — never a useful outcome and
            # a known hvsrpy edge on very low-variance curves (its iterative
            # std rejection collapses). Keep all windows so the user still sees
            # a curve; manual overrides (applied on top) can still exclude any.
            _log.warning("hvsr_auto_rejected_all", n_windows=len(self._windows))
            auto_mask = np.ones(len(self._windows), dtype=bool)
        return frequency, amplitude, auto_mask

    def _effective_mask(
        self, ids: tuple[int, ...], auto_mask: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Overlay manual overrides on the auto mask (by stable window id)."""
        effective = auto_mask.copy()
        overridden = np.zeros(len(ids), dtype=bool)
        for i, wid in enumerate(ids):
            if wid in self._overrides:
                effective[i] = self._overrides[wid]
                overridden[i] = True
        return effective, overridden

    def _build_result(
        self,
        frequency: np.ndarray,
        amplitude: np.ndarray,
        auto_mask: np.ndarray,
        overridden: np.ndarray,
        effective: np.ndarray,
    ) -> HvsrResult:
        valid_idx = np.flatnonzero(effective)
        n_freq = frequency.shape[0]
        if valid_idx.size == 0:
            # All windows excluded (e.g. user rejected everything): produce an
            # honest empty result rather than NaNs from hvsrpy statistics.
            nan = np.full(n_freq, np.nan)
            reliability = tuple(
                SesameCriterion(name, False, "no valid windows") for name in _RELIABILITY_NAMES
            )
            clarity = tuple(
                SesameCriterion(name, False, "no valid windows") for name in _CLARITY_NAMES
            )
            empty = (np.empty(0), np.empty(0))
            return self._result(
                frequency,
                amplitude,
                nan,
                nan,
                nan,
                float("nan"),
                float("nan"),
                float("nan"),
                auto_mask,
                overridden,
                effective,
                reliability,
                clarity,
                False,
                False,
                empty,
                empty,
                empty,
            )

        valid_amp = amplitude[valid_idx]
        log_amp = np.log(np.maximum(valid_amp, _AMP_FLOOR))
        mean_curve = np.exp(np.mean(log_amp, axis=0))
        lognormal_sigma = np.std(log_amp, axis=0)
        median_curve = np.median(valid_amp, axis=0)
        peak_idx = int(np.argmax(mean_curve))
        f0_hz = float(frequency[peak_idx])
        a0 = float(mean_curve[peak_idx])
        per_window_peaks = frequency[np.argmax(valid_amp, axis=1)]
        f0_sigma = float(np.std(per_window_peaks)) if per_window_peaks.size > 1 else 0.0

        reliability, clarity, rel_ok, cla_ok = self._sesame(
            frequency, mean_curve, lognormal_sigma, f0_hz, f0_sigma, int(valid_idx.size)
        )
        psd_z = self._channel_psd(valid_idx, "z")
        psd_n = self._channel_psd(valid_idx, "n")
        psd_e = self._channel_psd(valid_idx, "e")
        return self._result(
            frequency,
            amplitude,
            mean_curve,
            median_curve,
            lognormal_sigma,
            f0_hz,
            f0_sigma,
            a0,
            auto_mask,
            overridden,
            effective,
            reliability,
            clarity,
            rel_ok,
            cla_ok,
            psd_z,
            psd_n,
            psd_e,
        )

    def _sesame(
        self,
        frequency: np.ndarray,
        mean_curve: np.ndarray,
        sigma: np.ndarray,
        f0_hz: float,
        f0_sigma: float,
        passing: int,
    ) -> tuple[tuple[SesameCriterion, ...], tuple[SesameCriterion, ...], bool, bool]:
        """Evaluate SESAME reliability (3) + clarity (6) via hvsrpy.sesame."""
        import hvsrpy.sesame as sesame

        s = self._settings
        search = (s.freqmin_hz, s.freqmax_hz)
        try:
            rel = np.asarray(
                sesame.reliability(
                    s.window_length_s,
                    passing,
                    frequency,
                    mean_curve,
                    sigma,
                    search_range_in_hz=search,
                    verbose=0,
                )
            )
            cla = np.asarray(
                sesame.clarity(
                    frequency,
                    mean_curve,
                    sigma,
                    f0_sigma,
                    search_range_in_hz=search,
                    verbose=0,
                )
            )
        except Exception as exc:
            _log.warning("hvsr_sesame_failed", error=str(exc))
            rel = np.zeros(3)
            cla = np.zeros(6)
        reliability = tuple(
            SesameCriterion(
                _RELIABILITY_NAMES[i],
                bool(rel[i] > 0),
                self._reliability_detail(i, f0_hz, passing),
            )
            for i in range(3)
        )
        clarity = tuple(
            SesameCriterion(_CLARITY_NAMES[i], bool(cla[i] > 0), f"f0={f0_hz:.3f} Hz")
            for i in range(6)
        )
        return reliability, clarity, bool(np.all(rel > 0)), bool(np.all(cla > 0))

    def _reliability_detail(self, i: int, f0_hz: float, passing: int) -> str:
        s = self._settings
        if i == 0:
            return f"f0={f0_hz:.3f} Hz vs 10/lw={10.0 / s.window_length_s:.3f} Hz"
        if i == 1:
            nc = s.window_length_s * passing * f0_hz
            return f"nc={nc:.0f} vs 200 (lw={s.window_length_s:.0f}s, N={passing})"
        return f"f0={f0_hz:.3f} Hz"

    def _channel_psd(
        self, valid_idx: np.ndarray, channel: Literal["z", "n", "e"]
    ) -> tuple[np.ndarray, np.ndarray]:
        """Welch PSD (dB rel. counts²/Hz) over the accepted windows.

        The accepted windows are concatenated before Welch. When rejection
        removes interior windows the concatenation glues temporally-disjoint
        segments, so Welch sees a seam at each join — negligible for this
        diagnostic PSD (Welch's Hann-windowed segmenting smears it, and this
        is a display aid, NOT the H/V science), but worth noting.

        Konno-Ohmachi smoothing (``settings.psd_smoothing``) is applied so the
        displayed/exported PSD is as readable as the H/V curve; the smoothed
        PSD lands on the same log-spaced centre frequencies.
        """
        fs = self._windows[0].fs
        segments = [getattr(self._windows[i], channel) for i in valid_idx]
        if not segments:
            return np.empty(0), np.empty(0)
        data = np.concatenate(segments)
        freqs, power = welch_psd(data, fs)
        return _maybe_smooth_psd(freqs, power, self._settings)

    def raw_channel_psds(self) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """Raw (un-smoothed) Welch PSD of each channel over ALL windows.

        A cheap, hvsrpy-free read for the EARLY PSD display (rule-11 friendly:
        no JIT, runs in a few ms) so the 3-channel PSD appears as soon as one
        window exists — long before the first full HVSR compute. Returns
        ``{}`` until there is at least one window.
        """
        if not self._windows:
            return {}
        fs = self._windows[0].fs
        out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for comp, attr in (("Z", "z"), ("N", "n"), ("E", "e")):
            data = np.concatenate([getattr(w, attr) for w in self._windows])
            freqs, power = welch_psd(data, fs)
            out[comp] = (freqs, power_to_db(power))
        return out

    def _result(
        self,
        frequency: np.ndarray,
        amplitude: np.ndarray,
        mean_curve: np.ndarray,
        median_curve: np.ndarray,
        lognormal_sigma: np.ndarray,
        f0_hz: float,
        f0_sigma: float,
        a0: float,
        auto_mask: np.ndarray,
        overridden: np.ndarray,
        effective: np.ndarray,
        reliability: tuple[SesameCriterion, ...],
        clarity: tuple[SesameCriterion, ...],
        rel_ok: bool,
        cla_ok: bool,
        psd_z: tuple[np.ndarray, np.ndarray],
        psd_n: tuple[np.ndarray, np.ndarray],
        psd_e: tuple[np.ndarray, np.ndarray],
    ) -> HvsrResult:
        return HvsrResult(
            frequency=frequency,
            window_curves=amplitude,
            mean_curve=mean_curve,
            median_curve=median_curve,
            lognormal_sigma=lognormal_sigma,
            f0_hz=f0_hz,
            f0_sigma=f0_sigma,
            a0=a0,
            window_ids=self.window_ids(),
            auto_accept_mask=auto_mask,
            manual_override_mask=overridden,
            effective_mask=effective,
            reliability=reliability,
            clarity=clarity,
            reliability_passed=rel_ok,
            clarity_passed=cla_ok,
            psd_z=psd_z,
            psd_n=psd_n,
            psd_e=psd_e,
            same_response=self._same_response,
            same_response_detail=self._same_response_detail,
            provenance=self._provenance,
            settings=self._settings,
            n_windows_total=len(self._windows),
            n_windows_valid=int(np.count_nonzero(effective)),
            device=self._device,
            station_key=self._station_key,
            t_start=self._windows[0].t_start,
            t_end=self._windows[-1].t_end,
        )


def responses_identical(
    provider: ResponseProvider | None,
    device: str,
    group: dict[str, str],
    t: UTCDateTime,
) -> tuple[bool, str]:
    """Whether the 3 components share an identical instrument response.

    H/V is a ratio, so an identical response on Z/N/E cancels and counts are
    valid. This is the honesty layer (never assume silently): it returns a
    ``(same_response, detail)`` pair the UI and report surface verbatim.

    Three cases, all explicit:

    * No response metadata configured → ``(True, "assumed …")``: counts are
      valid IFF the three components are identical sensors (the typical
      single 3C station). We cannot verify, so we say so.
    * Metadata present and the three responses match → ``(True, "verified …")``.
    * Metadata present and they differ → ``(False, "… differ …")``: H/V in
      counts is not physically correct; response removal (M11) would be needed.
    """
    if provider is None or not provider.is_configured(device):
        return (
            True,
            "Same-response assumed (no response metadata to verify): H/V in counts "
            "is valid only if Z/N/E are identical sensors — typical for a single "
            "3-component station.",
        )
    try:
        remover = provider.remover_for(device)
    except Exception as exc:
        return (
            True,
            f"Same-response assumed (response metadata could not be read: {exc}).",
        )
    if remover is None:
        return (True, "Same-response assumed (no response metadata configured).")

    fingerprints: dict[str, tuple[object, ...] | None] = {}
    for comp, nslc in group.items():
        fingerprints[comp] = remover.response_fingerprint(nslc, t)
    present = {c: fp for c, fp in fingerprints.items() if fp is not None}
    if len(present) < len(group):
        missing = sorted(set(group) - set(present))
        return (
            True,
            f"Same-response assumed (no response found for {', '.join(missing)} "
            "at this time): valid only if the components are identical sensors.",
        )
    uniques = set(present.values())
    if len(uniques) == 1:
        fp = next(iter(uniques))
        return (
            True,
            f"Same response verified across Z/N/E ({_fingerprint_str(fp)}): "
            "H/V is response-independent, so counts are valid.",
        )
    return (
        False,
        "Z/N/E responses DIFFER "
        f"({'; '.join(f'{c}: {_fingerprint_str(fp)}' for c, fp in present.items())}): "
        "H/V in counts is NOT physically correct — response removal (M11) is required.",
    )


def _fingerprint_str(fp: tuple[object, ...]) -> str:
    value, in_u, out_u, n_stages = fp
    return f"sensitivity {value} {in_u}->{out_u}, {n_stages} stages"


def _maybe_smooth_psd(
    freqs: np.ndarray, power: np.ndarray, settings: HvsrSettings
) -> tuple[np.ndarray, np.ndarray]:
    """Konno-Ohmachi smooth a Welch PSD (to dB), or return the raw PSD (dB).

    Reuses hvsrpy's ``konno_and_ohmachi`` smoothing operator (never hand-
    rolled). The smoothed PSD lands on the same log-spaced centre frequencies
    as the H/V curve. The DC bin is dropped (log-frequency smoothing).

    Unsupported-centre-frequency guard. hvsrpy's K-O operator only averages
    input bins whose ratio ``f/fc`` falls inside the narrow support window
    ``[10**(-3/b), 10**(+3/b)]`` (for ``b=40`` that is ``±18 %``). The Welch
    grid is *linearly* spaced (``df = fs/nperseg``), while the K-O centre
    frequencies are *log*-spaced and therefore much denser near the low edge
    of the band: a centre frequency can land in the gap between two Welch
    bins with NO bin inside its support. hvsrpy returns *exactly* ``0.0`` for
    such a centre frequency (its ``sumwindow == 0`` branch), which
    :func:`power_to_db` would floor to a non-physical ``-300 dB`` downward
    spike. A weighted average of strictly-positive PSD can never legitimately
    be ``<= 0``, so a non-positive smoothed value is an unambiguous sentinel
    for "the operator has no support here". We DROP those centre frequencies
    (clip the curve to where the smoothing is valid) rather than emit a
    sentinel spike — the same path feeds the on-screen and report PSD, so
    both stay clean. (Increasing the Welch frequency resolution would also
    close the gaps, but that is a separate display tuning; honest clipping is
    the minimal, operator-faithful fix.)
    """
    if not settings.psd_smoothing or freqs.size < 4:
        return freqs, power_to_db(power)
    import hvsrpy.smoothing as ko

    pos = freqs > 0
    f = freqs[pos]
    p = np.asarray(power[pos], dtype=np.float64).reshape(1, -1)
    fcs = settings.center_frequencies_hz()
    try:
        smoothed = np.asarray(
            ko.konno_and_ohmachi(f, p, fcs, bandwidth=settings.psd_konno_ohmachi_b)
        )[0]
    except Exception as exc:
        _log.warning("hvsr_psd_smoothing_failed", error=str(exc))
        return freqs, power_to_db(power)
    supported = smoothed > 0.0
    if not supported.all():
        _log.debug(
            "hvsr_psd_smoothing_dropped_unsupported_fcs",
            n_dropped=int(np.count_nonzero(~supported)),
            n_total=int(fcs.size),
        )
    return fcs[supported], power_to_db(smoothed[supported])


def slice_archive_windows(
    reader: ArchiveReader,
    device: str,
    group: dict[str, str],
    t_start: UTCDateTime,
    t_end: UTCDateTime,
    settings: HvsrSettings,
) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, UTCDateTime, float]]:
    """Slice an archived range into non-overlapping 3C windows for HVSR.

    The bridge between :class:`~seedlink_dashboard.storage.archive_reader.
    ArchiveReader` (read-only file access, storage layer) and the
    accumulator, for the archive measurement. Reads each component once over
    ``[t_start, t_end]`` then steps a ``window_length_s`` window with NO
    overlap. A window is dropped unless ALL THREE components (Z, N, E) have
    full, gap-free coverage of it (no masked samples, full sample count) —
    honest about gaps rather than feeding zeros or a partial component set
    (rule 8: the file is the truth).

    Returns a list of ``(z, n, e, t_start, fs)`` tuples in time order (each
    ready for :meth:`HvsrAccumulator.add_window`). Empty if the range holds
    no gap-free 3C window.
    """
    from seedlink_dashboard.core.models import StreamID

    wl = settings.window_length_s
    traces: dict[str, object] = {}
    fs = 0.0
    for comp in ("Z", "N", "E"):
        nslc = group.get(comp)
        if nslc is None:
            return []
        try:
            sid = StreamID.from_trace_id(nslc)
        except ValueError:
            return []
        st = reader.read_window(sid, t_start, t_end, device_name=device)
        if len(st) == 0:
            return []  # a component is missing → cannot form a 3C window
        tr = st[0]
        traces[comp] = tr
        fs = float(tr.stats.sampling_rate)
    if fs <= 0:
        return []

    need = max(1, round(wl * fs))
    windows: list[tuple[np.ndarray, np.ndarray, np.ndarray, UTCDateTime, float]] = []
    t = t_start
    while t + wl <= t_end + 1e-9:
        comp_arrays: dict[str, np.ndarray] = {}
        for comp, tr in traces.items():
            seg = tr.slice(t, t + wl)  # type: ignore[attr-defined]
            data = seg.data
            if np.ma.isMaskedArray(data) and np.ma.is_masked(data):
                break  # window straddles a gap in this component
            arr = np.ma.getdata(data).astype(np.float64)
            if arr.shape[0] < need:
                break  # short window (e.g. at a file boundary)
            comp_arrays[comp] = arr[:need]
        if len(comp_arrays) == 3:
            windows.append((comp_arrays["Z"], comp_arrays["N"], comp_arrays["E"], t, fs))
        t = t + wl  # non-overlapping
    return windows
