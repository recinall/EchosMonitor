"""Tests for `dsp.factory.build_chain`."""

from __future__ import annotations

import pytest

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
from echosmonitor.core.models import StreamID
from echosmonitor.dsp.factory import build_chain
from echosmonitor.dsp.stages import (
    Bandpass,
    Decimation,
    Detrend,
    Highpass,
    Lowpass,
    Notch,
    StaLta,
    Taper,
)

_SID = StreamID("IU", "ANMO", "00", "BHZ")


def _build_one_stage(stage_cfg, fs: float = 100.0, live: bool = True):  # type: ignore[no-untyped-def]
    return build_chain([stage_cfg], fs_in=fs, stream_id=_SID, live=live)


def test_factory_builds_each_stage_type() -> None:
    """Every stage type round-trips: pydantic config → DspChain → Stage instance."""
    cases: list[tuple[object, type]] = [
        (DetrendStage(type="detrend", kind="constant"), Detrend),
        (
            BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0, corners=4, zerophase=False),
            Bandpass,
        ),
        (HighpassStage(type="highpass", freq=1.0, corners=4, zerophase=False), Highpass),
        (LowpassStage(type="lowpass", freq=20.0, corners=4, zerophase=False), Lowpass),
        (NotchStage(type="notch", freq=45.0, quality=30.0), Notch),
        (DecimationStage(type="decimation", factor=4), Decimation),
        (
            StaLtaStage(
                type="sta_lta",
                sta=1.0,
                lta=10.0,
                on_threshold=3.5,
                off_threshold=1.5,
            ),
            StaLta,
        ),
    ]
    for cfg, expected_type in cases:
        chain = _build_one_stage(cfg)
        assert isinstance(chain._stages[0], expected_type), (
            f"{type(cfg).__name__} did not produce {expected_type.__name__}"
        )


def test_factory_raises_on_bandpass_above_nyquist() -> None:
    cfg = BandpassStage(type="bandpass", freqmin=10.0, freqmax=60.0, corners=4, zerophase=False)
    with pytest.raises(ConfigError, match="Nyquist"):
        _build_one_stage(cfg, fs=100.0)


def test_factory_raises_on_highpass_above_nyquist() -> None:
    cfg = HighpassStage(type="highpass", freq=60.0, corners=4, zerophase=False)
    with pytest.raises(ConfigError, match="Nyquist"):
        _build_one_stage(cfg, fs=100.0)


def test_factory_raises_on_lowpass_above_nyquist() -> None:
    cfg = LowpassStage(type="lowpass", freq=60.0, corners=4, zerophase=False)
    with pytest.raises(ConfigError, match="Nyquist"):
        _build_one_stage(cfg, fs=100.0)


def test_factory_raises_on_notch_above_nyquist() -> None:
    cfg = NotchStage(type="notch", freq=60.0, quality=30.0)
    with pytest.raises(ConfigError, match="Nyquist"):
        _build_one_stage(cfg, fs=100.0)


def test_factory_rejects_taper_in_live_chain() -> None:
    cfg = TaperStage(type="taper", max_pct=0.05)
    with pytest.raises(ConfigError, match="taper"):
        _build_one_stage(cfg, live=True)


def test_factory_accepts_taper_in_offline_chain() -> None:
    cfg = TaperStage(type="taper", max_pct=0.05)
    chain = _build_one_stage(cfg, live=False)
    assert isinstance(chain._stages[0], Taper)


def test_factory_warns_when_zerophase_true_in_live_chain(
    capture_structlog: list[dict[str, object]],
) -> None:
    """`zerophase=True` is rejected for live chains; the factory logs a
    structured warning instead of raising so the user gets a heads-up."""
    cfg = BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0, corners=4, zerophase=True)
    _build_one_stage(cfg, live=True)
    assert any(
        rec.get("event") == "dsp_chain_zerophase_forced_off" and rec.get("stage") == "bandpass"
        for rec in capture_structlog
    ), f"expected dsp_chain_zerophase_forced_off in {capture_structlog}"


def test_factory_propagates_fs_through_decimation() -> None:
    """A bandpass after a decimation stage validates against the decimated fs."""
    chain = build_chain(
        stages=[
            DecimationStage(type="decimation", factor=4),
            # 12 Hz < 12.5 Nyquist after dec — must succeed.
            BandpassStage(
                type="bandpass",
                freqmin=0.5,
                freqmax=12.0,
                corners=4,
                zerophase=False,
            ),
        ],
        fs_in=100.0,
        stream_id=_SID,
        live=True,
    )
    assert chain.fs_out == 25.0


def test_factory_raises_when_post_decimation_filter_exceeds_nyquist() -> None:
    """A bandpass that's valid at 100 Hz can become invalid after decimation."""
    with pytest.raises(ConfigError, match="Nyquist"):
        build_chain(
            stages=[
                DecimationStage(type="decimation", factor=4),
                # 20 Hz freqmax > 12.5 Hz Nyquist after factor=4 decimation
                BandpassStage(
                    type="bandpass",
                    freqmin=1.0,
                    freqmax=20.0,
                    corners=4,
                    zerophase=False,
                ),
            ],
            fs_in=100.0,
            stream_id=_SID,
            live=True,
        )


def test_factory_rejects_zero_or_negative_fs() -> None:
    with pytest.raises(ConfigError):
        build_chain(stages=[], fs_in=0.0, stream_id=_SID, live=True)
    with pytest.raises(ConfigError):
        build_chain(stages=[], fs_in=-1.0, stream_id=_SID, live=True)
