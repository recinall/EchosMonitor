"""Typed async REST client for Echos ``firmware_seedlink`` devices.

Skill reference: ``.claude/skills/echos-rest-api`` — the authoritative
endpoint map, Basic Auth + lockout semantics, and the hot-reload
(202 + ``restart-status`` poll) pattern. Read it before changing this file.

Design decisions baked in (rules 2, 7, 15):

- Pure networking module: no Qt, no file access, no global state. Callers
  run it off the GUI thread (the M1-C poller worker / asyncio under
  qasync) — rule 1 is the caller's responsibility, rule 2 is this file's.
- Every response is validated into a frozen pydantic model; raw dicts
  never cross the module boundary upward.
- Every failure maps to the closed set ``core.models.EchosErrorKind``
  via the ``EchosApiError`` hierarchy in ``core.exceptions``. Callers
  branch on exception type / ``kind``, never on message text.
- Every wait is bounded (rule 7): explicit httpx timeouts on every
  request, a wall-clock deadline + progress logging around the
  hot-reload restart poll, and asyncio cancellation as the interrupt
  path. GETs (public, idempotent) retry at most twice on transport
  failures; writes are NEVER retried (idempotency unknown).
- Lockout (rule 15): a 429 records the ``Retry-After`` window and every
  subsequent authenticated request fast-fails client-side with
  ``EchosLockedOut`` until the window expires — the app never hammers a
  locked device. GETs are public on this firmware and stay usable.
- Credentials: HTTP Basic over plain LAN HTTP. The password lives only
  in a private attribute, is excluded from ``repr``, and never appears
  in logs or exception messages (rule 15). Persistent storage
  (keyring / file fallback) is M1-B's job, not this module's.

JSON contract provenance: the READ contract was pinned against real
firmware (echos.local + pihw.local, fw 1aa72cbe, 2026-06-11) — every
GET model mirrors observed bodies; the test fake serves the same
shapes. Still unpinned because they are write-gated or unobserved:
the restart-status in-progress shape, the calibration sweep ``phase``
vocabulary, the seedlink client entry shape, and the network-config
POST schema (hence no network setter). Read models use
``extra="ignore"``; models that are POSTed back use ``extra="allow"``
so unmodelled firmware fields survive the read-modify-write.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Literal, TypeVar

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from echosmonitor.core.exceptions import (
    EchosApiError,
    EchosApiProtocolError,
    EchosAuthFailed,
    EchosLockedOut,
    EchosTimeout,
    EchosUnreachable,
)

_log = structlog.get_logger(__name__)


# The firmware's only privileged user (skill: every mutating endpoint
# authenticates as ``admin``).
_ADMIN_USER = "admin"

# Per-request transport bounds (skill: connect 5 s, read 10 s). Write and
# pool bounds are set explicitly so no httpx default sneaks in unbounded.
_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 10.0
_WRITE_TIMEOUT_S = 10.0
_POOL_TIMEOUT_S = 5.0

# GETs are public and idempotent on this firmware → bounded retries (≤2
# after the first attempt) on transport-level failures only. Writes get
# zero retries. The delay is constructor-overridable so tests run at 0.
_DEFAULT_GET_RETRIES = 2
_DEFAULT_RETRY_DELAY_S = 0.5

# Used when a 429 arrives without a parseable Retry-After header — the
# base of the device's exponential lockout ladder (30→60→…→3600 s).
_DEFAULT_LOCKOUT_S = 30.0

# Hot-reload restart poll (skill: ~500 ms cadence, bounded total wait
# ~30 s). The deadline is checked before each poll, so the worst case is
# bounded by ``timeout_s`` + one in-flight poll (itself httpx-bounded).
_RESTART_POLL_INTERVAL_S = 0.5
_RESTART_TIMEOUT_S = 30.0

# Password constraints enforced by the firmware (8-64 printable ASCII);
# validated client-side so a bad value fails before touching the device.
_PASSWORD_MIN_LEN = 8
_PASSWORD_MAX_LEN = 64


# ----------------------------------------------------------------------
# Typed response models (frozen; raw dicts never leave this module)
# ----------------------------------------------------------------------


class _FrozenModel(BaseModel):
    """Base for read-only wire models: immutable, additive-field tolerant."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class _FrozenRoundTripModel(BaseModel):
    """Base for models that are POSTed back (read-modify-write).

    ``extra="allow"`` so firmware fields this client does not model are
    preserved through GET → ``model_copy`` → POST instead of being
    silently dropped from the full-body write.
    """

    model_config = ConfigDict(frozen=True, extra="allow")


