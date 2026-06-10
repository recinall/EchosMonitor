"""Unit tests for ``core/echos_api.py`` against the in-memory firmware fake.

The fake (``tests/core/echos_fake.py``) defines the pinned JSON contract;
these tests pin the client's behaviour: typed responses, the closed error
set, retry bounds (GETs ≤2, writes 0), the 429 lockout fast-fail, the
7-step hot-reload poll, and the rule-15 guarantee that credentials never
reach logs or exception messages.

No wall-time assertions here — the bounded-wait tests assert *that* a
deadline fires, never how fast, so they stay in the default gate
(no ``perf`` marker needed).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from echosmonitor.core.echos_api import (
    CalibrationStatus,
    EchosAcquisitionConfig,
    EchosApiClient,
    EchosNetworkConfig,
    EchosStatus,
    OtaStatus,
    RestartStatus,
    SeedlinkClientInfo,
    SeedlinkServerConfig,
    SeedlinkServerStatus,
)
from echosmonitor.core.exceptions import (
    EchosApiProtocolError,
    EchosAuthFailed,
    EchosLockedOut,
    EchosTimeout,
    EchosUnreachable,
)
from tests.core.echos_fake import FakeEchosFirmware


def _basic_auth_header(password: str) -> str:
    return "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()


@pytest.fixture
def fw() -> FakeEchosFirmware:
    return FakeEchosFirmware()


@pytest.fixture
async def client(fw: FakeEchosFirmware) -> AsyncIterator[EchosApiClient]:
    async with EchosApiClient(
        "echos-test.local",
        password=fw.admin_password,
        transport=fw.transport,
        retry_delay_s=0.0,
    ) as c:
        yield c


def _make_client(fw: FakeEchosFirmware, password: str | None) -> EchosApiClient:
    return EchosApiClient(
        "echos-test.local", password=password, transport=fw.transport, retry_delay_s=0.0
    )


# ----------------------------------------------------------------------
# Typed GET endpoints
# ----------------------------------------------------------------------


async def test_get_status_typed(client: EchosApiClient) -> None:
    status = await client.get_status()
    assert isinstance(status, EchosStatus)
    assert status.firmware_version == "1.4.2"
    assert status.variant == "seedlink"
    assert status.gnss.satellites == 9
    assert status.gnss.pps_locked is True
    assert status.wifi.mode == "sta"
    assert status.wifi.rssi_dbm == -61


async def test_get_status_ignores_additive_fields(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    fw.status["future_field"] = {"nested": True}
    status = await client.get_status()
    assert status.firmware_version == "1.4.2"


async def test_get_acquisition_config_typed(client: EchosApiClient) -> None:
    config = await client.get_acquisition_config()
    assert isinstance(config, EchosAcquisitionConfig)
    assert config.osr == 64
    assert config.gains == (1, 1, 1, 8)


async def test_get_seedlink_status_and_clients(client: EchosApiClient) -> None:
    status = await client.get_seedlink_status()
    assert isinstance(status, SeedlinkServerStatus)
    assert status.client_count == 1
    clients = await client.get_seedlink_clients()
    assert len(clients) == 1
    assert isinstance(clients[0], SeedlinkClientInfo)
    assert clients[0].slot == 0
    assert clients[0].address == "192.168.1.10:54321"


async def test_get_seedlink_config_typed(client: EchosApiClient) -> None:
    config = await client.get_seedlink_config()
    assert isinstance(config, SeedlinkServerConfig)
    assert config.port == 18000
    assert config.record_size == 512
    assert config.has_password is False


async def test_get_stationxml_verbatim(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    xml = await client.get_stationxml()
    assert xml == fw.stationxml
    assert "<Latitude>45.4</Latitude>" in xml


async def test_get_ota_status_typed(client: EchosApiClient) -> None:
    ota = await client.get_ota_status()
    assert isinstance(ota, OtaStatus)
    assert ota.running_partition == "ota_0"


# ----------------------------------------------------------------------
# Writes: auth, full-body contract, no retries
# ----------------------------------------------------------------------


async def test_acquisition_roundtrip(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    config = await client.get_acquisition_config()
    updated = config.model_copy(update={"osr": 128})
    await client.set_acquisition_config(updated)
    assert fw.acquisition == {"osr": 128, "gains": [1, 1, 1, 8]}
    assert (await client.get_acquisition_config()).osr == 128


async def test_network_write_strips_has_password(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    config = await client.get_network_config()
    assert isinstance(config, EchosNetworkConfig)
    await client.set_network_config(
        config.model_copy(update={"ssid": "new-net"}), wifi_password="wifi-secret-1"
    )
    body = fw.last_post_body["/api/network/config"]
    assert "has_password" not in body
    assert body["password"] == "wifi-secret-1"
    assert fw.network["ssid"] == "new-net"
    assert fw.network["has_password"] is True


async def test_write_with_wrong_password_raises_auth_failed(fw: FakeEchosFirmware) -> None:
    bad = _make_client(fw, "wrong-password")
    with pytest.raises(EchosAuthFailed) as excinfo:
        await bad.reboot()
    assert excinfo.value.kind == "auth_failed"
    await bad.aclose()


async def test_write_without_password_fails_locally(fw: FakeEchosFirmware) -> None:
    anon = _make_client(fw, None)
    with pytest.raises(EchosAuthFailed):
        await anon.reboot()
    # The guard fires client-side: the device saw no traffic at all.
    assert fw.requests == []
    await anon.aclose()


async def test_reads_stay_public_without_password(fw: FakeEchosFirmware) -> None:
    anon = _make_client(fw, None)
    assert (await anon.get_status()).variant == "seedlink"
    await anon.aclose()


async def test_disconnect_client(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    await client.disconnect_client(0)
    assert fw.clients == []


async def test_disconnect_unknown_slot_is_protocol_error(client: EchosApiClient) -> None:
    with pytest.raises(EchosApiProtocolError):
        await client.disconnect_client(99)


# ----------------------------------------------------------------------
# Lockout (429 + Retry-After)
# ----------------------------------------------------------------------


async def test_locked_device_raises_locked_out(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    fw.locked = True
    with pytest.raises(EchosLockedOut) as excinfo:
        await client.reboot()
    assert excinfo.value.kind == "locked_out"
    assert excinfo.value.retry_after_s == 30.0


async def test_lockout_guard_never_hammers_device(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    fw.locked = True
    with pytest.raises(EchosLockedOut):
        await client.reboot()
    sent = len(fw.requests)
    # Within the Retry-After window the client fast-fails without traffic.
    with pytest.raises(EchosLockedOut) as excinfo:
        await client.reboot()
    assert len(fw.requests) == sent
    assert 0.0 < excinfo.value.retry_after_s <= 30.0
    assert client.lockout_remaining_s > 0.0


async def test_lockout_after_five_auth_failures(fw: FakeEchosFirmware) -> None:
    bad = _make_client(fw, "wrong-password")
    for _ in range(5):
        with pytest.raises(EchosAuthFailed):
            await bad.reboot()
    # Device is now locked: even correct credentials get 429.
    good = _make_client(fw, fw.admin_password)
    with pytest.raises(EchosLockedOut):
        await good.reboot()
    await bad.aclose()
    await good.aclose()


async def test_missing_retry_after_defaults(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    fw.locked = True
    fw.retry_after_header = False
    with pytest.raises(EchosLockedOut) as excinfo:
        await client.reboot()
    assert excinfo.value.retry_after_s == 30.0


# ----------------------------------------------------------------------
# Transport error mapping + retry policy
# ----------------------------------------------------------------------


async def test_get_retries_then_succeeds(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    fw.flaky["/api/status"] = 2
    status = await client.get_status()
    assert status.variant == "seedlink"
    assert len(fw.requests) == 3  # 1 attempt + 2 retries


async def test_get_retries_are_bounded(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    fw.flaky["/api/status"] = 10
    with pytest.raises(EchosUnreachable) as excinfo:
        await client.get_status()
    assert excinfo.value.kind == "unreachable"
    assert len(fw.requests) == 3  # never more than 1 + 2 retries


async def test_get_timeout_maps_to_echos_timeout(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    fw.timeout_paths.add("/api/status")
    with pytest.raises(EchosTimeout) as excinfo:
        await client.get_status()
    assert excinfo.value.kind == "timeout"
    assert len(fw.requests) == 3  # timeouts are retried like unreachable, same bound


async def test_writes_are_never_retried(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    fw.flaky["/api/system/reboot"] = 1
    with pytest.raises(EchosUnreachable):
        await client.reboot()
    assert fw.post_count("/api/system/reboot") == 1


async def test_non_json_body_is_protocol_error(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    fw.raw_responses["/api/status"] = httpx.Response(200, text="<html>not json</html>")
    with pytest.raises(EchosApiProtocolError) as excinfo:
        await client.get_status()
    assert excinfo.value.kind == "protocol"


async def test_schema_mismatch_is_protocol_error(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    fw.raw_responses["/api/status"] = httpx.Response(200, json={"unexpected": True})
    with pytest.raises(EchosApiProtocolError):
        await client.get_status()


async def test_http_500_is_protocol_error(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    fw.raw_responses["/api/status"] = httpx.Response(500, json={"error": "boom"})
    with pytest.raises(EchosApiProtocolError):
        await client.get_status()


# ----------------------------------------------------------------------
# Hot-reload: POST 202 + 7-step restart-status poll
# ----------------------------------------------------------------------


async def test_hot_reload_seven_steps(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    seen: list[RestartStatus] = []
    config = await client.get_seedlink_config()
    final = await client.apply_seedlink_config(
        config.model_copy(update={"port": 18001}),
        on_progress=seen.append,
        poll_interval_s=0.0,
        timeout_s=5.0,
    )
    assert final.state == "done"
    assert [s.step for s in seen] == [1, 2, 3, 4, 5, 6, 7]
    assert all(s.state == "in_progress" for s in seen[:-1])
    assert seen[-1].state == "done"
    # The new config is live on the device after the restart…
    assert fw.seedlink["port"] == 18001
    # …and the POST body was full (read-modify-write) minus read-only state.
    body = fw.last_post_body["/api/seedlink/config"]
    assert "has_password" not in body
    assert body["station"] == "ECH01"


async def test_hot_reload_partial_body_rejected(fw: FakeEchosFirmware) -> None:
    # Contract guard on the fake itself: a partial body must 400, proving
    # the read-modify-write client cannot get away with sending deltas.
    response = fw.handle(
        httpx.Request(
            "POST",
            "http://echos-test.local/api/seedlink/config",
            json={"port": 18001},
            headers={"Authorization": _basic_auth_header(fw.admin_password)},
        )
    )
    assert response.status_code == 400
    assert fw.restart_state == "idle"


async def test_hot_reload_failure_is_returned_not_raised(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    fw.fail_restart_at_step = 4
    config = await client.get_seedlink_config()
    final = await client.apply_seedlink_config(
        config, poll_interval_s=0.0, timeout_s=5.0
    )
    assert final.state == "failed"
    assert final.error == "simulated restart failure"
    # A failed restart must NOT apply the pending config.
    assert fw.seedlink["port"] == 18000


async def test_hot_reload_tolerates_transient_unreachable(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    # More consecutive failures than one GET's retry budget (1 + 2), so the
    # outer poll loop must absorb a whole failed GET and keep polling.
    fw.restart_unreachable_polls = 4
    config = await client.get_seedlink_config()
    final = await client.apply_seedlink_config(
        config.model_copy(update={"ring_records": 4096}),
        poll_interval_s=0.0,
        timeout_s=5.0,
    )
    assert final.state == "done"
    assert fw.seedlink["ring_records"] == 4096


async def test_hot_reload_poll_deadline_raises_timeout(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    fw.restart_hangs = True
    config = await client.get_seedlink_config()
    with pytest.raises(EchosTimeout):
        await client.apply_seedlink_config(config, poll_interval_s=0.01, timeout_s=0.1)


# ----------------------------------------------------------------------
# Calibration
# ----------------------------------------------------------------------


async def test_calibration_flow(fw: FakeEchosFirmware, client: EchosApiClient) -> None:
    assert (await client.get_calibration_status()).state == "idle"
    await client.start_calibration()
    seen: list[CalibrationStatus] = []
    for _ in range(10):
        status = await client.get_calibration_status()
        seen.append(status)
        if status.state == "done":
            break
    assert [s.phase for s in seen] == [1, 2, 3, 3]
    assert seen[-1].state == "done"
    results = await client.get_calibration_results()
    assert results.completed_at is not None
    assert [c.channel for c in results.channels] == ["HHZ", "HHN", "HHE"]
    assert results.channels[0].gain == pytest.approx(1.002)


# ----------------------------------------------------------------------
# Password change + rule 15 (credentials never logged)
# ----------------------------------------------------------------------


async def test_change_password_switches_client_credentials(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    await client.change_password("new-pass-123")
    assert fw.admin_password == "new-pass-123"
    # Subsequent writes authenticate with the new password.
    await client.reboot()


async def test_change_password_validates_locally(
    fw: FakeEchosFirmware, client: EchosApiClient
) -> None:
    with pytest.raises(ValueError, match="8-64"):
        await client.change_password("short")
    with pytest.raises(ValueError, match="printable ASCII"):
        await client.change_password("ottimo-ma-non-è-ascii")
    assert fw.requests == []  # neither attempt reached the device


async def test_credentials_never_in_logs_or_errors(
    fw: FakeEchosFirmware, capture_structlog: list[dict[str, Any]]
) -> None:
    real_secret = fw.admin_password
    wrong_secret = "wrong-password-42"
    bad = _make_client(fw, wrong_secret)
    with pytest.raises(EchosAuthFailed) as excinfo:
        await bad.reboot()
    fw.locked = True
    with pytest.raises(EchosLockedOut) as locked_info:
        await bad.reboot()
    logged = repr(capture_structlog)
    for secret in (real_secret, wrong_secret):
        assert secret not in logged
        assert secret not in str(excinfo.value)
        assert secret not in str(locked_info.value)
        assert secret not in repr(bad)
    await bad.aclose()
