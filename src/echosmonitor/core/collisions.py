"""Detect when two configured devices would produce the same SEED NSLC.

Pure: no Qt, no I/O, no global state. Two devices whose selectors overlap
on a concrete SEED station can emit identical NSLCs; before the SDS tree
was namespaced per device this collapsed both devices' archive files and
index rows together (extent/coverage collapse). The streaming engine logs
these as warnings at start-up so the operator can rename or re-target a
device. The device-namespaced SDS layout already prevents the corruption;
this is the informational catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from echosmonitor.config.schema import DeviceConfig, StreamSelectorConfig

_WILDCARD = "*"


@dataclass(frozen=True)
class NslcCollision:
    """One SEED NSLC produced by two or more devices.

    Attributes:
        nslc: The representative ``NET.STA.LOC.CHA`` (concrete field where
            one side is concrete, else ``*``).
        devices: The colliding device names, sorted.
    """

    nslc: str
    devices: tuple[str, ...]


def _fields_overlap(a: str, b: str) -> bool:
    """Two selector field values overlap iff equal or either is ``*``."""
    return a == b or _WILDCARD in (a, b)


def _representative_field(a: str, b: str) -> str:
    """Concrete value where one side is concrete, else ``*``."""
    if a != _WILDCARD:
        return a
    if b != _WILDCARD:
        return b
    return _WILDCARD


def _selectors_collide(
    a: StreamSelectorConfig,
    b: StreamSelectorConfig,
) -> str | None:
    """Return the representative NSLC if ``a`` and ``b`` collide, else ``None``.

    A collision requires all four fields to overlap AND the station field
    to be concrete (non-``*``) and equal on both sides — this suppresses
    the trivial all-wildcard ``*.*.*.*`` case where any two devices would
    "collide" with no real shared concrete stream.
    """
    if a.station == _WILDCARD or a.station != b.station:
        return None
    if not (
        _fields_overlap(a.network, b.network)
        and _fields_overlap(a.location, b.location)
        and _fields_overlap(a.channel, b.channel)
    ):
        return None
    net = _representative_field(a.network, b.network)
    loc = _representative_field(a.location, b.location)
    cha = _representative_field(a.channel, b.channel)
    return f"{net}.{a.station}.{loc}.{cha}"


def find_nslc_collisions(devices: Iterable[DeviceConfig]) -> list[NslcCollision]:
    """Find NSLCs that two or more devices would both produce.

    For every pair of devices and every pair of their selectors, test the
    overlap rule (see :func:`_selectors_collide`). Devices are grouped by
    the representative NSLC of each colliding pair; an
    :class:`NslcCollision` is emitted for each NSLC shared by at least two
    devices.

    Args:
        devices: The configured devices to check.

    Returns:
        One :class:`NslcCollision` per shared NSLC, devices sorted, the
        list sorted by ``nslc``.
    """
    device_list = list(devices)
    nslc_to_devices: dict[str, set[str]] = {}
    for i in range(len(device_list)):
        for j in range(i + 1, len(device_list)):
            dev_a = device_list[i]
            dev_b = device_list[j]
            for sel_a in dev_a.selectors:
                for sel_b in dev_b.selectors:
                    nslc = _selectors_collide(sel_a, sel_b)
                    if nslc is None:
                        continue
                    bucket = nslc_to_devices.setdefault(nslc, set())
                    bucket.add(dev_a.name)
                    bucket.add(dev_b.name)
    collisions = [
        NslcCollision(nslc=nslc, devices=tuple(sorted(names)))
        for nslc, names in nslc_to_devices.items()
        if len(names) >= 2
    ]
    collisions.sort(key=lambda c: c.nslc)
    return collisions