class DevicePosition(_FrozenModel):
    """``position`` block of ``GET /api/status`` (GNSS-derived)."""

    latitude: float
    longitude: float
    altitude: float = 0.0
    satellites: int = 0
    quality: int = 0


class PpsStatus(_FrozenModel):
    """``pps`` block of ``GET /api/status``."""

    offset_us: int = 0
    period_us: int = 0
    pulse_count: int = 0
    pll_locked: bool = False


class EchosStatus(_FrozenModel):
    """System snapshot from ``GET /api/status`` (pinned 2026-06-11)."""

    state: str
    firmware_version: str
    project_name: str = ""
    samples_acquired: int = 0
    missed_samples: int = 0
    time_synchronized: bool = False
    gnss_time_valid: bool = False
    time_sync_type: str = ""
    ntp_synchronized: bool = False
    wifi_mode: str = ""
    wifi_connected: bool = False
    position: DevicePosition | None = None
    pps: PpsStatus | None = None
    free_heap: int = 0


class SeedMetadata(_FrozenRoundTripModel):
    """``seed_metadata`` block of the acquisition config."""

    network: str = ""
    station: str = ""
    location: str = ""
    channel: str = ""


class EchosAcquisitionConfig(_FrozenRoundTripModel):
    """Acquisition config from ``GET /api/config`` (pinned 2026-06-11).

    ``osr`` is the ADC oversampling register setting (small integer, not
    the literal ratio). Per-channel PGA gains are flat ``gain_ch0..3``
    fields on the wire; :attr:`gains` is the ordered convenience view.
    """

    osr: int
    gain_ch0: int
    gain_ch1: int
    gain_ch2: int
    gain_ch3: int
    seed_metadata: SeedMetadata | None = None

    @property
    def gains(self) -> tuple[int, int, int, int]:
        return (self.gain_ch0, self.gain_ch1, self.gain_ch2, self.gain_ch3)


class KnownNetwork(_FrozenModel):
    """One stored WiFi network (credential-safe: presence flag only)."""

    ssid: str = ""
    has_password: bool = False


class EchosNetworkConfig(_FrozenModel):
    """WiFi/network config from ``GET /api/network/config`` (pinned 2026-06-11).

    READ-ONLY in this client: the firmware's POST schema for this
    endpoint is not yet pinned, and a guessed write can take a device
    off the network (decision log 2026-06-11).
    """

    known_networks: tuple[KnownNetwork, ...] = ()
    ap_ssid: str = ""
    has_ap_password: bool = False
    mdns_hostname: str = ""
    ntp_enabled: bool = True
    ntp_server: str = ""


class SeedlinkServerStatus(_FrozenModel):
    """Snapshot from ``GET /api/seedlink/status`` (pinned 2026-06-11).

    ``ring_slots_used == ring_slots_total`` is the steady state once the
    circular buffer has wrapped — it means "full history available",
    not "overflowing".
    """

    running: bool = False
    uptime_ms: int = 0
    active_clients: int = 0
    max_clients: int = 0
    ring_slots_used: int = 0
    ring_slots_total: int = 0
    current_sample_rate_sps: float = 0.0
    port: int = 18000

    @property
    def uptime_s(self) -> float:
        return self.uptime_ms / 1000.0

    @property
    def ring_used_pct(self) -> float:
        if self.ring_slots_total <= 0:
            return 0.0
        return 100.0 * self.ring_slots_used / self.ring_slots_total


class SeedlinkClientInfo(_FrozenModel):
    """One connected client from ``GET /api/seedlink/clients``.

    UNPINNED: no live client was connected during the 2026-06-11 contract
    smoke, so the entry shape is defensive — every field defaulted.
    Re-pin when a real client entry is observed.
    """

    slot: int = 0
    address: str = ""
    connected_s: float = 0.0
    packets_sent: int = 0


class _SeedlinkClientList(_FrozenModel):
    clients: tuple[SeedlinkClientInfo, ...] = ()
    count: int = 0


