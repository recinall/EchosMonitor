"""Echos device discovery — mDNS browse + typed REST probe (M6).

Skills: ``echos-rest-api`` (the probe contract) and ``qt-worker-threading``
(this worker copies the :class:`~echosmonitor.core.echos_status.
EchosStatusWorker` canon: parentless QObject on a dedicated QThread,
queued slots, ``asyncio.run`` inside the slot with a threadsafe
cancel-on-stop). Read both before changing.

Wire contract pinned 2026-06-12 against real fw 1aa72cbe (echos.local):
the firmware advertises ``ADS131M04-WebServer._http._tcp.local.`` on the
REST port with TXT ``version=1.0``, ``board=ESP32-S3``. The advert is
only a candidate PREFILTER (politeness — don't GET every printer on the
LAN); confirmation is the typed probe of the PUBLIC ``GET /api/status``
+ ``GET /api/seedlink/config`` (credential-less, read-only — it can
never trip the auth lockout, rule 15). A node that does not advertise
(e.g. a Pi-hosted SeedLink) is added manually instead — discovery is a
convenience, never the only path.

The ``zeroconf`` import is lazy: a stripped install degrades to
``discoveryFailed("unavailable", …)`` and manual add keeps working.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from PySide6.QtCore import QObject, Signal, Slot

from echosmonitor.core.echos_api import EchosApiClient
from echosmonitor.core.exceptions import EchosApiError
from echosmonitor.core.models import DiscoveredEchos

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_log = structlog.get_logger(__name__)

# How long the mDNS browse listens before resolving candidates. Long
# enough for one query/response round on a quiet LAN; the dialog can
# always re-scan.
_BROWSE_WINDOW_S = 4.0

# Resolve timeout for one candidate's AsyncServiceInfo request (ms).
_RESOLVE_TIMEOUT_MS = 2000

# Aggregate budget for resolving the browsed adverts (rule 7: each resolve
# is bounded but a LAN full of printers must not make the SUM unbounded).
# Name-hint matches resolve first, so the budget starves strangers, not
# Echos nodes.
_RESOLVE_BUDGET_S = 10.0

# Cap on collected adverts per scan (rule 5: bounded, drop logged).
_MAX_ADVERTS = 64

_SERVICE_TYPE = "_http._tcp.local."

# Candidate prefilter, pinned from the real advert (see module docstring).
# Case-insensitive substring/prefix — a second device on the same LAN gets
# mDNS-conflict-renamed ("ADS131M04-WebServer (2)"), so never match exact.
_NAME_HINT = "ads131m04"
_BOARD_HINT = "esp32"


class DiscoveryUnavailableError(Exception):
    """zeroconf is not importable — the discovery FEATURE is absent."""


def is_echos_candidate(instance: str, board: str) -> bool:
    """The mDNS prefilter: does this ``_http._tcp`` advert look like Echos?

    Pure and deliberately loose (substring/prefix, case-insensitive): the
    typed probe is the real gate, this only spares non-candidates a GET.
    """
    return _NAME_HINT in instance.lower() or board.lower().startswith(_BOARD_HINT)


def _note_advert(names: list[str], name: str) -> None:
    """Collect one browsed advert: deduped (an Added→Removed→Added flap
    must not probe twice) and capped (rule 5 — bounded, drop logged)."""
    if name in names:
        return
    if len(names) >= _MAX_ADVERTS:
        _log.warning("echos_discovery_adverts_capped", cap=_MAX_ADVERTS, dropped=name)
        return
    names.append(name)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One prefiltered mDNS advert, pre-probe (worker-internal)."""

    instance: str
    hostname: str  # trailing dot stripped
    address: str  # first IPv4
    http_port: int
    board: str


def _default_client_factory(address: str, http_port: int) -> EchosApiClient:
    # No GET retries: a candidate that does not answer promptly is simply
    # not reported this scan; retries would only stack latency per host.
    return EchosApiClient(address, http_port, get_retries=0, retry_delay_s=0.0)


