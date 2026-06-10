"""Unit tests for `core.models`."""

from __future__ import annotations

import pytest

from echosmonitor.core.models import ConnState, DeviceStatus, StreamID, StreamSelector


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
