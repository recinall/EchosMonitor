"""Pure-function tests for :func:`diff_devices` (M4 stage B).

Twelve cases covering the truth table the design plan specifies:

* empty -> empty
* add only / remove only / rename
* host change / port change / connect_timeout change / max_delay change
* selectors reordered (no diff) / selectors changed
* dsp_chain changed only / chain reordered
* both selectors AND chain changed -> restart only

No fixtures, no qtbot, no I/O — just calls into the pure helper.
"""

from __future__ import annotations

from echosmonitor.config.schema import (
    BandpassStage,
    DetrendStage,
    DeviceConfig,
    ReconnectConfig,
    StreamSelectorConfig,
)
from echosmonitor.core.config_diff import diff_devices


def _device(
    name: str = "iris",
    host: str = "rtserve.iris.washington.edu",
    port: int = 18000,
    selectors: list[StreamSelectorConfig] | None = None,
    dsp_chain: list[object] | None = None,
    reconnect: ReconnectConfig | None = None,
) -> DeviceConfig:
    return DeviceConfig(
        name=name,
        host=host,
        port=port,
        selectors=selectors or [StreamSelectorConfig(network="IU", station="ANMO")],
        dsp_chain=dsp_chain or [],  # type: ignore[arg-type]
        reconnect=reconnect or ReconnectConfig(),
    )


def test_empty_to_empty_is_empty_diff() -> None:
    diff = diff_devices([], [])
    assert diff.is_empty
    assert diff.added == ()
    assert diff.removed == ()
    assert diff.restart == ()
    assert diff.chain_only == ()


def test_add_only() -> None:
    diff = diff_devices([], [_device("iris")])
    assert [d.name for d in diff.added] == ["iris"]
    assert diff.removed == ()
    assert diff.restart == ()
    assert diff.chain_only == ()


def test_remove_only() -> None:
    diff = diff_devices([_device("iris")], [])
    assert diff.added == ()
    assert list(diff.removed) == ["iris"]
    assert diff.restart == ()
    assert diff.chain_only == ()


def test_rename_is_one_added_plus_one_removed() -> None:
    """Renaming a device is structurally a delete + add — there's no
    cheap migration path for per-stream state across a name change."""
    old = [_device("iris")]
    new = [_device("iris-renamed")]
    diff = diff_devices(old, new)
    assert [d.name for d in diff.added] == ["iris-renamed"]
    assert list(diff.removed) == ["iris"]
    assert diff.restart == ()
    assert diff.chain_only == ()


def test_host_change_triggers_restart() -> None:
    old = [_device("iris", host="a.example.com")]
    new = [_device("iris", host="b.example.com")]
    diff = diff_devices(old, new)
    assert diff.added == ()
    assert diff.removed == ()
    assert [d.host for d in diff.restart] == ["b.example.com"]
    assert diff.chain_only == ()


def test_port_change_triggers_restart() -> None:
    old = [_device("iris", port=18000)]
    new = [_device("iris", port=18001)]
    diff = diff_devices(old, new)
    assert [d.port for d in diff.restart] == [18001]
    assert diff.chain_only == ()


def test_selectors_reordered_same_set_is_no_diff() -> None:
    sel_a = StreamSelectorConfig(network="IU", station="ANMO")
    sel_b = StreamSelectorConfig(network="IU", station="COLA")
    old = [_device("iris", selectors=[sel_a, sel_b])]
    new = [_device("iris", selectors=[sel_b, sel_a])]
    diff = diff_devices(old, new)
    assert diff.is_empty


def test_selectors_changed_triggers_restart() -> None:
    sel_a = StreamSelectorConfig(network="IU", station="ANMO")
    sel_b = StreamSelectorConfig(network="IU", station="COLA")
    old = [_device("iris", selectors=[sel_a])]
    new = [_device("iris", selectors=[sel_a, sel_b])]
    diff = diff_devices(old, new)
    assert [d.name for d in diff.restart] == ["iris"]
    assert diff.chain_only == ()


def test_dsp_chain_changed_only_is_chain_only() -> None:
    chain_a = [DetrendStage(type="detrend")]
    chain_b = [
        DetrendStage(type="detrend"),
        BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0),
    ]
    old = [_device("iris", dsp_chain=chain_a)]  # type: ignore[arg-type]
    new = [_device("iris", dsp_chain=chain_b)]  # type: ignore[arg-type]
    diff = diff_devices(old, new)
    assert diff.added == ()
    assert diff.removed == ()
    assert diff.restart == ()
    assert [d.name for d in diff.chain_only] == ["iris"]


def test_both_selectors_and_chain_changed_is_restart_only() -> None:
    """Restart implicitly reinstalls the chain, so listing in chain_only
    too would just cause duplicate work."""
    sel_a = StreamSelectorConfig(network="IU", station="ANMO")
    sel_b = StreamSelectorConfig(network="IU", station="COLA")
    chain_a = [DetrendStage(type="detrend")]
    chain_b = [BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)]
    old = [_device("iris", selectors=[sel_a], dsp_chain=chain_a)]  # type: ignore[arg-type]
    new = [_device("iris", selectors=[sel_b], dsp_chain=chain_b)]  # type: ignore[arg-type]
    diff = diff_devices(old, new)
    assert [d.name for d in diff.restart] == ["iris"]
    assert diff.chain_only == ()


def test_reconnect_connect_timeout_change_triggers_restart() -> None:
    old = [_device("iris", reconnect=ReconnectConfig(connect_timeout_s=10.0))]
    new = [_device("iris", reconnect=ReconnectConfig(connect_timeout_s=15.0))]
    diff = diff_devices(old, new)
    assert [d.name for d in diff.restart] == ["iris"]


def test_reconnect_max_delay_change_triggers_restart() -> None:
    old = [_device("iris", reconnect=ReconnectConfig(max_delay_s=60.0))]
    new = [_device("iris", reconnect=ReconnectConfig(max_delay_s=120.0))]
    diff = diff_devices(old, new)
    assert [d.name for d in diff.restart] == ["iris"]


def test_chain_reordered_same_stages_is_chain_only() -> None:
    """Chain order is semantic: a detrend after a bandpass is not the
    same chain as a detrend before it."""
    detrend = DetrendStage(type="detrend")
    bandpass = BandpassStage(type="bandpass", freqmin=1.0, freqmax=10.0)
    old = [_device("iris", dsp_chain=[detrend, bandpass])]  # type: ignore[arg-type]
    new = [_device("iris", dsp_chain=[bandpass, detrend])]  # type: ignore[arg-type]
    diff = diff_devices(old, new)
    assert diff.added == ()
    assert diff.removed == ()
    assert diff.restart == ()
    assert [d.name for d in diff.chain_only] == ["iris"]
