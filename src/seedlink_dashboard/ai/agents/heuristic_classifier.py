"""Heuristic feature classifier — the interpretable learning baseline (M10 Stage C).

This is the FIRST *learning* agent (``requires_fit=True``): it learns what
"normal" looks like on the user's own channel from a baseline window, then
classifies each inference window as ``noise`` / ``unknown`` / ``event`` by
how far its hand-crafted feature vector sits from that learned baseline. It
is the INTERPRETABLE control the ML anomaly detector (the autoencoder) is
judged against — every number it produces has a name.

Import discipline: this agent is **torch-free** and runs in the DEFAULT test
gate. It imports only stdlib + numpy + scipy (via the pure ``dsp`` helpers)
+ the torch-free ``ai`` base. It reuses the project's pure DSP functions
(``dsp.stages.sta_lta_ratio``, ``dsp.psd.welch_psd``) rather than
reimplementing them in ``ai/``.

Feature vector (fixed length 8, computed on the primary / first component;
documented order is :data:`_FEATURE_NAMES`):

0. ``rms``               — RMS amplitude of the sub-window.
1. ``dominant_hz``       — frequency of the peak FFT magnitude bin.
2. ``spectral_centroid`` — magnitude-weighted mean frequency (Hz).
3. ``zcr``               — zero-crossing rate (crossings / sample).
4. ``sta_lta_peak``      — peak recursive STA/LTA ratio over the sub-window.
5. ``band_low_frac``     — fraction of periodogram power in the low band.
6. ``band_mid_frac``     — fraction in the mid band.
7. ``band_high_frac``    — fraction in the high band.

The three band fractions sum to ~1; they are kept as three features (not
two) for interpretability — the classifier standardises each independently.

Classification rule (``classify_window`` / ``infer``): standardise the
window's feature vector against the learned per-feature mean ``mu`` and std
``sigma`` (``sigma`` guarded ``> 0``), then take the Euclidean distance
``d = sqrt(mean_i ((f_i - mu_i) / sigma_i) ** 2)`` (a per-feature RMS
z-distance, so it is scale-free in the number of features). The learned
``event_threshold`` anchors on the 95th-percentile (and worst) of the
baseline sub-windows' own distances, then is scaled by :data:`_EVENT_MARGIN`
to leave headroom for the larger out-of-sample spread of a single fresh
window (the in-sample baseline distances under-estimate it, since they
defined ``mu`` / ``sigma``); ``unknown`` starts at :data:`_UNKNOWN_FRACTION`
of it:

* ``d >= event_threshold``                      → ``"event"``
* ``unknown_threshold <= d < event_threshold``  → ``"unknown"``
* ``d < unknown_threshold``                      → ``"noise"``

Confidence is a monotone squashing of the distance,
``conf = d / (d + event_threshold)`` in ``(0, 1)``, so it is 0.5 exactly at
the event threshold and rises toward 1 for ever-larger distances.

Only ``event`` and ``unknown`` windows emit an annotation (``noise`` returns
``[]`` so quiet windows do not flood the table). The annotation's ``phase``
is the class label (``"event"`` / ``"unknown"``) which drives the marker
colour via the single-source ``marker_style.marker_color``.

Domain honesty: this is *signal features*, not an earthquake-specific model,
so its :class:`DomainSpec` is ``instrument_agnostic`` and ``rate_agnostic``
— it adapts to whatever instrument / sample rate the channel is, so the
engagement UI must NOT emit the pretrained picker's "not a seismometer /
data will be resampled" warning for it.
"""

from __future__ import annotations

import json

import numpy as np

from seedlink_dashboard.ai.base import (
    AgentParam,
    AIAgent,
    AIAnnotation,
    FitContext,
    FitResult,
    InferContext,
)
from seedlink_dashboard.ai.domain import DomainSpec
from seedlink_dashboard.dsp.psd import welch_psd
from seedlink_dashboard.dsp.stages import sta_lta_ratio

# Feature-vector layout (documented order — the serialized state stores this
# so a loaded model can sanity-check it).
_FEATURE_NAMES: tuple[str, ...] = (
    "rms",
    "dominant_hz",
    "spectral_centroid",
    "zcr",
    "sta_lta_peak",
    "band_low_frac",
    "band_mid_frac",
    "band_high_frac",
)
_N_FEATURES = len(_FEATURE_NAMES)

# Sub-window the feature vector is computed over (seconds). The fit slices the
# baseline into non-overlapping sub-windows of this length; inference computes
# one feature vector over the (whole) inference window resampled to the same
# convention by simply using the inference window directly.
_ANALYSIS_SECONDS = 4.0

