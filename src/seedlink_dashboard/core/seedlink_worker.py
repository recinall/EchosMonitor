"""SeedLink network worker — one QObject per device, run in a QThread.

The worker owns an `EasySeedLinkClient` and re-spawns it on disconnect with
exponential backoff (1, 2, 4, …, 60 s; reset to 1 s after a session that
lasted at least `_RESET_AFTER_CONNECTED_S` seconds).

Two non-obvious decisions are baked in:

1. The client's capability cache is pre-populated with `multistation` to
   avoid an `INFO:CAPABILITIES` round-trip on every connect. Real public
   servers all support multi-station mode; the only mode this app uses.
2. `conn.disconnect` is wrapped so any socket loss flips
   `terminate_flag = True`, causing `client.run()` to exit promptly via
   the `on_terminate` callback instead of falling into ObsPy's internal
   30-second auto-reconnect loop. We always drive reconnect from this
   worker so the state machine and backoff stay observable.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import select
import socket
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog
from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient
from obspy.core.utcdatetime import UTCDateTime
from PySide6.QtCore import QObject, QThread, Signal, Slot

from seedlink_dashboard.core.models import ConnState, WorkerDiagnostics

if TYPE_CHECKING:
    from obspy.core.trace import Trace

    from seedlink_dashboard.config import ReconnectConfig
    from seedlink_dashboard.core.models import FailureKind, StreamSelector

# No module-level structlog binding here. Each ``SeedLinkWorker`` creates
# and binds its own logger in ``__init__`` so every line a worker emits
# carries ``device=<name>``; sharing one bound logger across workers
# would let the last-bound device name leak into another worker's log
# line under contention.
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 60.0
_RESET_AFTER_CONNECTED_S = 30.0
_STOP_POLL_MS = 50
_STOP_WAIT_TIMEOUT_S = 2.0
# Forced socket recv timeout used when stop() is closing the socket out
# from under the worker thread. Short enough that obspy's blocking
# `recv` wakes up within one frame of stop(), long enough that the
# kernel actually has a chance to honour it.
_STOP_RECV_TIMEOUT_S = 0.1
_SLPACKET_SIZE_BYTES = 520
# After this many *consecutive* failed connect attempts, the worker
# escalates to a single ERROR-level log line; subsequent failures stay at
# WARNING until the next CONNECTED resets the counter. Avoids spamming
# ERROR for a transient outage while still surfacing a sustained one.
_FAILING_REPEATEDLY_THRESHOLD = 5


_OBSPY_SEEDLINK_LOGGER_NAME = "obspy.clients.seedlink"


class _StationRejectionFilter(logging.Filter):
    """Surfaces obspy's "station rejected" markers so the worker can act on them.

    Why a logging filter instead of catching ``SeedLinkException``: obspy's
    ``SeedLinkConnection.collect()`` raises ``SeedLinkException("no stations
    accepted")`` (``seedlinkconnection.py:1543`` in obspy 1.4) but
    **catches and swallows it** in the same function (around
    ``seedlinkconnection.py:848``). The exception never reaches our
    ``client.run()`` caller; obspy just logs the error and re-enters
    ``SL_DOWN`` to retry. The only reliable cross-version capture point
    is the ERROR-level log records obspy emits at:

      * ``seedlinkconnection.py:1524`` — ``"response: station not accepted, skipping"``
        (per-rejected-station)
      * ``seedlinkconnection.py:1543`` — ``"no stations accepted"``
        (raised then swallowed; we see it because obspy logs the
        re-raised text inside ``collect()``'s ``except``)

    This filter is brittle to obspy renaming the markers; if a future
    obspy bump hides the rejection, the integration test in
    ``tests/core/test_protocol_rejection.py`` fails fast. See POSTMORTEMS
    2026-05-10 entry "Silent SeedLink protocol rejection".

    Multi-worker safety: ``logging.getLogger("obspy.clients.seedlink")``
    is a process-global singleton. With N concurrent workers, every
    obspy log record fans out to every installed filter. The worker
    therefore captures its own thread id at ``__init__`` time and
    short-circuits records whose ``thread`` field does not match —
    obspy emits its rejection markers from inside ``client.run()``,
    which executes on the worker thread that called it, so this
    matches exactly the records belonging to *this* session. Without
    this guard, a single misconfigured worker would set the
    ``all_rejected`` flag on every other worker's filter, causing
    healthy workers to misclassify their next session-end as
    ``protocol_rejected``.
    """

    _MARKER_TOTAL = "no stations accepted"
    _MARKER_PER_STATION = "station not accepted"

    def __init__(self) -> None:
        super().__init__()
        self.all_rejected: bool = False
        self.per_station_rejections: int = 0
        # Captured at construction time; ``__init__`` runs on the
        # worker thread that's about to call ``client.run()``, which
        # is the same thread obspy will emit its log records from.
        self._owner_thread_ident: int = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        # Reject records emitted on a different thread — they belong
        # to a sibling worker's session, not ours. ``record.thread``
        # is the thread id at emit time (see logging.LogRecord docs).
        if record.thread != self._owner_thread_ident:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if self._MARKER_PER_STATION in msg:
            self.per_station_rejections += 1
        if self._MARKER_TOTAL in msg:
            self.all_rejected = True
        return True


class _PreflightError(Exception):
    """Raised by ``SeedLinkWorker._tcp_preflight`` to communicate a
    classified failure to the surrounding session loop without exposing
    the underlying ``socket`` exception type to callers.

    The ``kind`` attribute is one of ``timeout | refused | dns |
    unknown`` and drives both the structured log entry and the value
    surfaced in the GUI's Diagnostics column.
    """

    __slots__ = ("kind", "message")

    def __init__(self, kind: FailureKind, message: str) -> None:
        super().__init__(message)
        self.kind: FailureKind = kind
        self.message = message


class _Client(EasySeedLinkClient):  # type: ignore[misc]  # obspy lacks stubs
    """EasySeedLinkClient subclass that fans data into a callback.

    Pre-populates the capability cache so `select_stream` does not issue
    an `INFO:CAPABILITIES` request, and wraps `conn.disconnect` so that
    socket loss raises `terminate_flag` and `run()` returns instead of
    silently reconnecting.
    """

    def __init__(
        self,
        server_url: str,
        on_packet: Callable[[Trace], None],
        on_terminate_cb: Callable[[], None],
    ) -> None:
        super().__init__(server_url, autoconnect=False)
        self._EasySeedLinkClient__capabilities = ["multistation"]
        self._on_packet = on_packet
        self._on_terminate_cb = on_terminate_cb

        original_disconnect = self.conn.disconnect

        def _disconnect_with_terminate() -> None:
            self.conn.terminate_flag = True
            original_disconnect()

        self.conn.disconnect = _disconnect_with_terminate

    def on_data(self, trace: Trace) -> None:
        self._on_packet(trace)

    def on_terminate(self) -> None:
        self._on_terminate_cb()


class SeedLinkWorker(QObject):
    """Per-device worker. Move to a QThread; signals fire on Qt's loop.

    Connect timing is bounded by a TCP preflight (``socket.create_connection``
    with ``ReconnectConfig.connect_timeout_s``) so a SYN-blackholed host no
    longer hangs the worker for the OS default ``tcp_syn_retries`` window.
    Each attempt is classified into one of ``timeout | refused | dns |
    unknown``, surfaced via the ``diagnosticsUpdated`` signal, and logged.

    All worker waits are interruptible by ``stop()`` within ~100 ms:

    - The backoff sleep (``WAITING_RETRY``) is chunked via
      ``_sleep_interruptible`` polling ``self._stop`` every ~50 ms.
    - The TCP preflight uses a non-blocking socket + bounded ``select``
      loop (chunk = 100 ms) so ``stop()`` mid-preflight breaks the loop
      within one chunk regardless of how long is left of
      ``connect_timeout_s``.

    This preserves the worker-shutdown contract from POSTMORTEMS
    2026-05-08 (``stop()`` returns only after ``run()`` has fully
    unwound, capped at 2 s).
    """

    packetReceived = Signal(object)  # ObsPy Trace  # noqa: N815  Qt convention
    stateChanged = Signal(int, str)  # ConnState as int, message  # noqa: N815
    errorOccurred = Signal(str)  # noqa: N815
    statsUpdated = Signal(int, int)  # cumulative packets, bytes  # noqa: N815
    # Carries a frozen ``WorkerDiagnostics`` snapshot. Object-typed so
    # PySide6 forwards the dataclass intact without metatype gymnastics.
    diagnosticsUpdated = Signal(object)  # noqa: N815

    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        selectors: list[StreamSelector] | None = None,
        reconnect: ReconnectConfig | None = None,
    ) -> None:
        super().__init__()
        self._name = name
        self._host = host
        self._port = port
        # Defensively reject a shared mutable default by accepting None and
        # constructing the list inside the body. Even though the previous
        # signature took ``list[StreamSelector]`` without a default, making
        # the None branch explicit guards against future callers passing
        # the same list to two workers and discovering aliasing the hard
        # way.
        self._selectors = list(selectors) if selectors is not None else []
        if reconnect is None:
            from seedlink_dashboard.config.schema import ReconnectConfig as _ReconnectConfig

            reconnect = _ReconnectConfig()
        self._reconnect = reconnect
        self._stop = False
        # ``_run_done`` is set whenever run() is *not* executing — initially
        # True because run() has not yet been invoked by the QThread event
        # loop. run() clears it on entry and sets it again in its finally
        # block. stop() waits on this event so that when stop() returns,
        # run() has truly exited (or was never started) and no queued
        # signal can still be in flight from the worker thread to a
        # soon-to-be-deleted bridge/engine receiver. Without this wait,
        # thread.quit()+wait() in the engine could race with a late
        # stateChanged.emit() and segfault inside Qt's event dispatch.
        self._run_done = threading.Event()
        self._run_done.set()
        self._client: _Client | None = None
        self._connected_at: float = 0.0
        self._packets = 0
        self._bytes = 0
        # Diagnostic counters. Reset on every successful CONNECTED so the
        # surfaced numbers reflect the *current* failure streak only.
        self._attempt_count: int = 0
        self._consecutive_failures: int = 0
        self._last_failure_kind: FailureKind | None = None
        self._next_attempt_at: UTCDateTime | None = None
        self._since_first_attempt_at: UTCDateTime | None = None
        self._last_failure_detail: dict[str, object] | None = None
        # Per-worker structlog binding. Avoids a module-level shared
        # logger and ensures every line emitted by this worker carries
        # ``device=<name>`` so multi-device logs are unambiguous.
        self._log = structlog.get_logger(__name__).bind(
            device=name,
            host=host,
            port=port,
        )

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    @Slot()
    def run(self) -> None:
        """Main loop. Runs in the worker thread; never call from GUI thread.

        Each iteration runs one session via ``_run_one_session`` (which
        emits CONNECTING and either CONNECTED or RECONNECTING+session-end),
        then — if the session was not the result of a successful connect
        held until reset — sleeps for the current backoff in
        ``WAITING_RETRY``. The two amber states stay distinguishable in
        the UI: CONNECTING means "actively trying", WAITING_RETRY means
        "sleeping until next try".
        """
        # Clear before doing any work so a stop() racing with thread.start()
        # cannot observe `_run_done == True` and skip the wait.
        self._run_done.clear()
        try:
            initial = max(_BACKOFF_INITIAL_S, float(self._reconnect.initial_delay_s))
            cap = max(initial, float(self._reconnect.max_delay_s))
            backoff = initial

            while not self._stop:
                duration = self._run_one_session()
                if self._stop:
                    break

                if duration >= _RESET_AFTER_CONNECTED_S:
                    backoff = initial

                # Surface "I am sleeping until next try" before the sleep
                # actually starts, so the UI shows WAITING_RETRY for the
                # whole backoff window rather than blinking through it.
                self._next_attempt_at = UTCDateTime() + backoff
                self._emit_diagnostics()
                self._emit_state(
                    ConnState.WAITING_RETRY,
                    f"retrying in {backoff:.1f}s",
                )
                self._sleep_interruptible(backoff)
                backoff = min(backoff * 2.0, cap)

            self._emit_state(ConnState.STOPPED, "stopped")
        finally:
            # Guarantees stop() can observe a fully-unwound run() before
            # the engine quits the thread.
            self._run_done.set()

    def _run_one_session(self) -> float:
        """Single connect/run/disconnect cycle. Returns connected duration in seconds.

        Connect is a two-step dance:

        1. ``_tcp_preflight`` — a plain ``socket.create_connection`` with
           the configured ``connect_timeout_s``. Fails fast and classified
           on a SYN-blackholed / refused / DNS-broken host.
        2. ``EasySeedLinkClient.connect()`` — only after the preflight
           succeeded. obspy owns its own socket; we don't wrap it.

        A ``_StationRejectionFilter`` is attached to the
        ``obspy.clients.seedlink`` logger for the duration of the session
        so that a SeedLink server's "no stations accepted" rejection —
        which obspy raises and then catches inside ``collect()``,
        invisibly to our caller — can be surfaced as a structured
        ``FailureKind = "protocol_rejected"``. Filter is removed in the
        ``finally`` block to avoid leaking entries into the global
        logger across sessions.
        """
        client: _Client | None = None
        connected_at = 0.0
        # Bookkeeping for this attempt — set before any state emission so
        # diagnostics carry the right counters from the very first signal.
        self._attempt_count += 1
        if self._since_first_attempt_at is None:
            self._since_first_attempt_at = UTCDateTime()
        attempt_started = time.monotonic()
        self._emit_diagnostics()
        self._emit_state(
            ConnState.CONNECTING,
            f"connecting to {self._host}:{self._port}",
        )
        self._log.info(
            "seedlink_connect_attempting",
            attempt_n=self._attempt_count,
            connect_timeout_s=float(self._reconnect.connect_timeout_s),
        )

        rej_filter = _StationRejectionFilter()
        obspy_logger = logging.getLogger(_OBSPY_SEEDLINK_LOGGER_NAME)
        obspy_logger.addFilter(rej_filter)
        try:
            self._tcp_preflight()
            client = _Client(
                f"{self._host}:{self._port}",
                on_packet=self._handle_packet,
                on_terminate_cb=lambda: None,
            )
            # netdly: short reconnect interval if obspy ever falls back to its
            # internal loop (we normally drive reconnect ourselves).
            # netto: leave at the default 120 s — setting it to 0 would map to
            # a non-blocking socket via settimeout(0).
            client.conn.netdly = 1
            client.connect()
            # Expose the client AS SOON AS the socket is up so a concurrent
            # stop() can close that socket — the poll-and-close loop in
            # stop() relies on `self._client` becoming visible before
            # ``client.run()`` is entered.
            self._client = client
            # Honour a stop() that fired during connect(): bailing here
            # avoids entering ``client.run()`` only to fight obspy back
            # out of a blocking recv.
            if self._stop:
                with contextlib.suppress(Exception):
                    client.conn.terminate_flag = True
                self._close_client_socket(client)
                return 0.0

            for sel in self._selectors:
                selector = f"{sel.location}{sel.channel}".strip()
                if not selector:
                    raise ValueError(f"empty selector for stream {sel}")
                client.select_stream(sel.network, sel.station, selector)

            self._on_connect_success(
                elapsed_ms=int((time.monotonic() - attempt_started) * 1000.0),
            )
            connected_at = time.monotonic()
            self._connected_at = connected_at

            client.run()
            # run() returned via on_terminate (server hung up, stop() set
            # the flag, or — for a protocol_rejected session — obspy's
            # disconnect-on-rejection path tripped our terminate_flag
            # override). The rejection branch records the real cause and
            # emits a more specific RECONNECTING message; the generic
            # branch keeps the original "server disconnected" wording.
            if not self._stop and rej_filter.all_rejected:
                self._record_protocol_rejection(rej_filter)
                self._emit_state(
                    ConnState.RECONNECTING,
                    "server rejected the requested stations",
                )
            elif not self._stop:
                self._emit_state(ConnState.RECONNECTING, "server disconnected")
        except _PreflightError as exc:
            # An external stop() that fires mid-preflight surfaces here as
            # well; it's a clean shutdown, not a failure. Suppress the
            # WARNING log and counter bump so the no-WARNING-during-stop
            # contract from POSTMORTEMS 2026-05-08 is preserved.
            if self._stop:
                self._log.debug("seedlink_preflight_aborted", error=exc.message)
            else:
                self._record_failure(exc.kind, exc.message)
        except Exception as exc:
            # During an external stop() the socket is closed from another
            # thread, and obspy surfaces that as EBADF or similar — that's
            # expected, not a real error, so don't propagate it as one.
            if self._stop:
                self._log.debug("seedlink_session_shutdown", error=str(exc))
            else:
                msg = str(exc) or exc.__class__.__name__
                self.errorOccurred.emit(msg)
                # Defense in depth: if some future obspy version DOES
                # propagate ``SeedLinkException("no stations accepted")``
                # to our caller, classify correctly here instead of
                # falling into "unknown". The logging filter still
                # caught the marker even though the exception bubbled.
                if rej_filter.all_rejected:
                    self._record_protocol_rejection(rej_filter)
                else:
                    # Anything reaching this branch is post-preflight, so
                    # it's an obspy-side or session error (client.connect(),
                    # select_stream, or client.run()) rather than a
                    # connect-classification failure. Funnel under
                    # "unknown" so the GUI surfaces *something* in the
                    # diagnostics column; the log carries the real text.
                    self._record_failure("unknown", msg)
        finally:
            obspy_logger.removeFilter(rej_filter)
            self._client = None
            self._close_client_socket(client)
            self._connected_at = 0.0

        return (time.monotonic() - connected_at) if connected_at > 0 else 0.0

    def _record_protocol_rejection(self, rej_filter: _StationRejectionFilter) -> None:
        """Translate a tripped ``_StationRejectionFilter`` into a structured failure.

        Captures the configured selectors verbatim (rather than parsing
        per-station markers from the obspy log) because obspy's
        per-rejection log line ``"response: station not accepted,
        skipping"`` does not include the station identifier — and even
        if it did, the whole-set rejection is already what the user
        needs to act on (open Stations browser → pick a real station →
        re-subscribe). ``rejection_count`` is included for operators
        diagnosing partial misconfigurations from the log.
        """
        selectors_repr = [
            f"{sel.network}.{sel.station}.{sel.location}.{sel.channel}" for sel in self._selectors
        ]
        detail: dict[str, object] = {
            "rejected_selectors": selectors_repr,
            "rejection_count": rej_filter.per_station_rejections,
        }
        self._log.warning(
            "seedlink_protocol_rejected",
            rejected_selectors=selectors_repr,
            rejection_count=rej_filter.per_station_rejections,
        )
        self._record_failure(
            "protocol_rejected",
            "server rejected all requested stations",
            detail=detail,
        )

    @Slot()
    def stop(self) -> None:
        """Signal the worker to exit and block until `run()` has fully unwound.

        The naive "read self._client once and close its socket" approach
        loses a race: the worker can be between ``client.connect()`` and
        ``self._client = client`` when stop() runs, so the socket-close
        is skipped, then ``client.run()`` blocks on ``recv`` forever.

        We instead poll ``self._client`` while waiting for ``_run_done``.
        Each pass closes the socket if the worker has now exposed it,
        and re-applies ``terminate_flag``. The wait still caps at
        ``_STOP_WAIT_TIMEOUT_S`` so the GUI thread is not blocked by a
        misbehaving session — but in practice the first or second poll
        catches the worker, recv returns, run() unwinds, and the wait
        succeeds well before the cap.

        ``_run_done`` is initialised set, so if ``run()`` has not yet
        been entered the loop drops out immediately.
        """
        self._stop = True
        deadline = time.monotonic() + _STOP_WAIT_TIMEOUT_S
        poll_s = 0.02
        while True:
            client = self._client
            if client is not None:
                with contextlib.suppress(Exception):
                    client.conn.terminate_flag = True
                self._close_client_socket(client)
            if self._run_done.wait(timeout=poll_s):
                return
            if time.monotonic() >= deadline:
                self._log.warning(
                    "seedlink_worker_stop_timeout",
                    timeout_s=_STOP_WAIT_TIMEOUT_S,
                )
                return
            poll_s = min(poll_s * 2.0, 0.2)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _handle_packet(self, trace: Trace) -> None:
        self._packets += 1
        self._bytes += _SLPACKET_SIZE_BYTES
        self.packetReceived.emit(trace)
        self.statsUpdated.emit(self._packets, self._bytes)

    def _emit_state(self, state: ConnState, msg: str) -> None:
        self._log.info("seedlink_state", state=state.name, message=msg)
        self.stateChanged.emit(int(state), msg)

    def _emit_diagnostics(self) -> None:
        """Emit a fresh ``WorkerDiagnostics`` snapshot.

        Called whenever any field in the snapshot changes (attempt
        counter increment, failure classification, next-attempt schedule,
        successful reset). The engine's bridge forwards this to a slot
        that mutates the corresponding ``DeviceStatus`` so the
        DevicePanel's 1 Hz timer renders up-to-date diagnostics.
        """
        # ``last_failure_detail`` is shallow-copied so a subsequent
        # mutation on the worker side cannot bleed into a snapshot the
        # GUI thread is already reading. The schema is JSON-friendly
        # (ints, strs, list[str]) so a shallow copy is sufficient.
        detail = dict(self._last_failure_detail) if self._last_failure_detail is not None else None
        self.diagnosticsUpdated.emit(
            WorkerDiagnostics(
                attempt_count=self._attempt_count,
                last_failure_kind=self._last_failure_kind,
                next_attempt_at=self._next_attempt_at,
                since_first_attempt_at=self._since_first_attempt_at,
                last_failure_detail=detail,
            )
        )

    def _tcp_preflight(self) -> None:
        """Open a plain TCP probe with our own bounded timeout, then close it.

        Implementation uses a non-blocking socket + ``select`` loop so the
        wait is broken by ``self._stop`` within ~100 ms regardless of how
        far we are from the connect timeout. The naive
        ``socket.create_connection(..., timeout=N)`` form blocks the worker
        thread inside the kernel for the full N seconds with no way for
        ``stop()`` to interrupt it; that breaks the worker-shutdown
        contract from POSTMORTEMS 2026-05-08 (every QThread-hosted worker
        MUST guarantee ``stop()`` returns only after ``run()`` has fully
        unwound). With the chunked select, ``run()`` unwinds within one
        poll period of ``stop()`` even mid-preflight.

        Why a probe instead of wrapping obspy's ``connect()``: obspy owns
        its own socket lifecycle, including a default ``netto`` of 120 s
        applied after the handshake. Wrapping its connect would either
        require monkey-patching obspy internals or risk leaking a
        half-open socket. The probe approach is a few extra microseconds
        on success and bounds the worst case on failure to exactly
        ``connect_timeout_s``.

        Failures are translated into a closed ``FailureKind`` set so the
        GUI / logs can treat the cause deterministically.
        """
        timeout = float(self._reconnect.connect_timeout_s)
        deadline = time.monotonic() + timeout

        # DNS resolution is itself blocking but bounded by the resolver
        # config; pulling it out before the socket handshake gives us a
        # clean dns/timeout split (a getaddrinfo hang is reported as
        # "dns" / "unknown" rather than masquerading as a connect timeout).
        try:
            infos = socket.getaddrinfo(self._host, self._port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise _PreflightError("dns", f"DNS lookup failed: {exc}") from exc
        except OSError as exc:
            raise _PreflightError("unknown", f"{exc.__class__.__name__}: {exc}") from exc
        if not infos:
            raise _PreflightError("dns", "DNS lookup returned no addresses")

        family, socktype, proto, _canonname, sockaddr = infos[0]
        sock = socket.socket(family, socktype, proto)
        sock.setblocking(False)
        try:
            with contextlib.suppress(OSError):
                sock.connect(sockaddr)
            # Common cases for non-blocking connect: BlockingIOError
            # (EINPROGRESS) on Linux/mac; we just enter the select loop.
            while not self._stop:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _PreflightError(
                        "timeout",
                        f"TCP connect timed out after {timeout:.1f}s",
                    )
                # Cap each select to ~100 ms so the next iteration
                # observes self._stop within one poll period of stop().
                chunk = min(remaining, 0.1)
                _r, writable, _x = select.select([], [sock], [], chunk)
                if not writable:
                    continue
                err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err == 0:
                    return  # connected
                if err in (errno.ECONNREFUSED,):
                    raise _PreflightError("refused", "connection refused")
                if err in (errno.ETIMEDOUT,):
                    raise _PreflightError(
                        "timeout",
                        f"TCP connect timed out after {timeout:.1f}s",
                    )
                if err in (errno.EHOSTUNREACH, errno.ENETUNREACH):
                    raise _PreflightError("unknown", f"network unreachable (errno={err})")
                raise _PreflightError("unknown", f"connect failed: errno={err}")
            # _stop fired; surface as a non-classification (caller's
            # outer ``if self._stop`` check will skip the failure log).
            raise _PreflightError("unknown", "preflight aborted by stop()")
        finally:
            with contextlib.suppress(OSError):
                sock.close()

    def _record_failure(
        self,
        kind: FailureKind,
        message: str,
        *,
        detail: dict[str, object] | None = None,
    ) -> None:
        """Update diagnostic counters + log this failed attempt.

        Logs at WARNING by default. After ``_FAILING_REPEATEDLY_THRESHOLD``
        consecutive failures, escalates to a single ERROR entry once and
        stays at WARNING afterwards until the next successful CONNECTED
        resets the counter (see ``_on_connect_success``).

        ``detail`` is the kind-specific structured context that flows
        into ``WorkerDiagnostics.last_failure_detail``. ``None`` for
        kinds that have no extra context. Callers MUST NOT mutate the
        dict after handing it over — the worker stores it by reference.
        """
        self._consecutive_failures += 1
        self._last_failure_kind = kind
        self._last_failure_detail = detail
        next_retry_in_s: float | None = None
        if self._next_attempt_at is not None:
            # The run() loop will set ``_next_attempt_at`` to a real
            # backoff target after this method returns, but on the very
            # first failure of an attempt cycle ``next_attempt_at`` may
            # still be None — in which case the log line just omits the
            # value rather than guessing.
            try:
                next_retry_in_s = max(0.0, float(self._next_attempt_at - UTCDateTime()))
            except Exception:
                next_retry_in_s = None
        self._emit_diagnostics()
        self._log.warning(
            "seedlink_connect_failed",
            kind=kind,
            attempt_n=self._attempt_count,
            consecutive_failures=self._consecutive_failures,
            error=message,
            next_retry_in_s=next_retry_in_s,
        )
        if self._consecutive_failures == _FAILING_REPEATEDLY_THRESHOLD:
            self._log.error(
                "seedlink_connect_failing_repeatedly",
                kind=kind,
                consecutive_failures=self._consecutive_failures,
                threshold=_FAILING_REPEATEDLY_THRESHOLD,
            )

    def _on_connect_success(self, elapsed_ms: int) -> None:
        """Hook invoked the moment select_stream calls have succeeded.

        Resets the failure counters so the next backoff cycle starts from
        ``initial_delay_s`` and the GUI's diagnostics column clears, then
        emits the CONNECTED state. The structured INFO log here closes
        the bracket opened by ``seedlink_connect_attempting``.
        """
        self._log.info(
            "seedlink_connect_established",
            attempt_n=self._attempt_count,
            elapsed_ms=elapsed_ms,
        )
        self._attempt_count = 0
        self._consecutive_failures = 0
        self._last_failure_kind = None
        self._last_failure_detail = None
        self._next_attempt_at = None
        self._since_first_attempt_at = None
        self._emit_diagnostics()
        self._emit_state(
            ConnState.CONNECTED,
            f"connected to {self._host}:{self._port}",
        )

    def _sleep_interruptible(self, total_seconds: float) -> None:
        """Sleep up to total_seconds in small chunks so `stop()` is responsive."""
        deadline = time.monotonic() + total_seconds
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            QThread.msleep(min(_STOP_POLL_MS, int(remaining * 1000) + 1))

    @staticmethod
    def _close_client_socket(client: _Client | None) -> None:
        """Wake a worker thread parked inside obspy's blocking ``recv``.

        Three nudges, in order:

        1. ``settimeout`` to a small value — Linux does not always wake a
           ``recv`` blocked on an fd that another thread closed. A live
           timeout makes the kernel return the call within the deadline,
           independently of any cross-thread close.
        2. ``shutdown(SHUT_RDWR)`` — surfaces an EOF / EBADF on the
           kernel side so the in-flight ``recv`` returns 0 bytes.
        3. ``close`` — releases the fd. Order matters: timeout first
           (changes the in-flight call's behaviour), then shutdown, then
           close.

        Without (1), the leaked-worker race observed in tight ``pytest -q``
        loops re-emerges: the fd close was a no-op for the parked recv,
        the worker thread stayed alive past stop(), and pytest's next
        test eventually triggered Qt's "QThread destroyed while running"
        abort.
        """
        if client is None:
            return
        sock: Any = getattr(client.conn, "socket", None)
        if sock is None:
            return
        with contextlib.suppress(OSError):
            sock.settimeout(_STOP_RECV_TIMEOUT_S)
        with contextlib.suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            sock.close()
