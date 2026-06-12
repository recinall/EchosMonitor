"""Unit tests for `core.models`."""

from __future__ import annotations

import pytest

from echosmonitor.core.models import (
    ClockHealth,
    ConnState,
    DeviceStatus,
    EchosDeviceSnapshot,
    StreamID,
    StreamSelector,
)


def test_stream_id_nslc_property() -> None:
    sid = StreamID("IU", "ANMO", "00", "BHZ")
    assert sid.nslc == "IU.ANMO.00.BHZ"


def test_stream_id_from_trace_id_round_trip() -> None:
    sid = StreamID.from_trace_id("IV.MILN..HHZ")
    assert sid == StreamID("IV", "MILN", "", "HHZ")
    # The empty location is preserved verbatim — no normalisation.
    assert sid.nslc == "IV.MILN..HHZ"


def test_stream_id_from_trace_id_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="exactly four"):
        StreamID.from_trace_id("IU.ANMO.BHZ")
    with pytest.raises(ValueError, match="exactly four"):
        StreamID.from_trace_id("IU.ANMO.00.BHZ.extra")


def test_stream_selector_is_namedtuple_with_wildcards() -> None:
    sel = StreamSelector(network="IV", station="*", location="*", channel="HH?")
    assert sel.network == "IV"
    assert sel.station == "*"
    assert sel[3] == "HH?"  # NamedTuple positional access


def test_conn_state_is_int_compatible() -> None:
    # Engine signals carry the int form for cross-thread queueing.
    assert int(ConnState.CONNECTED) == 2
    assert ConnState(2) is ConnState.CONNECTED


def test_device_status_defaults() -> None:
    s = DeviceStatus(name="lab")
    assert s.state is ConnState.DISCONNECTED
    assert s.last_event_at is None
    assert s.last_error is None
    assert s.packets_received == 0
    assert s.bytes_received == 0


def _clock_snapshot(**overrides: object) -> EchosDeviceSnapshot:
    base: dict[str, object] = {
        "device": "echos-field-01",
        "firmware_version": "1.4.2",
        "uptime_s": 60.0,
        "gnss_fix": False,
        "gnss_satellites": 0,
        "pps_locked": False,
        "clients_connected": 0,
        "ring_used_pct": 0.0,
        "calibration_state": "idle",
        "polled_at": 1.0,
    }
    base.update(overrides)
    return EchosDeviceSnapshot(**base)  # type: ignore[arg-type]


def test_clock_health_verdict_ladder() -> None:
    """M6: the closed verdict derives from booleans only, best → worst.

    ``time_sync_type`` is a free-form firmware composite ("RMC+PPS+NTP")
    and must never influence the verdict.
    """
    assert _clock_snapshot(gnss_fix=True, pps_locked=True).clock_health() is ClockHealth.PPS
    assert _clock_snapshot(gnss_fix=True).clock_health() is ClockHealth.GNSS
    assert _clock_snapshot(ntp_synchronized=True).clock_health() is ClockHealth.NTP
    # time_synchronized ALONE is holdover (clock set once, every live
    # source gone, crystal drifting) — never reported as NTP (reviewer
    # finding: that would claim a source/accuracy the device never said).
    assert _clock_snapshot(time_synchronized=True).clock_health() is ClockHealth.HOLDOVER
    assert _clock_snapshot().clock_health() is ClockHealth.UNSYNCED
    # PPS lock without GNSS time is a transient — never reported as PPS.
    assert _clock_snapshot(pps_locked=True).clock_health() is ClockHealth.UNSYNCED
    # The free-form string alone proves nothing.
    assert _clock_snapshot(time_sync_type="RMC+PPS+NTP").clock_health() is ClockHealth.UNSYNCED


def test_clock_fields_default_pessimistic() -> None:
    """Constructors that predate M6 can only ever err toward UNSYNCED."""
    snapshot = _clock_snapshot()
    assert snapshot.time_synchronized is False
    assert snapshot.ntp_synchronized is False
    assert snapshot.time_sync_type == ""
    assert snapshot.pps_offset_us == 0