class StationXmlProfile(_FrozenRoundTripModel):
    """``stationxml`` block of the seedlink config (instrument response)."""

    sensor_sens: float = 0.0
    pz_pole_re: float = 0.0
    pz_pole_im: float = 0.0
    norm_factor: float = 0.0
    adc_vref: float = 0.0
    creation_date: str = ""


# Read-only / informational keys stripped from the seedlink write body.
# ``max_clients`` and ``keep_queue_depth`` are compile-time per the
# device's own ``note``; ``source``/``modifiable``/``note`` are echoes.
_SEEDLINK_WRITE_EXCLUDE = frozenset(
    {"source", "modifiable", "note", "max_clients", "keep_queue_depth"}
)


class SeedlinkServerConfig(_FrozenRoundTripModel):
    """SeedLink server config from ``GET /api/seedlink/config`` (pinned 2026-06-11).

    Writes go through :meth:`EchosApiClient.apply_seedlink_config`
    (read-modify-write, full body minus ``_SEEDLINK_WRITE_EXCLUDE``,
    hot-reload). Per the device's own ``note``: POST persists port /
    ring_buffer_kb / auth_required / record_size_bytes to NVS; port,
    ring and record size apply via the in-place restart; auth_required
    hot-applies on the next HELLO.
    """

    port: int
    ring_buffer_kb: int
    auth_required: bool = False
    record_size_bytes: Literal[512, 4096] = 512
    emit_hn1: bool = False
    max_clients: int = 0  # compile-time, read-only
    keep_queue_depth: int = 0  # compile-time, read-only
    stationxml: StationXmlProfile | None = None


class RestartStatus(_FrozenModel):
    """``GET /api/seedlink/restart-status`` (idle shape pinned 2026-06-11).

    Observed at rest: ``{"state": "idle", "applied": {}}``. The
    in-progress shape is WRITE-GATED (it only exists during a real
    authenticated config apply) and not yet pinned — ``step`` /
    ``total_steps`` / ``step_name`` are kept lenient for the test fake's
    7-step ladder and for whatever the real firmware reports.
    """

    state: str
    step: int = 0
    total_steps: int = 0
    step_name: str = ""
    error: str | None = None
    applied: dict[str, object] = Field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        # Provisional terminal heuristic until the real in-progress
        # shape is pinned: an explicit "done", or back to "idle" with a
        # non-empty ``applied`` echo of what the restart installed.
        return self.state == "done" or (self.state == "idle" and bool(self.applied))

    @property
    def is_failed(self) -> bool:
        return self.state in ("failed", "error")


class CalibrationStatus(_FrozenModel):
    """``GET /api/calibrate/status`` (idle shape pinned 2026-06-11).

    The sweep walks the PGA gain ladder (``total_gains`` = 8 observed),
    not a fixed 3-phase plan as first assumed. ``phase`` vocabulary
    beyond "idle" is WRITE-GATED (visible only during a real sweep) —
    treat it as an open string.
    """

    phase: str
    current_gain: int = 0
    total_gains: int = 0
    progress_percent: float = 0.0


class ChannelCalibration(_FrozenModel):
    """Per-channel block of one gain step in ``GET /api/calibration``."""

    offset_raw: int = 0
    offset_mv: float = 0.0
    noise_rms_uv: float = 0.0
    noise_bits: float = 0.0


class GainCalibration(_FrozenModel):
    """One PGA gain step of the calibration sweep results."""

    gain: int
    channels: tuple[ChannelCalibration, ...] = ()


class CalibrationResults(_FrozenModel):
    """``GET /api/calibration`` (pinned 2026-06-11): per-gain, per-channel."""

    valid: bool = False
    timestamp: str = ""
    gains: tuple[GainCalibration, ...] = ()


class OtaStatus(_FrozenModel):
    """``GET /api/ota/status`` (pinned 2026-06-11)."""

    current_version: str = ""
    new_version: str = ""
    state: str = "idle"
    progress_percent: float = 0.0
    error_message: str = ""
    is_running: bool = False
    running_partition: str = ""


_ModelT = TypeVar("_ModelT", bound=BaseModel)


# ----------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------


