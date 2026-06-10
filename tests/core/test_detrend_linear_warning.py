"""The 'linear detrend in a live chain' warning fires once per stream
per session — not once per stage instance.

Regression for the log-spam bug: the warn-once flag used to live on the
``Detrend`` stage and reset on every chain rebuild, so a config tweak
(chain hot-reload) or a reconnect re-installed the chain → fresh stage
→ flag reset → the warning fired again. The guard now lives on the
engine, keyed by ``(device, nslc)`` for the session, and the warning
carries the device + nslc so the user knows which stream it names.
"""

from __future__ import annotations

from echosmonitor.config.schema import (
    AppConfig,
    BandpassStage,
    DetrendStage,
    DeviceConfig,
    ReconnectConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.models import StreamID
from echosmonitor.core.streaming_engine import StreamingEngine

_NSLC = "NET.STA.00.HHZ"
_SID = StreamID(network="NET", station="STA", location="00", channel="HHZ")
_FS = 100.0
_WARN_EVENT = "dsp_detrend_linear_in_live_chain"


def _engine_with_chain(kind: str) -> StreamingEngine:
    cfg = RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name="dev",
                host="localhost",
                port=18000,
                reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
                selectors=[
                    StreamSelectorConfig(network="NET", station="STA", location="00", channel="HHZ")
                ],
                dsp_chain=[
                    DetrendStage(type="detrend", kind=kind),
                    BandpassStage(
                        type="bandpass",
                        freqmin=1.0,
                        freqmax=10.0,
                        corners=4,
                        zerophase=False,
                    ),
                ],
            )
        ],
    )
    return StreamingEngine(cfg)


def _warns(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("event") == _WARN_EVENT]


def test_linear_detrend_warns_once_per_session_across_reinstalls(qtbot, capture_structlog) -> None:
    """Three chain installs for the SAME stream within one engine session
    emit the warning exactly once (the chain-reinstall spam scenario)."""
    engine = _engine_with_chain("linear")
    for _ in range(3):
        engine._maybe_install_chain("dev", _NSLC, _SID, _FS)

    warns = _warns(capture_structlog)
    assert len(warns) == 1, f"expected one warning, got {len(warns)}"
    # The warning names the stream so the user knows which one it is.
    assert warns[0]["device"] == "dev"
    assert warns[0]["nslc"] == _NSLC
    assert warns[0]["log_level"] == "warning"


def test_linear_detrend_warns_again_in_a_fresh_session(qtbot, capture_structlog) -> None:
    """A fresh engine (app restart) starts with an empty guard set and
    reminds the user once more — acceptable per the per-session scope."""
    _engine_with_chain("linear")._maybe_install_chain("dev", _NSLC, _SID, _FS)
    _engine_with_chain("linear")._maybe_install_chain("dev", _NSLC, _SID, _FS)
    assert len(_warns(capture_structlog)) == 2


def test_constant_detrend_never_warns(qtbot, capture_structlog) -> None:
    """The live-safe ``constant`` kind is silent no matter how many times
    the chain is installed."""
    engine = _engine_with_chain("constant")
    for _ in range(3):
        engine._maybe_install_chain("dev", _NSLC, _SID, _FS)
    assert _warns(capture_structlog) == []


def test_demean_detrend_never_warns(qtbot, capture_structlog) -> None:
    """``demean`` is a schema alias for ``constant`` and must NOT warn.

    The remap ``demean → constant`` happens only in ``Detrend.__init__``;
    at config level ``DetrendStage.kind`` stays the literal ``"demean"``.
    The engine's guard matches ``kind == "linear"`` against the config, so
    ``demean`` is silent — this locks that config-vs-runtime distinction."""
    engine = _engine_with_chain("demean")
    for _ in range(3):
        engine._maybe_install_chain("dev", _NSLC, _SID, _FS)
    assert _warns(capture_structlog) == []


def test_two_streams_each_warn_once(qtbot, capture_structlog) -> None:
    """The guard is per (device, nslc): a second stream on the same device
    still gets its own one-time reminder."""
    engine = _engine_with_chain("linear")
    other_nslc = "NET.STA.00.HHN"
    engine._maybe_install_chain("dev", _NSLC, _SID, _FS)
    engine._maybe_install_chain("dev", _NSLC, _SID, _FS)
    engine._maybe_install_chain("dev", other_nslc, StreamID("NET", "STA", "00", "HHN"), _FS)
    warns = _warns(capture_structlog)
    assert len(warns) == 2
    assert {w["nslc"] for w in warns} == {_NSLC, other_nslc}
