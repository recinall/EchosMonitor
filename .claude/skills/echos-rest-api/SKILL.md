---
name: echos-rest-api
description: Authoritative reference for talking to Echos firmware_seedlink devices — REST endpoints, HTTP Basic Auth + lockout semantics, the hot-reload (202 + restart-status poll) pattern, calibration, StationXML, OTA, and the SeedLink v3 TCP command set on port 18000. ALWAYS consult this before writing or reviewing any code in core/echos_api.py, the device configuration dialog, the status poller, the first-run wizard, device discovery, or any test fake of the firmware.
---

# Echos `firmware_seedlink` API

The device is an ESP32-S3 node: HTTP REST server (default port 80, plain
HTTP) + SeedLink v3 TCP server (default port 18000). 3 velocimeter channels
(+ optional 4th HN1, runtime toggle `emit_hn1`). No SD, no DSP, no file
manager on this variant.

## Conventions

- Every `GET` status/config endpoint is **public** (no credentials),
  read-only. Every mutating `POST`/`DELETE` requires **HTTP Basic Auth**
  (user `admin`). Mutating paths also accept public `OPTIONS` preflight.
- Plain HTTP: Basic Auth is base64-on-the-wire. NEVER log the Authorization
  header or password. Store the password in the OS keyring (fallback: local
  file outside the YAML, chmod 600, warn once).
- `has_password` is a boolean in config responses — the device never returns
  the password in any form.
- JSON request/response bodies; `Content-Type: application/json` on POST.

## Auth lockout (must be handled by the client)

After 5 consecutive auth failures the device locks: every authenticated
request returns `429 Too Many Requests` with a `Retry-After` header.
Exponential ladder: 30 s → 60 s → 120 s → … capped at 3600 s. A success
resets the counter; lockout state is RAM-only (reboot clears it, but reboot
is itself gated). Client behaviour: on 429, read `Retry-After`, surface it
in the UI, disable further writes until expiry, never auto-retry inside the
window.

First boot after clean flash: random 12-char password printed once to
serial; the app cannot recover it — the first-run wizard must instruct the
user to read serial or factory-reset (button B ≥ 5 s → AP mode at
`http://192.168.4.1`).

Password change: `POST /api/auth/password` body
`{"new_password": "..."}` (8–64 printable ASCII) → `200
{"status":"password_updated"}`. Update the keyring atomically with the
device, in that order only after the 200.

## REST endpoints (seedlink variant)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/status` | public | System snapshot (uptime, GNSS, wifi, fw version) |
| GET | `/api/config` | public | Acquisition config (OSR, per-channel gains, …) |
| POST | `/api/config` | admin | Write acquisition config |
| GET | `/api/network/config` | public | WiFi/network (credential-safe) |
| POST | `/api/network/config` | admin | Write WiFi/network |
| GET | `/api/seedlink/status` | public | Uptime / client count / ring usage |
| GET | `/api/seedlink/clients` | public | Active client list + diagnostics |
| GET | `/api/seedlink/config` | public | Port, ring size, auth gate, record size (512/4096), `emit_hn1`, StationXML profile |
| POST | `/api/seedlink/config` | admin | **Hot-reload**: returns `202`; poll below |
| GET | `/api/seedlink/restart-status` | public | 7-step in-place restart progress |
| POST | `/api/seedlink/disconnect/{id}` | admin | Kick a client by slot id |
| GET | `/api/calibration` | public | Latest calibration results (in-RAM, 3-phase) |
| POST | `/api/calibrate/full` | admin | Start full calibration sweep |
| GET | `/api/calibrate/status` | public | Calibration progress (poll) |
| GET | `/api/stationxml` | public | FDSN StationXML 1.2 — channel codes match the streamed set; full response (sensitivity + geophone poles/zeros + ADC stage). **Source of device lat/lon and obspy Inventory for deconvolution/HVSR.** |
| GET | `/api/ota/status` | public | Partition / state |
| POST | `/api/ota/update/url` | admin | OTA from URL |
| POST | `/api/ota/upload` | admin | Binary body; optional `X-Firmware-SHA256` (64-hex) verified streaming; mismatch → `400 {"error":"checksum_mismatch"}`. Same-variant only. |
| POST | `/api/ota/rollback` | admin | Roll back image |
| POST | `/api/system/reboot` | admin | Reboot |
| POST | `/api/auth/password` | admin | See above |
| POST | `/api/gnss/ubx` | admin | UBX passthrough; `503` when Kconfig-disabled — treat as "feature absent", not error |
| GET | `/api/diag/reset-reason` | public | Kconfig-gated reset log |

No `/api/schedules`, no trigger control, no DSP endpoints, no file manager
on this variant — do not implement client code for them.

## Hot-reload pattern (the one tricky write)

`POST /api/seedlink/config` applies port / ring size / auth / OSR changes
via an in-place 7-step restart, **no reboot**:

1. POST the FULL config object (read-modify-write: GET current → mutate →
   POST; never send partial bodies).
2. Expect `202`. Poll `GET /api/seedlink/restart-status` (e.g. every 500 ms,
   bounded total wait ~30 s, rule 7 logging) until done/failed.
3. The SeedLink TCP connection drops during the restart — the app's
   SeedLinkWorker will reconnect via its normal backoff; if the PORT
   changed, push the new port through ConfigStore so the worker restarts on
   the right endpoint (engine diff handles it).
4. Surface each step + outcome in the dialog.

## Position / metadata

`GET /api/stationxml` is the truth for NET/STA/LOC/CHA codes, sample rate,
instrument response and station coordinates. Parse with
`obspy.read_inventory(..., format="STATIONXML")`. Derive the app's
selectors from its channel list instead of asking the user to type NSLCs.
Manual position override in app config wins over StationXML (rule 16).

## SeedLink TCP (port 18000) — for fakes/tests

Banner: `SeedLink v3.3 (Echos) :: SLPROTO:3.0`. Commands: `HELLO`,
`STATION`/`SELECT` (4-tuple, `*`/`?` wildcards), `DATA`/`FETCH` (optional
sequence number), `TIME YYYY,MM,DD,hh,mm,ss` (server-side seek),
`INFO STATIONS|STREAMS|CAPABILITIES` (XML), `END`/`BYE`,
`USER`/`PASSWORD` when the runtime auth gate is on. Records 512 B or
4096 B MiniSEED. When the auth gate is enabled the app must send
USER/PASSWORD before DATA — obspy's EasySeedLinkClient does not do this:
check whether the gate is on via `/api/seedlink/config` and either warn the
user or extend the worker's handshake.

## Client implementation rules (core/echos_api.py)

- httpx.AsyncClient per device, explicit timeouts (connect 5 s, read 10 s),
  no retries on writes (idempotency unknown), bounded retries (≤2) on GETs.
- All methods typed; responses validated into frozen pydantic models
  (`EchosStatus`, `EchosConfig`, `SeedlinkServerConfig`, …) — never pass raw
  dicts upward.
- Runs off the GUI thread (poller worker / asyncio under qasync) — rule 1.
- Map errors to a closed set: `auth_failed`, `locked_out(retry_after_s)`,
  `unreachable`, `timeout`, `protocol` — the dialog branches on these, never
  on message text.
