"""Schema-level tests for the M5 archive configuration surface.

The archive block is loaded at config time but the writer enforces
``encoding`` ↔ dtype compatibility at write time (we don't know the
dtype until the first packet). These tests lock the *static* contract:
defaults, range bounds, and the existence of the per-device override
plus the top-level ``app.archive_root`` fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from echosmonitor.config.schema import (
    AppConfig,
    ArchiveConfig,
    DeviceConfig,
    RootConfig,
)


def test_archive_config_defaults() -> None:
    cfg = ArchiveConfig()
    assert cfg.enabled is False
    assert cfg.format == "mseed_sds"
    assert cfg.root_dir is None
    assert cfg.encoding == "STEIM2"
    assert cfg.record_length == 512
    assert cfg.max_open_files == 32
    assert cfg.fsync_interval_s == 5.0
    assert cfg.queue_max == 1024


def test_archive_config_accepts_overrides() -> None:
    cfg = ArchiveConfig(
        enabled=True,
        root_dir=Path("/var/lib/echos/archive"),
        encoding="FLOAT32",
        record_length=4096,
        max_open_files=128,
        fsync_interval_s=1.0,
        queue_max=4096,
    )
    assert cfg.enabled is True
    assert cfg.root_dir == Path("/var/lib/echos/archive")
    assert cfg.encoding == "FLOAT32"
    assert cfg.record_length == 4096
    assert cfg.max_open_files == 128
    assert cfg.fsync_interval_s == 1.0
    assert cfg.queue_max == 4096


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("encoding", "STEIM3"),
        ("encoding", "INT32"),
        ("record_length", 100),
        ("record_length", 8192),
        ("max_open_files", 0),
        ("max_open_files", 2048),
        ("fsync_interval_s", 0.0),
        ("fsync_interval_s", 120.0),
        ("queue_max", 0),
        ("queue_max", 5_000_000),
    ],
)
def test_archive_config_rejects_out_of_range(field: str, bad_value: object) -> None:
    with pytest.raises(ValidationError):
        ArchiveConfig(**{field: bad_value})


def test_archive_config_is_frozen() -> None:
    """Same _Base contract as every other config model."""
    cfg = ArchiveConfig()
    with pytest.raises(ValidationError):
        cfg.enabled = True  # type: ignore[misc]


def test_app_config_archive_root_default() -> None:
    """Top-level ``archive_root`` defaults to None — engine resolves at start."""
    app = AppConfig()
    assert app.archive_root is None


def test_app_config_archive_root_accepts_path() -> None:
    app = AppConfig(archive_root=Path("/tmp/archive"))
    assert app.archive_root == Path("/tmp/archive")


def test_device_config_archive_default_factory() -> None:
    """Every DeviceConfig has its own ArchiveConfig instance, defaulted."""
    dev = DeviceConfig(name="x", host="example.com")
    assert isinstance(dev.archive, ArchiveConfig)
    assert dev.archive.enabled is False


def test_device_config_archive_override() -> None:
    dev = DeviceConfig(
        name="x",
        host="example.com",
        archive={"enabled": True, "encoding": "FLOAT32", "record_length": 1024},
    )
    assert dev.archive.enabled is True
    assert dev.archive.encoding == "FLOAT32"
    assert dev.archive.record_length == 1024
    # Unspecified fields still default.
    assert dev.archive.fsync_interval_s == 5.0


def test_root_config_propagates_archive_root_default() -> None:
    """A RootConfig with no overrides has app.archive_root=None and
    every device's archive disabled by default — preserving the
    'archive defaults to off' invariant."""
    cfg = RootConfig()
    assert cfg.app.archive_root is None
    assert cfg.devices == []


def test_root_config_rejects_devices_colliding_on_archive_dir() -> None:
    """Two DISTINCT device names that sanitize to the same per-device SDS
    segment must be rejected at load — sharing one physical SDS tree is the
    cross-device collision the per-device layout exists to prevent."""
    with pytest.raises(ValidationError) as exc:
        RootConfig(
            devices=[
                DeviceConfig(name="Echos", host="a.example.com"),
                DeviceConfig(name="Echos_", host="b.example.com"),
            ]
        )
    msg = str(exc.value)
    assert "Echos" in msg and "Echos_" in msg
    assert "same archive directory" in msg


def test_root_config_rejects_separator_variants_colliding() -> None:
    """``"a/b"`` and ``"a b"`` both sanitize to ``"a_b"`` → rejected."""
    with pytest.raises(ValidationError):
        RootConfig(
            devices=[
                DeviceConfig(name="a/b", host="a.example.com"),
                DeviceConfig(name="a b", host="b.example.com"),
            ]
        )


def test_root_config_allows_devices_with_distinct_archive_dirs() -> None:
    """Distinct sanitized segments (the normal Echos / Echos_WK case) load
    cleanly — the guard only fires on a genuine segment collision."""
    cfg = RootConfig(
        devices=[
            DeviceConfig(name="Echos", host="a.example.com"),
            DeviceConfig(name="Echos_WK", host="b.example.com"),
        ]
    )
    assert [d.name for d in cfg.devices] == ["Echos", "Echos_WK"]
