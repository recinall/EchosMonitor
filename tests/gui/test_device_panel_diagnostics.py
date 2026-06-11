"""GUI tests for the DevicePanel Diagnostics column + tooltip.

Drives the panel directly with synthetic ``DeviceStatus`` snapshots via
a fake status provider — no real engine, no network, no QThread. This
isolates the formatting / state-routing logic from worker behaviour
covered by ``tests/core/test_seedlink_worker_timeout.py``.
"""

from __future__ import annotations

from collections.abc import Callable

from obspy.core.utcdatetime import UTCDateTime

from echosmonitor.core.models import ConnState, DeviceStatus
from echosmonitor.gui.widgets.device_panel import DevicePanel


def _make_panel_with_status(
    qtbot, statuses: dict[str, DeviceStatus]
) -> tuple[DevicePanel, Callable[[], dict[str, DeviceStatus]]]:
    """Build a DevicePanel wired to a closure-based status provider."""
    panel = DevicePanel()
    qtbot.addWidget(panel)

    # Status provider — we capture the dict by reference so tests can
    # mutate it between ticks if they want to drive transitions.
    def provider() -> dict[str, DeviceStatus]:
        return statuses

    panel.set_status_provider(provider)
    return panel, provider


def test_diagnostics_column_populates_in_waiting_retry(qtbot) -> None:
    """When a device is in WAITING_RETRY with attempts > 0, the
    Diagnostics column shows attempt counter, last failure kind, and
    a countdown to the next retry."""
    statuses = {
        "test-dev": DeviceStatus(
            name="test-dev",
            state=ConnState.WAITING_RETRY,
            attempt_count=3,
            last_failure_kind="timeout",
            next_attempt_at=UTCDateTime() + 4.0,
            since_first_attempt_at=UTCDateTime() - 30.0,
        )
    }
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.set_connect_timeouts({"test-dev": 10.0})
    panel.set_device_endpoints({"test-dev": ("10.255.255.1", 18000)})
    # Drive a state transition so the row exists with the right state colour.
    panel.on_device_state("test-dev", int(ConnState.WAITING_RETRY))
    # Force a stats tick so diagnostics formatting runs synchronously.
    panel._refresh_stats()

    text = panel._diagnostics_text_for_test("test-dev")
    assert "attempt 3" in text, f"diagnostics column missing attempt counter; got: {text!r}"
    assert "timeout" in text, f"diagnostics column missing failure kind; got: {text!r}"
    assert "next:" in text, f"diagnostics column missing next-retry; got: {text!r}"


def test_diagnostics_column_empty_when_connected(qtbot) -> None:
    """A healthy CONNECTED device shows no diagnostics (clutter-free)."""
    statuses = {
        "healthy": DeviceStatus(
            name="healthy",
            state=ConnState.CONNECTED,
            attempt_count=0,  # reset by _on_connect_success
            last_failure_kind=None,
        )
    }
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.on_device_state("healthy", int(ConnState.CONNECTED))
    panel._refresh_stats()

    assert panel._diagnostics_text_for_test("healthy") == ""
    assert panel._tooltip_text_for_test("healthy") == ""


def test_tooltip_contains_failure_kind_and_nc_hint(qtbot) -> None:
    """Tooltip body in WAITING_RETRY includes the humanised failure
    description AND the manual-test ``nc -vz`` command using the
    device's configured host:port."""
    statuses = {
        "test-dev": DeviceStatus(
            name="test-dev",
            state=ConnState.WAITING_RETRY,
            attempt_count=3,
            last_failure_kind="timeout",
            next_attempt_at=UTCDateTime() + 4.0,
            since_first_attempt_at=UTCDateTime() - 30.0,
        )
    }
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.set_connect_timeouts({"test-dev": 10.0})
    panel.set_device_endpoints({"test-dev": ("10.255.255.1", 18000)})
    panel.on_device_state("test-dev", int(ConnState.WAITING_RETRY))
    panel._refresh_stats()

    tooltip = panel._tooltip_text_for_test("test-dev")
    assert "Connection failed 3 times" in tooltip, (
        f"tooltip missing attempt count; got: {tooltip!r}"
    )
    assert "timed out" in tooltip, f"tooltip missing failure description; got: {tooltip!r}"
    assert "nc -vz 10.255.255.1 18000" in tooltip, f"tooltip missing nc hint; got: {tooltip!r}"


