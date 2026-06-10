"""Build a :class:`DspChain` from typed pydantic stage configs.

The factory is the only place where YAML/pydantic field names meet stage
constructor signatures. Schema validation (e.g. positive `corners`,
`freqmin < freqmax`) has already happened in pydantic; the factory's job
is to validate cross-stage invariants that the schema can't see (e.g.
`bandpass.freqmax < fs/2` *after* upstream decimations).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from echosmonitor.config.schema import (
    BandpassStage,
    DecimationStage,
    DetrendStage,
    HighpassStage,
    LowpassStage,
    NotchStage,
    StaLtaStage,
    TaperStage,
)
from echosmonitor.core.exceptions import ConfigError
from echosmonitor.dsp.chain import DspChain
from echosmonitor.dsp.stages import (
    Bandpass,
    Decimation,
    Detrend,
    Highpass,
    Lowpass,
    Notch,
    Stage,
    StaLta,
    Taper,
)

if TYPE_CHECKING:
    from echosmonitor.config.schema import DspStageConfig
    from echosmonitor.core.models import StreamID

_log = structlog.get_logger(__name__)


def build_chain(
    stages: list[DspStageConfig],
    fs_in: float,
    stream_id: StreamID,
    *,
    live: bool = True,
) -> DspChain:
    """Assemble a :class:`DspChain` from a list of pydantic stage configs.

    Args:
        stages: ordered list of stage configurations as parsed from YAML.
        fs_in: sample rate of the input stream in Hz.
        stream_id: SEED identifier — used only for log binding.
        live: if True (default), the chain is destined for the live engine.
            Live chains forbid taper stages and force `zerophase=False` on
            any IIR filter (warn-logging once per offending stage).

    Raises:
        ConfigError: a stage cannot be constructed safely against `fs_in`,
            or `live=True` together with a `TaperStage`.
    """
    log = _log.bind(stream=stream_id.nslc)
    if fs_in <= 0:
        raise ConfigError(f"fs_in must be > 0, got {fs_in}")

    fs_current = float(fs_in)
    built: list[Stage] = []

    for cfg in stages:
        stage = _build_one(cfg, fs_current, stream_id, live, log)
        built.append(stage)
        fs_current = stage.fs_out

    return DspChain(built, fs_in=fs_in, stream_id=stream_id)


def _build_one(
    cfg: DspStageConfig,
    fs: float,
    stream_id: StreamID,
    live: bool,
    log: structlog.stdlib.BoundLogger,
) -> Stage:
    if isinstance(cfg, DetrendStage):
        return Detrend(fs=fs, kind=cfg.kind)

    if isinstance(cfg, TaperStage):
        if live:
            raise ConfigError(
                "taper stage is not permitted in live DSP chains — "
                "live IIR filtering relies on continuous packets and a per-packet "
                "taper would inject discontinuities every 50 ms"
            )
        return Taper(fs=fs, max_pct=cfg.max_pct)

    if isinstance(cfg, BandpassStage):
        if cfg.freqmax >= 0.5 * fs:
            raise ConfigError(
                f"bandpass.freqmax ({cfg.freqmax} Hz) must be < Nyquist ({0.5 * fs} Hz) for fs={fs}"
            )
        if live and cfg.zerophase:
            log.warning("dsp_chain_zerophase_forced_off", stage="bandpass")
        return Bandpass(
            fs=fs,
            freqmin=cfg.freqmin,
            freqmax=cfg.freqmax,
            corners=cfg.corners,
        )

    if isinstance(cfg, HighpassStage):
        if cfg.freq >= 0.5 * fs:
            raise ConfigError(f"highpass.freq ({cfg.freq} Hz) must be < Nyquist ({0.5 * fs} Hz)")
        if live and cfg.zerophase:
            log.warning("dsp_chain_zerophase_forced_off", stage="highpass")
        return Highpass(fs=fs, freq=cfg.freq, corners=cfg.corners)

    if isinstance(cfg, LowpassStage):
        if cfg.freq >= 0.5 * fs:
            raise ConfigError(f"lowpass.freq ({cfg.freq} Hz) must be < Nyquist ({0.5 * fs} Hz)")
        if live and cfg.zerophase:
            log.warning("dsp_chain_zerophase_forced_off", stage="lowpass")
        return Lowpass(fs=fs, freq=cfg.freq, corners=cfg.corners)

    if isinstance(cfg, NotchStage):
        if cfg.freq >= 0.5 * fs:
            raise ConfigError(f"notch.freq ({cfg.freq} Hz) must be < Nyquist ({0.5 * fs} Hz)")
        return Notch(fs=fs, freq=cfg.freq, quality=cfg.quality)

    if isinstance(cfg, DecimationStage):
        return Decimation(fs=fs, factor=cfg.factor)

    if isinstance(cfg, StaLtaStage):
        # Schema already validates: sta < lta, off_threshold <= on_threshold.
        return StaLta(
            fs=fs,
            sta_s=cfg.sta,
            lta_s=cfg.lta,
            on_thr=cfg.on_threshold,
            off_thr=cfg.off_threshold,
            nslc=stream_id.nslc,
        )

    raise ConfigError(f"unknown DSP stage type: {type(cfg).__name__}")