class EchosDiscoveryWorker(QObject):
    """One-shot LAN scans for Echos nodes, on a dedicated worker thread.

    Owner (the discovery dialog) requests a scan via a queued connection
    to :meth:`discover`; results stream back as one ``deviceDiscovered``
    per confirmed node, terminated by ``discoveryFinished(count)`` or
    ``discoveryFailed(kind, message)``. ``stop()`` is a plain method
    (NOT a Slot — InfoWorker canon) that cancels the in-flight asyncio
    work from any thread, so teardown never waits out an HTTP timeout
    (rule 7).
    """

    deviceDiscovered = Signal(object)  # DiscoveredEchos  # noqa: N815
    discoveryFinished = Signal(int)  # confirmed count  # noqa: N815
    discoveryFailed = Signal(str, str)  # kind, message  # noqa: N815

    def __init__(
        self,
        client_factory: Callable[[str, int], EchosApiClient] | None = None,
        browse: Callable[[], Awaitable[list[_Candidate]]] | None = None,
    ) -> None:
        super().__init__()
        self._client_factory = client_factory or _default_client_factory
        self._browse = browse or self._zeroconf_browse
        self._stop_flag = False
        # Guards ``_in_flight`` (same shape as EchosStatusWorker): stop()
        # on the GUI thread must never observe a half-installed task.
        self._lock = threading.Lock()
        self._in_flight: tuple[asyncio.AbstractEventLoop, asyncio.Task[object]] | None = None

    # ------------------------------------------------------------------
    # Slot — runs on the worker thread (queued from the dialog)
    # ------------------------------------------------------------------
    @Slot()
    def discover(self) -> None:
        """Run one bounded scan: browse → prefilter → probe → emit.

        Scans serialize naturally (slots run one at a time on the worker
        thread); the dialog disables its re-scan button while one is in
        flight, so there is no queue to bound.
        """
        if self._stop_flag:
            return
        started = time.monotonic()
        _log.info("echos_discovery_started")
        try:
            # Confirmed devices stream out via deviceDiscovered as each
            # probe lands (queued, GUI renders rows live); the return is
            # only the count for the terminal signal.
            confirmed = asyncio.run(self._discover_async())
        except asyncio.CancelledError:
            _log.info("echos_discovery_canceled")
            return
        except DiscoveryUnavailableError as exc:
            _log.warning("echos_discovery_unavailable", error=str(exc))
            self.discoveryFailed.emit("unavailable", str(exc))
            return
        except Exception as exc:  # never crash the worker thread
            _log.exception("echos_discovery_unexpected_error", error=str(exc))
            self.discoveryFailed.emit("protocol", f"unexpected: {type(exc).__name__}: {exc}")
            return
        _log.info(
            "echos_discovery_finished",
            found=confirmed,
            elapsed_s=round(time.monotonic() - started, 3),
        )
        if self._stop_flag:
            return
        self.discoveryFinished.emit(confirmed)

    @Slot(str, int)
    def probe_host(self, host: str, http_port: int) -> None:
        """One-shot MANUAL probe of a user-entered host (M6 wizard).

        Covers the two non-mDNS paths: a factory-fresh device in AP mode
        (``192.168.4.1``) and nodes that do not advertise (Pi-hosted).
        Same typed public gate as the scan, but a failure IS surfaced —
        the user explicitly asked about this host, so
        ``discoveryFailed(kind, …)`` carries the transport verdict.
        """
        if self._stop_flag:
            return
        host = host.strip().rstrip(".")
        if not host:
            return
        _log.info("echos_discovery_manual_probe", host=host, http_port=http_port)
        candidate = _Candidate(
            instance=host,
            # A .local name doubles as the stable config host; a raw IP
            # gives the prefill nothing better than the address itself.
            hostname=host if host.casefold().endswith(".local") else "",
            address=host,
            http_port=int(http_port),
            board="",
        )
        try:
            device = asyncio.run(self._probe_one_async(candidate))
        except asyncio.CancelledError:
            _log.info("echos_discovery_manual_probe_canceled", host=host)
            return
        except EchosApiError as exc:
            _log.info("echos_discovery_manual_probe_failed", host=host, kind=exc.kind)
            self.discoveryFailed.emit(exc.kind, str(exc))
            return
        except Exception as exc:  # never crash the worker thread
            _log.exception("echos_discovery_manual_probe_error", host=host, error=str(exc))
            self.discoveryFailed.emit("protocol", f"unexpected: {type(exc).__name__}: {exc}")
            return
        if device is not None and not self._stop_flag:
            self.deviceDiscovered.emit(device)

    async def _probe_one_async(self, candidate: _Candidate) -> DiscoveredEchos | None:
        """Run one cancellable probe with the in-flight registration."""
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None  # always inside asyncio.run
        with self._lock:
            if self._stop_flag:
                return None
            self._in_flight = (loop, task)
        try:
            return await self._probe(candidate)
        finally:
            with self._lock:
                self._in_flight = None

    # ------------------------------------------------------------------
    # Plain method (NOT a Slot). Callable from any thread.
    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Cancel any in-flight scan and refuse new ones. Idempotent."""
        with self._lock:
            self._stop_flag = True
            in_flight = self._in_flight
        if in_flight is not None:
            loop, task = in_flight
            # The loop may finish between the lock release and this call;
            # a closed loop raises RuntimeError — the scan is already
            # over, which is what we wanted.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)

    # ------------------------------------------------------------------
    # Internals — worker thread only
    # ------------------------------------------------------------------
    async def _discover_async(self) -> int:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None  # always inside asyncio.run
        with self._lock:
            if self._stop_flag:
                return 0
            self._in_flight = (loop, task)
        try:
            candidates = await self._browse()
            confirmed = 0
            for candidate in candidates:
                if self._stop_flag:
                    break
                try:
                    device = await self._probe(candidate)
                except EchosApiError as exc:
                    # Not an Echos node this scan — logged, never surfaced
                    # (a printer matching the prefilter must not scare the
                    # user); the manual probe path DOES surface the kind.
                    _log.info(
                        "echos_discovery_probe_rejected",
                        host=candidate.address,
                        instance=candidate.instance,
                        kind=exc.kind,
                    )
                    continue
                if not self._stop_flag:
                    self.deviceDiscovered.emit(device)  # rows stream in live
                    confirmed += 1
            return confirmed
        finally:
            with self._lock:
                self._in_flight = None

    async def _probe(self, candidate: _Candidate) -> DiscoveredEchos:
        """Confirm one candidate via the typed PUBLIC endpoints.

        Raises ``EchosApiError`` when the host fails transport or schema
        validation. The StationXML fetch (selector derivation for the
        wizard) is best-effort: its failure degrades to ``channels=()``,
        never to a rejected node.
        """
        async with self._client_factory(candidate.address, candidate.http_port) as client:
            status = await client.get_status()
            seedlink = await client.get_seedlink_config()
            channels: tuple[str, ...] = ()
            try:
                from echosmonitor.core.echos_device_worker import parse_stationxml_channels

                channels = parse_stationxml_channels(await client.get_stationxml())
            except EchosApiError as exc:
                _log.info(
                    "echos_discovery_stationxml_unavailable",
                    host=candidate.address,
                    kind=exc.kind,
                )
        return DiscoveredEchos(
            instance=candidate.instance,
            hostname=candidate.hostname,
            address=candidate.address,
            http_port=candidate.http_port,
            seedlink_port=seedlink.port,
            firmware_version=status.firmware_version,
            project_name=status.project_name,
            board=candidate.board,
            channels=channels,
        )

    async def _zeroconf_browse(self) -> list[_Candidate]:
        """Browse ``_http._tcp`` for one window and resolve the candidates."""
        try:
            from zeroconf import IPVersion, ServiceStateChange, Zeroconf
            from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf
        except ImportError as exc:  # pragma: no cover - exercised via injection
            raise DiscoveryUnavailableError(
                "zeroconf is not installed — device discovery is unavailable; "
                "add devices manually by host name"
            ) from exc

        names: list[str] = []

        def _on_change(
            zeroconf: Zeroconf,
            service_type: str,
            name: str,
            state_change: ServiceStateChange,
        ) -> None:
            # Runs in the asyncio.run loop on THIS worker thread (verified
            # against zeroconf 0.149: the loop is adopted at construction).
            del zeroconf, service_type
            if state_change is ServiceStateChange.Added:
                _note_advert(names, name)

        azc = AsyncZeroconf()
        browser = None
        try:
            browser = AsyncServiceBrowser(azc.zeroconf, _SERVICE_TYPE, handlers=[_on_change])
            await asyncio.sleep(_BROWSE_WINDOW_S)
            # Resolve name-hint matches FIRST: the aggregate budget below
            # must starve the office printers, never the Echos nodes.
            ordered = sorted(names, key=lambda n: _NAME_HINT not in n.lower())
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _RESOLVE_BUDGET_S
            candidates: list[_Candidate] = []
            resolved = 0
            for name in ordered:
                if self._stop_flag:
                    break
                remaining_s = deadline - loop.time()
                if remaining_s <= 0:
                    _log.warning(
                        "echos_discovery_resolve_budget_exhausted",
                        budget_s=_RESOLVE_BUDGET_S,
                        resolved=resolved,
                        dropped=len(ordered) - resolved,
                    )
                    break
                info = AsyncServiceInfo(_SERVICE_TYPE, name)
                timeout_ms = min(_RESOLVE_TIMEOUT_MS, int(remaining_s * 1000.0))
                resolved += 1
                if not await info.async_request(azc.zeroconf, timeout=timeout_ms):
                    continue
                properties = {
                    key.decode("utf-8", "replace"): (
                        value.decode("utf-8", "replace") if isinstance(value, bytes) else ""
                    )
                    for key, value in (info.properties or {}).items()
                    if key
                }
                instance = name.removesuffix("." + _SERVICE_TYPE)
                board = properties.get("board", "")
                if not is_echos_candidate(instance, board):
                    continue
                addresses = info.parsed_addresses(IPVersion.V4Only)
                if not addresses:
                    continue
                candidates.append(
                    _Candidate(
                        instance=instance,
                        hostname=(info.server or "").rstrip("."),
                        address=addresses[0],
                        http_port=int(info.port or 80),
                        board=board,
                    )
                )
            _log.info(
                "echos_discovery_browsed",
                adverts=len(names),
                candidates=len(candidates),
            )
            return candidates
        finally:
            # async_close() does NOT cancel a directly-constructed browser
            # (verified against zeroconf 0.149) — cancel it explicitly,
            # including on the CancelledError path.
            if browser is not None:
                with contextlib.suppress(Exception):
                    await browser.async_cancel()
            await azc.async_close()


__all__ = [
    "DiscoveryUnavailableError",
    "EchosDiscoveryWorker",
    "is_echos_candidate",
]