def test_tooltip_humanizes_each_failure_kind(qtbot) -> None:
    """Each closed FailureKind value gets a distinct humanised string in
    the tooltip body. Catches typos when adding new kinds."""
    expected = {
        "timeout": "timed out",
        "refused": "connection refused",
        "dns": "DNS lookup failed",
        "unknown": "unknown error",
    }
    for kind, snippet in expected.items():
        statuses = {
            "dev": DeviceStatus(
                name="dev",
                state=ConnState.WAITING_RETRY,
                attempt_count=1,
                last_failure_kind=kind,  # type: ignore[arg-type]
                next_attempt_at=UTCDateTime() + 2.0,
                since_first_attempt_at=UTCDateTime() - 5.0,
            )
        }
        panel, _ = _make_panel_with_status(qtbot, statuses)
        panel.set_connect_timeouts({"dev": 10.0})
        panel.set_device_endpoints({"dev": ("example.com", 18000)})
        panel.on_device_state("dev", int(ConnState.WAITING_RETRY))
        panel._refresh_stats()

        tooltip = panel._tooltip_text_for_test("dev")
        assert snippet in tooltip, (
            f"tooltip for kind={kind!r} missing {snippet!r}; got: {tooltip!r}"
        )


def test_tooltip_appears_in_connecting_after_2x_timeout(qtbot) -> None:
    """If a device stays in CONNECTING beyond ``2 * connect_timeout``,
    the tooltip should appear anyway — at that point we've blown past
    the budget and the user benefits from the manual-test hint even
    without a WAITING_RETRY emission yet."""
    statuses = {
        "stuck": DeviceStatus(
            name="stuck",
            state=ConnState.CONNECTING,
            attempt_count=1,
            last_failure_kind="timeout",
            since_first_attempt_at=UTCDateTime() - 25.0,  # > 2 * 10.0
        )
    }
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.set_connect_timeouts({"stuck": 10.0})
    panel.set_device_endpoints({"stuck": ("10.255.255.1", 18000)})
    panel.on_device_state("stuck", int(ConnState.CONNECTING))
    panel._refresh_stats()

    tooltip = panel._tooltip_text_for_test("stuck")
    assert tooltip != "", "stuck CONNECTING beyond 2x timeout should show tooltip"
    assert "nc -vz 10.255.255.1 18000" in tooltip


def test_tooltip_absent_in_connecting_within_budget(qtbot) -> None:
    """Within ``2 * connect_timeout``, a CONNECTING device shows no
    tooltip — early CONNECTING is normal, not a problem."""
    statuses = {
        "fresh": DeviceStatus(
            name="fresh",
            state=ConnState.CONNECTING,
            attempt_count=1,
            last_failure_kind=None,
            since_first_attempt_at=UTCDateTime() - 1.0,  # well under budget
        )
    }
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.set_connect_timeouts({"fresh": 10.0})
    panel.set_device_endpoints({"fresh": ("example.com", 18000)})
    panel.on_device_state("fresh", int(ConnState.CONNECTING))
    panel._refresh_stats()

    assert panel._tooltip_text_for_test("fresh") == ""


def test_diagnostics_column_has_six_columns(qtbot) -> None:
    """Defensive: the panel is built with 6 columns (Stream, the M2-C
    Acq badge, State, Diagnostics, Stats + the M1-C Echos status
    column). A future header relayout that drops a column would
    silently break diagnostics or Echos rendering — this test catches
    that without depending on the exact column indexes."""
    statuses: dict[str, DeviceStatus] = {}
    panel, _ = _make_panel_with_status(qtbot, statuses)
    assert panel._tree.columnCount() == 6
    headers = [panel._tree.headerItem().text(i) for i in range(6)]
    assert headers == ["Stream", "Acq", "State", "Diagnostics", "Stats", "Echos"]
