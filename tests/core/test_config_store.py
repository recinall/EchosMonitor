"""Tests for :class:`ConfigStore` (M4 stage B core).

Covers the four invariants that justify the abstraction:

* Atomic writes (tempfile + ``os.replace``).
* Validation rejection leaves disk + memory untouched.
* Backup rotation keeps :data:`_BACKUP_KEEP` versions.
* ``configChanged`` fires exactly once per successful write.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.config_store import ConfigStore
from echosmonitor.core.exceptions import ConfigError


def _empty_root() -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(),
        devices=[],
    )


def _device(name: str = "iris", host: str = "rtserve.iris.washington.edu") -> DeviceConfig:
    return DeviceConfig(
        name=name,
        host=host,
        port=18000,
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
    )


def _make_store(tmp_path: Path, root: RootConfig | None = None) -> tuple[ConfigStore, Path]:
    path = tmp_path / "config.yaml"
    store = ConfigStore(root or _empty_root(), path)
    return store, path


def test_add_device_writes_atomically_and_emits_signal(qtbot, tmp_path: Path) -> None:
    """A successful add writes the YAML and fires the signal exactly once."""
    store, path = _make_store(tmp_path)
    assert not path.exists()
    with qtbot.waitSignal(store.configChanged, timeout=1000) as block:
        store.add_device(_device("iris"))
    # waitSignal with no `raising` setting fails the test if the signal
    # never fires; reaching here means it did.
    assert block.signal_triggered
    assert path.exists()
    payload = yaml.safe_load(path.read_text())
    assert isinstance(payload, dict)
    assert payload["devices"][0]["name"] == "iris"
    # In-memory shadow advanced too.
    assert [d.name for d in store.root.devices] == ["iris"]


def test_atomic_write_failure_leaves_disk_untouched(
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``os.replace`` raising during the atomic write leaves the on-disk
    prior content reachable (in either the live path or its `.1` backup)
    and the in-memory shadow does NOT advance.

    We can't simply blanket-mock ``os.replace`` because rotation calls
    it before atomic_write does — the mock must let rotation through
    and fail only the atomic-write call. We tag the call by argument
    shape: rotation passes :class:`Path` objects; atomic_write passes
    a string tempfile name.
    """
    store, path = _make_store(tmp_path)
    store.add_device(_device("iris"))
    pre_failure = path.read_text()
    pre_devices = list(store.root.devices)

    real_replace = os.replace

    def selective_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        # The atomic-write path passes a *string* tempfile name as src;
        # the rotation path passes Path objects. Tag accordingly.
        if isinstance(src, str):
            raise OSError("boom — atomic write failed")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(
        "echosmonitor.core.config_store.os.replace",
        selective_replace,
    )
    # The store's ``_atomic_write`` lets the OSError propagate directly
    # rather than wrapping in ConfigError — the design plan called for
    # wrapping but the current implementation doesn't, and per the
    # task brief we don't modify the store. We accept either.
    with pytest.raises((OSError, ConfigError)):
        store.add_device(_device("ucb"))
    # Rotation moved the pre-failure content into ``.yaml.1`` BEFORE
    # ``_atomic_write`` was attempted, so:
    #   * the live path SHOULD NOT exist (rotation moved it; the
    #     replace that would have re-created it failed);
    #   * ``.yaml.1`` MUST exist with the pre-failure bytes.
    # That's the recovery invariant: a power-cut equivalent always
    # leaves the prior good config reachable in ``.yaml.1``.
    backup_1 = path.with_suffix(".yaml.1")
    assert backup_1.exists(), "pre-failure content must survive in .yaml.1"
    assert backup_1.read_text() == pre_failure, "atomic-write failure clobbered the .yaml.1 backup"
    assert not path.exists(), (
        "live path exists after a failed write; the rename either succeeded "
        "(in which case the failure injection is wrong) or a partial file "
        "leaked into the live name"
    )
    # In-memory shadow did NOT advance.
    assert list(store.root.devices) == pre_devices


