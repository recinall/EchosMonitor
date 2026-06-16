"""Core data models shared by the streaming engine and GUI.

These types stay deliberately Qt-free and obspy-light so they can be
imported anywhere — including from `gui/` modules that must not pull in
the SeedLink client.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Literal, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable

    from obspy.core.utcdatetime import UTCDateTime


# Closed set of failure causes the worker classifies a connection attempt
# into. Kept as a ``Literal`` so the GUI can render it deterministically
# and tests can assert exact strings. When extending, add the value here
# AND in ``_FAILURE_KIND_HUMANIZED`` in ``gui/widgets/device_panel.py``.
#
# Values:
#   timeout              — TCP preflight exceeded ``connect_timeout_s``.
#   refused              — peer answered RST during the TCP handshake.
#   dns                  — ``getaddrinfo`` failed or returned no addresses.
#   unknown              — any other connect / session failure that did
#                          not match a more specific bucket.
#   protocol_rejected    — TCP handshake succeeded and SeedLink HELLO
#                          completed, but the server returned ``ERROR\r\n``
#                          to one or more ``STATION`` requests and obspy's
#                          internal "no stations accepted" path fired.
#                          Surfaced via a ``logging.Filter`` because obspy
#                          catches and swallows the underlying
#                          ``SeedLinkException`` inside
#                          ``SeedLinkConnection.collect()``.
#   protocol_unsupported — RESERVED. Server speaks SeedLink but lacks a
#                          capability we need (e.g. ``info:streams``).
#                          No worker code emits this yet; kept in the
#                          literal so the panel surface and any future
#                          producer share one closed set.
FailureKind = Literal[
    "timeout",
    "refused",
    "dns",
    "unknown",
    "protocol_rejected",
    "protocol_unsupported",
]


# Closed set of failure causes the Echos REST client (core/echos_api.py)
# classifies a request into (skill: echos-rest-api). The device dialog and
# status poller branch on this — never on message text. Carried by the
# ``EchosApiError`` hierarchy in core/exceptions.py.
#
# Values:
#   auth_failed — the device rejected the admin credentials (HTTP 401),
#                 or a write was attempted with no password configured.
#   locked_out  — the device's auth lockout is active (HTTP 429); the
#                 exception carries ``retry_after_s``. Also raised by the
#                 client-side guard that refuses to hammer the device
#                 before the window expires (rule 15).
#   unreachable — network-level failure (DNS, refused, reset) before a
#                 response arrived.
#   timeout     — connect/read deadline elapsed (rule 7 bound).
#   protocol    — the device answered, but not in the expected shape
#                 (unexpected status code, non-JSON body, schema mismatch).
EchosErrorKind = Literal[
    "auth_failed",
    "locked_out",
    "unreachable",
    "timeout",
    "protocol",
]


@dataclass(frozen=True, slots=True)
class EchosPollTarget:
    """One device the M1-C status poller should poll (config → worker).

    Built by the GUI from ``DeviceConfig`` (name/host) + its ``echos``
    section and pushed to :class:`~echosmonitor.core.echos_status.
    EchosStatusWorker` via a queued ``Signal(object)`` carrying a tuple
    of these (rule 4: frozen payloads, isinstance-guarded on receipt).
    Polling uses only the firmware's PUBLIC GET endpoints, so no
    credentials travel with the target.
    """

    name: str
    host: str
    http_port: int = 80
    poll_interval_s: float = 5.0


class ClockHealth(StrEnum):
    """Closed clock-discipline verdict for a device's timestamps (M6).

    Declared best → worst — declaration order ONLY: do not compare
    members (StrEnum compares as strings, and string order does not
    follow severity). On a seismic node the timestamps are only as
    trustworthy as the clock discipline: GNSS with a locked PPS PLL is
    sample-accurate; GNSS time without PPS is second-accurate; NTP is
    network-accuracy; HOLDOVER means the clock was set once but every
    live source is gone (an ESP32 crystal free-runs at tens of ppm —
    seconds/day drift), and UNSYNCED means the record times were never
    set at all. The last two are attention states the UI must shout.
    """

    PPS = "pps"  # GNSS time valid AND the PPS PLL is locked
    GNSS = "gnss"  # GNSS time valid, PPS not locked
    NTP = "ntp"  # the device REPORTS NTP-synchronized (no GNSS)
    HOLDOVER = "holdover"  # synchronized once, all live sources lost — drifting
    UNSYNCED = "unsynced"  # no time source at all


@dataclass(frozen=True, slots=True)
class EchosDeviceSnapshot:
    """One successful Echos status poll (worker → GUI wire payload).

    Aggregates ``GET /api/status`` (firmware, uptime, GNSS, clock sync),
    ``GET /api/seedlink/status`` (clients, ring) and
    ``GET /api/calibrate/status`` (calibration state) into the flat,
    Qt-free shape the DevicePanel's Echos column renders. Frozen so a
    single instance can cross the thread boundary via a queued
    ``Signal(object)`` safely.

    ``polled_at`` is ``time.monotonic()`` at poll completion — for
    staleness arithmetic on the GUI side, not wall-clock display.

    The clock fields (M6) default to their pessimistic values so the
    derived :meth:`clock_health` can only ever err toward UNSYNCED, never
    toward a false "synchronized".
    """

    device: str
    firmware_version: str
    uptime_s: float
    gnss_fix: bool
    gnss_satellites: int
    pps_locked: bool
    clients_connected: int
    ring_used_pct: float
    calibration_state: str
    polled_at: float
    # --- clock sync (M6: /api/status time block) ----------------------
    time_synchronized: bool = False
    ntp_synchronized: bool = False
    # Free-form firmware string ("RMC+PPS+NTP" on fw 1aa72cbe) — display
    # only, NEVER branched on (the vocabulary is not pinned).
    time_sync_type: str = ""
    pps_offset_us: int = 0

    def clock_health(self) -> ClockHealth:
        """Derive the closed clock verdict from the sync booleans.

        ``gnss_fix`` mirrors the firmware's ``gnss_time_valid`` (the
        poller maps it 1:1); ``time_sync_type`` is deliberately ignored
        here — it is an unpinned composite string.
        """
        if self.gnss_fix and self.pps_locked:
            return ClockHealth.PPS
        if self.gnss_fix:
            return ClockHealth.GNSS
        if self.ntp_synchronized:
            return ClockHealth.NTP
        if self.time_synchronized:
            # The clock WAS set, but no live source backs it now: holdover,
            # not "NTP" — claiming network accuracy here would be the false
            # "synchronized" this model promises never to report.
            return ClockHealth.HOLDOVER
        return ClockHealth.UNSYNCED


@dataclass(frozen=True, slots=True)
class DiscoveredEchos:
    """One mDNS-discovered AND probe-confirmed Echos node (M6).

    Produced by :class:`~echosmonitor.core.discovery.EchosDiscoveryWorker`:
    the mDNS advert (instance/TXT) is only the candidate prefilter — every
    field below the fold is confirmed by the typed PUBLIC probe
    (``GET /api/status`` + ``GET /api/seedlink/config``), so a row in the
    discovery dialog is always a real, reachable Echos node. Frozen:
    crosses the worker→GUI boundary via a queued ``Signal(object)``.

    ``hostname`` is the mDNS name (``echos.local``) — preferred for the
    device config because it survives DHCP lease changes; ``address`` is
    the resolved IPv4 the probe actually used.
    """

    instance: str  # mDNS instance name (e.g. "ADS131M04-WebServer")
    hostname: str  # mDNS hostname, no trailing dot (e.g. "echos.local")
    address: str  # IPv4 the probe used
    http_port: int  # REST port from the advert
    seedlink_port: int  # from /api/seedlink/config — the DeviceConfig.port
    firmware_version: str  # from /api/status
    project_name: str  # from /api/status
    board: str  # TXT "board" (e.g. "ESP32-S3"), display-only
    # NSLC strings from the device StationXML (M6 wizard: exact selector
    # derivation). () when the document was unavailable/unparseable —
    # the probe still confirms, selectors just stay manual.
    channels: tuple[str, ...] = ()


# Separator used to namespace per-stream engine state by device. The same
# NSLC arriving from two different SeedLink servers must not share a ring
# buffer, coalescer, or chain — keying by ``f"{device}{DEVICE_KEY_SEP}{nslc}"``
# keeps the two independent. The character is "/" so the composite key
# reads naturally in logs ("iris/IU.ANMO.00.BHZ"). It must NOT collide with
# any character that can appear inside an NSLC; SEED's grammar excludes
# "/" by definition, so this is safe.
DEVICE_KEY_SEP = "/"


def device_stream_key(device_name: str, nslc: str) -> str:
    """Compose the engine-internal key for one stream on one device."""
    return f"{device_name}{DEVICE_KEY_SEP}{nslc}"


class StreamID(NamedTuple):
    """SEED naming tuple: network, station, location, channel."""

    network: str
    station: str
    location: str
    channel: str

    @property
    def nslc(self) -> str:
        return f"{self.network}.{self.station}.{self.location}.{self.channel}"

    @classmethod
    def from_trace_id(cls, trace_id: str) -> StreamID:
        """Parse an ObsPy `Trace.id` string ("NET.STA.LOC.CHA").

        Raises:
            ValueError: if the string does not have exactly four
                dot-separated parts.
        """
        parts = trace_id.split(".")
        if len(parts) != 4:
            raise ValueError(
                f"trace_id must have exactly four dot-separated parts, got {trace_id!r}"
            )
        network, station, location, channel = parts
        return cls(network=network, station=station, location=location, channel=channel)


class StreamSelector(NamedTuple):
    """Stream selection with optional wildcards (`*`, `?`)."""

    network: str
    station: str
    location: str
    channel: str


class ConnState(IntEnum):
    """Connection lifecycle states reported by `SeedLinkWorker`.

    Two amber states are deliberately distinct:

    - ``CONNECTING`` — the worker is actively attempting a TCP handshake,
      either for the first time or as the next try after a backoff.
    - ``WAITING_RETRY`` — the previous attempt failed (or a connected
      session dropped) and the worker is sleeping until the next attempt.

    ``RECONNECTING`` is kept for the *transient* moment a CONNECTED
    session drops; the worker then transitions through ``WAITING_RETRY``
    while it sleeps, then back to ``CONNECTING`` for the next attempt.
    """

    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    RECONNECTING = 3
    WAITING_RETRY = 4
    STOPPED = 5


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """One row of the ``sessions`` index, as read back by the DAO.

    ``project_name`` is the raw user-chosen name (rule 14); ``None``
    identifies the sessionless monitoring index (detection-only rows).
    ``closed_dirty`` marks a session that was found still open on a
    later launch and closed administratively — its ``ended_at`` is the
    close time, not the real end of recording. ``reindexed`` marks a
    row SYNTHESIZED by the M3-D re-indexer (DB was missing — the span
    is the data extent, the name is the directory name). Timestamps
    are ISO-8601 UTC strings (lexicographic == chronological).
    """

    id: int
    project_name: str | None
    started_at: str
    ended_at: str | None
    closed_dirty: bool
    host: str
    devices: tuple[str, ...]
    reindexed: bool = False


@dataclass(frozen=True, slots=True)
class SessionEntry:
    """One browsable session: its index row plus where it lives on disk.

    Produced by :func:`echosmonitor.storage.sessions.discover_sessions`
    for the Archive tab's session browser (M3-A). ``session_root`` is
    the directory the per-device SDS trees hang from (a project dir for
    recorded sessions; the bare base root for the sessionless
    monitoring index) and ``db_path`` is that root's ``archive.db`` —
    together they are everything a reader needs to reach a CLOSED
    session's data without any live engine context (rule 14).

    Paths are strings, not ``Path``: the entry crosses Qt signal
    boundaries and feeds loader requests that snapshot plain strings.
    """

    record: SessionRecord
    session_root: str
    db_path: str


def three_component_groups_from_pairs(
    pairs: Iterable[tuple[str, str]],
) -> dict[str, dict[str, dict[str, str]]]:
    """Map ``device -> station_key -> {Z,N,E: nslc}`` for 3C-capable stations.

    A station is 3C-capable when it has a vertical (``Z``/``3``) plus two
    horizontals (``N``/``E`` or ``1``/``2``). Horizontals map to hvsrpy's
    ``ns``/``ew`` BY ORIENTATION CODE, never alphabetically: ``N`` or ``1``
    → N, ``E`` or ``2`` → E (SEED convention — ``1``/``2`` are the
    numeric-orientation equivalents of ``N``/``E``); ``Z``/``3`` is the
    vertical. The orientation char is the last character of the SEED
    channel code (``parts[3][2]``). Mapping by ``sorted()`` of the full
    NSLC string is WRONG: ``…HHE`` < ``…HHN`` would put East into N and
    swap the science inputs to hvsrpy (M6.6-A). Pure: consumed by the live
    HVSR/Archive widgets (over engine buffer keys) and by the
    archive-browser worker (over DB stream rows).
    """
    by_device: dict[str, dict[str, dict[str, str]]] = {}
    raw: dict[tuple[str, str], dict[str, str]] = {}
    for device, nslc in pairs:
        parts = nslc.split(".")
        if len(parts) != 4 or len(parts[3]) < 3:
            continue
        orient = parts[3][2]
        station_key = f"{parts[0]}.{parts[1]}.{parts[2]}.{parts[3][:2]}"
        raw.setdefault((device, station_key), {})[orient] = nslc
    for (device, station), orients in raw.items():
        vertical = orients.get("Z") or orients.get("3")
        north = orients.get("N") or orients.get("1")
        east = orients.get("E") or orients.get("2")
        if vertical is None or north is None or east is None:
            continue
        group = {"Z": vertical, "N": north, "E": east}
        by_device.setdefault(device, {})[station] = group
    return by_device


class AcquisitionState(IntEnum):
    """Per-device user acquisition state (CLAUDE.md rule 13).

    Distinct from :class:`ConnState`: ``ConnState`` describes what the
    SeedLink *socket* is doing; ``AcquisitionState`` describes what the
    *user asked for*. A device the user set to MONITORING may cycle
    through CONNECTING/WAITING_RETRY at the connection level while its
    acquisition state stays MONITORING throughout.

    - ``IDLE`` — no worker, no traffic, no disk writes. The launch state
      of every device; nothing leaves it without an explicit user action.
    - ``MONITORING`` — live SeedLink streaming into ring buffers for
      display/analysis; **zero archive writes**.
    - ``RECORDING`` — monitoring plus an SDS archive writer (rule 14).
    """

    IDLE = 0
    MONITORING = 1
    RECORDING = 2


@dataclass(slots=True)
class DeviceStatus:
    """Snapshot of one SeedLink device's connection state and counters.

    ``attempt_count`` resets to 0 on every successful CONNECTED transition
    so the diagnostics column reflects the *current* failure streak only.
    ``since_first_attempt_at`` is set when the streak begins and cleared
    on success — the difference between it and now is how long the user
    has been waiting for this device to come up.

    ``last_failure_detail`` carries optional structured context for the
    current ``last_failure_kind``. Schema is per-kind:

      * ``protocol_rejected`` →
        ``{"rejected_selectors": list[str], "rejection_count": int}``
      * everything else → ``None`` today; reserved for future kinds.
    """

    name: str
    state: ConnState = ConnState.DISCONNECTED
    last_event_at: UTCDateTime | None = None
    last_error: str | None = None
    packets_received: int = 0
    bytes_received: int = 0
    attempt_count: int = 0
    last_failure_kind: FailureKind | None = None
    next_attempt_at: UTCDateTime | None = None
    since_first_attempt_at: UTCDateTime | None = None
    last_failure_detail: dict[str, object] | None = None

    # M5 archive fields. Default to "off": they only become live while
    # the device is in the RECORDING state (M2 rule 13 — a writer is
    # attached by ``start_recording``, never by config).
    # ``archive_files_open`` is the count of distinct SDS paths the
    # writer has *touched* this session (not the LRU live-fd count).
    # ``archive_drops_total`` was removed in M6.5-A: the engine no
    # longer has a drop point on the archive seam (recorded packets are
    # posted straight to the storage thread; backpressure is the
    # advisory in-flight gauge on ``StreamingEngine.archiveBackpressure``).
    archive_enabled: bool = False
    archive_bytes_written: int = 0
    archive_files_open: int = 0
    archive_last_write_at: UTCDateTime | None = None
    archive_last_error: str | None = None

    # M5 stage B: gap / overlap counters fed from the gap detector.
    # ``archive_last_gap_at`` is the timestamp of the most recent
    # discontinuity detected on any of this device's streams.
    archive_gaps_total: int = 0
    archive_overlaps_total: int = 0
    archive_last_gap_at: UTCDateTime | None = None

    # M8 detection counters. ``detections_total`` counts NEW detection
    # rows this device produced this session (open onsets + closed-in-one-
    # packet triggers); a later close of an open row does not re-count.
    # ``last_detection_at`` is the ``t_on`` of the most recent one.
    detections_total: int = 0
    last_detection_at: UTCDateTime | None = None


@dataclass(frozen=True, slots=True)
class WorkerDiagnostics:
    """Wire payload from ``SeedLinkWorker`` to the engine's status sink.

    Frozen so a single instance can be safely passed across threads via a
    ``QueuedConnection`` signal without the worker thread mutating it
    after the GUI thread has snapshotted it.

    ``last_failure_detail``: per-kind structured context (see
    :class:`DeviceStatus`). ``None`` when the current failure kind has
    no extra context. The mapping is treated as immutable by all
    consumers; a new ``WorkerDiagnostics`` instance is built per emit so
    mutating an aliased dict would still be safe in practice, but
    mutation is contractually disallowed.
    """

    attempt_count: int
    last_failure_kind: FailureKind | None
    next_attempt_at: UTCDateTime | None
    since_first_attempt_at: UTCDateTime | None
    last_failure_detail: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class Trigger:
    """STA/LTA trigger event.

    `t_off` is `None` while the trigger is still open at the boundary of a
    DSP packet — the next packet that drops below `off_thr` finalises it.
    """

    nslc: str
    t_on: UTCDateTime
    t_off: UTCDateTime | None
    peak_ratio: float


@dataclass(slots=True)
class Detection:
    """A persisted, device-scoped detection event.

    Relationship to :class:`Trigger`. A ``Trigger`` is the transient,
    per-stream output of the STA/LTA DSP tap (:class:`dsp.stages.StaLta`):
    it carries only an NSLC and lives just long enough for the chain to
    read it. A ``Detection`` is what the streaming engine *records* when a
    trigger fires — it is scoped to the ``(device, nslc)`` pair (two
    devices publishing the same NSLC produce independent detections),
    annotated with a wall-clock ``detected_at``, tagged with the detector
    ``kind``, and assigned a DB row ``id`` once persisted.

    A single trigger maps to exactly one ``detections`` row:

    * A trigger that opens at a packet boundary first surfaces with
      ``t_off=None`` — recorded as an open row. When it later drops below
      ``off_thr`` the same row's ``t_off`` (and final ``score``) are
      updated in place; ``id`` ties the close to the open.
    * A trigger that opens and closes within one packet surfaces already
      finalised (``t_off`` set) and is recorded as a single closed row.

    ``kind`` is ``'sta_lta'``. ``score`` is the detector-agnostic
    magnitude — the peak STA/LTA ratio. ``meta`` holds JSON-friendly extras
    (thresholds, window lengths) and round-trips through the DAO's
    ``meta_json`` column.
    """

    device: str
    nslc: str
    kind: str
    t_on: UTCDateTime
    t_off: UTCDateTime | None
    score: float
    detected_at: UTCDateTime
    meta: dict[str, object]
    # Set once the row is persisted (``record_detection`` return value);
    # ``None`` for an in-flight detection not yet written. ``recent_detections``
    # always populates it.
    id: int | None = None