# STA/LTA windows for the sta_lta_peak feature (seismic-dsp skill defaults,
# scaled so lta fits inside the analysis sub-window).
_STA_SECONDS = 0.5
_LTA_SECONDS = 2.5

# Band edges as FRACTIONS of Nyquist (rate-agnostic by construction — the
# agent adapts to any fs). low = [0, lo*Ny), mid = [lo*Ny, hi*Ny), high =
# [hi*Ny, Ny].
_BAND_LO_FRAC = 0.1
_BAND_HI_FRAC = 0.4

# The learned event threshold anchors on this percentile of the baseline
# (in-sample) distances, then is scaled by _EVENT_MARGIN to leave headroom
# for the natural out-of-sample spread of a single fresh window (in-sample
# baseline distances under-estimate it because they defined mu/sigma).
_EVENT_PERCENTILE = 95.0
# An event must deviate this many times further than the worst baseline
# window — interpretable and gives wide separation from out-of-sample noise.
_EVENT_MARGIN = 3.0
# The "unknown" band starts at this fraction of the event threshold.
_UNKNOWN_FRACTION = 0.6
# Guard so a perfectly flat feature across the baseline never yields sigma=0.
_SIGMA_FLOOR = 1e-9

# Progress cadence: call ``context.progress`` at most this many times so the
# structured-log channel stays alive without spamming (rule 7).
_PROGRESS_EVERY = 8


def extract_features(samples: np.ndarray, fs: float) -> np.ndarray:
    """Compute the fixed-length feature vector for one sub-window.

    Pure: no state, no I/O. Returns a float64 array of length
    :data:`_N_FEATURES` in the documented :data:`_FEATURE_NAMES` order. A
    degenerate (empty / zero-variance / too-short) input yields a finite,
    all-defined vector (zeros where a ratio is undefined) so the downstream
    standardisation never sees NaNs.

    Args:
        samples: 1-D primary-component samples.
        fs: Sample rate in Hz (must be > 0).

    Returns:
        Float64 feature vector, length 8.
    """
    x = np.asarray(samples, dtype=np.float64)
    feat = np.zeros(_N_FEATURES, dtype=np.float64)
    if x.size == 0 or fs <= 0:
        return feat

    # 0. RMS amplitude.
    feat[0] = float(np.sqrt(np.mean(x * x)))

    # FFT magnitude spectrum (single-sided).
    spectrum = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(x.size, d=1.0 / fs)
    mag_sum = float(spectrum.sum())
    if mag_sum > 0.0 and freqs.size > 0:
        # 1. Dominant frequency (peak magnitude bin).
        feat[1] = float(freqs[int(np.argmax(spectrum))])
        # 2. Spectral centroid (magnitude-weighted mean frequency).
        feat[2] = float(np.sum(freqs * spectrum) / mag_sum)

    # 3. Zero-crossing rate (sign changes / sample).
    if x.size > 1:
        signs = np.signbit(x)
        feat[3] = float(np.count_nonzero(signs[1:] != signs[:-1])) / float(x.size)

    # 4. STA/LTA peak — reuse the pure dsp helper (do NOT reimplement).
    ratio = sta_lta_ratio(x, _STA_SECONDS, _LTA_SECONDS, fs)
    if ratio.size > 0:
        finite = ratio[np.isfinite(ratio)]
        if finite.size > 0:
            feat[4] = float(np.max(finite))

    # 5-7. Band-energy fractions from the Welch periodogram (reuse dsp.psd).
    f_psd, p_psd = welch_psd(x, fs, detrend="constant")
    total = float(p_psd.sum())
    if total > 0.0 and f_psd.size > 0:
        nyq = fs * 0.5
        lo_edge = _BAND_LO_FRAC * nyq
        hi_edge = _BAND_HI_FRAC * nyq
        low_mask = f_psd < lo_edge
        mid_mask = (f_psd >= lo_edge) & (f_psd < hi_edge)
        high_mask = f_psd >= hi_edge
        feat[5] = float(p_psd[low_mask].sum()) / total
        feat[6] = float(p_psd[mid_mask].sum()) / total
        feat[7] = float(p_psd[high_mask].sum()) / total

    return feat


def _standardized_distance(feat: np.ndarray, mu: np.ndarray, sigma: np.ndarray) -> float:
    """Per-feature RMS z-distance of ``feat`` from the baseline (mu, sigma)."""
    z = (feat - mu) / sigma
    return float(np.sqrt(np.mean(z * z)))


