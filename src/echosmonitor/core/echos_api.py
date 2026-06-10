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

JSON contract provenance: the firmware's exact field names are not yet
pinned against a real device — the wire contract is defined by the test
fake (``tests/core/echos_fake.py``), derived from the skill. Models use
``extra="ignore"`` so additive firmware fields cannot break parsing;
verify field names against real firmware before M1-C relies on them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Literal, TypeVar

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

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
    """Base for all wire models: immutable, tolerant of additive fields."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class GnssStatus(_FrozenModel):
    """GNSS block of ``GET /api/status``."""

    fix: bool
    satellites: int
    pps_locked: bool


class WifiStatus(_FrozenModel):
    """WiFi block of ``GET /api/status`` (credential-safe by contract)."""

    mode: Literal["sta", "ap"]
    ssid: str
    rssi_dbm: int
    ip: str


class EchosStatus(_FrozenModel):
    """System snapshot from ``GET /api/status``."""

    firmware_version: str
    variant: str
    uptime_s: float
    gnss: GnssStatus
    wifi: WifiStatus


class EchosAcquisitionConfig(_FrozenModel):
    """Acquisition config from ``GET /api/config`` (OSR + per-channel gains).

    ``gains`` is ordered by channel index; length 3 (velocimeter only) or
    4 (with HN1) depending on the hardware fit.
    """

    osr: int
    gains: tuple[int, ...]


class EchosNetworkConfig(_FrozenModel):
    """WiFi/network config from ``GET /api/network/config``.

    ``has_password`` is read-only device state (the device never returns
    the password itself); it is excluded from write bodies.
    """

    mode: Literal["sta", "ap"]
    ssid: str
    hostname: str
    has_password: bool = False


class SeedlinkServerStatus(_FrozenModel):
    """Snapshot from ``GET /api/seedlink/status``."""

    uptime_s: float
    client_count: int
    ring_used_pct: float


class SeedlinkClientInfo(_FrozenModel):
    """One connected client from ``GET /api/seedlink/clients``."""

    slot: int
    address: str
    connected_s: float
    packets_sent: int


class _SeedlinkClientList(_FrozenModel):
    clients: tuple[SeedlinkClientInfo, ...]


class SeedlinkServerConfig(_FrozenModel):
    """SeedLink server config from ``GET /api/seedlink/config``.

    Writes go through :meth:`EchosApiClient.apply_seedlink_config`
    (read-modify-write, full body, hot-reload). ``has_password`` is
    read-only device state and is excluded from the write body.
    """

    port: int
    ring_records: int
    record_size: Literal[512, 4096]
    auth_enabled: bool
    emit_hn1: bool
    network: str
    station: str
    stationxml_profile: str
    has_password: bool = False


class RestartStatus(_FrozenModel):
    """Progress of the 7-step in-place restart (``GET /api/seedlink/restart-status``)."""

    state: Literal["idle", "in_progress", "done", "failed"]
    step: int
    total_steps: int
    step_name: str = ""
    error: str | None = None


class CalibrationStatus(_FrozenModel):
    """Progress of the 3-phase calibration sweep (``GET /api/calibrate/status``)."""

    state: Literal["idle", "running", "done", "failed"]
    phase: int
    total_phases: int
    progress_pct: float
    error: str | None = None


class ChannelCalibration(_FrozenModel):
    """Per-channel result block of ``GET /api/calibration``."""

    channel: str
    gain: float
    offset_counts: float
    noise_rms_counts: float


class CalibrationResults(_FrozenModel):
    """Latest calibration results (in-RAM on the device; lost on reboot)."""

    completed_at: str | None
    channels: tuple[ChannelCalibration, ...]


class OtaStatus(_FrozenModel):
    """Partition / image state from ``GET /api/ota/status``."""

    running_partition: str
    ota_state: str
    app_version: str


_ModelT = TypeVar("_ModelT", bound=_FrozenModel)


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

    # -- network config ---------------------------------------------------

    async def get_network_config(self) -> EchosNetworkConfig:
        return self._validate(EchosNetworkConfig, await self._get("/api/network/config"))

    async def set_network_config(
        self, config: EchosNetworkConfig, wifi_password: str | None = None
    ) -> None:
        """Write the FULL network config; ``wifi_password`` only when changing it.

        ``has_password`` is device-computed state and is stripped from the
        body; the WiFi password is write-only and never stored on this
        object nor logged.
        """
        body = config.model_dump(exclude={"has_password"})
        if wifi_password is not None:
            body["password"] = wifi_password
        await self._post("/api/network/config", body=body)

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
        ``state == "failed"`` (a device-reported restart failure is domain
        state, not a transport error, so it does not raise).

        Raises:
            EchosTimeout: the restart did not reach a terminal state
                within ``timeout_s``.
        """
        started = time.monotonic()
        _log.info("echos_seedlink_reload_started", host=self._host, port=config.port)
        await self._post(
            "/api/seedlink/config",
            body=config.model_dump(exclude={"has_password"}),
            expect=(202,),
        )
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
                if status.state in ("done", "failed"):
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
        """Kick off the full 3-phase calibration sweep (poll status after)."""
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
