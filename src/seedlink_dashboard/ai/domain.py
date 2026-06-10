"""Domain-of-validity checking — the honesty layer (M9).

A pretrained seismic model is trained on a specific instrument class,
frequency band, event type and sampling rate. Applying it outside that
domain (e.g. PhaseNet — broadband seismometers @100 Hz for tectonic
earthquakes — on an accelerometer ``HN*`` @500 Hz) can produce
meaningless picks. This module lets an agent *declare* its domain
(:class:`DomainSpec`) and lets the engagement UI honestly *warn* when a
chosen stream falls outside it (:func:`compatibility`).

The check **informs, never blocks**: the project scope is explicitly
mixed/experimental, so the user may proceed on any severity — but the
warning must be honest and prominent.

This module is torch-free; it reasons purely about SEED channel naming
and sampling rates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

# SEED instrument codes (channel[1]) that denote a *seismometer* — the
# instrument class pretrained pickers are trained on. "H" = high-gain
# seismometer, "L" = low-gain seismometer, "G"/"M" = gravimeter/mass
# position (rare). Accelerometers are "N" (and legacy "A"/"L" varies);
# pressure/infrasound "D"; etc. We treat the explicit seismometer set as
# in-domain and everything else as a warning.
_SEISMOMETER_INSTRUMENT_CODES = frozenset({"H", "L"})

# A native rate this far below the model's trained rate cannot be
# meaningfully up-sampled (e.g. LH @1 Hz into a 100 Hz model): there is
# simply no high-frequency content for the model to key on.
_HOPELESS_UPSAMPLE_RATIO = 0.5


class Severity(IntEnum):
    """Ordered so the *worst* severity across checks wins via ``max``."""

    OK = 0
    WARNING = 1
    LIKELY_INVALID = 2


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """An agent's declared domain of validity.

    ``trained_sampling_rate`` is the rate the model expects;
    ``fs_tolerance`` is the absolute Hz window treated as an exact match
    (outside it we resample, which is a WARNING unless within tolerance).
    """

    expected_instrument: str  # human label, e.g. "broadband seismometer"
    expected_band_hz: tuple[float, float]  # (fmin, fmax) the model keys on
    expected_event_type: str  # e.g. "tectonic earthquake"
    trained_sampling_rate: float
    required_components: int = 3
    allow_single_component: bool = False
    fs_tolerance: float = 0.5
    notes: str = ""
    # M10 Stage C — domain-AGNOSTIC opt-outs for *learning* agents (the
    # heuristic classifier, the autoencoder) that adapt to whatever the
    # channel is. The M9 honesty layer assumed a fixed instrument/rate domain
    # (correct for a PRETRAINED picker/detector); a learning agent that
    # re-fits on the user's own channel must NOT emit the "not a seismometer /
    # data will be resampled" warnings. Defaults are ``False`` so every
    # pretrained spec is byte-for-byte unchanged.
    instrument_agnostic: bool = False  # skip the SEED instrument-class check
    rate_agnostic: bool = False  # skip the fs-resample / fs-too-low checks


@dataclass(frozen=True, slots=True)
class StreamMeta:
    """The minimum a stream advertises for a domain check.

    ``band_code`` / ``instrument_code`` / ``orientation`` are the three
    letters of a SEED channel code (e.g. ``"HHZ"`` → ``"H"``, ``"H"``,
    ``"Z"``). ``n_components`` is how many components the user engaged on
    this station group (1 or 3).
    """

    nslc: str
    fs: float
    band_code: str
    instrument_code: str
    orientation: str
    n_components: int = 1


@dataclass(frozen=True, slots=True)
class DomainCheck:
    """Result of a compatibility check: a severity plus an honest message."""

    severity: Severity
    message: str
    reasons: tuple[str, ...] = field(default_factory=tuple)


def stream_meta_from_nslc(nslc: str, fs: float, n_components: int = 1) -> StreamMeta:
    """Parse a SEED channel code out of an NSLC string into a :class:`StreamMeta`.

    Falls back to empty/unknown codes for malformed channels rather than
    raising — the domain check then degrades to a frequency-only verdict.
    """
    parts = nslc.split(".")
    channel = parts[3] if len(parts) == 4 else ""
    band = channel[0] if len(channel) >= 1 else ""
    instrument = channel[1] if len(channel) >= 2 else ""
    orientation = channel[2] if len(channel) >= 3 else ""
    return StreamMeta(
        nslc=nslc,
        fs=float(fs),
        band_code=band,
        instrument_code=instrument,
        orientation=orientation,
        n_components=n_components,
    )


def compatibility(metas: list[StreamMeta], spec: DomainSpec) -> DomainCheck:
    """Rank a stream group against an agent's domain spec.

    Returns the *worst* severity across all checks with a human message
    that names the specific mismatch(es). Empty ``metas`` is treated as
    ``LIKELY_INVALID`` (nothing to run on).
    """
    if not metas:
        return DomainCheck(
            Severity.LIKELY_INVALID,
            "No streams selected — nothing to run the agent on.",
            ("no_streams",),
        )

    reasons: list[tuple[Severity, str, str]] = []  # (severity, tag, message)

    # 1. Component count.
    n_components = max(m.n_components for m in metas)
    if n_components < spec.required_components and not spec.allow_single_component:
        reasons.append(
            (
                Severity.LIKELY_INVALID,
                "components",
                f"{spec.expected_instrument} model needs {spec.required_components} "
                f"components (Z/N/E); only {n_components} selected.",
            )
        )
    elif n_components < spec.required_components and spec.allow_single_component:
        reasons.append(
            (
                Severity.WARNING,
                "components",
                f"Running on {n_components} component(s); the model expects "
                f"{spec.required_components}. Single-component picks are degraded.",
            )
        )

    # 2. Instrument class (SEED channel[1]). Skipped entirely for a
    # domain-agnostic learning agent, which adapts to any instrument.
    for m in metas:
        if (
            not spec.instrument_agnostic
            and m.instrument_code
            and m.instrument_code not in _SEISMOMETER_INSTRUMENT_CODES
        ):
            reasons.append(
                (
                    Severity.WARNING,
                    "instrument",
                    f"Channel {m.nslc.split('.')[-1]} is not a seismometer "
                    f"(instrument code '{m.instrument_code}'); "
                    f"{spec.expected_instrument} picks may be meaningless.",
                )
            )
            break

    # 3. Sampling rate vs trained rate. Skipped entirely for a rate-agnostic
    # learning agent, which re-fits at whatever rate the channel runs (no
    # resampling, no trained rate to compare against).
    worst_fs = min(m.fs for m in metas)
    highest_fs = max(m.fs for m in metas)
    trained = spec.trained_sampling_rate
    if spec.rate_agnostic:
        pass
    elif worst_fs < trained * _HOPELESS_UPSAMPLE_RATIO:
        reasons.append(
            (
                Severity.LIKELY_INVALID,
                "fs_too_low",
                f"Native rate {worst_fs:g} Hz is far below the model's trained "
                f"{trained:g} Hz; up-sampling cannot recover the missing band.",
            )
        )
    elif (
        abs(highest_fs - trained) > spec.fs_tolerance or abs(worst_fs - trained) > spec.fs_tolerance
    ):
        reasons.append(
            (
                Severity.WARNING,
                "fs_resample",
                f"Stream rate ({worst_fs:g}-{highest_fs:g} Hz) differs from the "
                f"trained {trained:g} Hz; data will be resampled before inference.",
            )
        )

    if not reasons:
        return DomainCheck(
            Severity.OK,
            f"In-domain: {spec.expected_instrument} @{trained:g} Hz for "
            f"{spec.expected_event_type}.",
            (),
        )

    severity = max(r[0] for r in reasons)
    message = " ".join(r[2] for r in reasons)
    if severity >= Severity.WARNING:
        message += " Proceed only if you understand this."
    tags = tuple(r[1] for r in reasons)
    return DomainCheck(severity, message, tags)