class HeuristicClassifier(AIAgent):
    """Interpretable feature-distance classifier (noise / unknown / event)."""

    def __init__(self, *, analysis_seconds: float = _ANALYSIS_SECONDS) -> None:
        self._analysis_seconds = float(analysis_seconds)
        # Learned state (None until fit / load_state).
        self._mu: np.ndarray | None = None
        self._sigma: np.ndarray | None = None
        self._event_threshold: float | None = None
        self._unknown_threshold: float | None = None
        # The fs the baseline was learned at (provenance; the agent is
        # rate-agnostic so this is informational, not enforced).
        self._fit_fs: float | None = None

    # ------------------------------------------------------------------
    # AIAgent interface
    # ------------------------------------------------------------------
    @property
    def name(self) -> str:
        return "Heuristic feature classifier"

    @property
    def kind(self) -> str:
        return "heuristic_class"

    @property
    def domain_spec(self) -> DomainSpec:
        # Domain-AGNOSTIC: signal features, not an earthquake model. The
        # opt-out flags make the honesty layer skip the instrument / rate
        # warnings (it adapts to whatever the channel is).
        return DomainSpec(
            expected_instrument="any waveform",
            expected_band_hz=(0.0, 0.0),
            expected_event_type="signal anomaly (learned baseline)",
            trained_sampling_rate=0.0,
            required_components=1,
            allow_single_component=True,
            instrument_agnostic=True,
            rate_agnostic=True,
            notes=(
                "Learns a baseline of hand-crafted signal features from the "
                "user's own channel; works on any instrument / sample rate. "
                "Not earthquake-specific — flags windows that deviate from "
                "the learned normal."
            ),
        )

    def required_sampling_rate(self) -> float | None:
        # Rate-agnostic: the engine must not resample for this agent.
        return None

    def required_components(self) -> int:
        return 1

    def engage_params(self) -> list[AgentParam]:
        return [
            AgentParam(
                "analysis_seconds",
                "Analysis window (s)",
                "float",
                _ANALYSIS_SECONDS,
                minimum=1.0,
                maximum=60.0,
                step=1.0,
                decimals=1,
            ),
        ]

    def warm_up(self) -> None:
        # Nothing heavy to load — the model IS the learned baseline, restored
        # by fit / load_state. Guard that a usable baseline exists.
        if self._mu is None or self._sigma is None or self._event_threshold is None:
            raise RuntimeError("HeuristicClassifier.warm_up before fit/load_state")

    # ------------------------------------------------------------------
    # M10 fit-then-infer overrides
    # ------------------------------------------------------------------
    @property
    def requires_fit(self) -> bool:
        return True

    def fit(self, context: FitContext) -> FitResult:
        """Learn per-feature mean/std and an event distance threshold.

        Slices the baseline into non-overlapping analysis sub-windows,
        computes the feature vector for each, then learns ``mu`` / ``sigma``
        per feature and the 95th-percentile distance as the event threshold.
        Polls ``context.should_stop`` between sub-windows and reports
        progress periodically (rule 7).
        """
        arr = _primary(context.samples_by_component)
        fs = context.fs
        self._fit_fs = float(fs)
        n_sub = max(1, int(self._analysis_seconds * fs))
        n_windows = max(1, arr.size // n_sub) if arr.size >= n_sub else 0

        feats: list[np.ndarray] = []
        for i in range(n_windows):
            if context.should_stop():
                # Interrupted: leave the baseline UNSET (rule 7 observable
                # interruption — no usable threshold learned).
                return FitResult(summary="fit interrupted", n_windows=0)
            sub = arr[i * n_sub : (i + 1) * n_sub]
            feats.append(extract_features(sub, fs))
            if n_windows >= _PROGRESS_EVERY and (i % (n_windows // _PROGRESS_EVERY + 1) == 0):
                context.progress((i + 1) / n_windows, "learning feature baseline")

        if not feats:
            # Baseline too short for even one sub-window: fall back to a single
            # vector over the whole baseline so the agent still has a normal.
            if context.should_stop():
                return FitResult(summary="fit interrupted", n_windows=0)
            feats = [extract_features(arr, fs)]

        matrix = np.vstack(feats)
        mu = matrix.mean(axis=0)
        sigma = matrix.std(axis=0)
        sigma = np.maximum(sigma, _SIGMA_FLOOR)
        self._mu = mu
        self._sigma = sigma

        distances = np.array(
            [_standardized_distance(f, mu, sigma) for f in feats], dtype=np.float64
        )
        # Anchor on the 95th-percentile (and worst) in-sample distance, then
        # scale by the margin to clear the out-of-sample spread of a single
        # fresh quiet window (which the in-sample baseline under-estimates).
        anchor = max(
            float(np.percentile(distances, _EVENT_PERCENTILE)),
            float(distances.max()),
        )
        event_thr = anchor * _EVENT_MARGIN
        if event_thr <= _SIGMA_FLOOR:
            # Degenerate baseline (one window or perfectly self-similar): a
            # standardized distance margin of 2.0 is a sane default.
            event_thr = float(_EVENT_MARGIN)
        self._event_threshold = event_thr
        self._unknown_threshold = _UNKNOWN_FRACTION * event_thr

        context.progress(1.0, "baseline learned")
        return FitResult(
            summary=(
                f"learned baseline over {len(feats)} sub-window(s); "
                f"event distance threshold {event_thr:.3f}"
            ),
            n_windows=len(feats),
            meta={
                "event_threshold": event_thr,
                "unknown_threshold": self._unknown_threshold,
                "n_features": _N_FEATURES,
                "fs": self._fit_fs,
            },
        )

    def serialize_state(self) -> bytes | None:
        if (
            self._mu is None
            or self._sigma is None
            or self._event_threshold is None
            or self._unknown_threshold is None
        ):
            return None
        payload = {
            "feature_order": list(_FEATURE_NAMES),
            "mu": self._mu.tolist(),
            "sigma": self._sigma.tolist(),
            "event_threshold": self._event_threshold,
            "unknown_threshold": self._unknown_threshold,
            "fit_fs": self._fit_fs,
        }
        return json.dumps(payload).encode("utf-8")

    def load_state(self, data: bytes) -> None:
        obj = json.loads(data.decode("utf-8"))
        self._mu = np.asarray(obj["mu"], dtype=np.float64)
        self._sigma = np.maximum(np.asarray(obj["sigma"], dtype=np.float64), _SIGMA_FLOOR)
        self._event_threshold = float(obj["event_threshold"])
        self._unknown_threshold = float(obj["unknown_threshold"])
        self._fit_fs = obj.get("fit_fs")

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------
    def _classify_feat(self, feat: np.ndarray) -> tuple[str, float, float]:
        """Core rule on a precomputed feature vector → ``(label, conf, d)``."""
        assert self._mu is not None and self._sigma is not None
        assert self._event_threshold is not None
        d = _standardized_distance(feat, self._mu, self._sigma)
        if d >= self._event_threshold:
            label = "event"
        elif self._unknown_threshold is not None and d >= self._unknown_threshold:
            label = "unknown"
        else:
            label = "noise"
        # Monotone confidence in (0, 1): 0.5 at the event threshold.
        conf = d / (d + self._event_threshold) if (d + self._event_threshold) > 0 else 0.0
        return label, float(conf), d

    def classify_window(self, samples: np.ndarray, fs: float) -> tuple[str, float]:
        """Classify one window → ``(class_label, confidence)``.

        A small PURE helper (no annotation emission, no context) so tests can
        assert the ``"noise"`` / ``"event"`` verdict directly. Requires a
        learned baseline (fit or load_state) — raises otherwise.
        """
        if self._mu is None or self._sigma is None or self._event_threshold is None:
            raise RuntimeError("classify_window before fit/load_state")
        label, conf, _d = self._classify_feat(extract_features(samples, fs))
        return label, conf

    def infer(self, context: InferContext) -> list[AIAnnotation]:
        if self._mu is None or self._sigma is None or self._event_threshold is None:
            raise RuntimeError("HeuristicClassifier.infer before fit/load_state")
        if context.n_samples == 0:
            return []
        arr = _primary(context.samples_by_component)
        feat = extract_features(arr, context.fs)
        label, conf, d = self._classify_feat(feat)
        if label == "noise":
            # Do not flood the table with every quiet window.
            return []

        n = context.n_samples
        window_t_end = context.t_start + n / context.fs
        nslc = context.nslc_by_component.get("Z") or next(iter(context.nslc_by_component.values()))
        t_mid = context.t_start + (n / 2) / context.fs
        meta: dict[str, object] = {
            "phase": label,
            "class": label,
            "distance": d,
            "event_threshold": self._event_threshold,
            "unknown_threshold": self._unknown_threshold,
            # Small JSON-friendly feature snapshot (no full arrays — the
            # vector is only 8 numbers).
            "features": {name: float(v) for name, v in zip(_FEATURE_NAMES, feat, strict=True)},
        }
        return [
            AIAnnotation(
                device=context.device,
                nslc=nslc,
                kind=self.kind,
                phase=label,
                t=t_mid,
                score=conf,
                model_name="heuristic_classifier",
                model_weights="fitted",
                window_t_start=context.t_start,
                window_t_end=window_t_end,
                meta=meta,
            )
        ]


def _primary(samples_by_component: dict[str, np.ndarray]) -> np.ndarray:
    """The primary component samples (Z if present, else the first)."""
    arr = samples_by_component.get("Z")
    if arr is None:
        arr = next(iter(samples_by_component.values()))
    return np.asarray(arr, dtype=np.float64)
