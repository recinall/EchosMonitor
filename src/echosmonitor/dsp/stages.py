"""Per-channel stateful DSP stages.

Each stage exposes the :class:`Stage` protocol and is **pure** in the
CLAUDE.md sense: no Qt, no I/O, no global state. Stages do hold their own
streaming state (filter `zi`, decimation tail, STA/LTA history) so that
`process()` can be called repeatedly with adjacent packets and produce the
same output as a single one-shot call (modulo a warm-up window for the
recursive filters).

The chain orchestrates these stages — see :mod:`echosmonitor.dsp.chain`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

import numpy as np
from scipy.signal import (
    decimate,
    iirfilter,
    iirnotch,
    lfilter,
    lfilter_zi,
)

if TYPE_CHECKING:
    from obspy.core.utcdatetime import UTCDateTime

    from echosmonitor.core.models import Trigger

# Recursive-mean detrend tracks ~30 s of history.
_DETREND_TRACK_SECONDS = 30.0
_MAX_DECIMATION_FACTOR = 16


class Stage(Protocol):
    """Streaming DSP stage contract.

    Implementations must:

    * be cheap to construct (no I/O, no model loads),
    * preserve internal state across `process()` calls,
    * never modify their inputs in place.
    """

    fs: float

    @property
    def fs_out(self) -> float:
        """Output sample rate. Equal to `fs` for non-decimating stages."""
        ...

    @property
    def triggers(self) -> list[Trigger]:
        """Triggers emitted by the most recent `process()` call. Empty for
        non-tap stages. The chain reads this after each `process()`."""
        ...

    def reset(self) -> None:
        """Drop all streaming state. Next `process()` warms up from scratch."""
        ...

    def process(self, x: np.ndarray, t_start: UTCDateTime) -> np.ndarray:
        """Run the stage on `x`. Same length out for non-decimating stages.

        `t_start` is the wall-clock timestamp of the first sample of `x`,
        carried only so STA/LTA can attach absolute times to triggers.
        """
        ...


def sta_lta_ratio(x: np.ndarray, sta_s: float, lta_s: float, fs: float) -> np.ndarray:
    """Recursive STA/LTA ratio of ``x`` (pure; no state).

    A one-shot helper for *displaying* the "why did this fire?" curve over
    a finite buffer — the live detector uses the stateful :class:`StaLta`
    stage instead. Window lengths are given in seconds and converted to
    samples with the same rounding rule as :class:`StaLta` so the curve
    matches what the live detector saw. Returns an empty array when the
    buffer is too short to have seen a full LTA window (the ratio would be
    dominated by the estimator's initial conditions).

    Args:
        x: 1-D samples.
        sta_s: Short-term average window, seconds.
        lta_s: Long-term average window, seconds (must exceed ``sta_s``).
        fs: Sample rate (Hz).

    Returns:
        Float64 ratio array the same length as ``x``, or an empty array.
    """
    from obspy.signal.trigger import recursive_sta_lta

    if x.size == 0 or fs <= 0:
        return np.empty(0, dtype=np.float64)
    nsta = max(1, round(sta_s * fs))
    nlta = max(nsta + 1, round(lta_s * fs))
    if x.size <= nlta:
        return np.empty(0, dtype=np.float64)
    ratio = recursive_sta_lta(x.astype(np.float64, copy=False), nsta, nlta)
    return np.asarray(ratio, dtype=np.float64)


class _BaseStage:
    """Mixin that gives every stage a sensible default `fs_out` and an empty
    `triggers` list. Concrete stages override the bits they need."""

    fs: float

    def __init__(self, fs: float) -> None:
        if fs <= 0:
            raise ValueError(f"fs must be > 0, got {fs}")
        self.fs = float(fs)
        self._triggers: list[Trigger] = []

    @property
    def fs_out(self) -> float:
        return self.fs

    @property
    def triggers(self) -> list[Trigger]:
        return self._triggers

    def reset(self) -> None:  # pragma: no cover — overridden by stateful stages
        self._triggers = []


class Detrend(_BaseStage):
    """Remove a slowly-varying baseline.

    `kind="constant"` subtracts a recursive mean — cheap, no per-buffer
    discontinuities, recommended for live use.
    `kind="linear"` does a per-buffer least-squares detrend; useful for
    offline-style analysis but introduces visible discontinuities at packet
    boundaries when the trend really is non-stationary. The "this is
    discouraged in a live chain" warning is emitted once per stream per
    session by the engine at chain-install time (see
    ``StreamingEngine._maybe_install_chain``) — NOT here, because a
    per-instance flag reset on every chain rebuild and re-spammed the log.
    `kind="demean"` is an alias for `"constant"` (kept for schema compat).
    """

    def __init__(self, fs: float, kind: Literal["linear", "constant", "demean"]) -> None:
        super().__init__(fs)
        self._kind = "constant" if kind == "demean" else kind
        self._mean = 0.0
        self._initialized = False
        # Smoothing factor: blend each new buffer's mean toward the running
        # mean over ~30 s of samples. Per-buffer alpha is N_buf / N_track,
        # capped at 1 so a short buffer cannot oscillate.
        self._track_n = max(1, int(_DETREND_TRACK_SECONDS * fs))

    def reset(self) -> None:
        super().reset()
        self._mean = 0.0
        self._initialized = False

    def process(self, x: np.ndarray, _t_start: UTCDateTime) -> np.ndarray:
        if x.size == 0:
            return x
        if self._kind == "linear":
            n = x.size
            t = np.arange(n, dtype=np.float64)
            x64 = x.astype(np.float64)
            slope, intercept = np.polyfit(t, x64, 1)
            detrended: np.ndarray = (x64 - (slope * t + intercept)).astype(np.float64)
            return detrended
        # constant / demean
        buf_mean = float(np.mean(x))
        if not self._initialized:
            self._mean = buf_mean
            self._initialized = True
        else:
            alpha = min(1.0, x.size / self._track_n)
            self._mean = (1.0 - alpha) * self._mean + alpha * buf_mean
        out: np.ndarray = x.astype(np.float64) - self._mean
        return out


class _IIR(_BaseStage):
    """Common stateful causal IIR machinery shared by band/high/low/notch.

    On the very first call, `zi` is scaled by `x[0]` so the filter starts
    from a steady state matching the input — without this you see a long
    transient as the filter ramps up from zero.
    """

    def __init__(self, fs: float, b: np.ndarray, a: np.ndarray) -> None:
        super().__init__(fs)
        self._b = b
        self._a = a
        self._zi: np.ndarray = lfilter_zi(b, a)
        self._initialized = False

    def reset(self) -> None:
        super().reset()
        self._zi = lfilter_zi(self._b, self._a)
        self._initialized = False

    def process(self, x: np.ndarray, _t_start: UTCDateTime) -> np.ndarray:
        if x.size == 0:
            return x
        if not self._initialized:
            self._zi = self._zi * float(x[0])
            self._initialized = True
        y, zi_new = lfilter(self._b, self._a, x, zi=self._zi)
        self._zi = np.asarray(zi_new, dtype=np.float64)
        out: np.ndarray = np.asarray(y, dtype=np.float64)
        return out


class Bandpass(_IIR):
    """Stateful causal Butterworth band-pass."""

    def __init__(
        self,
        fs: float,
        freqmin: float,
        freqmax: float,
        corners: int = 4,
    ) -> None:
        if freqmin <= 0:
            raise ValueError(f"freqmin must be > 0, got {freqmin}")
        if freqmax <= freqmin:
            raise ValueError(f"freqmax ({freqmax}) must be > freqmin ({freqmin})")
        if freqmax >= 0.5 * fs:
            raise ValueError(f"freqmax ({freqmax}) must be < Nyquist ({0.5 * fs})")
        ny = 0.5 * fs
        b, a = iirfilter(
            corners,
            [freqmin / ny, freqmax / ny],
            btype="band",
            ftype="butter",
        )
        super().__init__(fs, np.asarray(b), np.asarray(a))


class Highpass(_IIR):
    """Stateful causal Butterworth high-pass."""

    def __init__(self, fs: float, freq: float, corners: int = 4) -> None:
        if freq <= 0 or freq >= 0.5 * fs:
            raise ValueError(f"freq ({freq}) must be in (0, Nyquist {0.5 * fs})")
        ny = 0.5 * fs
        b, a = iirfilter(corners, freq / ny, btype="highpass", ftype="butter")
        super().__init__(fs, np.asarray(b), np.asarray(a))


class Lowpass(_IIR):
    """Stateful causal Butterworth low-pass."""

    def __init__(self, fs: float, freq: float, corners: int = 4) -> None:
        if freq <= 0 or freq >= 0.5 * fs:
            raise ValueError(f"freq ({freq}) must be in (0, Nyquist {0.5 * fs})")
        ny = 0.5 * fs
        b, a = iirfilter(corners, freq / ny, btype="lowpass", ftype="butter")
        super().__init__(fs, np.asarray(b), np.asarray(a))


class Notch(_IIR):
    """Stateful IIR notch (single-frequency rejection)."""

    def __init__(self, fs: float, freq: float, quality: float = 30.0) -> None:
        if freq <= 0 or freq >= 0.5 * fs:
            raise ValueError(f"freq ({freq}) must be in (0, Nyquist {0.5 * fs})")
        if quality <= 0:
            raise ValueError(f"quality must be > 0, got {quality}")
        b, a = iirnotch(freq, quality, fs)
        super().__init__(fs, np.asarray(b), np.asarray(a))


class Decimation(_BaseStage):
    """Anti-aliased downsampler.

    Maintains a tail of input samples between calls so the IIR anti-alias
    filter sees continuous data and the output is sample-accurate (no
    duplicated or missing samples at packet boundaries).
    """

    def __init__(self, fs: float, factor: int) -> None:
        if factor < 2 or factor > _MAX_DECIMATION_FACTOR:
            raise ValueError(f"decimation factor must be 2..{_MAX_DECIMATION_FACTOR}, got {factor}")
        super().__init__(fs)
        self._factor = int(factor)
        # Keep enough history that the leading edge of each new chunk is
        # well past the IIR anti-alias filter's transient.
        self._tail_len = max(2 * self._factor, 16)
        self._tail: np.ndarray = np.empty(0, dtype=np.float64)

    @property
    def fs_out(self) -> float:
        return self.fs / self._factor

    def reset(self) -> None:
        super().reset()
        self._tail = np.empty(0, dtype=np.float64)

    def process(self, x: np.ndarray, _t_start: UTCDateTime) -> np.ndarray:
        if x.size == 0:
            return x
        x64 = x.astype(np.float64, copy=False)
        # Prepend tail so the filter has continuous context.
        combined = np.concatenate([self._tail, x64]) if self._tail.size > 0 else x64
        # decimate() requires len(input) > some filter order; for short
        # buffers fall back to no-op decimation.
        if combined.size <= self._factor * 2:
            self._tail = combined.copy()
            return np.empty(0, dtype=np.float64)
        decimated = decimate(combined, self._factor, ftype="iir", zero_phase=False)
        # Drop the first `tail_len // factor` samples so we only keep the
        # part of the output corresponding to fresh input.
        skip = self._tail.size // self._factor
        # Update tail: keep the last `tail_len` samples of the input view.
        if combined.size > self._tail_len:
            self._tail = combined[-self._tail_len :].copy()
        else:
            self._tail = combined.copy()
        return np.asarray(decimated[skip:], dtype=np.float64)


class StaLta(_BaseStage):
    """Recursive STA/LTA tap.

    Returns the input passed through unchanged — STA/LTA is observational,
    not transformative. New triggers (and OFFs of previously-open triggers)
    are appended to ``self._triggers``; the chain reads them after each
    `process()` call and clears the list.
    """

    def __init__(
        self,
        fs: float,
        sta_s: float,
        lta_s: float,
        on_thr: float,
        off_thr: float,
        nslc: str,
    ) -> None:
        if sta_s <= 0 or lta_s <= 0:
            raise ValueError(f"sta_s and lta_s must be > 0, got {sta_s}, {lta_s}")
        if sta_s >= lta_s:
            raise ValueError(f"sta_s ({sta_s}) must be < lta_s ({lta_s})")
        if off_thr > on_thr:
            raise ValueError(f"off_thr ({off_thr}) must be <= on_thr ({on_thr})")
        super().__init__(fs)
        self._nsta = max(1, round(sta_s * fs))
        self._nlta = max(self._nsta + 1, round(lta_s * fs))
        self._on = float(on_thr)
        self._off = float(off_thr)
        self._nslc = nslc
        # Carry the last `_nlta` samples so the recursive estimator has
        # convergent statistics on the next call.
        self._history: np.ndarray = np.empty(0, dtype=np.float64)
        # Open trigger crossing packet boundaries.
        self._open_t_on: UTCDateTime | None = None
        self._open_peak: float = 0.0
        self._open_emitted_t_on: UTCDateTime | None = None
        # Total samples seen so far — we suppress trigger detection until
        # the LTA estimator has converged on roughly nlta samples of input.
        # Without this guard, the first samples have near-zero LTA energy
        # and produce huge, spurious ratios.
        self._samples_seen = 0

    def reset(self) -> None:
        super().reset()
        self._history = np.empty(0, dtype=np.float64)
        self._open_t_on = None
        self._open_peak = 0.0
        self._open_emitted_t_on = None
        self._samples_seen = 0

    def process(self, x: np.ndarray, t_start: UTCDateTime) -> np.ndarray:
        from obspy.signal.trigger import recursive_sta_lta

        from echosmonitor.core.models import Trigger

        self._triggers = []
        if x.size == 0:
            return x

        x64 = x.astype(np.float64, copy=False)
        full = np.concatenate([self._history, x64]) if self._history.size > 0 else x64

        ratio = recursive_sta_lta(full, self._nsta, self._nlta)
        # Only inspect the part of `ratio` that corresponds to new samples.
        new_ratio = ratio[self._history.size :]

        # Update history (cap at LTA window for a bounded memory footprint).
        if full.size > self._nlta:
            self._history = full[-self._nlta :].copy()
        else:
            self._history = full.copy()

        # Until the LTA estimator has seen at least one full LTA window of
        # samples, the ratio is dominated by the recursive estimator's
        # initial conditions and produces spurious giant values. Skip
        # detection during that warm-up.
        warmup_remaining = max(0, self._nlta - self._samples_seen)
        self._samples_seen += x.size

        active = self._open_t_on is not None
        for i, r in enumerate(new_ratio):
            if i < warmup_remaining:
                continue
            t = t_start + i / self.fs
            if not active and r >= self._on:
                self._open_t_on = t
                self._open_peak = float(r)
                active = True
            elif active:
                self._open_peak = max(self._open_peak, float(r))
                if r < self._off:
                    assert self._open_t_on is not None
                    self._triggers.append(
                        Trigger(
                            nslc=self._nslc,
                            t_on=self._open_t_on,
                            t_off=t,
                            peak_ratio=self._open_peak,
                        )
                    )
                    self._open_t_on = None
                    self._open_peak = 0.0
                    active = False

        # If a trigger is still open at end of buffer, emit a t_off=None
        # event so downstream consumers see the onset promptly. The next
        # packet will emit a finalising event with the real t_off — but we
        # only emit the open marker once per trigger to keep the log clean.
        if active and self._open_t_on is not None and self._open_emitted_t_on != self._open_t_on:
            self._triggers.append(
                Trigger(
                    nslc=self._nslc,
                    t_on=self._open_t_on,
                    t_off=None,
                    peak_ratio=self._open_peak,
                )
            )
            self._open_emitted_t_on = self._open_t_on

        return x


class Taper(_BaseStage):
    """Hann taper on the head and tail of each buffer.

    Useful for offline pipelines that filter a finite buffer once. The
    factory rejects this stage in live chains, where applying a taper to
    every packet would inject discontinuities at the packet boundaries.
    """

    def __init__(self, fs: float, max_pct: float) -> None:
        if not 0.0 < max_pct <= 0.5:
            raise ValueError(f"max_pct must be in (0, 0.5], got {max_pct}")
        super().__init__(fs)
        self._max_pct = float(max_pct)

    def process(self, x: np.ndarray, _t_start: UTCDateTime) -> np.ndarray:
        if x.size == 0:
            return x
        n = x.size
        m = max(1, int(n * self._max_pct))
        if 2 * m >= n:
            m = max(1, n // 2)
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, m))
        window = np.ones(n, dtype=np.float64)
        window[:m] = ramp
        window[-m:] = ramp[::-1]
        return x.astype(np.float64) * window
