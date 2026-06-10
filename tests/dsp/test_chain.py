"""End-to-end tests for `DspChain`."""

from __future__ import annotations

import numpy as np
from obspy import UTCDateTime

from echosmonitor.config.schema import (
    BandpassStage,
    DetrendStage,
    StaLtaStage,
)
from echosmonitor.core.models import StreamID
from echosmonitor.dsp.factory import build_chain


def test_chain_detrend_bandpass_sta_lta_emits_trigger_on_planted_event() -> None:
    """A planted high-amplitude burst on top of pink-ish noise should be
    detected by the STA/LTA tap at the end of the chain."""
    fs = 100.0
    duration_s = 60
    n = int(duration_s * fs)
    rng = np.random.default_rng(seed=11)
    # Slow drift + broadband noise + planted burst.
    drift = 5.0 * np.linspace(0, 1, n)
    noise = rng.standard_normal(n)
    onset_s = 45.0
    onset_idx = int(onset_s * fs)
    burst = np.zeros_like(noise)
    burst[onset_idx : onset_idx + int(2 * fs)] = 12.0 * rng.standard_normal(int(2 * fs))
    x = drift + noise + burst

    sid = StreamID("IU", "ANMO", "00", "BHZ")
    chain = build_chain(
        stages=[
            DetrendStage(type="detrend", kind="constant"),
            BandpassStage(
                type="bandpass",
                freqmin=1.0,
                freqmax=10.0,
                corners=4,
                zerophase=False,
            ),
            StaLtaStage(
                type="sta_lta",
                sta=1.0,
                lta=10.0,
                on_threshold=3.5,
                off_threshold=1.5,
            ),
        ],
        fs_in=fs,
        stream_id=sid,
        live=True,
    )

    t0 = UTCDateTime(2026, 5, 8, 0, 0, 0)
    out_chunks: list[np.ndarray] = []
    triggers: list = []
    chunk = 100
    for i in range(0, n, chunk):
        result = chain.process(x[i : i + chunk], t0 + i / fs)
        out_chunks.append(result.samples)
        triggers.extend(result.triggers)

    out = np.concatenate(out_chunks)
    # No decimation in this chain — output length must match input length.
    assert out.shape == x.shape
    # The planted event must produce at least one trigger near the onset.
    assert len(triggers) >= 1, "expected at least one trigger from planted burst"
    onset_truth = t0 + onset_s
    closest_drift = min(abs(float(trig.t_on - onset_truth)) for trig in triggers)
    assert closest_drift < 1.0, f"closest trigger {closest_drift:.3f}s from true onset"


def test_chain_with_decimation_changes_fs_out() -> None:
    """A chain with decimation reports the correct `fs_out`."""
    from echosmonitor.config.schema import DecimationStage

    sid = StreamID("IU", "ANMO", "00", "BHZ")
    chain = build_chain(
        stages=[
            DetrendStage(type="detrend", kind="constant"),
            DecimationStage(type="decimation", factor=4),
        ],
        fs_in=100.0,
        stream_id=sid,
        live=True,
    )
    assert chain.fs_in == 100.0
    assert chain.fs_out == 25.0


def test_empty_chain_is_identity() -> None:
    sid = StreamID("X", "Y", "", "Z")
    chain = build_chain(stages=[], fs_in=50.0, stream_id=sid, live=True)
    assert chain.fs_out == 50.0
    rng = np.random.default_rng(seed=12)
    x = rng.standard_normal(100)
    result = chain.process(x, UTCDateTime(2026, 1, 1))
    np.testing.assert_array_equal(result.samples, x)
    assert result.triggers == []


def test_sta_lta_open_marker_emitted_once_then_finalised() -> None:
    """A trigger that spans a packet boundary must:

    * emit exactly one `t_off=None` marker on the packet where it opens,
    * stay quiet on subsequent packets while still active,
    * emit a finalising trigger with a real `t_off > t_on` on the packet
      where the ratio drops below `off_threshold`.
    """
    from echosmonitor.dsp.stages import StaLta

    fs = 100.0
    nlta_s = 5.0
    rng = np.random.default_rng(seed=42)
    # Build a signal: 30 s of warm-up noise, then a 10 s burst, then 10 s
    # of low-amplitude background again so the trigger finalises.
    n_warmup = int(30 * fs)
    n_burst = int(10 * fs)
    n_after = int(15 * fs)
    warmup = rng.standard_normal(n_warmup)
    burst = 20.0 * rng.standard_normal(n_burst)
    after = rng.standard_normal(n_after)
    x = np.concatenate([warmup, burst, after])

    sta_lta = StaLta(
        fs=fs,
        sta_s=1.0,
        lta_s=nlta_s,
        on_thr=4.0,
        off_thr=1.5,
        nslc="X.Y.Z.W",
    )
    t0 = UTCDateTime(2026, 1, 1)
    chunk = 100  # one second per chunk
    open_markers: list = []
    finalised: list = []
    for i in range(0, x.size, chunk):
        sta_lta.process(x[i : i + chunk], t0 + i / fs)
        for trig in sta_lta.triggers:
            if trig.t_off is None:
                open_markers.append(trig)
            else:
                finalised.append(trig)

    # Exactly one open marker per opened trigger — even though the burst
    # spans many chunks.
    assert len(open_markers) == 1, (
        f"expected one t_off=None marker, got {len(open_markers)}: {open_markers}"
    )
    # Eventually finalised once the ratio falls below off_threshold.
    assert len(finalised) >= 1, f"expected at least one finalised trigger, got {finalised}"
    # The marker's t_on matches the finalised trigger's t_on.
    assert open_markers[0].t_on == finalised[0].t_on
    # Finalised event has t_off > t_on.
    assert finalised[0].t_off is not None
    assert finalised[0].t_off > finalised[0].t_on


def test_chain_reset_clears_state() -> None:
    """After reset(), feeding the same input twice produces identical output."""
    sid = StreamID("X", "Y", "", "Z")
    chain = build_chain(
        stages=[
            DetrendStage(type="detrend", kind="constant"),
            BandpassStage(
                type="bandpass",
                freqmin=1.0,
                freqmax=10.0,
                corners=4,
                zerophase=False,
            ),
        ],
        fs_in=100.0,
        stream_id=sid,
        live=True,
    )
    rng = np.random.default_rng(seed=13)
    x = rng.standard_normal(200)

    out1 = chain.process(x, UTCDateTime(2026, 1, 1)).samples
    chain.reset()
    out2 = chain.process(x, UTCDateTime(2026, 1, 1)).samples

    np.testing.assert_allclose(out1, out2, atol=1e-9)
