"""In-memory fake of the Echos ``firmware_seedlink`` REST API.

Wire contract source: ``.claude/skills/echos-rest-api``. This fake *is*
the pinned JSON contract for ``core/echos_api.py`` until the field names
are verified against real firmware (see the module docstring there).

Designed for reuse beyond the M1-A unit tests: the M1-D device dialog
tests drive the same ``FakeEchosFirmware`` through a real
``EchosApiClient`` by passing ``transport=fake.transport``.

Simulated device behaviours:

- HTTP Basic Auth (user ``admin``) on every POST; public GETs.
- Auth lockout: 5 consecutive failures → every authenticated request
  answers 429 with ``Retry-After`` (header omittable via
  ``retry_after_header=False``).
- Hot-reload: ``POST /api/seedlink/config`` validates a FULL body,
  answers 202 and starts the 7-step in-place restart; each
  ``restart-status`` poll advances one step; the pending config is
  applied when the restart completes. Knobs: ``fail_restart_at_step``,
  ``restart_hangs``, ``restart_unreachable_polls`` (raises
  ``httpx.ConnectError`` for the first N polls, like the real device's
  HTTP server dropping mid-restart).
- Calibration: 3 phases, one per status poll.
- Fault injection: ``flaky[path] = n`` raises ``ConnectError`` for the
  next n requests to ``path``; ``timeout_paths`` raises ``ReadTimeout``;
  ``raw_responses[path]`` overrides the response entirely.

Every request (including faulted ones) is appended to ``requests`` as
``(method, path)`` so tests can assert exact attempt counts (retry
bounds, lockout fast-fail without device traffic).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx

_RESTART_STEP_NAMES = (
    "drain_clients",
    "stop_server",
    "reallocate_ring",
    "apply_config",
    "start_server",
    "announce",
    "ready",
)

# Required keys for full-body writes (read-modify-write contract): a POST
# missing any of these is a partial body and gets a 400, like the firmware.
_ACQUISITION_KEYS = frozenset({"osr", "gains"})
_NETWORK_KEYS = frozenset({"mode", "ssid", "hostname"})
_SEEDLINK_KEYS = frozenset(
    {
        "port",
        "ring_records",
        "record_size",
        "auth_enabled",
        "emit_hn1",
        "network",
        "station",
        "stationxml_profile",
    }
)

# Minimal-but-valid FDSN StationXML 1.2: 3 velocimeter channels, the
# shape the firmware's /api/stationxml serves. Parsed by obspy in
# core/echos_device_worker._parse_channels for selector derivation.
_STATIONXML = """<?xml version="1.0" encoding="UTF-8"?>
<FDSNStationXML xmlns="http://www.fdsn.org/xml/station/1" schemaVersion="1.2">
  <Source>Echos firmware_seedlink</Source>
  <Created>2026-01-01T00:00:00Z</Created>
  <Network code="XX">
    <Station code="ECH01">
      <Latitude>45.4</Latitude>
      <Longitude>11.9</Longitude>
      <Elevation>20.0</Elevation>
      <Site><Name>Echos field node</Name></Site>
      <Channel code="HHZ" locationCode="">
        <Latitude>45.4</Latitude>
        <Longitude>11.9</Longitude>
        <Elevation>20.0</Elevation>
        <Depth>0.0</Depth>
        <SampleRate>500.0</SampleRate>
      </Channel>
      <Channel code="HHN" locationCode="">
        <Latitude>45.4</Latitude>
        <Longitude>11.9</Longitude>
        <Elevation>20.0</Elevation>
        <Depth>0.0</Depth>
        <SampleRate>500.0</SampleRate>
      </Channel>
      <Channel code="HHE" locationCode="">
        <Latitude>45.4</Latitude>
        <Longitude>11.9</Longitude>
        <Elevation>20.0</Elevation>
        <Depth>0.0</Depth>
        <SampleRate>500.0</SampleRate>
      </Channel>
    </Station>
  </Network>
