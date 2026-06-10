"""Out-of-band SeedLink INFO client.

Used by the GUI's Stations dock to fetch a server's station/stream catalog
without disturbing the streaming worker. Two non-obvious decisions are
baked in:

1. We open a *separate* short-lived ``EasySeedLinkClient`` per fetch
   rather than reusing the streaming worker's. The streaming worker
   pre-populates ``_EasySeedLinkClient__capabilities = ["multistation"]``
   (see ``seedlink_worker._Client.__init__``); reusing that client to
   issue an INFO request would fight obspy's connection state machine
   (``__streaming_started`` flips True after ``run()``), so we keep
   the two cleanly separate.
2. Both the TCP preflight and the ``get_info`` call are bounded by a
   wall-clock deadline + a ``CancellationToken``. The naive
   ``client.get_info(level)`` blocks indefinitely if the server stalls
   mid-response; we adapt the streaming worker's "close-the-socket
   from another thread" trick — a small daemon watchdog watches both
   the deadline and the cancel token, and on either trip nudges the
   socket via the same 3-step pattern as
   ``SeedLinkWorker._close_client_socket`` (settimeout, shutdown, close).

The chosen framing path for the test fake is **SLPACKET** (8-byte
``SLINFO`` header + 512-byte ASCII-encoded MSEED record carrying the XML
chunk). obspy's INFO response parser, ``SeedLinkConnection.collect``,
already handles this end-to-end; the fake just needs to produce the same
bytes a real SeisComP / IRIS Ringserver would. See
``tests/core/fakes.py::FakeSeedLinkServer._reply_info`` for the
inverse of this code.
"""

from __future__ import annotations

import contextlib
import errno
import select
import socket
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient

from echosmonitor.core.exceptions import (
    InfoCanceled,
    InfoError,
    InfoProtocolError,
    InfoTimeout,
)

_log = structlog.get_logger(__name__)


# Default wall-clock deadline applied to a fetch when the caller does not
# pass one explicitly. 30 s is generous for STATIONS / STREAMS over a
# slow trans-continental link while still bounding the worst case so the
# UI never appears wedged.
_INFO_DEFAULT_TIMEOUT_S = 30.0
# How often the TCP preflight loop wakes to check ``cancel`` / the
# deadline. 100 ms is the same cadence the streaming worker uses for its
# stop-poll loop and keeps cancel latency well under one frame even on a
# blackholed host.
_PREFLIGHT_POLL_S = 0.1
# Deadline for joining the daemon watchdog thread once the main thread
# is done with the fetch. The watchdog is short-lived (it wakes every
# 100 ms) so 1 s is comfortable; if it ever exceeds this, we log a
# WARNING but never block the caller indefinitely.
_WATCHDOG_JOIN_S = 1.0
# Forced socket recv timeout used by the watchdog when it nudges the
# in-flight ``get_info`` recv. Mirrors ``SeedLinkWorker._STOP_RECV_TIMEOUT_S``
# — Linux does not always wake a recv when another thread closes the fd,
# so we set a live timeout first to make the kernel return.
_WATCHDOG_RECV_TIMEOUT_S = 0.1


InfoKind = Literal[
    "ID",
    "CAPABILITIES",
    "STATIONS",
    "STREAMS",
    "GAPS",
    "CONNECTIONS",
]


# ----------------------------------------------------------------------
# Public typed-result dataclasses
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """Server identification block from ``INFO ID``.

    Attributes:
        version: Software version string as reported by the server (e.g.
            ``"3.4.2"``). Format varies between vendors — stored verbatim.
        organization: Free-form organization name; may be empty string
            when the server does not publish one.
        started_at: ISO-ish timestamp the server started, or ``None`` if
            absent. Format varies between vendors — stored verbatim.
        capabilities: Tuple of capability tokens parsed from the
            ``<capabilities>`` block. Empty tuple if absent.
    """

    version: str
    organization: str
    started_at: str | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StationInfo:
    """One station entry from ``INFO STATIONS``."""

    network: str
    station: str
    description: str | None
    begin: str | None
    end: str | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """One stream entry from ``INFO STREAMS``."""

    network: str
    station: str
    location: str
    channel: str
    type: str
    begin: str | None
    end: str | None
    sampling_rate: float | None


