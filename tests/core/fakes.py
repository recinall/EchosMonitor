"""Asyncio-based fake SeedLink v3 server for offline tests.

Implements just enough of the protocol that ObsPy's `EasySeedLinkClient`
can connect, negotiate streams, and consume packets:

  - HELLO handshake (server ID + version line).
  - STATION / SELECT / DATA echoed with `OK\\r\\n`.
  - END terminates negotiation.
  - After END, streams 520-byte SLPACKETs (8-byte SLINK header + 512-byte
    STEIM2 MiniSEED record) at a configurable cadence.
  - INFO ID / CAPABILITIES / STATIONS / STREAMS / GAPS / CONNECTIONS
    answered with SLPACKETs whose 8-byte header is ``SLINFO  `` for
    intermediate chunks and ``SLINFO`` (no trailing ``*``) for the
    terminating chunk; the 512-byte payload is an ASCII MSEED record
    carrying the XML chunk. Driven by ``stations`` / ``streams`` config.

The streaming worker pre-populates ``multistation`` capability so it
never sends INFO. The InfoClient (``core/info.py``) sends INFO without
ever entering streaming mode; the two paths are deliberately separate.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import socket
import struct
from dataclasses import dataclass, field

import numpy as np
from obspy import Trace, UTCDateTime

_log = logging.getLogger(__name__)


# 8-byte SLPACKET header convention for INFO packets:
#   intermediate chunk → b"SLINFO *" (asterisk at byte 7)
#   terminating chunk  → b"SLINFO  " (anything other than '*' at byte 7)
# obspy's ``SLPacket.get_type`` distinguishes the two by checking byte 7
# (``self.SLHEADSIZE - 1``) for ``'*'`` — see slpacket.py line 189.
_INFO_HEADER_INTERMEDIATE = b"SLINFO *"
_INFO_HEADER_TERMINATOR = b"SLINFO  "
_MSEED_RECORD_BYTES = 512


@dataclass
class _Session:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    seq: int = 0
    sample_idx: int = 0
    streaming: bool = False
    closed: bool = False


@dataclass(frozen=True)
class FakeStation:
    """Fake STATIONS entry. Defaults make every field optional."""

    network: str
    station: str
    description: str = ""
    begin: str = "2024-01-01T00:00:00"
    end: str = "2099-12-31T23:59:59"
    latitude: float | None = None
    longitude: float | None = None


@dataclass(frozen=True)
class FakeStream:
    """Fake STREAMS entry. Mirrors the production ``StreamInfo`` shape."""

    network: str
    station: str
    location: str
    channel: str
    type: str = "D"
    sampling_rate: float = 100.0
    begin: str = "2024-01-01T00:00:00"
    end: str = "2099-12-31T23:59:59"


@dataclass
class FakeSeedLinkServerConfig:
    network: str = "IV"
    station: str = "MILN"
    location: str = ""
    channel: str = "HHZ"
    sampling_rate: float = 100.0
    samples_per_record: int = 50
    packet_interval_s: float = 0.05
    sine_freq_hz: float = 1.0
    sine_amplitude: float = 1000.0
    # Catalog driving INFO STATIONS / INFO STREAMS responses. Default
    # empty so existing streaming tests (which never issue INFO) stay
    # byte-for-byte unaffected.
    stations: tuple[FakeStation, ...] = ()
    streams: tuple[FakeStream, ...] = ()
    # When True, accept the connection (TCP + HELLO succeeds) but never
    # reply to INFO requests — used to exercise the InfoClient's
    # wall-clock deadline in ``test_info_client::test_fetch_timeout``.
    info_silent_mode: bool = False
    # When set, replace the would-be INFO XML response with this raw
    # bytes payload. Used to simulate malformed XML so the InfoClient's
    # protocol-error path can be tested deterministically.
    info_bad_xml: bytes | None = None
    # Optional list of XML chunk overrides for ID / CAPABILITIES etc.,
    # keyed by uppercase INFO level. When unset, the fake synthesizes
    # XML from ``stations`` / ``streams``.
    info_xml_overrides: dict[str, str] = field(default_factory=dict)
    # When True, the fake responds ``ERROR\r\n`` to every ``STATION``
    # command (other negotiation verbs still get ``OK``). This drives
    # obspy's "response: station not accepted, skipping" → "no stations
    # accepted" path so the worker can be tested against the
    # ``protocol_rejected`` classification. Mutable at runtime: the
    # ``test_worker_recovers_when_server_starts_accepting`` test flips
    # this field while the server is already running.
    reject_all_stations: bool = False


class FakeSeedLinkServer:
    """Asyncio TCP server speaking just enough SeedLink v3 for tests.

    Bind with `host=127.0.0.1, port=0` to let the OS pick a free port; read
    `.host`/`.port` after `start()` to find out which one.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        config: FakeSeedLinkServerConfig | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._cfg = config or FakeSeedLinkServerConfig()
        self._server: asyncio.base_events.Server | None = None
        self._sessions: list[_Session] = []
        self._stream_tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def config(self) -> FakeSeedLinkServerConfig:
        return self._cfg

    @property
    def active_session_count(self) -> int:
        return len([s for s in self._sessions if not s.closed])

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._handle_session, self._host, self._port)
        sock = self._server.sockets[0]
        self._port = sock.getsockname()[1]
        _log.info("fake seedlink server listening on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        # Close sessions first so handler tasks observe their stream as
        # closed and unwind cleanly via the OSError branch.
        for session in list(self._sessions):
            await self._close_session(session)
        self._sessions.clear()

        # Cancel and await every still-running handler task. Without this
        # await, on Python 3.11 `Server.wait_closed()` does not block on
        # outstanding handlers, so they keep running on a soon-to-be-torn
        # asyncio loop and can touch freed selector state on rapid teardown.
        for task in list(self._stream_tasks):
            if not task.done():
                task.cancel()
        for task in list(self._stream_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._stream_tasks.clear()

        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def inject_disconnect(self) -> None:
        """Abruptly drop every active client connection (server-side close).

        Sets `SO_LINGER` with timeout 0 on the underlying socket and closes —
        this sends a TCP RST. A graceful FIN-close (the asyncio default)
        leaves an `EasySeedLinkClient` in a 0-byte recv loop indefinitely
        until its 120 s network timeout fires.
        """
        linger_off = struct.pack("ii", 1, 0)
        for session in list(self._sessions):
            transport = session.writer.transport
            sock = transport.get_extra_info("socket")
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger_off)
            with contextlib.suppress(Exception):
                transport.close()
            await self._close_session(session)
        self._sessions.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _handle_session(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        session = _Session(reader=reader, writer=writer)
        self._sessions.append(session)
        # Register the running task so stop() can await it. Without this the
        # set was never populated and stop() returned before handlers really
        # finished — fine on Python 3.12 (Server.wait_closed waits for
        # handlers) but flaky on 3.11 where it does not.
        task = asyncio.current_task()
        if task is not None:
            self._stream_tasks.add(task)
            task.add_done_callback(self._stream_tasks.discard)
        try:
            await self._negotiate(session)
            session.streaming = True
            await self._stream(session)
        except (
            asyncio.IncompleteReadError,
            asyncio.CancelledError,
            ConnectionResetError,
            BrokenPipeError,
            OSError,
        ):
            pass
        except Exception as exc:
            _log.warning("fake server session error: %s", exc)
        finally:
            await self._close_session(session)

    async def _close_session(self, session: _Session) -> None:
        if session.closed:
            return
        session.closed = True
        with contextlib.suppress(Exception):
            session.writer.close()
        with contextlib.suppress(Exception):
            await session.writer.wait_closed()

    async def _negotiate(self, session: _Session) -> None:
        while True:
            raw = await session.reader.readuntil(b"\r")
            cmd = raw.rstrip(b"\r\n").decode("ascii", errors="replace").strip()
            upper = cmd.upper()

            if upper == "HELLO":
                session.writer.write(b"SeedLink v3.2 FakeSeedLink\r\nFakeSeedLink\r\n")
                await session.writer.drain()
            elif upper == "END":
                return
            elif upper.startswith("INFO"):
                # INFO is answered out-of-band of the streaming negotiation:
                # we emit one or more SLPACKETs carrying the XML response,
                # then return to the negotiation loop. The InfoClient
                # closes the socket after one INFO exchange so we never
                # transition to streaming mode for INFO-only sessions.
                if self._cfg.info_silent_mode:
                    # Accept the request but never reply — drives the
                    # InfoClient's wall-clock deadline path. Stay in the
                    # loop so a follow-up cancel / close from the client
                    # is still observed.
                    continue
                level = upper[len("INFO") :].strip()
                xml_bytes = self._render_info_response(level)
                await self._send_info_packets(session, xml_bytes)
            elif upper.startswith("STATION"):
                if self._cfg.reject_all_stations:
                    session.writer.write(b"ERROR\r\n")
                else:
                    session.writer.write(b"OK\r\n")
                await session.writer.drain()
            elif (
                upper.startswith("SELECT")
                or upper == "DATA"
                or upper.startswith("DATA ")
                or upper.startswith("FETCH")
                or upper.startswith("TIME")
                or upper == "BATCH"
            ):
                session.writer.write(b"OK\r\n")
                await session.writer.drain()
            else:
                session.writer.write(b"ERROR\r\n")
                await session.writer.drain()

    def _render_info_response(self, level: str) -> bytes:
        """Build the XML payload for one INFO level.

        ``level`` is the post-``INFO`` portion of the command, already
        upper-cased and stripped — e.g. ``"STATIONS"`` or
        ``"STREAMS IU_ANMO"``. Anything unrecognised becomes an empty
        ``<seedlink/>`` document; that's accepted by the InfoClient
        parser and just produces an empty list, which is what a real
        server would return for an unknown level on most implementations.
        """
        if self._cfg.info_bad_xml is not None:
            return self._cfg.info_bad_xml
        cfg = self._cfg
        # Allow per-level XML overrides for tests that want to drive
        # parsing edge cases without rebuilding the whole response.
        if level in cfg.info_xml_overrides:
            return cfg.info_xml_overrides[level].encode("utf-8")

        if level == "ID":
            xml = (
                '<?xml version="1.0"?>'
                '<seedlink software="FakeSeedLink v3.2" '
                'organization="FakeOrg" started="2026-01-01T00:00:00">'
                "<capabilities>"
                '<capability name="dialup"/>'
                '<capability name="multistation"/>'
                '<capability name="info:streams"/>'
                "</capabilities>"
                "</seedlink>"
            )
            return xml.encode("utf-8")
        if level == "CAPABILITIES":
            xml = (
                '<?xml version="1.0"?>'
                "<capabilities>"
                '<capability name="dialup"/>'
                '<capability name="multistation"/>'
                "</capabilities>"
            )
            return xml.encode("utf-8")
        if level == "STATIONS":
            return self._render_stations_xml(cfg.stations)
        if level.startswith("STREAMS"):
            # Optional ``STREAMS NET_STA`` filter — emulate server-side
            # filtering so the InfoClient's fetch_streams test exercises
            # the network/station filter on the wire.
            tail = level[len("STREAMS") :].strip()
            streams = cfg.streams
            if tail and "_" in tail:
                net_filter, sta_filter = tail.split("_", 1)
                streams = tuple(
                    s for s in streams if s.network == net_filter and s.station == sta_filter
                )
            return self._render_streams_xml(streams)
        # GAPS / CONNECTIONS / unknown — empty document.
        return b'<?xml version="1.0"?><seedlink/>'

    @staticmethod
    def _render_stations_xml(stations: tuple[FakeStation, ...]) -> bytes:
        out = ['<?xml version="1.0"?><seedlink>']
        for st in stations:
            attrs = [
                f'network="{st.network}"',
                f'name="{st.station}"',
                f'description="{st.description}"',
                f'begin_time="{st.begin}"',
                f'end_time="{st.end}"',
            ]
            if st.latitude is not None:
                attrs.append(f'latitude="{st.latitude}"')
            if st.longitude is not None:
                attrs.append(f'longitude="{st.longitude}"')
            out.append(f"<station {' '.join(attrs)}/>")
        out.append("</seedlink>")
        return "".join(out).encode("utf-8")

    @staticmethod
    def _render_streams_xml(streams: tuple[FakeStream, ...]) -> bytes:
        # Group streams by (network, station) so the document mirrors
        # the SeisComP shape: <station>...<stream/>...<stream/></station>.
        groups: dict[tuple[str, str], list[FakeStream]] = {}
        for s in streams:
            groups.setdefault((s.network, s.station), []).append(s)
        out = ['<?xml version="1.0"?><seedlink>']
        for (net, sta), bucket in groups.items():
            out.append(f'<station network="{net}" name="{sta}">')
            for s in bucket:
                out.append(
                    f'<stream location="{s.location}" seedname="{s.channel}" '
                    f'type="{s.type}" sampling_rate="{s.sampling_rate}" '
                    f'begin_time="{s.begin}" end_time="{s.end}"/>'
                )
            out.append("</station>")
        out.append("</seedlink>")
        return "".join(out).encode("utf-8")

    async def _send_info_packets(self, session: _Session, xml: bytes) -> None:
        """Frame ``xml`` as one or more 520-byte SLINFO packets.

        Each packet is a 8-byte header (``SLINFO *`` for intermediate,
        ``SLINFO  `` for the terminator) plus a 512-byte ASCII MSEED
        record carrying up to ~440 bytes of XML in its data section.
        We size the chunk by trial: build a record from N XML bytes and
        check the record stays at exactly 512 bytes; if it overflows
        we shrink the chunk. In practice ASCII MSEED fits ~440 bytes
        of payload comfortably.
        """
        chunk_size = 400  # safe upper bound for ASCII MSEED w/ 512-byte reclen
        offset = 0
        # Always emit at least one packet, even for empty XML, so the
        # client's ``while True: collect()`` loop sees a TYPE_SLINFT.
        if not xml:
            xml = b" "
        while offset < len(xml):
            chunk = xml[offset : offset + chunk_size]
            offset += chunk_size
            is_last = offset >= len(xml)
            header = _INFO_HEADER_TERMINATOR if is_last else _INFO_HEADER_INTERMEDIATE
            record = self._make_info_record(chunk)
            session.writer.write(header + record)
            await session.writer.drain()

    @staticmethod
    def _make_info_record(payload: bytes) -> bytes:
        """Wrap ``payload`` in a 512-byte ASCII MSEED record.

        Uses ``encoding="ASCII"`` and a synthetic NSLC of ``XX.INFO..INF``
        so the record is unambiguously not seismic data. obspy's
        ``SLPacket.get_string_payload`` reads the ``samplecnt`` data
        bytes back from the record, which is exactly what we put in.
        """
        data = np.frombuffer(payload, dtype="|S1")
        tr = Trace(
            data=data,
            header={
                "network": "XX",
                "station": "INFO",
                "location": "",
                "channel": "INF",
                "starttime": UTCDateTime(2026, 1, 1),
                "sampling_rate": 1.0,
            },
        )
        buf = io.BytesIO()
        tr.write(buf, format="MSEED", encoding="ASCII", reclen=512)
        rec = buf.getvalue()
        if len(rec) != _MSEED_RECORD_BYTES:  # pragma: no cover - defensive
            raise RuntimeError(f"expected {_MSEED_RECORD_BYTES}-byte INFO record, got {len(rec)}")
        return rec

    async def _stream(self, session: _Session) -> None:
        cfg = self._cfg
        n = cfg.samples_per_record
        sr = cfg.sampling_rate
        start = UTCDateTime()
        try:
            while not session.closed:
                packet_t = start + (session.sample_idx / sr)
                samples = self._make_samples(session.sample_idx, n, sr)
                record = self._make_mseed_record(packet_t, samples)
                header = ("SL%06X" % (session.seq & 0xFFFFFF)).encode("ascii")
                if len(header) != 8:  # pragma: no cover - defensive
                    raise RuntimeError(f"unexpected header length: {len(header)}")
                session.writer.write(header + record)
                await session.writer.drain()
                session.seq = (session.seq + 1) & 0xFFFFFF
                session.sample_idx += n
                await asyncio.sleep(cfg.packet_interval_s)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _make_samples(self, start_idx: int, n: int, sr: float) -> np.ndarray:
        cfg = self._cfg
        idx = np.arange(start_idx, start_idx + n)
        return (cfg.sine_amplitude * np.sin(2.0 * np.pi * cfg.sine_freq_hz * idx / sr)).astype(
            np.int32
        )

    def _make_mseed_record(self, starttime: UTCDateTime, samples: np.ndarray) -> bytes:
        cfg = self._cfg
        tr = Trace(
            data=samples,
            header={
                "network": cfg.network,
                "station": cfg.station,
                "location": cfg.location,
                "channel": cfg.channel,
                "starttime": starttime,
                "sampling_rate": cfg.sampling_rate,
            },
        )
        buf = io.BytesIO()
        tr.write(buf, format="MSEED", encoding="STEIM2", reclen=512)
        data = buf.getvalue()
        if len(data) != 512:  # pragma: no cover - defensive
            raise RuntimeError(f"expected 512-byte record, got {len(data)} bytes")
        return data
