"""DevicePanel Echos column (M1-C): snapshot rendering + failure state."""

from __future__ import annotations

from typing import Any

from echosmonitor.core.models import EchosDeviceSnapshot
from echosmonitor.gui.widgets.device_panel import DevicePanel


def _snapshot(**overrides: Any) -> EchosDeviceSnapshot:
    base: dict[str, Any] = {
        "device": "echos-field-01",
        "firmware_version": "1.4.2",
        "uptime_s": 7200.0,
        "gnss_fix": True,
        "gnss_satellites": 9,
        "pps_locked": True,
        "clients_connected": 1,
        "ring_used_pct": 12.5,
        "calibration_state": "idle",
        "polled_at": 1000.0,
    }
    base.update(overrides)
    return EchosDeviceSnapshot(**base)


def _make_panel(qtbot: Any) -> DevicePanel:
    panel = DevicePanel()
    qtbot.addWidget(panel)
    # Rows are pre-created from the store in production; the engine state
    # signal is the lightest equivalent for widget-level tests.
    panel.on_device_state("echos-field-01", 0)
    return panel


def test_snapshot_renders_compact_summary(qtbot: Any) -> None:
    panel = _make_panel(qtbot)
    panel.on_echos_snapshot(_snapshot())
    text = panel._echos_text_for_test("echos-field-01")
    assert "fw 1.4.2" in text
    assert "up 2h" in text
    assert "1 cli" in text
    assert "ring 12%" in text  # ring_used_pct rendered 0-decimal
    assert "GNSS 9sat" in text
    # Idle calibration is steady-state noise — omitted from the column.
    assert "cal" not in text


def test_snapshot_tooltip_carries_full_detail(qtbot: Any) -> None:
    panel = _make_panel(qtbot)
    panel.on_echos_snapshot(_snapshot())
    tooltip = panel._echos_tooltip_for_test("echos-field-01")
    assert "Firmware 1.4.2" in tooltip
    assert "9 satellites" in tooltip
    assert "PPS locked" in tooltip
    assert "ring 12.5% used" in tooltip
    assert "Calibration: idle" in tooltip


def test_active_calibration_appears_in_column(qtbot: Any) -> None:
    panel = _make_panel(qtbot)
    panel.on_echos_snapshot(_snapshot(calibration_state="running"))
    assert "cal running" in panel._echos_text_for_test("echos-field-01")
    panel.on_echos_snapshot(_snapshot(calibration_state="failed"))
    assert "cal failed" in panel._echos_text_for_test("echos-field-01")
    # "done" persists in device RAM until reboot — column omits it (the
    # tooltip still carries it); only running/failed warrant column space.
    panel.on_echos_snapshot(_snapshot(calibration_state="done"))
    assert "cal" not in panel._echos_text_for_test("echos-field-01")
    assert "Calibration: done" in panel._echos_tooltip_for_test("echos-field-01")


def test_clock_health_token_in_column(qtbot: Any) -> None:
    """M6: the clock verdict is first-class column health, not tooltip
    trivia — one token per closed ClockHealth state, with the "(!)"
    attention suffix on the unsynced state."""
    panel = _make_panel(qtbot)
    panel.on_echos_snapshot(_snapshot())  # gnss fix + pps locked
    assert "clk PPS" in panel._echos_text_for_test("echos-field-01")
    panel.on_echos_snapshot(_snapshot(pps_locked=False))
    assert "clk GNSS" in panel._echos_text_for_test("echos-field-01")
    panel.on_echos_snapshot(
        _snapshot(gnss_fix=False, gnss_satellites=0, pps_locked=False, ntp_synchronized=True)
    )
    assert "clk NTP" in panel._echos_text_for_test("echos-field-01")
    panel.on_echos_snapshot(
        _snapshot(gnss_fix=False, gnss_satellites=0, pps_locked=False, time_synchronized=True)
    )
    assert "clk hold (!)" in panel._echos_text_for_test("echos-field-01")
    panel.on_echos_snapshot(_snapshot(gnss_fix=False, gnss_satellites=0, pps_locked=False))
    assert "clk none (!)" in panel._echos_text_for_test("echos-field-01")


def test_clock_health_tooltip_detail(qtbot: Any) -> None:
    """M6: the tooltip carries the verdict sentence, the firmware's
    free-form sync string verbatim, and the PPS offset when locked."""
    panel = _make_panel(qtbot)
    panel.on_echos_snapshot(_snapshot(time_sync_type="RMC+PPS+NTP", pps_offset_us=-4))
    tooltip = panel._echos_tooltip_for_test("echos-field-01")
    assert "PPS locked" in tooltip
    assert "sync RMC+PPS+NTP" in tooltip
    assert "PPS offset -4" in tooltip
    panel.on_echos_snapshot(_snapshot(gnss_fix=False, gnss_satellites=0, pps_locked=False))
    tooltip = panel._echos_tooltip_for_test("echos-field-01")
    assert "NOT SYNCHRONIZED" in tooltip
    assert "PPS offset" not in tooltip  # offset is meaningless unlocked
    panel.on_echos_snapshot(
        _snapshot(gnss_fix=False, gnss_satellites=0, pps_locked=False, time_synchronized=True)
    )
    assert "holdover" in panel._echos_tooltip_for_test("echos-field-01")


def test_no_gnss_fix_is_explicit(qtbot: Any) -> None:
    panel = _make_panel(qtbot)
    panel.on_echos_snapshot(_snapshot(gnss_fix=False, gnss_satellites=0, pps_locked=False))
    assert "GNSS no fix" in panel._echos_text_for_test("echos-field-01")
    assert "PPS not locked" in panel._echos_tooltip_for_test("echos-field-01")


def test_late_payload_for_removed_device_is_dropped(qtbot: Any) -> None:
    # Regression (M1-C review): a poll in flight when its device is
    # removed lands AFTER the row is gone — it must not resurrect a
    # ghost row. Rows are only ever pre-created from the store/engine.
    panel = DevicePanel()
    qtbot.addWidget(panel)
    assert panel._device_count_for_test() == 0
    panel.on_echos_snapshot(_snapshot())
    panel.on_echos_poll_failed("echos-field-01", "unreachable", "late failure")
    assert panel._device_count_for_test() == 0


def test_poll_failure_replaces_stale_numbers(qtbot: Any) -> None:
    panel = _make_panel(qtbot)
    panel.on_echos_snapshot(_snapshot())
    panel.on_echos_poll_failed("echos-field-01", "unreachable", "device unreachable (GET /api/status)")
    text = panel._echos_text_for_test("echos-field-01")
    assert text == "(unreachable)"
    assert "fw 1.4.2" not in text
    assert "poll failed" in panel._echos_tooltip_for_test("echos-field-01")
    # A later good poll restores the live rendering.
    panel.on_echos_snapshot(_snapshot())
    assert "fw 1.4.2" in panel._echos_text_for_test("echos-field-01")


def test_non_snapshot_payload_is_ignored(qtbot: Any) -> None:
    panel = _make_panel(qtbot)
    panel.on_echos_snapshot("garbage")  # rule 4 isinstance guard
    assert panel._echos_text_for_test("echos-field-01") == ""


def test_uptime_formatting_brackets(qtbot: Any) -> None:
    panel = _make_panel(qtbot)
    cases = [(42.0, "up 42s"), (1800.0, "up 30m"), (7200.0, "up 2h"), (259200.0, "up 3d")]
    for uptime, expected in cases:
        panel.on_echos_snapshot(_snapshot(uptime_s=uptime))
        assert expected in panel._echos_text_for_test("echos-field-01")