@dataclass(frozen=True, slots=True)
class GapInfo:
    """One gap entry from ``INFO GAPS``."""

    stream_id: str
    t_start: str
    t_end: str
    samples_missing: int


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    """One connection entry from ``INFO CONNECTIONS``."""

    client: str
    host: str
    extra: tuple[tuple[str, str], ...]


InfoResult = (
    ServerIdentity
    | tuple[str, ...]
    | list[StationInfo]
    | list[StreamInfo]
    | list[GapInfo]
    | list[ConnectionInfo]
)


# ----------------------------------------------------------------------
# Cancellation primitive
# ----------------------------------------------------------------------


class CancellationToken:
    """Thin wrapper around ``threading.Event`` for cooperative cancel.

    Used by the GUI to abort an in-flight ``fetch`` when e.g. the
    Stations dock is closed before the server has finished replying.
    Wrapping ``Event`` rather than exposing it directly keeps the
    cancel surface small and prevents accidental misuse of
    ``Event.wait`` (which would block the GUI thread).
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        """Mark the operation as canceled. Safe from any thread."""
        self._event.set()

    def clear(self) -> None:
        """Reset the token so it can be reused for a fresh fetch."""
        self._event.clear()

    def is_set(self) -> bool:
        """Return ``True`` once :meth:`set` has been called."""
        return self._event.is_set()


# ----------------------------------------------------------------------
# Internal: minimal EasySeedLinkClient subclass
# ----------------------------------------------------------------------


class _InfoClient(EasySeedLinkClient):  # type: ignore[misc]  # obspy lacks stubs
    """EasySeedLinkClient subclass tailored for one-shot INFO fetches.

    Pre-populates the capability cache with ``multistation`` so that
    ``get_info`` does NOT first issue an ``INFO:CAPABILITIES`` round-trip
    (the test fake does not answer that), and so the fetch is exactly
    one INFO exchange wide.
    """

    def __init__(self, server_url: str) -> None:
        super().__init__(server_url, autoconnect=False)
        # Same trick as ``SeedLinkWorker._Client``: pre-seed the
        # capability cache so ``get_info`` skips the implicit
        # CAPABILITIES probe inside obspy.
        self._EasySeedLinkClient__capabilities = ["multistation"]


# ----------------------------------------------------------------------
# TCP preflight (mirrors SeedLinkWorker._tcp_preflight)
# ----------------------------------------------------------------------


def _tcp_preflight(
    host: str,
    port: int,
    deadline: float,
    cancel: CancellationToken | None,
) -> None:
    """Bounded, cancel-aware TCP probe.

    Mirrors ``SeedLinkWorker._tcp_preflight`` but adapted for an
    arbitrary deadline and cancellation token rather than a per-worker
    ``self._stop`` flag. Failures are translated into ``InfoError`` /
    ``InfoTimeout`` / ``InfoCanceled`` so the caller never sees a raw
    socket exception.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise InfoError("dns", f"DNS lookup failed: {exc}") from exc
    except OSError as exc:
        raise InfoError("unknown", f"{exc.__class__.__name__}: {exc}") from exc
    if not infos:
        raise InfoError("dns", "DNS lookup returned no addresses")

    family, socktype, proto, _canonname, sockaddr = infos[0]
    sock = socket.socket(family, socktype, proto)
    sock.setblocking(False)
    try:
        with contextlib.suppress(OSError):
            sock.connect(sockaddr)
        while True:
            if cancel is not None and cancel.is_set():
                raise InfoCanceled("preflight canceled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InfoTimeout("TCP connect timed out after deadline")
            chunk = min(remaining, _PREFLIGHT_POLL_S)
            _r, writable, _x = select.select([], [sock], [], chunk)
            if not writable:
                continue
            err = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err == 0:
                return
            if err == errno.ECONNREFUSED:
                raise InfoError("refused", "connection refused")
            if err == errno.ETIMEDOUT:
                raise InfoTimeout("TCP connect timed out (kernel ETIMEDOUT)")
            if err in (errno.EHOSTUNREACH, errno.ENETUNREACH):
                raise InfoError("unknown", f"network unreachable (errno={err})")
            raise InfoError("unknown", f"connect failed: errno={err}")
    finally:
        with contextlib.suppress(OSError):
            sock.close()


# ----------------------------------------------------------------------
# Internal: deadline+cancel watchdog
# ----------------------------------------------------------------------


def _close_client_socket(client: _InfoClient) -> None:
    """3-step nudge of obspy's recv-blocked socket.

    Same pattern as ``SeedLinkWorker._close_client_socket``: a live
    timeout makes any in-flight ``recv`` return within one deadline
    (Linux does not always wake a parked recv when another thread
    closes the fd); ``shutdown(SHUT_RDWR)`` then surfaces an EOF /
    EBADF; ``close`` finally releases the descriptor. Order matters.
    """
    sock: Any = getattr(client.conn, "socket", None)
    if sock is None:
        return
    with contextlib.suppress(OSError):
        sock.settimeout(_WATCHDOG_RECV_TIMEOUT_S)
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def _trip_client(client: _InfoClient) -> None:
    """Force ``client.get_info`` to unwind cleanly.

    Setting ``conn.terminate_flag`` makes obspy's ``collect`` loop
    return ``SLPacket.SLTERMINATE`` on its next pass, which
    ``EasySeedLinkClient.get_info`` translates into an
    ``EasySeedLinkClientException``. Without this flag, an IOError
    raised by the in-flight recv would be caught by ``collect`` and
    drive a silent reconnect — defeating the deadline. The 3-step
    socket nudge afterwards wakes the recv so the loop reaches its
    next iteration promptly.
    """
    with contextlib.suppress(Exception):
        client.conn.terminate_flag = True
    _close_client_socket(client)


@dataclass
class _Watchdog:
    """Handle returned by ``_spawn_watchdog`` so the caller can stop it.

    The ``done`` event is the watchdog's "main thread succeeded, please
    exit" signal — set by ``_fetch_internal``'s finally block. Without
    it the watchdog would idle until the deadline before exiting on its
    own, and ``join(timeout=_WATCHDOG_JOIN_S)`` would always log a
    bogus "join timeout" warning on the success path.
    """

    thread: threading.Thread
    done: threading.Event


def _spawn_watchdog(
    client: _InfoClient,
    deadline: float,
    cancel: CancellationToken | None,
    on_trip: Callable[[str], None],
) -> _Watchdog:
    """Spawn a daemon thread that closes the client socket on deadline / cancel.

    The thread waits on a per-call ``done`` Event with a small timeout
    so cancel latency is bounded by one poll period, and the deadline
    is observed within the same window. ``on_trip(reason)`` is invoked
    exactly once with ``"timeout"`` or ``"canceled"`` to record which
    event tripped first — the calling thread reads this back to choose
    the right exception type when its ``get_info`` recv unwinds.

    Returns:
        ``_Watchdog`` whose ``done`` event the caller sets in its
        finally block on the success path so the watchdog exits without
        firing.
    """
    tripped = threading.Event()
    done = threading.Event()

    def _run() -> None:
        while True:
            if done.is_set():
                # Main thread succeeded; bail without touching the socket.
                return
            if cancel is not None and cancel.is_set():
                if not tripped.is_set():
                    on_trip("canceled")
                    tripped.set()
                _trip_client(client)
                return
            now = time.monotonic()
            if now >= deadline:
                if not tripped.is_set():
                    on_trip("timeout")
                    tripped.set()
                _trip_client(client)
                return
            # Cap each wait at _PREFLIGHT_POLL_S so cancel / done
            # latency stays bounded by one poll period regardless of
            # how far away the deadline is. ``Event.wait`` returns
            # immediately on set so the success path joins fast.
            wait_s = min(_PREFLIGHT_POLL_S, max(0.0, deadline - now))
            done.wait(timeout=wait_s)

    thread = threading.Thread(target=_run, name="info-watchdog", daemon=True)
    thread.start()
    return _Watchdog(thread=thread, done=done)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def fetch(
    host: str,
    port: int,
    kind: InfoKind,
    *,
    timeout_s: float = _INFO_DEFAULT_TIMEOUT_S,
    cancel: CancellationToken | None = None,
) -> InfoResult:
    """Fetch one INFO level from a SeedLink server and parse it.

    The call is bounded by a TCP preflight + a wall-clock deadline on
    the ``get_info`` exchange itself; both are interruptible via
    ``cancel`` within roughly ``_PREFLIGHT_POLL_S``. The connection is
    fully torn down before returning, regardless of success or failure.

    Args:
        host: SeedLink server hostname or IP.
        port: SeedLink server port (default protocol port is 18000).
        kind: One of the closed ``InfoKind`` set.
        timeout_s: Wall-clock deadline applied to the entire fetch
            (preflight + INFO exchange + parse). Default
            ``_INFO_DEFAULT_TIMEOUT_S`` (30 s).
        cancel: Optional cooperative cancellation token. ``cancel.set()``
            from another thread aborts the fetch with ``InfoCanceled``.

    Returns:
        Parsed result whose concrete type depends on ``kind``:

        - ``"ID"`` → :class:`ServerIdentity`
        - ``"CAPABILITIES"`` → ``tuple[str, ...]``
        - ``"STATIONS"`` → ``list[StationInfo]``
        - ``"STREAMS"`` → ``list[StreamInfo]``
        - ``"GAPS"`` → ``list[GapInfo]``
        - ``"CONNECTIONS"`` → ``list[ConnectionInfo]``

    Raises:
        InfoTimeout: The wall-clock deadline elapsed.
        InfoCanceled: ``cancel.set()`` was called before completion.
        InfoError: Network-level failure (DNS, refused, unreachable).
        InfoProtocolError: Server replied with malformed XML or a
            structure not matching the requested ``kind``.
    """
    return _fetch_internal(host, port, kind, level=kind, timeout_s=timeout_s, cancel=cancel)


def fetch_streams(
    host: str,
    port: int,
    *,
    network: str | None = None,
    station: str | None = None,
    timeout_s: float = _INFO_DEFAULT_TIMEOUT_S,
    cancel: CancellationToken | None = None,
) -> list[StreamInfo]:
    """Fetch ``INFO STREAMS`` with optional NSLC filtering.

    When both ``network`` and ``station`` are provided, the request is
    sent as ``INFO STREAMS NET_STA`` so the server itself filters the
    response (the ``_`` separator is what SeisComP / IRIS Ringserver
    expect). When only one or neither is provided, the request is the
    plain ``INFO STREAMS`` and filtering happens client-side; some
    servers reject the partial form so we don't try to send it.

    Args:
        host: SeedLink server hostname or IP.
        port: SeedLink server port.
        network: Optional 2-character SEED network code. When combined
            with ``station``, drives server-side filtering.
        station: Optional SEED station code.
        timeout_s: Wall-clock deadline applied to the entire fetch.
        cancel: Optional cooperative cancellation token.

    Returns:
        List of :class:`StreamInfo` matching the filter (or all known
        streams if neither filter was given).
    """
    # Server-side filter via ``INFO STREAMS NET_STA`` when both filters
    # are present — fewer bytes on the wire and faster on busy
    # ringservers with thousands of stations. Otherwise fall back to
    # ``INFO STREAMS`` and filter client-side.
    level = f"STREAMS {network}_{station}" if (network and station) else "STREAMS"
    result = _fetch_internal(host, port, "STREAMS", level=level, timeout_s=timeout_s, cancel=cancel)
    # _parse_streams always returns list[StreamInfo]; cast through Any
    # rather than isinstance because the union-typed ``InfoResult`` makes
    # mypy lose the parametric type on the list branch.
    streams: list[StreamInfo] = result  # type: ignore[assignment]

    if network or station:
        streams = [
            s
            for s in streams
            if (not network or s.network == network) and (not station or s.station == station)
        ]
    return streams


def _fetch_internal(
    host: str,
    port: int,
    kind: InfoKind,
    *,
    level: str,
    timeout_s: float,
    cancel: CancellationToken | None,
) -> InfoResult:
    """Shared implementation behind ``fetch`` / ``fetch_streams``.

    ``kind`` drives parsing dispatch; ``level`` is the literal string
    sent in the ``INFO ...`` command (so ``fetch_streams`` can pass
    ``"STREAMS IU_ANMO"`` while still parsing as STREAMS).
    """
    log = _log.bind(host=host, port=port, kind=kind)
    log.info("info_fetch_start", timeout_s=timeout_s)

    started = time.monotonic()
    deadline = started + max(0.0, float(timeout_s))

    # Phase 1: bounded TCP preflight. Fails fast and classified.
    try:
        _tcp_preflight(host, port, deadline, cancel)
    except InfoCanceled:
        log.debug("info_fetch_canceled", phase="preflight")
        raise
    except InfoTimeout as exc:
        log.warning("info_fetch_failed", reason="timeout", phase="preflight", error=str(exc))
        raise
    except InfoError as exc:
        log.warning("info_fetch_failed", reason=exc.kind, phase="preflight", error=str(exc))
        raise

    # Phase 2: connect + INFO + close, bounded by the same deadline via
    # a daemon watchdog that nudges the socket on cancel / deadline.
    # The watchdog is spawned BEFORE ``client.connect`` because obspy's
    # ``SeedLinkConnection.connect`` does its own blocking
    # ``sock.connect`` + ``say_hello`` recv with a default 120 s
    # ``netto`` timeout — uncovered, this would silently hang up to
    # ~120 s on a route flap between preflight and connect, violating
    # CLAUDE.md rule 7. The watchdog handles a not-yet-connected client
    # safely: ``_close_client_socket`` no-ops on a ``None`` socket and
    # ``conn.terminate_flag = True`` is propagated regardless.
    client = _InfoClient(f"{host}:{port}")
    trip_reason: dict[str, str] = {}

    def _record_trip(reason: str) -> None:
        # Single-writer pattern: the watchdog calls this exactly once
        # before closing the socket, so the main thread can read it
        # without locking.
        trip_reason.setdefault("reason", reason)

    watchdog = _spawn_watchdog(client, deadline, cancel, _record_trip)

    try:
        log.info("info_connect_attempting")
        connect_started = time.monotonic()
        try:
            client.connect()
        except Exception as exc:
            # Same trip-reason interpretation as ``get_info`` below:
            # the watchdog is the canonical source of truth for *why*
            # the connect unwound. A bare exception with no trip is a
            # genuine network / handshake failure.
            reason = trip_reason.get("reason")
            if reason == "canceled":
                log.debug("info_fetch_canceled", phase="connect")
                raise InfoCanceled("INFO connect canceled") from exc
            if reason == "timeout":
                log.warning("info_fetch_failed", reason="timeout", phase="connect")
                raise InfoTimeout(f"SeedLink connect timed out after {timeout_s:.1f}s") from exc
            log.warning(
                "info_fetch_failed",
                reason="unknown",
                phase="connect",
                error=f"{exc.__class__.__name__}: {exc}",
            )
            raise InfoError("unknown", f"SeedLink connect failed: {exc}") from exc
        log.info(
            "info_connect_ok",
            elapsed_ms=int((time.monotonic() - connect_started) * 1000.0),
        )

        try:
            xml = client.get_info(level)
        except Exception as exc:
            reason = trip_reason.get("reason")
            if reason == "canceled":
                log.debug("info_fetch_canceled", phase="get_info")
                raise InfoCanceled("INFO fetch canceled") from exc
            if reason == "timeout":
                log.warning("info_fetch_failed", reason="timeout", phase="get_info")
                raise InfoTimeout(f"INFO {level} timed out after {timeout_s:.1f}s") from exc
            log.warning(
                "info_fetch_failed",
                reason="unknown",
                phase="get_info",
                error=f"{exc.__class__.__name__}: {exc}",
            )
            raise InfoError("unknown", f"INFO exchange failed: {exc}") from exc
    finally:
        # Tell the watchdog the main path is done. On success this
        # avoids a spurious "join timeout" warning; on failure the
        # watchdog has already tripped, so this is a no-op.
        watchdog.done.set()
        watchdog.thread.join(timeout=_WATCHDOG_JOIN_S)
        if watchdog.thread.is_alive():
            log.warning("info_watchdog_join_timeout", timeout_s=_WATCHDOG_JOIN_S)
        # Disconnect the client. obspy's ``close`` calls
        # conn.disconnect() which is safe on an already-closed socket.
        with contextlib.suppress(Exception):
            client.close()
        # Belt and suspenders: in case obspy's disconnect leaves a stale
        # socket, nudge it ourselves. Wrapped exceptions suppressed —
        # the socket may already be gone after the watchdog's close.
        with contextlib.suppress(Exception):
            _close_client_socket(client)

    # Phase 3: parse. Any XML error becomes InfoProtocolError.
    payload_bytes = len(xml.encode("utf-8")) if isinstance(xml, str) else len(xml)
    try:
        result = _parse(kind, xml)
    except ET.ParseError as exc:
        log.warning("info_fetch_failed", reason="protocol", phase="parse", error=str(exc))
        raise InfoProtocolError(f"failed to parse INFO {kind} XML: {exc}") from exc
    except InfoProtocolError as exc:
        log.warning("info_fetch_failed", reason="protocol", phase="parse", error=str(exc))
        raise

    elapsed_ms = int((time.monotonic() - started) * 1000.0)
    log.info("info_fetch_ok", elapsed_ms=elapsed_ms, payload_bytes=payload_bytes)
    return result


# ----------------------------------------------------------------------
# XML parsing — tolerant of the two common dialects (SeisComP and the
# obspy-test fake).
# ----------------------------------------------------------------------


def _parse(kind: InfoKind, xml: str) -> InfoResult:
    """Dispatch to the right parser for ``kind``.

    Tolerant of both the SeisComP / IRIS Ringserver dialect (capitalised
    elements, ``begin_time`` etc.) and the lower-case dialect used by
    older obspy test fakes.
    """
    root = ET.fromstring(xml)
    if kind == "ID":
        return _parse_id(root)
    if kind == "CAPABILITIES":
        return _parse_capabilities(root)
    if kind == "STATIONS":
        return _parse_stations(root)
    if kind == "STREAMS":
        return _parse_streams(root)
    if kind == "GAPS":
        return _parse_gaps(root)
    if kind == "CONNECTIONS":
        return _parse_connections(root)
    raise InfoProtocolError(f"unknown INFO kind: {kind!r}")


def _attr(elem: ET.Element, *names: str) -> str | None:
    """Return the first matching attribute, case-insensitive on the name.

    Vendor XML disagrees on case (``begin_time`` vs ``BeginTime``) and on
    naming (``name`` vs ``station``), so callers pass the candidates in
    preferred order and we pick the first that exists.
    """
    lowered = {k.lower(): v for k, v in elem.attrib.items()}
    for name in names:
        v = lowered.get(name.lower())
        if v is not None:
            return v
    return None


def _opt_float(value: str | None) -> float | None:
    """Parse an optional float attribute, returning ``None`` on empty/garbage.

    Logs a debug entry rather than raising — INFO XML often carries
    whitespace-only or empty-string numeric attributes for stations
    with unknown coordinates, and we don't want one bad row to fail
    the whole fetch.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        _log.debug("info_optional_float_malformed", value=value)
        return None


def _opt_int(value: str | None) -> int | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def _iter_descendants(root: ET.Element, tag: str) -> list[ET.Element]:
    """Find ``<tag>`` elements anywhere under ``root``, including the root itself.

    Some servers wrap stations in ``<seedlink>`` / ``<station_list>``;
    others emit ``<station>`` directly under the doc root. ``iter()``
    handles both shapes uniformly.
    """
    return [e for e in root.iter() if _local_name(e.tag).lower() == tag.lower()]


def _local_name(tag: str) -> str:
    """Strip XML namespace from ``{ns}local`` if present."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _parse_id(root: ET.Element) -> ServerIdentity:
    # SeisComP root is ``<seedlink software="..." organization="...">``;
    # obspy-test fake mirrors that shape.
    software = _attr(root, "software", "version") or ""
    organization = _attr(root, "organization") or ""
    started_at = _attr(root, "started", "started_at", "start_time")
    capabilities: list[str] = []
    for cap in _iter_descendants(root, "capability"):
        name = _attr(cap, "name")
        if name:
            capabilities.append(name)
    return ServerIdentity(
        version=software,
        organization=organization,
        started_at=started_at,
        capabilities=tuple(capabilities),
    )


def _parse_capabilities(root: ET.Element) -> tuple[str, ...]:
    out: list[str] = []
    for cap in _iter_descendants(root, "capability"):
        name = _attr(cap, "name")
        if name:
            out.append(name)
    return tuple(out)


def _parse_stations(root: ET.Element) -> list[StationInfo]:
    stations = _iter_descendants(root, "station")
    out: list[StationInfo] = []
    for st in stations:
        network = _attr(st, "network", "net") or ""
        station = _attr(st, "name", "station", "sta") or ""
        if not network or not station:
            # Skip rows missing the two mandatory keys rather than fail
            # the whole fetch — vendor XML occasionally includes a
            # ``<station>`` placeholder for un-provisioned slots.
            continue
        out.append(
            StationInfo(
                network=network,
                station=station,
                description=_attr(st, "description", "desc"),
                begin=_attr(st, "begin_time", "begin", "start_time"),
                end=_attr(st, "end_time", "end", "stop_time"),
                latitude=_opt_float(_attr(st, "latitude", "lat")),
                longitude=_opt_float(_attr(st, "longitude", "lon")),
            )
        )
    return out


def _parse_streams(root: ET.Element) -> list[StreamInfo]:
    out: list[StreamInfo] = []
    for st in _iter_descendants(root, "station"):
        network = _attr(st, "network", "net") or ""
        station = _attr(st, "name", "station", "sta") or ""
        if not network or not station:
            continue
        for stream in st:
            if _local_name(stream.tag).lower() != "stream":
                continue
            out.append(
                StreamInfo(
                    network=network,
                    station=station,
                    location=_attr(stream, "location", "loc") or "",
                    channel=_attr(stream, "seedname", "channel", "cha") or "",
                    type=_attr(stream, "type") or "D",
                    begin=_attr(stream, "begin_time", "begin", "start_time"),
                    end=_attr(stream, "end_time", "end", "stop_time"),
                    sampling_rate=_opt_float(_attr(stream, "sampling_rate", "samprate")),
                )
            )
    return out


def _parse_gaps(root: ET.Element) -> list[GapInfo]:
    out: list[GapInfo] = []
    for gap in _iter_descendants(root, "gap"):
        stream_id = _attr(gap, "stream_id", "stream", "id") or ""
        t_start = _attr(gap, "begin_time", "begin", "start_time") or ""
        t_end = _attr(gap, "end_time", "end", "stop_time") or ""
        samples = _opt_int(_attr(gap, "samples", "samples_missing", "missing")) or 0
        if not stream_id:
            continue
        out.append(
            GapInfo(
                stream_id=stream_id,
                t_start=t_start,
                t_end=t_end,
                samples_missing=samples,
            )
        )
    return out


def _parse_connections(root: ET.Element) -> list[ConnectionInfo]:
    out: list[ConnectionInfo] = []
    for conn in _iter_descendants(root, "connection"):
        client = _attr(conn, "client", "name") or ""
        host = _attr(conn, "host", "address", "ip") or ""
        # Stash any remaining attributes as kv pairs — vendors disagree
        # on which fields are mandatory, so we surface the lot rather
        # than silently dropping data the GUI might want to show.
        consumed = {"client", "name", "host", "address", "ip"}
        extra = tuple((k, v) for k, v in conn.attrib.items() if k.lower() not in consumed)
        out.append(ConnectionInfo(client=client, host=host, extra=extra))
    return out


# Exported for callers that want to type-annotate explicitly.
__all__ = [
    "CancellationToken",
    "ConnectionInfo",
    "GapInfo",
    "InfoKind",
    "InfoResult",
    "ServerIdentity",
    "StationInfo",
    "StreamInfo",
    "fetch",
    "fetch_streams",
]
