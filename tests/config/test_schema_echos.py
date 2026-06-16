"""Schema tests for the M1-B `echos:` device section (rule 15 surface).

Locks: defaults (`http_port: 80`, `poll_interval_s: 5.0`, no override),
bounds, `extra="forbid"` strictness, the None-means-generic-SeedLink
contract, and the commented example block shipped in ``default.yaml``
(same lockstep pattern as ``test_docs_yaml_contract.py``).
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from echosmonitor.config.schema import DeviceConfig, EchosDeviceConfig, PositionOverride

_DEVICE_KWARGS = {"name": "echos-field-01", "host": "echos.local"}


# Verbatim copy of the Echos example commented into the bundled
# ``config/default.yaml`` (with the position_override line uncommented so
# every field is exercised). If the YAML comment changes, update this in
# lockstep — test_bundled_default_yaml_contains_echos_example pins it.
_DEFAULT_YAML_ECHOS_BLOCK = """
- name: echos-field-01
  host: echos.local
  port: 18000
  echos:
    http_port: 80
    poll_interval_s: 5.0
    position_override: { lat: 45.4, lon: 11.9, elev_m: 20.0 }
"""


def test_device_without_echos_section_is_generic_seedlink() -> None:
    device = DeviceConfig(**_DEVICE_KWARGS)
    assert device.echos is None


def test_echos_section_defaults() -> None:
    device = DeviceConfig(**_DEVICE_KWARGS, echos=EchosDeviceConfig())
    assert device.echos is not None
    assert device.echos.http_port == 80
    assert device.echos.poll_interval_s == 5.0
    assert device.echos.poll_interval_streaming_s == 30.0  # M6.6-C
    assert device.echos.position_override is None


def test_position_override_roundtrip() -> None:
    override = PositionOverride(lat=45.4, lon=11.9, elev_m=20.0)
    config = EchosDeviceConfig(position_override=override)
    assert config.position_override == override
    assert config.position_override.elev_m == 20.0


@pytest.mark.parametrize(
    "field, value",
    [
        ("http_port", 0),
        ("http_port", 65536),
        ("poll_interval_s", 0.5),
        ("poll_interval_s", 3601.0),
        ("poll_interval_streaming_s", 0.5),
        ("poll_interval_streaming_s", 3601.0),
    ],
)
def test_echos_bounds_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        EchosDeviceConfig(**{field: value})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lat": 90.5, "lon": 0.0},
        {"lat": 0.0, "lon": -180.5},
        {"lat": 0.0, "lon": 0.0, "elev_m": 9500.0},
    ],
)
def test_position_override_bounds_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        PositionOverride(**kwargs)


def test_echos_section_is_strict() -> None:
    # extra="forbid": the admin password must not sneak into the YAML
    # under any key (rule 15) — unknown keys fail validation loudly.
    with pytest.raises(ValidationError):
        EchosDeviceConfig.model_validate({"http_port": 80, "password": "nope"})


def test_default_yaml_echos_example_block_parses() -> None:
    blocks = yaml.safe_load(_DEFAULT_YAML_ECHOS_BLOCK)
    assert isinstance(blocks, list) and len(blocks) == 1
    device = DeviceConfig(**blocks[0])
    assert device.name == "echos-field-01"
    assert device.echos is not None
    assert device.echos.http_port == 80
    assert device.echos.position_override is not None
    assert device.echos.position_override.lat == 45.4


def test_bundled_default_yaml_contains_echos_example() -> None:
    from importlib.resources import files

    text = files("echosmonitor.config").joinpath("default.yaml").read_text(encoding="utf-8")
    assert "name: echos-field-01" in text
    assert "host: echos.local" in text
    assert "http_port: 80" in text
    assert "poll_interval_s: 5.0" in text
    assert "position_override: { lat: 45.4, lon: 11.9, elev_m: 20.0 }" in text
    # The YAML must never gain a password-shaped key in the echos section.
    assert "password" not in text.replace("admin password", "")