class EchosApiClient:
    """One async HTTP client per Echos device.

    Use as an async context manager, or call :meth:`aclose` explicitly.
    All methods raise only ``EchosApiError`` subclasses (plus
    ``ValueError`` for client-side input validation in
    :meth:`change_password`).
    """

    def __init__(
        self,
        host: str,
        http_port: int = 80,
        password: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        get_retries: int = _DEFAULT_GET_RETRIES,
        retry_delay_s: float = _DEFAULT_RETRY_DELAY_S,
    ) -> None:
        self._host = host
        self._password = password
        self._get_retries = get_retries
        self._retry_delay_s = retry_delay_s
        # time.monotonic() deadline of the device's auth lockout window;
        # 0.0 means "not locked". Set on every 429, checked before every
        # authenticated request (rule 15: never hammer).
        self._locked_until = 0.0
        self._client = httpx.AsyncClient(
            base_url=f"http://{host}:{http_port}",
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT_S,
                read=_READ_TIMEOUT_S,
                write=_WRITE_TIMEOUT_S,
                pool=_POOL_TIMEOUT_S,
            ),
            transport=transport,
        )

    def __repr__(self) -> str:
        # Deliberately omits the password (rule 15).
        return f"EchosApiClient(host={self._host!r}, base_url={str(self._client.base_url)!r})"

    @property
    def host(self) -> str:
        return self._host

    @property
    def lockout_remaining_s(self) -> float:
        """Seconds left in the known device lockout window (0 when usable)."""
        return max(0.0, self._locked_until - time.monotonic())

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> EchosApiClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- system ---------------------------------------------------------

    async def get_status(self) -> EchosStatus:
        return self._validate(EchosStatus, await self._get("/api/status"))

    async def get_stationxml(self) -> str:
        """Raw FDSN StationXML document (parse with obspy in core/positions.py)."""
        return (await self._get("/api/stationxml")).text

    async def get_ota_status(self) -> OtaStatus:
        return self._validate(OtaStatus, await self._get("/api/ota/status"))

    async def reboot(self) -> None:
        await self._post("/api/system/reboot")

    async def change_password(self, new_password: str) -> None:
        """Set a new admin password; on success the client switches to it.

        The keyring update is the caller's job (M1-B) and must happen only
        after this returns, mirroring the skill's update-after-200 rule.

        Raises:
            ValueError: if ``new_password`` violates the firmware's
                8-64 printable-ASCII constraint (checked client-side so
                the device never sees an invalid attempt).
        """
        if not _PASSWORD_MIN_LEN <= len(new_password) <= _PASSWORD_MAX_LEN:
            raise ValueError(
                f"password must be {_PASSWORD_MIN_LEN}-{_PASSWORD_MAX_LEN} characters"
            )
        if not all(32 <= ord(c) <= 126 for c in new_password):
            raise ValueError("password must be printable ASCII")
        await self._post("/api/auth/password", body={"new_password": new_password})
        self._password = new_password
        _log.info("echos_password_changed", host=self._host)

    # -- acquisition config ----------------------------------------------

    async def get_acquisition_config(self) -> EchosAcquisitionConfig:
        return self._validate(EchosAcquisitionConfig, await self._get("/api/config"))

    async def set_acquisition_config(self, config: EchosAcquisitionConfig) -> None:
        """Write the FULL acquisition config (read-modify-write; never partial)."""
        await self._post("/api/config", body=config.model_dump())

    # -- network config (READ-ONLY: write schema not yet pinned) -----------

    async def get_network_config(self) -> EchosNetworkConfig:
        # No setter on purpose: the firmware's POST schema for
        # /api/network/config is unverified, and a guessed write can take
        # a device off the network (decision log 2026-06-11).
        return self._validate(EchosNetworkConfig, await self._get("/api/network/config"))

    # -- seedlink server ---------------------------------------------------

    async def get_seedlink_status(self) -> SeedlinkServerStatus:
        return self._validate(SeedlinkServerStatus, await self._get("/api/seedlink/status"))

    async def get_seedlink_clients(self) -> tuple[SeedlinkClientInfo, ...]:
        wrapper = self._validate(_SeedlinkClientList, await self._get("/api/seedlink/clients"))
        return wrapper.clients

    async def get_seedlink_config(self) -> SeedlinkServerConfig:
        return self._validate(SeedlinkServerConfig, await self._get("/api/seedlink/config"))

    async def get_restart_status(self) -> RestartStatus:
        return self._validate(RestartStatus, await self._get("/api/seedlink/restart-status"))

    async def disconnect_client(self, slot: int) -> None:
        """Kick one SeedLink client by slot id (from :meth:`get_seedlink_clients`)."""
        await self._post(f"/api/seedlink/disconnect/{slot}")

    async def apply_seedlink_config(
        self,
        config: SeedlinkServerConfig,
        *,
        on_progress: Callable[[RestartStatus], None] | None = None,
        poll_interval_s: float = _RESTART_POLL_INTERVAL_S,
        timeout_s: float = _RESTART_TIMEOUT_S,
    ) -> RestartStatus:
        """Write the FULL seedlink config via the hot-reload pattern.

        POSTs the config (expects 202), then polls
        ``/api/seedlink/restart-status`` until the 7-step in-place restart
        reports ``done`` or ``failed``, invoking ``on_progress`` with each
        observed snapshot. The HTTP server may drop briefly mid-restart, so
        transient unreachable/timeout poll errors are tolerated until the
        wall-clock deadline (rule 7: bounded, observable, interruptible —
        cancellation propagates from the surrounding task).

        Returns the terminal ``RestartStatus``; callers branch on
        ``is_failed`` (a device-reported restart failure is domain
        state, not a transport error, so it does not raise). A 200
        (instead of 202) means the firmware applied the change without a
        restart (e.g. auth_required hot-applies) — synthesised as an
        immediate ``done``.

        Raises:
            EchosTimeout: the restart did not reach a terminal state
                within ``timeout_s``.
        """
        started = time.monotonic()
        _log.info("echos_seedlink_reload_started", host=self._host, port=config.port)
        body = config.model_dump()
        for key in _SEEDLINK_WRITE_EXCLUDE:
            body.pop(key, None)
        response = await self._post(
            "/api/seedlink/config",
            body=body,
            expect=(200, 202),
        )
        if response.status_code == 200:
            _log.info("echos_seedlink_reload_no_restart", host=self._host)
            return RestartStatus(state="done", applied=body)
        while True:
            elapsed = time.monotonic() - started
            if elapsed > timeout_s:
                _log.warning(
                    "echos_seedlink_reload_timeout",
                    host=self._host,
                    elapsed_s=round(elapsed, 3),
                )
                raise EchosTimeout(
                    f"seedlink restart on {self._host} did not finish within {timeout_s:.0f}s"
                )
            try:
                status = await self.get_restart_status()
            except (EchosUnreachable, EchosTimeout):
                _log.debug("echos_restart_poll_unreachable", host=self._host)
            else:
                if on_progress is not None:
                    on_progress(status)
                if status.is_done or status.is_failed:
                    _log.info(
                        "echos_seedlink_reload_finished",
                        host=self._host,
                        state=status.state,
                        error=status.error,
                        elapsed_s=round(time.monotonic() - started, 3),
                    )
                    return status
            await asyncio.sleep(poll_interval_s)

    # -- calibration -------------------------------------------------------

    async def start_calibration(self) -> None:
        """Kick off the full PGA-gain-ladder calibration sweep (poll after)."""
        await self._post("/api/calibrate/full", expect=(200, 202))

    async def get_calibration_status(self) -> CalibrationStatus:
        return self._validate(CalibrationStatus, await self._get("/api/calibrate/status"))

    async def get_calibration_results(self) -> CalibrationResults:
        return self._validate(CalibrationResults, await self._get("/api/calibration"))

    # -- request plumbing ---------------------------------------------------

    async def _get(self, path: str) -> httpx.Response:
        """Public GET with bounded transport retries (≤``_get_retries``)."""
        attempts = self._get_retries + 1
        for attempt in range(1, attempts + 1):
            started = time.monotonic()
            try:
                response = await self._client.get(path)
            except httpx.HTTPError as exc:
                error = self._map_transport_error(exc, "GET", path)
                if attempt < attempts:
                    _log.debug(
                        "echos_get_retry",
                        host=self._host,
                        path=path,
                        attempt=attempt,
                        kind=error.kind,
                    )
                    await asyncio.sleep(self._retry_delay_s)
                    continue
                _log.warning(
                    "echos_request_failed",
                    host=self._host,
                    method="GET",
                    path=path,
                    kind=error.kind,
                    elapsed_s=round(time.monotonic() - started, 3),
                )
                raise error from exc
            return self._check_status(response, method="GET", path=path, expect=(200,))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _post(
        self,
        path: str,
        *,
        body: dict[str, object] | None = None,
        expect: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        """Authenticated write: lockout-guarded, NEVER retried."""
        self._ensure_writable()
        started = time.monotonic()
        try:
            response = await self._client.post(
                path,
                json=body,
                auth=httpx.BasicAuth(_ADMIN_USER, self._password or ""),
            )
        except httpx.HTTPError as exc:
            error = self._map_transport_error(exc, "POST", path)
            _log.warning(
                "echos_request_failed",
                host=self._host,
                method="POST",
                path=path,
                kind=error.kind,
                elapsed_s=round(time.monotonic() - started, 3),
            )
            raise error from exc
        return self._check_status(response, method="POST", path=path, expect=expect)

    def _ensure_writable(self) -> None:
        """Fast-fail authenticated requests during a known lockout window."""
        remaining = self.lockout_remaining_s
        if remaining > 0:
            raise EchosLockedOut(
                remaining,
                f"device {self._host} auth lockout active for another {remaining:.0f}s",
            )
        if self._password is None:
            raise EchosAuthFailed(f"no admin password configured for device {self._host}")

    def _check_status(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
        expect: tuple[int, ...],
    ) -> httpx.Response:
        code = response.status_code
        if code in expect:
            return response
        if code == 401:
            _log.warning("echos_auth_failed", host=self._host, method=method, path=path)
            raise EchosAuthFailed(f"device {self._host} rejected admin credentials")
        if code == 429:
            retry_after = self._parse_retry_after(response)
            self._locked_until = time.monotonic() + retry_after
            _log.warning(
                "echos_locked_out",
                host=self._host,
                method=method,
                path=path,
                retry_after_s=retry_after,
            )
            raise EchosLockedOut(
                retry_after,
                f"device {self._host} auth lockout active; retry after {retry_after:.0f}s",
            )
        _log.warning(
            "echos_request_failed",
            host=self._host,
            method=method,
            path=path,
            kind="protocol",
            status_code=code,
        )
        raise EchosApiProtocolError(f"unexpected HTTP {code} for {method} {path}")

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float:
        header = response.headers.get("Retry-After")
        try:
            value = float(header) if header is not None else _DEFAULT_LOCKOUT_S
        except ValueError:
            return _DEFAULT_LOCKOUT_S
        return max(0.0, value)

    def _map_transport_error(self, exc: httpx.HTTPError, method: str, path: str) -> EchosApiError:
        if isinstance(exc, httpx.TimeoutException):
            return EchosTimeout(f"{method} {path} on {self._host} timed out")
        return EchosUnreachable(f"device {self._host} unreachable ({method} {path})")

    def _validate(self, model: type[_ModelT], response: httpx.Response) -> _ModelT:
        try:
            return model.model_validate_json(response.content)
        except ValidationError as exc:
            # ``exc`` enumerates field names only — never credential values.
            raise EchosApiProtocolError(
                f"device {self._host} response failed {model.__name__} validation: {exc}"
            ) from exc


async def fetch_stationxml(client: EchosApiClient) -> str | None:
    """Best-effort fetch of a device's FDSN StationXML (M6.6-B).

    Wraps the credential-less public ``GET /api/stationxml`` so a fetch
    NEVER raises across the worker boundary: any transport/protocol error
    is logged once and yields ``None``, so acquisition proceeds and the
    analysis path simply degrades to counts (rule 7 — observable, bounded;
    rule 15 — public GET, never trips the auth lockout).
    """
    try:
        xml = await client.get_stationxml()
    except EchosApiError as exc:
        _log.warning("stationxml_fetch_failed", kind=exc.kind, error=str(exc))
        return None
    except Exception as exc:  # defensive: never propagate from the helper
        _log.warning("stationxml_fetch_unexpected_error", error=str(exc))
        return None
    if not xml.strip():
        _log.warning("stationxml_fetch_empty")
        return None
    return xml
