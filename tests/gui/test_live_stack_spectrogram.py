"""Tests for LiveStack's M6 spectrogram pane integration.

Focuses on the per-device toggle (decision 3 in the plan), default-ON
heuristic (frequency-altering chain implies user cares about
spectrogram), and column dispatch into the matching SpectrogramView.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QSettings

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
from echosmonitor.gui.widgets.live_stack import LiveStack


def _cfg_with_device(name: str, *, with_bandpass: bool) -> RootConfig:
    chain: list = []
    if with_bandpass:
        chain = [
            DetrendStage(type="detrend", kind="constant"),
            BandpassStage(
                type="bandpass",
                freqmin=1.0,
                freqmax=10.0,
                corners=4,
                zerophase=False,
            ),
        ]
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(refresh_hz=20, default_window_seconds=10),
        devices=[
            DeviceConfig(
                name=name,
                host="127.0.0.1",
                port=18000,
                reconnect=ReconnectConfig(),
                selectors=[StreamSelectorConfig()],
                dsp_chain=chain,
            )
        ],
    )


def test_add_stream_creates_spectrogram_pane(qtbot) -> None:
    cfg = _cfg_with_device("dev", with_bandpass=True)
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    stack.add_stream("dev", "N.S.L.HHZ", fs=100.0)
    spec = stack.spec_view_for("dev", "N.S.L.HHZ")
    assert spec is not None


def test_default_on_for_filtering_chain(qtbot) -> None:
    """A chain with a bandpass means the user wants frequency-content
    visibility — default the spectrogram pane to visible."""
    QSettings().clear()  # Wipe any prior persisted toggle from another test.
    cfg = _cfg_with_device("dev-fp", with_bandpass=True)
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    stack.add_stream("dev-fp", "N.S.L.HHZ", fs=100.0)
    group = stack._device_group_for_test("dev-fp")
    assert group is not None
    assert group.is_spec_visible() is True


def test_default_off_for_no_filter(qtbot) -> None:
    QSettings().clear()
    cfg = _cfg_with_device("dev-noflt", with_bandpass=False)
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    stack.add_stream("dev-noflt", "N.S.L.HHZ", fs=100.0)
    group = stack._device_group_for_test("dev-noflt")
    assert group is not None
    assert group.is_spec_visible() is False


def test_qsettings_persists_user_toggle_choice(qtbot) -> None:
    QSettings().clear()
    cfg = _cfg_with_device("dev-qs", with_bandpass=False)
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    stack.add_stream("dev-qs", "N.S.L.HHZ", fs=100.0)
    group = stack._device_group_for_test("dev-qs")
    assert group is not None

    # User flips the toggle on. The handler writes through to QSettings.
    group.set_spec_visible(True)
    group._on_spec_toggled(True)  # ensure the persistence path runs

    # New stack picks up the persisted choice over the default-OFF.
    stack2 = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack2)
    stack2.add_stream("dev-qs", "N.S.L.HHZ", fs=100.0)
    group2 = stack2._device_group_for_test("dev-qs")
    assert group2 is not None
    assert group2.is_spec_visible() is True


def test_on_spectrogram_column_routes_to_matching_view(qtbot) -> None:
    cfg = _cfg_with_device("dev", with_bandpass=True)
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    stack.add_stream("dev", "N.S.L.HHZ", fs=100.0)
    spec = stack.spec_view_for("dev", "N.S.L.HHZ")
    col = np.linspace(0.0, 1.0, 65, dtype=np.float32)
    freqs = np.linspace(0.0, 50.0, 65, dtype=np.float32)
    stack.on_spectrogram_column("dev", "N.S.L.HHZ", col, freqs, None)
    assert spec is not None
    assert spec._column_count == 1


def test_on_spectrogram_column_unknown_stream_is_silent(qtbot) -> None:
    cfg = _cfg_with_device("dev", with_bandpass=True)
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    col = np.linspace(0.0, 1.0, 65, dtype=np.float32)
    freqs = np.linspace(0.0, 50.0, 65, dtype=np.float32)
    # Must not raise.
    stack.on_spectrogram_column("dev", "N.S.L.HHZ", col, freqs, None)


def test_update_processed_meta_resets_spectrogram(qtbot) -> None:
    cfg = _cfg_with_device("dev", with_bandpass=True)
    stack = LiveStack(window_seconds=10.0, cfg=cfg)
    qtbot.addWidget(stack)
    stack.add_stream("dev", "N.S.L.HHZ", fs=100.0)
    spec = stack.spec_view_for("dev", "N.S.L.HHZ")
    col = np.linspace(0.0, 1.0, 65, dtype=np.float32)
    freqs = np.linspace(0.0, 50.0, 65, dtype=np.float32)
    stack.on_spectrogram_column("dev", "N.S.L.HHZ", col, freqs, None)
    stack.update_processed_meta("dev", "N.S.L.HHZ", fs_out=50.0)
    assert spec is not None
    assert spec._fs == 50.0
    # The column buffer is cleared so the new fs's frequency axis takes over.
    assert spec._column_count == 0
