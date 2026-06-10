"""Stats-text formatting for the M5 archive surface in DevicePanel."""

from __future__ import annotations

from echosmonitor.core.models import DeviceStatus
from echosmonitor.gui.widgets.device_panel import _format_stats_text


def _status(**overrides: object) -> DeviceStatus:
    base: dict[str, object] = {
        "name": "dev",
        "packets_received": 1234,
        "bytes_received": 250 * 1024 * 1024,  # 250 MB
    }
    base.update(overrides)
    return DeviceStatus(**base)  # type: ignore[arg-type]


def test_archive_disabled_shows_only_pkts_and_bytes() -> None:
    text = _format_stats_text(_status())
    assert text == "1.2k pkts / 250.0 MB"
    assert "arch" not in text
    assert "files" not in text


def test_archive_enabled_appends_inline_summary() -> None:
    text = _format_stats_text(
        _status(
            archive_enabled=True,
            archive_bytes_written=45 * 1024 * 1024 + 614 * 1024,  # ~45.6 MB
            archive_files_open=3,
        )
    )
    assert text.startswith("1.2k pkts / 250.0 MB · arch ")
    assert "MB · 3 files" in text


def test_archive_zero_bytes_still_shows_when_enabled() -> None:
    """The suffix must appear as soon as the archive is enabled — operators
    need to see "0 files" rather than nothing while waiting for the first
    packet to land."""
    text = _format_stats_text(_status(archive_enabled=True))
    assert "arch 0 B · 0 files" in text


def test_archive_error_appends_bang_marker() -> None:
    text = _format_stats_text(
        _status(
            archive_enabled=True,
            archive_bytes_written=1024,
            archive_files_open=1,
            archive_last_error="filesystem unresponsive",
        )
    )
    assert text.endswith("(!)")


def test_archive_gap_count_appended_when_nonzero() -> None:
    text = _format_stats_text(
        _status(
            archive_enabled=True,
            archive_bytes_written=1024,
            archive_files_open=1,
            archive_gaps_total=2,
        )
    )
    assert "· 2 gaps" in text


def test_archive_zero_gaps_omits_suffix() -> None:
    text = _format_stats_text(
        _status(
            archive_enabled=True,
            archive_bytes_written=1024,
            archive_files_open=1,
            archive_gaps_total=0,
        )
    )
    assert "gaps" not in text


def test_archive_disabled_but_legacy_bytes_still_renders_summary() -> None:
    """A device whose archive was just disabled by hot-reload may still
    have nonzero counters from the previous session — show them rather
    than hide the history at the next 1 Hz tick."""
    text = _format_stats_text(
        _status(
            archive_enabled=False,
            archive_bytes_written=1024,
            archive_files_open=1,
        )
    )
    assert "arch 1.0 KB · 1 files" in text
