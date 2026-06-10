"""Regression: the first-run wizard MUST NOT show on a populated config.

Pins the conjunction rule from :func:`is_first_run`: the wizard fires
only when BOTH the user config file is missing AND the loaded config
has zero devices. Loosening that to either-or would either silently
overwrite an automated install's pre-populated YAML or interrupt the
user on every launch when they keep an empty config.

The check is at the call site (``__main__.main``); we test the same
predicate that gates it.
"""

from __future__ import annotations

from pathlib import Path

from echosmonitor.config.schema import (
    AppConfig,
    DeviceConfig,
    RootConfig,
    StreamSelectorConfig,
    UiConfig,
)
from echosmonitor.core.firstrun import is_first_run


def _device(name: str = "iris") -> DeviceConfig:
    return DeviceConfig(
        name=name,
        host="rtserve.iris.washington.edu",
        port=18000,
        selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
    )


def test_wizard_skipped_when_user_config_exists_with_devices(tmp_path: Path) -> None:
    """Established install: file present, devices configured."""
    user = tmp_path / "config.yaml"
    user.write_text("# already set up\n", encoding="utf-8")
    root = RootConfig(
        app=AppConfig(),
        ui=UiConfig(),
        devices=[_device("iris")],
    )
    assert is_first_run(user, root) is False, (
        "wizard would have shown on a populated config — that's a regression"
    )


def test_wizard_skipped_when_user_config_exists_but_empty(tmp_path: Path) -> None:
    """User explicitly keeps an empty config (e.g. a CI-shaped install)."""
    user = tmp_path / "config.yaml"
    user.write_text("devices: []\n", encoding="utf-8")
    root = RootConfig(app=AppConfig(), ui=UiConfig(), devices=[])
    assert is_first_run(user, root) is False


def test_wizard_skipped_when_devices_present_even_without_file(tmp_path: Path) -> None:
    """Automated install dropped a populated default into the bundled path.

    The user's per-user file does not yet exist (the default would be
    used as-is). We must NOT show the wizard — that would lose the
    install-time configuration on the next save.
    """
    missing = tmp_path / "config.yaml"
    root = RootConfig(
        app=AppConfig(),
        ui=UiConfig(),
        devices=[_device("ops")],
    )
    assert is_first_run(missing, root) is False
