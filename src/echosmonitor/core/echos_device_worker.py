"""Per-dialog Echos REST request worker (M1-D).

Skills: ``echos-rest-api`` (endpoints, read-modify-write, hot-reload,
lockout) and ``qt-worker-threading`` (this copies the proven
``EchosStatusWorker`` shape — queued request slots running
``asyncio.run`` on the worker thread, plain-method ``stop()`` with the
lock-registered task-cancel nudge).

One worker serves one open :class:`DeviceDialog`. Every slot takes the
target explicitly (host can change as the user edits the form), runs
the request synchronously inside the slot via ``asyncio.run`` (the
thread is never parked outside Qt's event loop), and reports back via
typed signals. Failures map to the closed ``EchosErrorKind`` set and
arrive on one ``failed`` signal carrying the operation name so the
dialog can re-enable exactly the surface that initiated the write.

Credentials (rule 15): the admin password is read from the
:class:`~echosmonitor.config.credentials.CredentialsStore` *on the
worker thread* (keyring access can block on D-Bus — never on the GUI
thread), is attached only to write requests by the API client, and
never appears in signals, logs or messages. ``changePassword`` updates
the device FIRST and the store only after the 200 (skill ordering).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

import structlog
from PySide6.QtCore import QObject, Signal, Slot

from echosmonitor.config.credentials import CredentialsStore
from echosmonitor.core.echos_api import (
    CalibrationStatus,
    EchosAcquisitionConfig,
    EchosApiClient,
    EchosNetworkConfig,
    OtaStatus,
    RestartStatus,
    SeedlinkServerConfig,
)
from echosmonitor.core.exceptions import EchosApiError, EchosLockedOut
from echosmonitor.core.models import EchosPollTarget

_log = structlog.get_logger(__name__)

_T = TypeVar("_T")

# Cadence of the hot-reload restart-status poll (skill: ~500 ms).
# Constructor-overridable so tests run the 7-step simulation instantly.
_RESTART_POLL_INTERVAL_S = 0.5


@dataclass(frozen=True, slots=True)
class EchosDeviceState:
    """Aggregate device snapshot for the dialog's server-side tabs.

    One ``requestLoad`` → one of these (rule 4 frozen payload). The
    pydantic members are themselves frozen. ``channels`` holds
    "NET.STA.LOC.CHA" strings parsed from the device StationXML —
    empty when the document was unavailable or unparseable (the rest
    of the load still succeeds; selector derivation just stays off).
    ``has_credentials`` tells the Connection tab whether a password is
    already stored for this device (the password itself never travels).
    """

    target: EchosPollTarget
    acquisition: EchosAcquisitionConfig
    seedlink: SeedlinkServerConfig
    network: EchosNetworkConfig
    ota: OtaStatus
    calibration: CalibrationStatus
    channels: tuple[str, ...]
    has_credentials: bool


def _default_client_factory(target: EchosPollTarget, password: str | None) -> EchosApiClient:
    return EchosApiClient(target.host, target.http_port, password)


def _parse_channels(xml: str) -> tuple[str, ...]:
    """Extract NSLC strings from a StationXML document (worker thread).

    obspy parsing is file/CPU work — exactly what this worker exists to
    keep off the GUI thread (rule 1). Parse failures degrade to ()
    because selector derivation is a convenience, not a load gate.
    """
    from obspy import read_inventory

    try:
        inventory = read_inventory(io.BytesIO(xml.encode("utf-8")), format="STATIONXML")
    except Exception as exc:
        _log.warning("echos_stationxml_unparseable", error_type=type(exc).__name__)
        return ()
    channels: list[str] = []
    for network in inventory:
        for station in network:
            for channel in station:
                channels.append(
                    f"{network.code}.{station.code}.{channel.location_code}.{channel.code}"
                )
    # Stable order, no duplicates (a profile can repeat epochs).
    return tuple(dict.fromkeys(channels))


class EchosDeviceWorker(QObject):
    """REST request engine behind one open device dialog.

    All request slots are queued from the GUI thread; results come back
    on the matching signal or on ``failed``. :meth:`stop` is a plain
    method (NOT a Slot) — call it directly, then ``thread.quit()`` +
    bounded ``thread.wait()`` (the dialog's ``done()`` does this).
    """

    # rule 4: object payloads are frozen dataclasses / frozen pydantic
    # models, isinstance-guarded at the receiver.
    loaded = Signal(object)  # EchosDeviceState
    applied = Signal(str)  # op name: "acquisition" | "network" | "calibrate_start" | "reboot"
    restartProgress = Signal(object)  # noqa: N815  # RestartStatus (one per observed step)
    seedlinkApplied = Signal(object)  # noqa: N815  # terminal RestartStatus (done OR failed)
    calibrationStatus = Signal(object)  # noqa: N815  # CalibrationStatus
    credentialStored = Signal(str)  # noqa: N815  # device key
    passwordChanged = Signal()  # noqa: N815
    # op, kind (closed EchosErrorKind), message, retry_after_s (0 unless locked_out)
    failed = Signal(str, str, str, float)

    def __init__(
        self,
        credentials: CredentialsStore,
        client_factory: Callable[[EchosPollTarget, str | None], EchosApiClient] | None = None,
        restart_poll_interval_s: float = _RESTART_POLL_INTERVAL_S,
    ) -> None:
        super().__init__()
        self._credentials = credentials
        self._factory = client_factory or _default_client_factory
        self._restart_poll_interval_s = restart_poll_interval_s
        self._stop_flag = False
        self._lock = threading.Lock()
        self._in_flight: tuple[asyncio.AbstractEventLoop, asyncio.Task[object]] | None = None

    # ------------------------------------------------------------------
    # Request slots — run on the worker thread (queued from the dialog)
    # ------------------------------------------------------------------
    @Slot(object)
    def requestLoad(self, target: object) -> None:  # noqa: N802 — Qt slot naming
        if not isinstance(target, EchosPollTarget):
            return
        state = self._run("load", self._load(target))
        if state is not None:
            self.loaded.emit(state)

    @Slot(object, object)
    def applyAcquisition(self, target: object, config: object) -> None:  # noqa: N802
        if not isinstance(target, EchosPollTarget) or not isinstance(
            config, EchosAcquisitionConfig
        ):
            return
        if self._run("acquisition", self._apply_acquisition(target, config)) is not None:
            self.applied.emit("acquisition")

    @Slot(object, object)
    def applySeedlink(self, target: object, config: object) -> None:  # noqa: N802
        if not isinstance(target, EchosPollTarget) or not isinstance(config, SeedlinkServerConfig):
            return
        final = self._run("seedlink", self._apply_seedlink(target, config))
        if final is not None:
            self.seedlinkApplied.emit(final)

    # NOTE: no applyNetwork — the firmware's network POST schema is not
    # pinned, so the client has no setter (decision log 2026-06-11).

    @Slot(object)
    def startCalibration(self, target: object) -> None:  # noqa: N802
        if not isinstance(target, EchosPollTarget):
            return
        if self._run("calibrate_start", self._start_calibration(target)) is not None:
            self.applied.emit("calibrate_start")

    @Slot(object)
    def pollCalibration(self, target: object) -> None:  # noqa: N802
        if not isinstance(target, EchosPollTarget):
            return
        status = self._run("calibrate_poll", self._poll_calibration(target))
        if status is not None:
            self.calibrationStatus.emit(status)

    @Slot(object, str)
    def changePassword(self, target: object, new_password: str) -> None:  # noqa: N802
        if not isinstance(target, EchosPollTarget):
            return
        result = self._run("password", self._change_password(target, new_password))
        if result is None:
            return
        # Skill ordering: keyring updates only AFTER the device's 200.
        self._credentials.set_password(target.name, new_password)
        _log.info("echos_device_password_rotated", device=target.name)
        self.passwordChanged.emit()

    @Slot(str, str)
    def storeCredential(self, device_key: str, password: str) -> None:  # noqa: N802
        """Persist the admin password locally (no device round-trip).

        Runs here (not on the GUI thread) because the keyring can block
        on D-Bus / unlock prompts (rule 1; CredentialsStore docstring).
        """
        if self._stop_flag or not device_key or not password:
            return
        self._credentials.set_password(device_key, password)
        self.credentialStored.emit(device_key)

    @Slot(object)
    def requestReboot(self, target: object) -> None:  # noqa: N802
        if not isinstance(target, EchosPollTarget):
            return
        if self._run("reboot", self._reboot(target)) is not None:
            self.applied.emit("reboot")

    # ------------------------------------------------------------------
    # Plain method (NOT a Slot). Callable from any thread.
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Stop the worker and cancel any in-flight request. Idempotent."""
        with self._lock:
            self._stop_flag = True
            in_flight = self._in_flight
        if in_flight is not None:
            loop, task = in_flight
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)

    # ------------------------------------------------------------------
    # Internals — worker thread only
    # ------------------------------------------------------------------
    def _run(self, op: str, coro: object) -> object | None:
        """Drive one request coroutine; map every failure to ``failed``.

        Returns the coroutine's result, or ``None`` after emitting
        ``failed`` / being cancelled — callers emit their success signal
        only on non-None. (``None`` is never a legitimate result here:
        every request coroutine returns a dataclass/model or ``True``.)
        """
        if self._stop_flag:
            _close_coroutine(coro)
            return None
        try:
            return asyncio.run(self._guard(coro))
        except asyncio.CancelledError:
            _log.info("echos_device_request_canceled", op=op)
        except EchosLockedOut as exc:
            _log.warning("echos_device_locked_out", op=op, retry_after_s=exc.retry_after_s)
            self.failed.emit(op, exc.kind, str(exc), exc.retry_after_s)
        except EchosApiError as exc:
            _log.warning("echos_device_request_failed", op=op, kind=exc.kind, error=str(exc))
            self.failed.emit(op, exc.kind, str(exc), 0.0)
        except Exception as exc:
            _log.exception("echos_device_unexpected_error", op=op, error=str(exc))
            self.failed.emit(op, "protocol", f"unexpected: {type(exc).__name__}: {exc}", 0.0)
        return None

    async def _guard(self, coro: object) -> object:
        """Register the running task so ``stop()`` can cancel it."""
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        with self._lock:
            if self._stop_flag:
                _close_coroutine(coro)
                raise asyncio.CancelledError
            self._in_flight = (loop, task)
        try:
            return await coro  # type: ignore[misc]
        finally:
            with self._lock:
                self._in_flight = None

    def _password_for(self, target: EchosPollTarget) -> str | None:
        return self._credentials.get_password(target.name)

    async def _load(self, target: EchosPollTarget) -> EchosDeviceState:
        password = self._password_for(target)
        async with self._factory(target, password) as client:
            acquisition = await client.get_acquisition_config()
            seedlink = await client.get_seedlink_config()
            network = await client.get_network_config()
            ota = await client.get_ota_status()
            calibration = await client.get_calibration_status()
            channels: tuple[str, ...] = ()
            try:
                channels = _parse_channels(await client.get_stationxml())
            except EchosApiError as exc:
                # Selector derivation is a convenience; the load stands.
                _log.warning(
                    "echos_stationxml_unavailable", device=target.name, kind=exc.kind
                )
        return EchosDeviceState(
            target=target,
            acquisition=acquisition,
            seedlink=seedlink,
            network=network,
            ota=ota,
            calibration=calibration,
            channels=channels,
            has_credentials=password is not None,
        )

    async def _apply_acquisition(
        self, target: EchosPollTarget, config: EchosAcquisitionConfig
    ) -> bool:
        async with self._factory(target, self._password_for(target)) as client:
            await client.set_acquisition_config(config)
        return True

    async def _apply_seedlink(
        self, target: EchosPollTarget, config: SeedlinkServerConfig
    ) -> RestartStatus:
        async with self._factory(target, self._password_for(target)) as client:
            return await client.apply_seedlink_config(
                config,
                on_progress=self.restartProgress.emit,
                poll_interval_s=self._restart_poll_interval_s,
            )

    async def _start_calibration(self, target: EchosPollTarget) -> bool:
        async with self._factory(target, self._password_for(target)) as client:
            await client.start_calibration()
        return True

    async def _poll_calibration(self, target: EchosPollTarget) -> CalibrationStatus:
        async with self._factory(target, None) as client:
            return await client.get_calibration_status()

    async def _change_password(self, target: EchosPollTarget, new_password: str) -> bool:
        async with self._factory(target, self._password_for(target)) as client:
            await client.change_password(new_password)
        return True

    async def _reboot(self, target: EchosPollTarget) -> bool:
        async with self._factory(target, self._password_for(target)) as client:
            await client.reboot()
        return True


def _close_coroutine(coro: object) -> None:
    """Close a never-awaited coroutine so CPython doesn't warn."""
    close = getattr(coro, "close", None)
    if callable(close):
        close()


__all__ = ["EchosDeviceState", "EchosDeviceWorker"]
