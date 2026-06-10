"""Echos device status poller — one shared worker on a dedicated QThread.

Skills: ``echos-rest-api`` (endpoints, lockout) and ``qt-worker-threading``
(the InfoWorker canon this class copies). Read both before changing.

Polls each configured Echos device's PUBLIC GET endpoints —
``/api/status``, ``/api/seedlink/status``, ``/api/calibrate/status`` —
at the device's ``poll_interval_s`` and emits one frozen
:class:`~echosmonitor.core.models.EchosDeviceSnapshot` per successful
poll. Public endpoints need no credentials, so this thread never touches
the keyring and can never trip the device's auth lockout (rule 15).

Threading model (skill patterns 1, 2, 4, 5):

* Parentless ``QObject`` moved to a ``QThread`` owned by ``MainWindow``;
  queued connections both ways; payloads are frozen dataclasses behind
  ``Signal(object)`` with isinstance guards on receipt (rule 4).
* The poll cadence comes from a ``QTimer`` constructed inside the
  queued ``start()`` slot — AFTER ``moveToThread`` — so its thread
  affinity is the worker thread (skill §5, reference ``MseedWriter``).
* Each due device is polled with ``asyncio.run`` *inside* the timer
  slot: the worker thread is never parked outside Qt's event loop
  between ticks, so queued ``configure`` slots always dispatch
  (skill §4 / POSTMORTEMS 2026-05-09b).
* ``stop()`` is a plain method (NOT a Slot), callable from any thread.
  The in-flight asyncio task is registered under a lock; ``stop()``
  cancels it via ``loop.call_soon_threadsafe(task.cancel)`` — the
  httpx/asyncio equivalent of the SeedLink worker's socket nudge — so
  a poll stuck on an unreachable device unwinds in milliseconds, not
  after the full HTTP timeout (rule 7).

Each poll's three GETs run SEQUENTIALLY on one keep-alive connection:
the ESP32's HTTP server has a handful of sockets and serves requests
serially — three parallel connections per tick per device would be
hostile for zero latency benefit on a LAN.

The poller client is built with ``get_retries=0``: a failed poll just
waits for the next tick, so transport retries would only stack latency
inside the slot (M1-A review note).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable

import structlog
from PySide6.QtCore import QObject, QTimer, Signal, Slot

from echosmonitor.core.echos_api import EchosApiClient
from echosmonitor.core.exceptions import EchosApiError
from echosmonitor.core.models import EchosDeviceSnapshot, EchosPollTarget

_log = structlog.get_logger(__name__)


# Scheduler tick. Each tick polls the devices whose ``poll_interval_s``
# has elapsed; 500 ms bounds how late a poll can start after its due
# time while keeping the idle wake-up cost negligible.
_TICK_MS = 500

# A snapshot poll is three sequential LAN GETs — sub-second when the
# device is healthy. Log a rule-7 elapsed warning when one runs longer
# than this (the device is up but crawling; worth surfacing).
_SLOW_POLL_WARN_S = 5.0


def _default_client_factory(target: EchosPollTarget) -> EchosApiClient:
    # No password: the poller only uses public GETs. get_retries=0 — see
    # module docstring.
    return EchosApiClient(target.host, target.http_port, get_retries=0)


class EchosStatusWorker(QObject):
    """Background status poller. One per application, owned by ``MainWindow``.

    Drive it with queued connections only:

    * ``QMetaObject.invokeMethod(worker, "start", QueuedConnection)``
      once, after ``thread.start()`` — constructs the worker-thread
      QTimer.
    * ``configure`` (queued ``Signal(object)``) with a
      ``tuple[EchosPollTarget, ...]`` — full replacement of the target
      set; newly-seen devices are polled on the next tick, known
      devices keep their due times.
    * :meth:`stop` directly (plain method) from the GUI thread, then
      ``thread.quit()`` + bounded ``thread.wait()``.

    ``client_factory`` exists for tests (inject an ``EchosApiClient``
    bound to a fake transport); production uses the default.
    """

    # One successful poll → one frozen EchosDeviceSnapshot (rule 4:
    # Signal(object) + isinstance guard at the receiver).
    snapshotReady = Signal(object)  # noqa: N815
    # device, kind (closed EchosErrorKind set), human-readable message.
    pollFailed = Signal(str, str, str)  # noqa: N815

    def __init__(
        self,
        client_factory: Callable[[EchosPollTarget], EchosApiClient] | None = None,
    ) -> None:
        super().__init__()
        self._client_factory = client_factory or _default_client_factory
        self._targets: tuple[EchosPollTarget, ...] = ()
        # Per-device monotonic deadline of the next poll. 0.0 = due now.
        self._next_due: dict[str, float] = {}
        self._timer: QTimer | None = None
        self._stop_flag = False
        # Guards the read-modify-write of ``_in_flight`` so ``stop()``
        # on the GUI thread can't observe a half-installed task while
        # the worker is between "task created" and "task registered"
        # (same shape as InfoWorker's token lock).
        self._lock = threading.Lock()
        self._in_flight: tuple[asyncio.AbstractEventLoop, asyncio.Task[object]] | None = None

    # ------------------------------------------------------------------
    # Slots — run on the worker thread (queued from GUI)
    # ------------------------------------------------------------------
    @Slot()
    def start(self) -> None:
        """Construct + start the poll timer (worker-thread affinity).

        Must be invoked via a queued connection AFTER ``moveToThread``
        so the QTimer lives on the worker thread (skill §5). Idempotent;
        a stopped worker stays stopped.
        """
        if self._timer is not None or self._stop_flag:
            return
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()
        _log.info("echos_status_worker_started", tick_ms=_TICK_MS)

    @Slot(object)
    def configure(self, targets: object) -> None:
        """Replace the polled device set (queued from the GUI thread).

        Payload contract (rule 4): ``tuple[EchosPollTarget, ...]``.
        Anything else is logged and ignored — a type drift upstream must
        not crash the worker thread.
        """
        if self._stop_flag:
            return
        if not isinstance(targets, tuple) or not all(
            isinstance(t, EchosPollTarget) for t in targets
        ):
            _log.warning("echos_status_bad_configure_payload", payload_type=type(targets).__name__)
            return
        self._targets = targets
        # Keep due times for devices that survive the change; new ones
        # poll on the next tick. Removed devices drop their entry.
        self._next_due = {t.name: self._next_due.get(t.name, 0.0) for t in targets}
        _log.info("echos_status_configured", device_count=len(targets))

    @Slot()
    def _on_tick(self) -> None:
        """Poll every due device, sequentially, on the worker thread.

        Worst case one tick blocks for (devices x HTTP timeout); during
        that window queued ``configure`` calls wait — acceptable for a
        status poller, and ``stop()`` still interrupts mid-poll via the
        task-cancel path because it is NOT queued behind this slot.
        """
        if self._stop_flag:
            return
        now = time.monotonic()
        for target in self._targets:
            if self._stop_flag:
                return
            if now < self._next_due.get(target.name, 0.0):
                continue
            self._poll_one(target)
            # Schedule from completion, not from the due time: a slow
            # device must not accumulate a poll backlog against itself.
            self._next_due[target.name] = time.monotonic() + target.poll_interval_s

    # ------------------------------------------------------------------
    # Plain method (NOT a Slot). Callable from any thread.
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Stop polling and cancel any in-flight poll. Idempotent.

        Must NOT be a ``@Slot`` — a queued stop would wait behind an
        in-flight poll slot (InfoWorker canon). The timer itself cannot
        be stopped cross-thread; the ``_stop_flag`` guard makes every
        subsequent tick a no-op until ``thread.quit()`` tears it down.
        """
        with self._lock:
            self._stop_flag = True
            in_flight = self._in_flight
        if in_flight is not None:
            loop, task = in_flight
            # The loop may finish between the lock release and this call;
            # a closed loop raises RuntimeError — the poll is already
            # over, which is what we wanted.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)

    # ------------------------------------------------------------------
    # Internals — worker thread only
    # ------------------------------------------------------------------
    def _poll_one(self, target: EchosPollTarget) -> None:
        started = time.monotonic()
        try:
            snapshot = asyncio.run(self._poll_async(target))
        except asyncio.CancelledError:
            _log.info("echos_poll_canceled", device=target.name)
            return
        except EchosApiError as exc:
            _log.warning(
                "echos_poll_failed",
                device=target.name,
                kind=exc.kind,
                error=str(exc),
                elapsed_s=round(time.monotonic() - started, 3),
            )
            self.pollFailed.emit(target.name, exc.kind, str(exc))
            return
        except Exception as exc:
            _log.exception("echos_poll_unexpected_error", device=target.name, error=str(exc))
            self.pollFailed.emit(
                target.name, "protocol", f"unexpected: {type(exc).__name__}: {exc}"
            )
            return
        elapsed = time.monotonic() - started
        if elapsed > _SLOW_POLL_WARN_S:
            _log.warning("echos_poll_slow", device=target.name, elapsed_s=round(elapsed, 3))
        if snapshot is not None and not self._stop_flag:
            self.snapshotReady.emit(snapshot)

    async def _poll_async(self, target: EchosPollTarget) -> EchosDeviceSnapshot | None:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None  # always inside asyncio.run
        with self._lock:
            if self._stop_flag:
                return None
            self._in_flight = (loop, task)
        try:
            async with self._client_factory(target) as client:
                # Sequential on purpose — see module docstring.
                status = await client.get_status()
                seedlink = await client.get_seedlink_status()
                calibration = await client.get_calibration_status()
        finally:
            with self._lock:
                self._in_flight = None
        return EchosDeviceSnapshot(
            device=target.name,
            firmware_version=status.firmware_version,
            uptime_s=status.uptime_s,
            gnss_fix=status.gnss.fix,
            gnss_satellites=status.gnss.satellites,
            pps_locked=status.gnss.pps_locked,
            clients_connected=seedlink.client_count,
            ring_used_pct=seedlink.ring_used_pct,
            calibration_state=calibration.state,
            polled_at=time.monotonic(),
        )


__all__ = ["EchosStatusWorker"]
