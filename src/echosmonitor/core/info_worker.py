"""Qt worker wrapping the synchronous ``core/info`` client.

One :class:`InfoWorker` lives on a dedicated ``QThread`` owned by the
main window — *not* per device, *not* inside the streaming engine.
Two non-obvious decisions are baked in:

1. **Single shared worker.** The streaming worker's ``client.run()``
   blocks the device QThread's event loop while connected, so a
   queued INFO slot dispatched onto that thread would never fire.
   INFO is also a UI concern (one click → one query) and serialises
   nicely on a single thread.
2. **No internal request queue, no ``run()``.** The worker thread's
   Qt event loop dispatches each request slot directly. While a slot
   is running ``info.fetch`` synchronously, no other slot dispatches —
   which is exactly what we want for UI-paced INFO requests. The
   first iteration of this module wrapped a Python ``queue.Queue`` /
   ``run()`` consumer pattern inside the QThread; it deadlocked,
   because Qt's queued connection cannot dispatch a slot to a thread
   whose stack is parked inside ``queue.get()``. POSTMORTEMS
   2026-05-09b records the failure mode.

Cancellation contract:

* ``stop()`` is a plain Python method (NOT a ``Slot``). It is invoked
  directly from the GUI thread on close. It writes ``_stop`` (atomic
  bool) and signals any in-flight :class:`CancellationToken`. Both
  primitives are thread-safe; a small lock guards the read-modify-
  write of ``_in_flight`` so a concurrent slot doesn't observe a
  half-installed token.
* The currently-running fetch sees ``cancel.is_set()`` in its
  preflight loop or its watchdog, raises :class:`InfoCanceled`, and
  the slot returns. The worker thread is then back in ``exec()`` and
  the host can ``thread.quit() + thread.wait()``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import QObject, Signal, Slot

from echosmonitor.core import info
from echosmonitor.core.exceptions import (
    InfoCanceled,
    InfoError,
    InfoProtocolError,
    InfoTimeout,
)
from echosmonitor.core.info import CancellationToken

if TYPE_CHECKING:
    from echosmonitor.core.info import (
        ServerIdentity,
        StationInfo,
        StreamInfo,
    )

_log = structlog.get_logger(__name__)

# Wall-clock deadline used for every INFO fetch dispatched by this
# worker. Matches the InfoClient default and is well above the
# diagnostic threshold (``2 * connect_timeout``) so a transcontinental
# server has time to reply.
_INFO_FETCH_TIMEOUT_S = 30.0


class InfoWorker(QObject):
    """Background INFO-fetch worker. One per application, owned by ``MainWindow``.

    Each request slot runs on the worker thread (queued from the GUI
    thread) and synchronously calls :func:`info.fetch` /
    :func:`info.fetch_streams`, then re-emits the typed result via the
    matching ``*Received`` signal — or :attr:`infoFailed` if any of
    the InfoError family was raised.

    The worker echoes the caller-supplied ``request_id`` in every reply
    signal. The UI compares it against its current pending id and
    discards stale replies (e.g. the user moved on before the server
    finished responding) — no need for cross-thread cancellation on
    the UI side.

    Threading model:

    * Slots are connected ``Qt.ConnectionType.QueuedConnection`` from
      the GUI; they execute on the worker QThread's event loop. While
      a slot is running, no other slot dispatches — single-threaded
      event loop semantics. That's intentional: INFO is UI-paced.
    * :meth:`stop` is a plain Python method, NOT a ``Slot``. It is
      called directly from the GUI thread (``MainWindow.closeEvent``)
      because a queued ``stop`` slot would also be stuck behind the
      in-flight fetch. ``stop`` only writes ``_stop`` and signals
      ``_in_flight`` — both thread-safe; a small lock guards the
      ``_in_flight`` read-modify-write.
    """

    # Signal names follow Qt's mixedCase convention; the project-wide
    # N815 lint exemption is applied per-line.
    stationsReceived = Signal(str, str, object)  # noqa: N815  # request_id, device_id, list[StationInfo]
    streamsReceived = Signal(str, str, object)  # noqa: N815  # request_id, device_id, list[StreamInfo]
    identityReceived = Signal(str, str, object)  # noqa: N815  # request_id, host_port, ServerIdentity
    infoFailed = Signal(str, str, str, str)  # noqa: N815  # request_id, device_id, kind, reason

    def __init__(self) -> None:
        super().__init__()
        self._stop = False
        # CancellationToken of the request currently being processed;
        # ``None`` between requests. ``stop()`` sets this so the in-
        # flight fetch unwinds within one watchdog poll period
        # (~100 ms) instead of waiting out the full timeout.
        self._in_flight: CancellationToken | None = None
        # Guards the read-modify-write of ``_in_flight`` so ``stop()``
        # on the GUI thread can't observe a half-installed token while
        # a slot on the worker thread is between "fresh token created"
        # and "stored in _in_flight".
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Slots — run on the worker thread (queued from GUI)
    # ------------------------------------------------------------------
    @Slot(str, str, str, int)
    def requestStations(  # noqa: N802 — Qt signal-style naming
        self,
        request_id: str,
        device_id: str,
        host: str,
        port: int,
    ) -> None:
        """Run an ``INFO STATIONS`` fetch synchronously on the worker thread.

        Connected via ``Qt.ConnectionType.QueuedConnection`` from the
        GUI; the body executes on the worker QThread's event loop.
        """
        if self._stop:
            return
        log = _log.bind(request_id=request_id, device_id=device_id, kind="stations")
        token = self._install_token()
        if token is None:
            self.infoFailed.emit(request_id, device_id, "STATIONS", "stopped")
            return
        try:
            stations = info.fetch(
                host,
                int(port),
                "STATIONS",
                timeout_s=_INFO_FETCH_TIMEOUT_S,
                cancel=token,
            )
            # _parse_stations always returns ``list[StationInfo]``;
            # cast through the union-typed return so mypy keeps the
            # parametric type on the emit.
            result_stations: list[StationInfo] = stations  # type: ignore[assignment]
            log.info("info_worker_stations_ok", count=len(result_stations))
            self.stationsReceived.emit(request_id, device_id, result_stations)
        except InfoCanceled:
            log.info("info_worker_canceled")
            self.infoFailed.emit(request_id, device_id, "STATIONS", "canceled")
        except InfoTimeout as exc:
            log.warning("info_worker_timeout", error=str(exc))
            self.infoFailed.emit(request_id, device_id, "STATIONS", f"timeout: {exc}")
        except InfoProtocolError as exc:
            log.warning("info_worker_protocol_error", error=str(exc))
            self.infoFailed.emit(request_id, device_id, "STATIONS", f"protocol: {exc}")
        except InfoError as exc:
            log.warning("info_worker_failed", reason=exc.kind, error=str(exc))
            self.infoFailed.emit(request_id, device_id, "STATIONS", f"{exc.kind}: {exc}")
        except Exception as exc:
            log.exception("info_worker_unexpected_error", error=str(exc))
            self.infoFailed.emit(
                request_id,
                device_id,
                "STATIONS",
                f"unexpected: {exc.__class__.__name__}: {exc}",
            )
        finally:
            self._clear_token()

    @Slot(str, str, str, int, str, str)
    def requestStreams(  # noqa: N802 — Qt signal-style naming
        self,
        request_id: str,
        device_id: str,
        host: str,
        port: int,
        network: str,
        station: str,
    ) -> None:
        """Run an ``INFO STREAMS`` fetch with optional NSLC filter.

        Empty ``network`` / ``station`` strings disable the per-side
        filter — the underlying client serves the unfiltered request
        in that case.
        """
        if self._stop:
            return
        log = _log.bind(request_id=request_id, device_id=device_id, kind="streams")
        token = self._install_token()
        if token is None:
            self.infoFailed.emit(request_id, device_id, "STREAMS", "stopped")
            return
        try:
            net_arg = network or None
            sta_arg = station or None
            streams = info.fetch_streams(
                host,
                int(port),
                network=net_arg,
                station=sta_arg,
                timeout_s=_INFO_FETCH_TIMEOUT_S,
                cancel=token,
            )
            result_streams: list[StreamInfo] = streams
            log.info("info_worker_streams_ok", count=len(result_streams))
            self.streamsReceived.emit(request_id, device_id, result_streams)
        except InfoCanceled:
            log.info("info_worker_canceled")
            self.infoFailed.emit(request_id, device_id, "STREAMS", "canceled")
        except InfoTimeout as exc:
            log.warning("info_worker_timeout", error=str(exc))
            self.infoFailed.emit(request_id, device_id, "STREAMS", f"timeout: {exc}")
        except InfoProtocolError as exc:
            log.warning("info_worker_protocol_error", error=str(exc))
            self.infoFailed.emit(request_id, device_id, "STREAMS", f"protocol: {exc}")
        except InfoError as exc:
            log.warning("info_worker_failed", reason=exc.kind, error=str(exc))
            self.infoFailed.emit(request_id, device_id, "STREAMS", f"{exc.kind}: {exc}")
        except Exception as exc:
            log.exception("info_worker_unexpected_error", error=str(exc))
            self.infoFailed.emit(
                request_id,
                device_id,
                "STREAMS",
                f"unexpected: {exc.__class__.__name__}: {exc}",
            )
        finally:
            self._clear_token()

    @Slot(str, str, str, int)
    def requestId(  # noqa: N802 — Qt signal-style naming
        self,
        request_id: str,
        label: str,
        host: str,
        port: int,
    ) -> None:
        """Run an ``INFO ID`` fetch.

        ``label`` is echoed back as the second argument of
        :attr:`identityReceived` — typically ``f"{host}:{port}"`` so
        the UI can correlate replies without knowing the device id.
        """
        if self._stop:
            return
        log = _log.bind(request_id=request_id, device_id=label, kind="id")
        token = self._install_token()
        if token is None:
            self.infoFailed.emit(request_id, label, "ID", "stopped")
            return
        try:
            identity = info.fetch(
                host,
                int(port),
                "ID",
                timeout_s=_INFO_FETCH_TIMEOUT_S,
                cancel=token,
            )
            result_id: ServerIdentity = identity  # type: ignore[assignment]
            log.info("info_worker_id_ok", version=result_id.version)
            self.identityReceived.emit(request_id, label, result_id)
        except InfoCanceled:
            log.info("info_worker_canceled")
            self.infoFailed.emit(request_id, label, "ID", "canceled")
        except InfoTimeout as exc:
            log.warning("info_worker_timeout", error=str(exc))
            self.infoFailed.emit(request_id, label, "ID", f"timeout: {exc}")
        except InfoProtocolError as exc:
            log.warning("info_worker_protocol_error", error=str(exc))
            self.infoFailed.emit(request_id, label, "ID", f"protocol: {exc}")
        except InfoError as exc:
            log.warning("info_worker_failed", reason=exc.kind, error=str(exc))
            self.infoFailed.emit(request_id, label, "ID", f"{exc.kind}: {exc}")
        except Exception as exc:
            log.exception("info_worker_unexpected_error", error=str(exc))
            self.infoFailed.emit(
                request_id,
                label,
                "ID",
                f"unexpected: {exc.__class__.__name__}: {exc}",
            )
        finally:
            self._clear_token()

    # ------------------------------------------------------------------
    # Plain method (NOT a Slot). Callable from any thread.
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Mark the worker stopped and cancel any in-flight fetch.

        Idempotent. Safe to call from any thread:

        * ``_stop`` write is atomic in CPython (single bool).
        * ``CancellationToken.set()`` is thread-safe (wraps
          :class:`threading.Event`).
        * The lock guards the read of ``_in_flight`` so a concurrent
          slot can't be between "fresh token created" and "stored as
          ``_in_flight``" while we're trying to cancel it.

        Must NOT be a ``@Slot`` — a queued ``stop`` slot would be
        stuck behind the in-flight fetch on the worker thread's event
        loop, defeating the cancellation. The host (``MainWindow``)
        invokes this directly on the GUI thread, then calls
        ``thread.quit() + thread.wait()``.
        """
        with self._lock:
            self._stop = True
            in_flight = self._in_flight
        if in_flight is not None:
            in_flight.set()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _install_token(self) -> CancellationToken | None:
        """Atomically install a fresh token as the in-flight cancellation.

        Returns ``None`` if ``stop()`` already fired — caller should
        emit ``infoFailed`` with reason ``"stopped"`` and bail.
        """
        token = CancellationToken()
        with self._lock:
            if self._stop:
                return None
            self._in_flight = token
        return token

    def _clear_token(self) -> None:
        """Drop the in-flight cancellation token. Always called in ``finally``."""
        with self._lock:
            self._in_flight = None


__all__ = ["InfoWorker"]