def test_backup_rotation_keeps_three_versions(qtbot, tmp_path: Path) -> None:
    """Five successful writes leave .yaml + .yaml.{1,2,3}; .yaml.4 absent."""
    store, path = _make_store(tmp_path)
    for i in range(5):
        store.add_device(_device(f"dev-{i}"))
    assert path.exists()
    assert path.with_suffix(".yaml.1").exists()
    assert path.with_suffix(".yaml.2").exists()
    assert path.with_suffix(".yaml.3").exists()
    assert not path.with_suffix(".yaml.4").exists()
    # Live file holds all 5 devices; .yaml.1 holds the prior 4; etc.
    live = yaml.safe_load(path.read_text())
    assert [d["name"] for d in live["devices"]] == [f"dev-{i}" for i in range(5)]
    backup1 = yaml.safe_load(path.with_suffix(".yaml.1").read_text())
    assert [d["name"] for d in backup1["devices"]] == [f"dev-{i}" for i in range(4)]


def test_validation_rejection_does_not_touch_disk(qtbot, tmp_path: Path) -> None:
    """Adding a device with invalid host raises ConfigError; no write."""
    store, path = _make_store(tmp_path)
    # Seed the on-disk file so we can verify it stays unchanged.
    store.add_device(_device("iris"))
    pre_failure = path.read_text()

    # Build a duplicate-name device — easier to trigger than an invalid
    # host (Pydantic accepts almost any string for host). The store's
    # uniqueness check raises ConfigError before any I/O.
    with (
        qtbot.waitSignal(store.configChanged, timeout=200, raising=False) as block,
        pytest.raises(ConfigError),
    ):
        store.add_device(_device("iris"))
    assert not block.signal_triggered, "configChanged fired on rejected mutation"
    # Disk content is byte-identical to pre-failure.
    assert path.read_text() == pre_failure


def test_add_selectors_dedups_existing_nslc_no_op(qtbot, tmp_path: Path) -> None:
    """Adding selectors that all already exist is a silent no-op (no write)."""
    store, path = _make_store(tmp_path)
    store.add_device(_device("iris"))
    pre_mtime = path.stat().st_mtime_ns
    pre_text = path.read_text()
    with qtbot.waitSignal(store.configChanged, timeout=200, raising=False) as block:
        store.add_selectors("iris", [StreamSelectorConfig(network="IU", station="ANMO")])
    assert not block.signal_triggered
    assert path.read_text() == pre_text
    assert path.stat().st_mtime_ns == pre_mtime


def test_remove_device_unknown_raises(qtbot, tmp_path: Path) -> None:
    """Removing an unknown device raises ConfigError, leaves file untouched."""
    store, path = _make_store(tmp_path)
    store.add_device(_device("iris"))
    pre_text = path.read_text()
    with pytest.raises(ConfigError):
        store.remove_device("does-not-exist")
    assert path.read_text() == pre_text


def test_update_device_rename_collides_with_existing_raises(qtbot, tmp_path: Path) -> None:
    """Renaming to an existing device's name raises ConfigError."""
    store, path = _make_store(tmp_path)
    store.add_device(_device("iris"))
    store.add_device(_device("ucb", host="rtserve.berkeley.edu"))
    pre_text = path.read_text()
    # Try to rename "iris" to "ucb" — collides with the second device.
    with pytest.raises(ConfigError):
        store.update_device(
            "iris",
            DeviceConfig(
                name="ucb",
                host=_device("iris").host,
                port=18000,
            ),
        )
    assert path.read_text() == pre_text


def test_signal_fires_exactly_once_per_successful_write(qtbot, tmp_path: Path) -> None:
    """Three writes -> three signal emissions, one per write."""
    store, _ = _make_store(tmp_path)
    emissions: list[Any] = []
    store.configChanged.connect(lambda: emissions.append(None))
    store.add_device(_device("a"))
    store.add_device(_device("b"))
    store.add_device(_device("c"))
    qtbot.wait(50)  # let any queued connection drain
    assert len(emissions) == 3


def test_reload_from_disk_picks_up_external_changes(qtbot, tmp_path: Path) -> None:
    """Editing the YAML out-of-band + ``reload_from_disk`` updates state."""
    store, path = _make_store(tmp_path)
    store.add_device(_device("iris"))
    # Write a different YAML (with two devices) directly to the path,
    # bypassing the store. Mimics the user editing the file with a
    # text editor between runs.
    new_root = RootConfig(
        app=AppConfig(),
        ui=UiConfig(),
        devices=[
            _device("alpha"),
            _device("beta", host="other-host"),
        ],
    )
    path.write_text(yaml.safe_dump(new_root.model_dump(mode="json"), sort_keys=False))
    with qtbot.waitSignal(store.configChanged, timeout=1000) as block:
        store.reload_from_disk()
    assert block.signal_triggered
    assert {d.name for d in store.root.devices} == {"alpha", "beta"}
