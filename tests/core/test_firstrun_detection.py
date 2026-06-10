"""Truth-table tests for :func:`is_first_run` (M4 stage C).

The detection rule is the conjunction of two predicates:

* user config file does NOT exist on disk
* loaded config has zero devices

Either alone is the wrong rule:

* Empty-devices alone would re-trigger the wizard on every launch
  for sophisticated users who keep an empty config.
* Missing-file alone would suppress automated installs that drop a
  populated YAML before first launch.

This module pins all four cells of the truth table.
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


def _empty_root() -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(),
        devices=[],
    )


def _populated_root() -> RootConfig:
    return RootConfig(
        app=AppConfig(),
        ui=UiConfig(),
        devices=[
            DeviceConfig(
                name="iris",
                host="rtserve.iris.washington.edu",
                port=18000,
                selectors=[StreamSelectorConfig(network="IU", station="ANMO")],
            )
        ],
    )


def test_first_run_when_no_file_and_no_devices(tmp_path: Path) -> None:
    """Truly fresh install: file absent, config empty -> wizard."""
    missing = tmp_path / "config.yaml"
    assert is_first_run(missing, _empty_root()) is True


def test_not_first_run_when_file_exists_even_if_empty(tmp_path: Path) -> None:
    """User intentionally keeps an empty config -> NO wizard."""
    user = tmp_path / "config.yaml"
    user.write_text("devices: []\n", encoding="utf-8")
    assert is_first_run(user, _empty_root()) is False


def test_not_first_run_when_file_missing_but_devices_present(tmp_path: Path) -> None:
    """Automated install dropped a populated default -> NO wizard.

    Without the file-absent guard alone we'd trigger the wizard here;
    without the devices-empty guard alone we'd trigger it whenever the
    user deleted their YAML. Conjunction is the only correct rule.
    """
    missing = tmp_path / "config.yaml"
    assert is_first_run(missing, _populated_root()) is False


def test_not_first_run_when_file_exists_and_devices_present(tmp_path: Path) -> None:
    """Established install: NO wizard."""
    user = tmp_path / "config.yaml"
    user.write_text(
        "devices:\n  - name: iris\n    host: rtserve.iris.washington.edu\n",
        encoding="utf-8",
    )
    assert is_first_run(user, _populated_root()) is False
