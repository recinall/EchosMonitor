"""Storage-thread writer for persist-on-detection events (M10 Stage D).

An :class:`EventPersister` is the storage half of the persist-on-detection
bridge. It LIVES ON THE STORAGE ``QThread`` (the engine's
``_archive_thread`` — the same thread :class:`~seedlink_dashboard.storage.
mseed_writer.MseedWriter` runs on); its :meth:`persist` slot is invoked via
``QueuedConnection`` so the file write never runs on the GUI / data-path
thread (CLAUDE.md rules 1, 8, 11).

The agent is storage-ignorant. The ONLY bridge is the engagement policy in
:class:`~seedlink_dashboard.core.ai_engine.AIEngine`: when an annotation's
:class:`~seedlink_dashboard.core.models.Detection` clears ``min_score`` the
policy builds an :class:`EventPersistRequest` and emits ``persistRequested``
(connected here). The agent only ever returned an ``AIAnnotation`` — it has
zero knowledge of files, the DAO, or this class.

Two waveform sources for the dedicated-window write (documented per the
plan):

* **Captured samples** (live detections): the policy captured the window
  from the ring on the GUI thread and passed them as
  ``(samples, fs, samples_t_start)`` so the persister never reaches back
  into the engine (rule 11).
* **SDS read** (archive detections, or a live capture that fell short):
  ``samples is None`` (or a fallback flag is set) → the persister reads the
  window from the SDS archive via :class:`~seedlink_dashboard.storage.
  archive_reader.ArchiveReader`.

Crash safety + atomicity (rule 8): the MiniSEED file is encoded into a temp
file in the SAME directory, fsynced, then ``os.replace``-d into place — the
M5 discipline. A failure never crashes the thread: it logs a warning and
emits ``persistFailed``. Rule 7 observability: start / done / elapsed and
the resulting file path + row id are logged.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import structlog
from obspy import Stream, Trace, UTCDateTime
from PySide6.QtCore import QObject, Signal, Slot

from seedlink_dashboard.core.models import StreamID
from seedlink_dashboard.storage.archive_reader import ArchiveReader

if TYPE_CHECKING:
    from seedlink_dashboard.storage.dao import ArchiveDao

_log = structlog.get_logger(__name__)

# Encoding for the dedicated-window file. STEIM2 for integer counts,
# FLOAT32 for float-dtype captures (the M5 / SKILL rule: STEIM only for
# integer data). Record length 512 — the standard, matching the SDS writer.
_RECORD_LENGTH = 512


@dataclass(frozen=True, slots=True)
class EventPersistRequest:
    """Self-describing persist request (rule 11: the persister never reaches
    back into the engine).

    ``mode`` is one of ``'dedicated_window'`` / ``'tag_in_sds'`` / ``'both'``
    (the config value). ``t_start`` / ``t_end`` are the FINAL resolved window
    bounds ``[t_on - pre, (t_off or t_on) + post]`` computed by the policy.

    Waveform source for the dedicated-window write:

    * ``samples`` set (with ``fs`` and ``samples_t_start``) → use the
      captured ring samples (live path).
    * ``samples is None`` → read the window from the SDS archive (archive
      path, or a live capture the policy could not satisfy from the ring).
    """

    device: str
    nslc: str
    detection_id: int | None
    mode: str
    t_start: UTCDateTime
    t_end: UTCDateTime
    score: float
    pre_seconds: float
    post_seconds: float
    # Captured waveform (live); None → read from SDS.
    samples: np.ndarray | None = None
    fs: float = 0.0
    samples_t_start: UTCDateTime | None = None
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EventPersistResult:
    """Success payload emitted on ``persisted`` for the UI / log."""

    detection_id: int | None
    device: str
    nslc: str
    mode: str
    file_path: str | None
    event_id: int


class EventPersister(QObject):
    """Writes persist-on-detection events on the storage thread.

    Owns the engine's :class:`ArchiveDao` (the DAO holds one connection per
    accessing thread via ``threading.local``, so calling it from THIS thread
    lazily creates a storage-thread connection — the same contract
    :class:`MseedWriter`'s sibling DAO use relies on). The events root is the
    archive root; event files land under ``<root>/events/``, OUTSIDE the SDS
    ``YEAR/NET/...`` waveform tree (curated collection, not raw archive).
    """

    # ``persisted(EventPersistResult)`` — emitted after the row is durable.
    persisted = Signal(object)
    # ``persistFailed(detection_id_or_-1, device, nslc, reason)``.
    persistFailed = Signal(object, str, str, str)  # noqa: N815

    def __init__(
        self,
        root: Path,
        dao: ArchiveDao,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._root = Path(root)
        self._dao = dao
        self._reader = ArchiveReader(self._root, dao)
        self._closed = False
        self._log = _log.bind(events_root=str(self._root / "events"))

    @Slot()
    def close(self) -> None:
        """Stop accepting requests. Idempotent. Wire to teardown."""
        self._closed = True

    @Slot(object)
    def persist(self, request: object) -> None:
        """Persist one event request. Never raises across the thread boundary."""
        if self._closed or not isinstance(request, EventPersistRequest):
            return
        t0 = time.monotonic()
        self._log.info(
            "event_persist_start",
            device=request.device,
            nslc=request.nslc,
            mode=request.mode,
            detection_id=request.detection_id,
            t_start=str(request.t_start),
            t_end=str(request.t_end),
        )
        try:
            self._persist(request)
        except Exception as exc:  # defensive — a bad request must not crash us
            self._log.warning(
                "event_persist_failed",
                device=request.device,
                nslc=request.nslc,
                mode=request.mode,
                error=str(exc),
            )
            self.persistFailed.emit(
                request.detection_id if request.detection_id is not None else -1,
                request.device,
                request.nslc,
                str(exc),
            )
            return
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        self._log.debug(
            "event_persist_done",
            device=request.device,
            nslc=request.nslc,
            mode=request.mode,
            elapsed_ms=round(elapsed_ms, 1),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _persist(self, request: EventPersistRequest) -> None:
        stream_id = self._dao.find_stream_id(request.device, request.nslc)
        if stream_id is None:
            # No stream row yet (the agent engaged but the metadata index
            # never saw this NSLC). Be honest rather than silently dropping:
            # surface it as a failure the UI/log can see (rule 7).
            raise RuntimeError(f"no stream row for {request.device}/{request.nslc}")

        do_window = request.mode in ("dedicated_window", "both")
        do_tag = request.mode in ("tag_in_sds", "both")

        if do_window:
            self._persist_dedicated_window(request, stream_id)
        if do_tag:
            self._persist_tag(request, stream_id)

    def _persist_dedicated_window(self, request: EventPersistRequest, stream_id: int) -> None:
        sid = StreamID.from_trace_id(request.nslc)
        trace = self._build_trace(request, sid)
        if trace is None or trace.stats.npts == 0:
            raise RuntimeError("no waveform available for dedicated-window event")
        # Trim to the resolved window (captured samples may extend past it;
        # the SDS read already trims, trimming again is a no-op there).
        trace = trace.copy()
        trace.trim(request.t_start, request.t_end)
        if trace.stats.npts == 0:
            raise RuntimeError("waveform does not overlap the requested window")

        path = self._event_path(sid, request)
        self._atomic_write(trace, path)

        # Rule 9: t_start / t_end / file_path come from the ACTUAL written
        # trace + the file just renamed into place, not the request deltas.
        event_id = self._dao.record_event(
            detection_id=request.detection_id,
            stream_id=stream_id,
            mode="dedicated_window",
            t_start=trace.stats.starttime,
            t_end=trace.stats.endtime,
            score=request.score,
            file_path=str(path),
            meta=dict(request.meta),
        )
        self._log.info(
            "event_persisted_dedicated_window",
            device=request.device,
            nslc=request.nslc,
            file_path=str(path),
            npts=int(trace.stats.npts),
            event_id=event_id,
        )
        self.persisted.emit(
            EventPersistResult(
                detection_id=request.detection_id,
                device=request.device,
                nslc=request.nslc,
                mode="dedicated_window",
                file_path=str(path),
                event_id=event_id,
            )
        )

    def _persist_tag(self, request: EventPersistRequest, stream_id: int) -> None:
        # No file write, no data duplication — just mark the region in the
        # already-archived SDS with an index row (file_path NULL).
        event_id = self._dao.record_event(
            detection_id=request.detection_id,
            stream_id=stream_id,
            mode="tag_in_sds",
            t_start=request.t_start,
            t_end=request.t_end,
            score=request.score,
            file_path=None,
            meta=dict(request.meta),
        )
        self._log.info(
            "event_persisted_tag_in_sds",
            device=request.device,
            nslc=request.nslc,
            event_id=event_id,
        )
        self.persisted.emit(
            EventPersistResult(
                detection_id=request.detection_id,
                device=request.device,
                nslc=request.nslc,
                mode="tag_in_sds",
                file_path=None,
                event_id=event_id,
            )
        )

    def _build_trace(self, request: EventPersistRequest, sid: StreamID) -> Trace | None:
        """Resolve the waveform: captured ring samples, else SDS read."""
        if request.samples is not None and request.fs > 0 and request.samples_t_start is not None:
            data = np.ascontiguousarray(request.samples)
            tr = Trace(data=data)
            tr.stats.network = sid.network
            tr.stats.station = sid.station
            tr.stats.location = sid.location
            tr.stats.channel = sid.channel
            tr.stats.sampling_rate = float(request.fs)
            tr.stats.starttime = request.samples_t_start
            return tr
        # SDS fallback (archive detections or a short live capture).
        st = self._reader.read_window(
            sid, request.t_start, request.t_end, device_name=request.device
        )
        if len(st) == 0:
            return None
        st.merge(method=1)
        tr = st[0]
        data = tr.data
        if np.ma.isMaskedArray(data) and np.ma.is_masked(data):
            # The curated window straddles an SDS gap. Be honest (rule 7 — no
            # silent fabrication): log how many samples are masked rather than
            # passing gap-fill values off as real data. We still keep the
            # underlying samples so the event is written (a partial curated
            # window beats none), but the gap is recorded in the log.
            n_masked = int(np.count_nonzero(np.ma.getmaskarray(data)))
            self._log.warning(
                "event_persist_window_has_gap",
                nslc=request.nslc,
                masked_samples=n_masked,
                total_samples=int(data.size),
            )
            tr.data = np.ma.getdata(data)
        elif np.ma.isMaskedArray(data):
            tr.data = np.ma.getdata(data)
        return tr

    def _event_path(self, sid: StreamID, request: EventPersistRequest) -> Path:
        """Deterministic ``events/<NET>.<STA>.<LOC>.<CHA>__<iso>__det<id>.mseed``."""
        events_dir = self._root / "events"
        t_on = request.t_start + request.pre_seconds  # reconstruct the onset
        slug = self._slug_time(t_on)
        det = request.detection_id if request.detection_id is not None else "na"
        name = f"{sid.network}.{sid.station}.{sid.location}.{sid.channel}__{slug}__det{det}.mseed"
        return events_dir / name

    @staticmethod
    def _slug_time(t: UTCDateTime) -> str:
        """Filesystem-safe ISO slug, e.g. ``2026-06-02T031459.250000Z``."""
        iso = str(t)  # 2026-06-02T03:14:59.250000Z
        return iso.replace(":", "")

    def _atomic_write(self, trace: Trace, path: Path) -> None:
        """Encode ``trace`` to MiniSEED and atomically place it at ``path``.

        Temp file in the SAME dir → write → fsync → ``os.replace`` (the M5
        atomic discipline). STEIM2 for integer counts, FLOAT32 for floats.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        encoding = "STEIM2" if trace.data.dtype.kind in ("i", "u") else "FLOAT32"
        out = trace.copy()
        if encoding == "STEIM2" and out.data.dtype != np.int32:
            out.data = out.data.astype(np.int32, copy=False)
        elif encoding == "FLOAT32" and out.data.dtype != np.float32:
            out.data = out.data.astype(np.float32, copy=False)
        buf = BytesIO()
        Stream([out]).write(
            buf,
            format="MSEED",
            encoding=encoding,
            reclen=_RECORD_LENGTH,
            byteorder=">",
            flush=True,
        )
        encoded = buf.getvalue()
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        # fsync the directory so the rename is durable after a crash.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:  # pragma: no cover - some filesystems reject dir fsync
            pass
        finally:
            os.close(dir_fd)