</FDSNStationXML>
"""


class FakeEchosFirmware:
    """Stateful fake served through ``httpx.MockTransport`` (see module docstring)."""

    def __init__(self, *, admin_password: str = "hunter22!pw") -> None:
        self.admin_password = admin_password
        self.requests: list[tuple[str, str]] = []
        # Last accepted POST body per path (for body-shape assertions).
        self.last_post_body: dict[str, dict[str, Any]] = {}

        self.status: dict[str, Any] = {
            "firmware_version": "1.4.2",
            "variant": "seedlink",
            "uptime_s": 3600.5,
            "gnss": {"fix": True, "satellites": 9, "pps_locked": True},
            "wifi": {"mode": "sta", "ssid": "field-net", "rssi_dbm": -61, "ip": "192.168.1.50"},
        }
        self.acquisition: dict[str, Any] = {"osr": 64, "gains": [1, 1, 1, 8]}
        self.network: dict[str, Any] = {
            "mode": "sta",
            "ssid": "field-net",
            "hostname": "echos",
            "has_password": True,
        }
        self.seedlink: dict[str, Any] = {
            "port": 18000,
            "ring_records": 2048,
            "record_size": 512,
            "auth_enabled": False,
            "emit_hn1": False,
            "network": "XX",
            "station": "ECH01",
            "stationxml_profile": "default",
            "has_password": False,
        }
        self.clients: list[dict[str, Any]] = [
            {"slot": 0, "address": "192.168.1.10:54321", "connected_s": 120.0, "packets_sent": 4096}
        ]
        self.ota: dict[str, Any] = {
            "running_partition": "ota_0",
            "ota_state": "valid",
            "app_version": "1.4.2",
        }
        self.stationxml = _STATIONXML

        # Auth / lockout state (RAM-only on the real device too).
        self.auth_failures = 0
        self.locked = False
        self.retry_after_s = 30
        self.retry_after_header = True

        # 7-step in-place restart simulation.
        self.restart_state = "idle"
        self.restart_step = 0
        self.total_restart_steps = len(_RESTART_STEP_NAMES)
        self.pending_seedlink: dict[str, Any] | None = None
        self.fail_restart_at_step: int | None = None
        self.restart_hangs = False
        self.restart_unreachable_polls = 0

        # 3-phase calibration simulation.
        self.cal_state = "idle"
        self.cal_phase = 0
        self.cal_total_phases = 3

        # Fault injection.
        self.flaky: dict[str, int] = {}
        self.timeout_paths: set[str] = set()
        self.raw_responses: dict[str, httpx.Response] = {}

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def post_count(self, path: str) -> int:
        return sum(1 for method, p in self.requests if method == "POST" and p == path)

    # -- request dispatch -------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        self.requests.append((method, path))

        if self.flaky.get(path, 0) > 0:
            self.flaky[path] -= 1
            raise httpx.ConnectError("simulated connect failure", request=request)
        if path in self.timeout_paths:
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        if path == "/api/seedlink/restart-status" and self.restart_unreachable_polls > 0:
            self.restart_unreachable_polls -= 1
            raise httpx.ConnectError("simulated mid-restart drop", request=request)
        if path in self.raw_responses:
            return self.raw_responses[path]

        if method == "GET":
            return self._handle_get(path)
        if method == "POST":
            denied = self._check_auth(request)
            if denied is not None:
                return denied
            return self._handle_post(path, request)
        return httpx.Response(405, json={"error": "method_not_allowed"})

    def _check_auth(self, request: httpx.Request) -> httpx.Response | None:
        if self.locked:
            headers = {"Retry-After": str(self.retry_after_s)} if self.retry_after_header else {}
            return httpx.Response(429, headers=headers, json={"error": "locked_out"})
        token = base64.b64encode(f"admin:{self.admin_password}".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {token}":
            self.auth_failures += 1
            if self.auth_failures >= 5:
                self.locked = True
            return httpx.Response(401, json={"error": "auth_failed"})
        self.auth_failures = 0
        return None

    # -- GET routes ---------------------------------------------------------

    def _handle_get(self, path: str) -> httpx.Response:
        if path == "/api/status":
            return httpx.Response(200, json=self.status)
        if path == "/api/config":
            return httpx.Response(200, json=self.acquisition)
        if path == "/api/network/config":
            return httpx.Response(200, json=self.network)
        if path == "/api/seedlink/status":
            return httpx.Response(
                200,
                json={
                    "uptime_s": 3600.0,
                    "client_count": len(self.clients),
                    "ring_used_pct": 12.5,
                },
            )
        if path == "/api/seedlink/clients":
            return httpx.Response(200, json={"clients": self.clients})
        if path == "/api/seedlink/config":
            return httpx.Response(200, json=self.seedlink)
        if path == "/api/seedlink/restart-status":
            return self._restart_status()
        if path == "/api/calibrate/status":
            return self._calibration_status()
        if path == "/api/calibration":
            return httpx.Response(
                200,
                json={
                    "completed_at": "2026-06-10T12:00:00Z" if self.cal_state == "done" else None,
                    "channels": [
                        {
                            "channel": "HHZ",
                            "gain": 1.002,
                            "offset_counts": 12.5,
                            "noise_rms_counts": 3.1,
                        },
                        {
                            "channel": "HHN",
                            "gain": 0.998,
                            "offset_counts": -4.0,
                            "noise_rms_counts": 2.9,
                        },
                        {
                            "channel": "HHE",
                            "gain": 1.001,
                            "offset_counts": 7.25,
                            "noise_rms_counts": 3.0,
                        },
                    ],
                },
            )
        if path == "/api/stationxml":
            return httpx.Response(
                200, text=self.stationxml, headers={"Content-Type": "application/xml"}
            )
        if path == "/api/ota/status":
            return httpx.Response(200, json=self.ota)
        return httpx.Response(404, json={"error": "not_found"})

    def _restart_status(self) -> httpx.Response:
        if self.restart_state == "in_progress" and not self.restart_hangs:
            self.restart_step += 1
            if (
                self.fail_restart_at_step is not None
                and self.restart_step >= self.fail_restart_at_step
            ):
                self.restart_state = "failed"
                return httpx.Response(
                    200,
                    json={
                        "state": "failed",
                        "step": self.restart_step,
                        "total_steps": self.total_restart_steps,
                        "step_name": _RESTART_STEP_NAMES[self.restart_step - 1],
                        "error": "simulated restart failure",
                    },
                )
            if self.restart_step >= self.total_restart_steps:
                self.restart_state = "done"
                if self.pending_seedlink is not None:
                    self.seedlink = {**self.seedlink, **self.pending_seedlink}
                    self.pending_seedlink = None
        step = min(max(self.restart_step, 1), self.total_restart_steps)
        return httpx.Response(
            200,
            json={
                "state": self.restart_state if self.restart_state != "idle" else "idle",
                "step": self.restart_step,
                "total_steps": self.total_restart_steps,
                "step_name": _RESTART_STEP_NAMES[step - 1] if self.restart_step > 0 else "",
                "error": None,
            },
        )

    def _calibration_status(self) -> httpx.Response:
        if self.cal_state == "running":
            self.cal_phase += 1
            if self.cal_phase > self.cal_total_phases:
                self.cal_state = "done"
                self.cal_phase = self.cal_total_phases
        return httpx.Response(
            200,
            json={
                "state": self.cal_state,
                "phase": self.cal_phase,
                "total_phases": self.cal_total_phases,
                "progress_pct": round(100.0 * self.cal_phase / self.cal_total_phases, 1),
                "error": None,
            },
        )

    # -- POST routes ----------------------------------------------------------

    def _handle_post(self, path: str, request: httpx.Request) -> httpx.Response:
        if path == "/api/config":
            return self._accept_full_body(path, request, _ACQUISITION_KEYS, self.acquisition)
        if path == "/api/network/config":
            body = self._json_body(request)
            if body is None or not _NETWORK_KEYS.issubset(body):
                return httpx.Response(400, json={"error": "invalid_config"})
            self.last_post_body[path] = body
            has_password = self.network["has_password"] or "password" in body
            self.network = {k: body[k] for k in _NETWORK_KEYS}
            self.network["has_password"] = has_password
            return httpx.Response(200, json={"status": "ok"})
        if path == "/api/seedlink/config":
            body = self._json_body(request)
            if body is None or not _SEEDLINK_KEYS.issubset(body):
                return httpx.Response(400, json={"error": "invalid_config"})
            self.last_post_body[path] = body
            self.pending_seedlink = body
            self.restart_state = "in_progress"
            self.restart_step = 0
            return httpx.Response(202, json={"status": "restarting"})
        if path.startswith("/api/seedlink/disconnect/"):
            slot = int(path.rsplit("/", 1)[-1])
            before = len(self.clients)
            self.clients = [c for c in self.clients if c["slot"] != slot]
            if len(self.clients) == before:
                return httpx.Response(404, json={"error": "no_such_client"})
            return httpx.Response(200, json={"status": "disconnected"})
        if path == "/api/calibrate/full":
            self.cal_state = "running"
            self.cal_phase = 0
            return httpx.Response(202, json={"status": "calibration_started"})
        if path == "/api/auth/password":
            body = self._json_body(request)
            new = body.get("new_password") if body else None
            if not isinstance(new, str) or not 8 <= len(new) <= 64:
                return httpx.Response(400, json={"error": "invalid_password"})
            self.admin_password = new
            return httpx.Response(200, json={"status": "password_updated"})
        if path == "/api/system/reboot":
            return httpx.Response(200, json={"status": "rebooting"})
        return httpx.Response(404, json={"error": "not_found"})

    def _accept_full_body(
        self,
        path: str,
        request: httpx.Request,
        required: frozenset[str],
        target: dict[str, Any],
    ) -> httpx.Response:
        body = self._json_body(request)
        if body is None or not required.issubset(body):
            return httpx.Response(400, json={"error": "invalid_config"})
        self.last_post_body[path] = body
        target.clear()
        target.update(body)
        return httpx.Response(200, json={"status": "ok"})

    @staticmethod
    def _json_body(request: httpx.Request) -> dict[str, Any] | None:
        try:
            body = json.loads(request.content.decode())
        except (ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None
