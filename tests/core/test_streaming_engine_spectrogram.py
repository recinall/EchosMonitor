"""Engine-level integration tests for the M6 spectrogram pipeline.

Exercises the engine end-to-end: a real fake server pushes packets,
the engine routes them through ``_SpectrogramRouter`` (chain or no
chain), and the test asserts (a) ``spectrogramColumnReady`` fires with
the expected cadence, (b) ``read_recent`` returns sensible data, and
(c) the engine survives stop/start cleanly with the new clear barrier.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import numpy as np
import pytest
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, Qt, Slot

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
from echosmonitor.core.models import device_stream_key
from echosmonitor.core.streaming_engine import StreamingEngine
from tests.core.fakes import FakeSeedLinkServer
from tests.core.test_seedlink_worker import fake_server, loop_thread  # noqa: F401


def _make_root_cfg(devices: list[DeviceConfig]) -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=devices,
    )


class _SpecSpy(QObject):
    """Records every (device, nslc) tuple seen on
    :attr:`StreamingEngine.spectrogramColumnReady`."""

    def __init__(self, engine: StreamingEngine) -> None:
        super().__init__()
        self.columns: list[tuple[str, str]] = []
        engine.spectrogramColumnReady.connect(
            self._on_column, type=Qt.ConnectionType.DirectConnection
        )

    @Slot(str, str, object, object, object)
    def _on_column(
        self,
        device: str,
        nslc: str,
        column: object,
        freqs: object,
        t_end: object,
    ) -> None:
        del column, freqs, t_end
        self.columns.append((device, nslc))


def _wait_until(predicate, timeout_s: float, qtbot) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        qtbot.wait(50)
        if predicate():
            return True
    return False


@pytest.fixture
def engine_no_chain(
    qtbot,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> Iterator[tuple[StreamingEngine, _SpecSpy, str]]:
    """Engine with one device, NO DSP chain — spectrograms run on raw."""
    nslc = (
        f"{fake_server.config.network}.{fake_server.config.station}."
        f"{fake_server.config.location}.{fake_server.config.channel}"
    )
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="fake",
                host=fake_server.host,
                port=fake_server.port,
                reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
                selectors=[
                    StreamSelectorConfig(
                        network=fake_server.config.network,
                        station=fake_server.config.station,
                        location=fake_server.config.location,
                        channel=fake_server.config.channel,
                    )
                ],
            )
        ]
    )
    engine = StreamingEngine(cfg)
    spy = _SpecSpy(engine)
    engine.start()
    try:
        yield engine, spy, nslc
    finally:
        engine.stop()


@pytest.fixture
def engine_with_chain(
    qtbot,
    fake_server: FakeSeedLinkServer,  # noqa: F811
) -> Iterator[tuple[StreamingEngine, _SpecSpy, str]]:
    """Engine with one device whose chain ends in a Bandpass — the
    spectrogram source is the *processed* path."""
    nslc = (
        f"{fake_server.config.network}.{fake_server.config.station}."
        f"{fake_server.config.location}.{fake_server.config.channel}"
    )
    cfg = _make_root_cfg(
        devices=[
            DeviceConfig(
                name="fake",
                host=fake_server.host,
                port=fake_server.port,
                reconnect=ReconnectConfig(initial_delay_s=1.0, max_delay_s=60.0),
                selectors=[
                    StreamSelectorConfig(
                        network=fake_server.config.network,
                        station=fake_server.config.station,
                        location=fake_server.config.location,
                        channel=fake_server.config.channel,
                    )
                ],
                dsp_chain=[
                    DetrendStage(type="detrend", kind="constant"),
                    BandpassStage(
                        type="bandpass",
                        freqmin=1.0,
                        freqmax=10.0,
                        corners=4,
                        zerophase=False,
                    ),
                ],
            )
        ]
    )
    engine = StreamingEngine(cfg)
    spy = _SpecSpy(engine)
    engine.start()
    try:
        yield engine, spy, nslc
    finally:
        engine.stop()


def test_no_chain_emits_spectrogram_columns_for_raw_path(qtbot, engine_no_chain) -> None:
    """Without a chain, the engine still runs the spectrogram on the
    raw packets — every newStreamSeen is followed by columns landing
    on ``spectrogramColumnReady``."""
    _engine, spy, nslc = engine_no_chain
    assert _wait_until(
        lambda: any(d == "fake" and ns == nslc for d, ns in spy.columns),
        timeout_s=8.0,
        qtbot=qtbot,
    ), f"no spectrogramColumnReady emissions for fake/{nslc}; got {spy.columns}"


def test_chain_emits_spectrogram_columns_from_processed_path(qtbot, engine_with_chain) -> None:
    """A chain-installed stream still produces spectrogram columns —
    the source is the processed signal, but the wire shape is the same.
    """
    _engine, spy, nslc = engine_with_chain
    assert _wait_until(
        lambda: any(d == "fake" and ns == nslc for d, ns in spy.columns),
        timeout_s=8.0,
        qtbot=qtbot,
    ), f"no spectrogramColumnReady emissions under chain for fake/{nslc}"


def test_processed_spectrogram_feed_anchors_to_raw_wall_clock(qtbot) -> None:
    """The processed-path spectrogram feed MUST carry the stream's latest
    RAW wall-clock end time, never ``None``.

    Regression for the dock-spectrogram X-axis bug: passing ``None`` here
    made :class:`SpectrogramView` fall back to ``time.time()`` once per
    column. Within a burst the per-column wall clocks differ by only
    microseconds, so the inferred column step collapsed and the
    ``DateAxisItem`` rendered a meaningless sub-second slice
    (the user-visible "20.000 … 21.799" seconds-of-minute ticks). The
    fix mirrors the processed *trace*, which anchors to the raw stream's
    latest end time (``TracePlot._latest_processed_t = _latest_raw_t``).

    This asserts the engine's behaviour directly (no server / no timing):
    on the old code ``captured == [None]`` and the test fails.
    """
    engine = StreamingEngine(_make_root_cfg(devices=[]))
    captured: list[object] = []
    engine._spectrogramFeedRequested.connect(
        lambda _d, _n, _s, t_end: captured.append(t_end),
        type=Qt.ConnectionType.DirectConnection,
    )
    nslc = "NET.STA.00.HHZ"
    key = device_stream_key("fake", nslc)
    anchor = UTCDateTime("2026-06-01T16:04:20.5")
    engine._latest_raw_endtime[key] = anchor

    engine._on_processed_for_spec("fake", nslc, np.zeros(8, dtype=np.float32))

    assert captured == [anchor]
    assert captured[0] is not None


def test_read_recent_returns_recent_samples(qtbot, engine_no_chain) -> None:
    """``read_recent`` exposes the per-stream RingBuffer for downstream
    PSD/preview consumers (M6 stages 2-3)."""
    engine, _spy, nslc = engine_no_chain
    assert _wait_until(
        lambda: engine._buffer_for_test("fake", nslc) is not None,
        timeout_s=5.0,
        qtbot=qtbot,
    )
    # Wait for a couple of seconds of data.
    qtbot.wait(800)
    samples, fs, t_end = engine.read_recent("fake", nslc, seconds=0.5)
    assert isinstance(samples, np.ndarray)
    assert samples.dtype == np.float32
    assert fs > 0
    assert t_end is not None
    assert samples.size > 0


def test_read_recent_unknown_stream_is_empty(engine_no_chain) -> None:
    engine, _spy, _nslc = engine_no_chain
    samples, fs, t_end = engine.read_recent("nope", "NET.STA.LOC.HHZ", 1.0)
    assert samples.size == 0
    assert fs == 0.0
    assert t_end is None


def test_read_recent_zero_seconds_returns_empty(engine_no_chain) -> None:
    engine, _spy, nslc = engine_no_chain
    samples, fs, _t = engine.read_recent("fake", nslc, 0.0)
    assert samples.size == 0
    assert fs == 0.0


def test_spectrogram_state_preserved_across_device_stop_restart(qtbot, engine_no_chain) -> None:
    """``_stop_device`` MUST preserve spectrogram router state alongside
    the ring buffer / chain dict — otherwise a transient reconnect kills
    the spectrogram silently. Mirrors the buffers-preserved contract
    documented on ``_stop_device``.
    """
    engine, spy, nslc = engine_no_chain
    assert _wait_until(
        lambda: any(d == "fake" and ns == nslc for d, ns in spy.columns),
        timeout_s=8.0,
        qtbot=qtbot,
    ), "spectrograms never started"

    # Stop the device. The buffer / chain / spec router state survives.
    engine._stop_device("fake")
    qtbot.wait(200)

    # Restart and confirm new columns flow on the SAME (device, nslc).
    n_before = len(spy.columns)
    engine._start_device_by_name("fake")
    assert _wait_until(
        lambda: (
            len(spy.columns) > n_before
            and any(d == "fake" and ns == nslc for d, ns in spy.columns[n_before:])
        ),
        timeout_s=8.0,
        qtbot=qtbot,
    ), f"no spectrogram columns after restart; n_before={n_before}, now={len(spy.columns)}"
