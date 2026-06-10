"""GUI tests for the DevicePanel ``protocol_rejected`` rendering surface.

Exercises the three behaviours added to the panel for misconfigured
SeedLink selectors:

  1. The Diagnostics column switches shape from
     ``"attempt N · last fail: K · next: Xs"`` to
     ``"rejected: N selectors · next: Xs"`` so the operator sees the
     actionable count, not a meaningless attempt counter.
  2. The tooltip pivots from the ``nc -vz`` reachability hint (which is
     irrelevant for protocol_rejected — the TCP handshake DID succeed)
     to a workflow hint pointing at the Stations browser.
  3. The state badge gains a ``" (!)"`` suffix in WAITING_RETRY so the
     row reads as misconfigured, not just slow.

These run against synthetic ``DeviceStatus`` snapshots driven through
the panel's status provider — no engine, no network. Pattern follows
``tests/gui/test_device_panel_diagnostics.py``.
"""

from __future__ import annotations

from collections.abc import Callable

from obspy.core.utcdatetime import UTCDateTime

from echosmonitor.core.models import ConnState, DeviceStatus
from echosmonitor.gui.widgets.device_panel import DevicePanel


def _make_panel_with_status(
    qtbot, statuses: dict[str, DeviceStatus]
) -> tuple[DevicePanel, Callable[[], dict[str, DeviceStatus]]]:
    panel = DevicePanel()
    qtbot.addWidget(panel)

    def provider() -> dict[str, DeviceStatus]:
        return statuses

    panel.set_status_provider(provider)
    return panel, provider


def _waiting_retry_status(
    name: str = "rejected-dev",
    *,
    rejected_selectors: list[str] | None = None,
    rejection_count: int | None = None,
) -> DeviceStatus:
    detail: dict[str, object] | None = None
    if rejected_selectors is not None:
        detail = {"rejected_selectors": rejected_selectors}
        if rejection_count is not None:
            detail["rejection_count"] = rejection_count
    return DeviceStatus(
        name=name,
        state=ConnState.WAITING_RETRY,
        attempt_count=1,
        last_failure_kind="protocol_rejected",
        next_attempt_at=UTCDateTime() + 4.0,
        since_first_attempt_at=UTCDateTime() - 5.0,
        last_failure_detail=detail,
    )


def test_protocol_rejected_diagnostics_text_shape(qtbot) -> None:
    """``rejected: N selectors · next: Xs`` — the column reflects the
    rejected-selector count from ``last_failure_detail`` rather than
    the meaningless attempt counter."""
    statuses = {
        "rejected-dev": _waiting_retry_status(
            rejected_selectors=["IV.MILN..HHZ", "IV.MILN..HHN", "IV.MILN..HHE"],
            rejection_count=3,
        )
    }
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.set_connect_timeouts({"rejected-dev": 10.0})
    panel.set_device_endpoints({"rejected-dev": ("echos.local", 18000)})
    panel.on_device_state("rejected-dev", int(ConnState.WAITING_RETRY))
    panel._refresh_stats()

    text = panel._diagnostics_text_for_test("rejected-dev")
    assert text.startswith("rejected: 3 selectors"), (
        f"diagnostics column did not adopt the rejection shape; got: {text!r}"
    )
    assert "next:" in text, f"diagnostics column missing next-retry; got: {text!r}"
    # The default attempt-style format must NOT also be emitted; that
    # would mean the special-case branch fell through.
    assert "attempt" not in text, f"diagnostics column leaked attempt counter; got: {text!r}"


def test_protocol_rejected_tooltip_mentions_station_browser(qtbot) -> None:
    """Tooltip in protocol_rejected MUST cross-reference the Stations
    browser (the resolution path) and NOT the ``nc -vz`` hint (the
    network-layer probe is irrelevant — the rejection is application-layer)."""
    statuses = {
        "rejected-dev": _waiting_retry_status(
            rejected_selectors=["IV.MILN..HHZ"],
            rejection_count=1,
        )
    }
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.set_connect_timeouts({"rejected-dev": 10.0})
    panel.set_device_endpoints({"rejected-dev": ("echos.local", 18000)})
    panel.on_device_state("rejected-dev", int(ConnState.WAITING_RETRY))
    panel._refresh_stats()

    tooltip = panel._tooltip_text_for_test("rejected-dev")
    assert "Server rejected" in tooltip, f"tooltip missing rejection summary; got: {tooltip!r}"
    assert "Stations browser" in tooltip, (
        f"tooltip missing Stations-browser cross-reference; got: {tooltip!r}"
    )
    assert "Refresh" in tooltip, f"tooltip missing Refresh-action hint; got: {tooltip!r}"
    assert "nc -vz" not in tooltip, (
        f"tooltip leaked the nc -vz hint into the rejection branch; got: {tooltip!r}"
    )


