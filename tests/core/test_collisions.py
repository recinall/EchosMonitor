"""Config-time NSLC-collision detection."""

from __future__ import annotations

from echosmonitor.config.schema import DeviceConfig, StreamSelectorConfig
from echosmonitor.core.collisions import NslcCollision, find_nslc_collisions


def _device(name: str, **sel: str) -> DeviceConfig:
    selector = StreamSelectorConfig(**sel)
    return DeviceConfig(name=name, host="h", port=18000, selectors=[selector])


def test_fires_for_identical_concrete_selectors() -> None:
    a = _device("Echos", network="XX", station="ECHOS", location="00", channel="HHZ")
    b = _device("Echos_WK", network="XX", station="ECHOS", location="00", channel="HHZ")

    collisions = find_nslc_collisions([a, b])

    assert collisions == [
        NslcCollision(nslc="XX.ECHOS.00.HHZ", devices=("Echos", "Echos_WK")),
    ]
    # devices sorted, both names present.
    assert collisions[0].devices == tuple(sorted(("Echos_WK", "Echos")))


def test_does_not_fire_for_distinct_stations() -> None:
    a = _device("A", network="XX", station="ALPHA", location="00", channel="HHZ")
    b = _device("B", network="XX", station="BRAVO", location="00", channel="HHZ")

    assert find_nslc_collisions([a, b]) == []


def test_does_not_fire_for_all_wildcard_selectors() -> None:
    # Two ``*.*.*.*`` devices would "overlap" trivially; the concrete-station
    # requirement suppresses this false positive.
    a = _device("A")  # all fields default to "*"
    b = _device("B")

    assert find_nslc_collisions([a, b]) == []


def test_wildcard_field_overlap_yields_representative_nslc() -> None:
    # Same concrete station, one side wildcards loc/cha — representative
    # NSLC carries the concrete value where one side has it.
    a = _device("A", network="XX", station="ECHOS", location="00", channel="HHZ")
    b = _device("B", network="XX", station="ECHOS", location="*", channel="*")

    collisions = find_nslc_collisions([a, b])
    assert collisions == [
        NslcCollision(nslc="XX.ECHOS.00.HHZ", devices=("A", "B")),
    ]


def test_single_device_never_collides() -> None:
    a = _device("Solo", network="XX", station="ECHOS", location="00", channel="HHZ")
    assert find_nslc_collisions([a]) == []