def test_protocol_rejected_tooltip_handles_missing_detail(qtbot) -> None:
    """If ``last_failure_detail`` is somehow missing on a
    protocol_rejected status, the tooltip still renders (no
    parenthetical selector count) without crashing."""
    statuses = {"rejected-dev": _waiting_retry_status(rejected_selectors=None)}
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.set_connect_timeouts({"rejected-dev": 10.0})
    panel.set_device_endpoints({"rejected-dev": ("echos.local", 18000)})
    panel.on_device_state("rejected-dev", int(ConnState.WAITING_RETRY))
    panel._refresh_stats()

    tooltip = panel._tooltip_text_for_test("rejected-dev")
    assert "Server rejected" in tooltip
    # Diagnostics column falls back to "?" when count is unknown.
    assert "rejected: ? selectors" in panel._diagnostics_text_for_test("rejected-dev")


def test_protocol_rejected_state_label_has_bang_suffix(qtbot) -> None:
    """The state badge for WAITING_RETRY + protocol_rejected ends with
    ``" (!)"``. Other failure kinds in WAITING_RETRY MUST NOT.

    Driven by ``_refresh_stats`` so the suffix tracks the current
    failure kind, not the most recent state transition. Idempotent
    across multiple ticks — a second ``_refresh_stats`` does NOT
    double-append.
    """
    rejected = _waiting_retry_status(
        rejected_selectors=["IV.MILN..HHZ"],
        rejection_count=1,
    )
    timeout_status = DeviceStatus(
        name="timeout-dev",
        state=ConnState.WAITING_RETRY,
        attempt_count=2,
        last_failure_kind="timeout",
        next_attempt_at=UTCDateTime() + 4.0,
        since_first_attempt_at=UTCDateTime() - 12.0,
    )
    statuses = {"rejected-dev": rejected, "timeout-dev": timeout_status}
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.set_connect_timeouts({"rejected-dev": 10.0, "timeout-dev": 10.0})
    panel.set_device_endpoints(
        {"rejected-dev": ("echos.local", 18000), "timeout-dev": ("10.255.255.1", 18000)}
    )
    panel.on_device_state("rejected-dev", int(ConnState.WAITING_RETRY))
    panel.on_device_state("timeout-dev", int(ConnState.WAITING_RETRY))
    panel._refresh_stats()

    rejected_label = panel._state_label_for_test("rejected-dev")
    timeout_label = panel._state_label_for_test("timeout-dev")
    assert rejected_label.endswith(" (!)"), (
        f"protocol_rejected badge missing (!) suffix; got: {rejected_label!r}"
    )
    assert "WAITING_RETRY" in rejected_label
    assert not timeout_label.endswith(" (!)"), (
        f"timeout badge wrongly carries (!) suffix; got: {timeout_label!r}"
    )

    # Idempotency: a second tick must not double-append.
    panel._refresh_stats()
    assert panel._state_label_for_test("rejected-dev") == rejected_label, (
        f"refresh_stats double-appended (!) suffix; got: "
        f"{panel._state_label_for_test('rejected-dev')!r}"
    )


def test_protocol_rejected_bang_suffix_strips_when_failure_kind_changes(qtbot) -> None:
    """If a future tick reports a different ``last_failure_kind``
    (e.g. the operator fixed the config and the worker now hits a
    different failure on its next attempt), the (!) suffix MUST be
    stripped — the rejection-specific marker is misleading once the
    underlying kind is no longer rejection."""
    statuses = {
        "rejected-dev": _waiting_retry_status(
            rejected_selectors=["IV.MILN..HHZ"], rejection_count=1
        )
    }
    panel, _ = _make_panel_with_status(qtbot, statuses)
    panel.set_connect_timeouts({"rejected-dev": 10.0})
    panel.set_device_endpoints({"rejected-dev": ("echos.local", 18000)})
    panel.on_device_state("rejected-dev", int(ConnState.WAITING_RETRY))
    panel._refresh_stats()
    assert panel._state_label_for_test("rejected-dev").endswith(" (!)")

    # Operator fixes config → next attempt hits a different failure.
    statuses["rejected-dev"] = DeviceStatus(
        name="rejected-dev",
        state=ConnState.WAITING_RETRY,
        attempt_count=2,
        last_failure_kind="timeout",
        next_attempt_at=UTCDateTime() + 4.0,
        since_first_attempt_at=UTCDateTime() - 12.0,
    )
    panel._refresh_stats()
    label = panel._state_label_for_test("rejected-dev")
    assert not label.endswith(" (!)"), (
        f"(!) suffix was not stripped after kind changed away from "
        f"protocol_rejected; got: {label!r}"
    )
