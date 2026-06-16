"""Per-device MiniSEED writer for the SDS archive (M5 stage A).

A :class:`MseedWriter` is a ``QObject`` that owns the file handles and
encoder state for one configured device. The streaming engine creates
one writer per device in the RECORDING state (M2 rule 13 — recording
is a user action, not an ``archive.enabled`` side effect) and moves it
to a shared storage ``QThread``; thereafter, the writer receives traces via
the ``write_trace`` slot and flushes via a periodic ``QTimer`` whose
interval is :class:`ArchiveConfig.fsync_interval_s`.

Design invariants:

* **Atomic record-level writes (best-effort) plus tail validation.**
  Each trace is encoded into an in-memory buffer and handed to
  ``os.write`` in a single syscall, so on Linux the kernel typically
  writes ``N x record_length`` bytes as N records or zero. POSIX
  does not formally guarantee this for regular files, so the real
  crash-safety guarantee comes from :meth:`_validate_or_truncate`:
  on first session-touch of any path that already exists on disk,
  the file size is realigned to the last good ``record_length``
  boundary before append. A torn tail from a prior crash is
  truncated, with one INFO log line; the writer never appends on
  top of an unaligned tail.
* **Crash recovery on first session-touch.** Files are opened lazily.
  If the path already exists on the filesystem, the first touch in
  this session validates the file size against ``record_length``: any
  unaligned tail is truncated to the last good boundary before
  appending. The ``_validated_paths`` set makes this idempotent within
  a session.
* **DB-after-fsync ordering** (CLAUDE.md rule 8 + plan stage B). The
  writer never persists metadata; the engine listens to ``writeOk``
  for live counters and to ``flushedFile`` (emitted from
  :meth:`flush_all` after fsync) for the durability claim that the
  metadata index relies on. ``flushedFile`` carries BOTH the
  per-fsync ``bytes_added`` delta (for additive
  ``streams.total_bytes``) AND the post-fsync ``file_size`` from
  ``os.fstat(fd)`` (for the ``files.bytes`` UPSERT, whose
  replace-by-path semantics demand the cumulative durable size, not
  a delta — POSTMORTEMS 2026-05-10).
* **Terminal-signal invariant (M6.5-A).** Every ``write_trace`` call
  emits exactly ONE terminal signal: ``writeOk`` XOR ``writeFailed``.
  The engine's archive in-flight gauge (its replacement for the old
  engine-side drop-oldest inbox) counts these acks against packets
  sent; a silent return here would make the gauge read permanently
  high and cry wolf about backpressure. The writer simply processes
  whatever the queued signal layer hands it — the only drops it ever
  performs itself are the slow-IO/ENOSPC pause paths, and those are
  ``writeFailed``-acknowledged per trace.
"""

from __future__ import annotations

import errno
import os
import time
from collections import OrderedDict, defaultdict
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import structlog
from obspy import Stream, UTCDateTime
from obspy.core.trace import Trace as _Trace

# Direct binding of obspy's MSEED write entry point (M6.5-C). Going
# through ``Stream.write(format="MSEED")`` re-resolves the format
# plugin via importlib.metadata on EVERY call — ~3 ms and an
# email-header parse per packet, which at the Echos packet rate
# (108-sample records, ~14 packets/s/device at 500 Hz x 3 ch) was 54 %
# of the writer's CPU in the M6.5-C profile. ``_write_mseed`` IS the
# function that entry point resolves to; the round-trip tests in
# tests/storage/test_mseed_writer.py pin the binding, so an obspy
# upgrade that moves it fails the gate loudly instead of silently.
from obspy.io.mseed.core import _write_mseed
from PySide6.QtCore import QObject, QTimer, Signal, Slot

from echosmonitor.config.schema import ArchiveConfig
from echosmonitor.core.models import StreamID
from echosmonitor.storage.sds import device_sds_root, sds_path, split_at_midnight

if TYPE_CHECKING:
    from obspy.core.trace import Trace

# Wall-time threshold above which a single write or fsync is considered
# slow enough to log per CLAUDE.md rule 7 (wait observability).
_SLOW_IO_WARN_MS: float = 1000.0

# After this many consecutive slow writes to the same path, the writer
# pauses that path: sets ``archive_last_error="filesystem unresponsive"``
# and silently drops further writes for ``_PAUSE_DURATION_S`` seconds.
_SLOW_IO_THRESHOLD = 3
_PAUSE_DURATION_S: float = 30.0

# The scalar-type class obspy's ``SAMPLETYPE`` map (obspy.io.mseed.headers)
# keys on for 32-bit integer samples — built the same way, ``np.dtype(
# np.int32).type``. On most platforms this *is* ``np.int32``, but on Windows
# an int32-WIDTH array can carry a different scalar class (``np.intc``) that is
# ``==``-equal by dtype yet absent from obspy's map, so ``_write_mseed``
# raises ``KeyError(<class 'numpy.intc'>)``. We canonicalise to this exact
# class before handing data to obspy (see :meth:`MseedWriter._encode`).
_OBSPY_INT32_TYPE = np.dtype(np.int32).type

# Open flag forcing binary (untranslated) I/O. On Windows os.open defaults to
# TEXT mode, which mangles 0x0A bytes in the binary MiniSEED stream; this flag
# is Windows-only (0 elsewhere), so the OR is a no-op on POSIX.
_O_BINARY = getattr(os, "O_BINARY", 0)

_log = structlog.get_logger(__name__)


class MseedWriter(QObject):
    """One MiniSEED writer per archive-enabled device.

    Lives on a storage ``QThread``. All slots are invoked via queued
    signals from the engine thread. Public signals are emitted on the
    storage thread; receivers (engine, DAO) connect with the default
    auto-connection, which re-queues onto their own thread.
    """

    # ``writeOk(device, nslc, bytes_written, path, split, encoding_chosen)``
    # Emitted once per ``write_trace`` invocation. ``split`` is True when
    # the trace was split at UTC midnight. ``bytes_written`` is the sum
    # across all files written by this call. ``path`` is the LAST file
    # touched (the post-midnight one when split=True).
    writeOk = Signal(str, str, int, object, bool, str)  # noqa: N815

    # ``writeFailed(device, nslc, reason)`` — soft errors that the engine
    # should surface on ``DeviceStatus.archive_last_error``. The writer
    # never raises; it logs and emits.
    writeFailed = Signal(str, str, str)  # noqa: N815

    # ``flushedFile(device, nslc, path, t_start, t_end, bytes_added,
    # file_size)`` — emitted from :meth:`flush_all` AFTER ``os.fsync``
    # returns, per the DB-after-fsync invariant. ``bytes_added`` is bytes
    # written since the previous successful fsync (consumed by Stage B's
    # additive ``streams.total_bytes`` accumulator). ``file_size`` is the
    # current durable size of the file, sampled via ``os.fstat(fd)``
    # after the fsync — this is what Stage B's ``files.bytes`` UPSERT
    # records, so the index always reflects on-disk truth across writer
    # lifetimes and process restarts. Conflating the two would either
    # break ``streams.total_bytes`` accumulation (if file_size is used)
    # or leave ``files.bytes`` reporting only the last fsync window's
    # delta (if bytes_added is used) — see POSTMORTEMS 2026-05-10
    # "Cross-session durability index lied about disk truth".
    flushedFile = Signal(str, str, object, object, object, int, int)  # noqa: N815

    def __init__(
        self,
        device_name: str,
        root: Path,
        cfg: ArchiveConfig,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._device_name = device_name
        self._root = Path(root)
        self._cfg = cfg
        self._log = _log.bind(device=device_name, archive_root=str(root))

        # Open file handles, keyed by SDS path. OrderedDict gives LRU
        # behaviour: the most-recently-touched path is moved to the end.
        self._open_files: OrderedDict[Path, int] = OrderedDict()

        # Paths whose tail has been crash-validated this session. Per-
        # path, per-session: validation runs at most once even when the
        # writer reopens a file after LRU eviction.
        self._validated_paths: set[Path] = set()

        # NSLCs for which we've already emitted an "encoding_downgraded"
        # INFO log. Used to gate the log to once per stream.
        self._encoding_logged: set[str] = set()

        # Per-path state for the flushedFile signal. Each entry tracks
        # the smallest starttime and largest endtime of samples written
        # since the last successful fsync, plus accumulated bytes.
        self._pending: dict[Path, _PendingFlush] = {}

        # Per-path slow-write counters and pause-until timestamps.
        self._slow_writes: dict[Path, int] = defaultdict(int)
        self._paused_until: dict[Path, float] = {}

        # The fsync timer is owned by the writer but constructed lazily
        # on ``start`` so its parent thread is the storage thread.
        self._fsync_timer: QTimer | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle slots
    # ------------------------------------------------------------------

    @Slot()
    def start(self) -> None:
        """Begin periodic fsync. Wire to ``QThread.started``."""
        if self._fsync_timer is not None:
            return  # idempotent — engine restart paths may re-emit
        timer = QTimer()
        timer.setInterval(int(self._cfg.fsync_interval_s * 1000))
        timer.timeout.connect(self.flush_all)
        timer.start()
        self._fsync_timer = timer
        self._log.info(
            "mseed_writer_started",
            fsync_interval_s=self._cfg.fsync_interval_s,
            max_open_files=self._cfg.max_open_files,
            encoding=self._cfg.encoding,
            record_length=self._cfg.record_length,
        )

    # ------------------------------------------------------------------
    # Hot path: write_trace
    # ------------------------------------------------------------------

    @Slot(str, object)
    def write_trace(self, nslc: str, trace: object) -> None:
        # Terminal-signal invariant (module docstring): every call past
        # the ``_closed`` guard emits exactly one ``writeOk`` XOR one
        # ``writeFailed`` — ``_write_one`` failures emit it and abort
        # the loop, so a midnight-split pair can never double-emit.
        # Post-close calls are unreachable in practice: the engine
        # discards the device's gauge before the blocking ``close_all``,
        # and queued ``write_trace`` events dispatch FIFO before it.
        if self._closed:
            return
        if not isinstance(trace, _Trace):  # defensive: Qt types as object
            self.writeFailed.emit(self._device_name, nslc, "non-trace payload")
            return

        try:
            sid = StreamID.from_trace_id(trace.id)
        except ValueError as exc:
            self.writeFailed.emit(self._device_name, nslc, f"bad trace id: {exc}")
            return

        try:
            traces = split_at_midnight(trace)
        except Exception as exc:  # extremely defensive
            self.writeFailed.emit(self._device_name, nslc, f"split failed: {exc}")
            return

        split = len(traces) > 1
        total_bytes = 0
        last_path: Path | None = None
        chosen_encoding: str = self._cfg.encoding
        for sub in traces:
            outcome = self._write_one(sid, nslc, sub)
            if outcome is None:
                # Failure already emitted; abort the rest of this trace
                # to avoid producing partial state for the post-midnight
                # half when the pre-midnight half failed.
                return
            n_bytes, path, encoding_chosen = outcome
            total_bytes += n_bytes
            last_path = path
            chosen_encoding = encoding_chosen

        if last_path is not None:
            self.writeOk.emit(
                self._device_name,
                nslc,
                total_bytes,
                last_path,
                split,
                chosen_encoding,
            )

    # ------------------------------------------------------------------
    # Hot path: flush_all (timer-driven)
    # ------------------------------------------------------------------

    @Slot()
    def flush_all(self) -> None:
        if self._closed or not self._open_files:
            return
        # Snapshot to a list so eviction during fsync (very unlikely on
        # the timer cadence but safe to assume) can't perturb iteration.
        for path, fd in list(self._open_files.items()):
            self._fsync_one(path, fd)

    def _fsync_one(self, path: Path, fd: int) -> None:
        t0 = time.monotonic()
        try:
            os.fsync(fd)
        except OSError as exc:
            self._log.error("mseed_writer_fsync_failed", path=str(path), error=str(exc))
            return
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if elapsed_ms > _SLOW_IO_WARN_MS:
            self._log.warning(
                "mseed_writer_fsync_slow",
                path=str(path),
                elapsed_ms=round(elapsed_ms, 1),
            )
        # Sample the durable file size BEFORE popping the pending tally,
        # so a transient fstat error doesn't strand the pending state.
        try:
            file_size = os.fstat(fd).st_size
        except OSError as exc:
            self._log.error("mseed_writer_fstat_failed", path=str(path), error=str(exc))
            return
        pending = self._pending.pop(path, None)
        if pending is not None and pending.bytes_added > 0:
            self._log.debug(
                "mseed_writer_flushed_file",
                path=str(path),
                bytes_added=pending.bytes_added,
                file_size=file_size,
            )
            self.flushedFile.emit(
                self._device_name,
                pending.nslc,
                path,
                pending.t_start,
                pending.t_end,
                pending.bytes_added,
                file_size,
            )

    # ------------------------------------------------------------------
    # Lifecycle slots — stop / close
    # ------------------------------------------------------------------

    @Slot()
    def close_all(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._fsync_timer is not None:
            self._fsync_timer.stop()
            self._fsync_timer = None
        # Final fsync + close all open files; emit any pending
        # flushedFile signals so the DAO sees the durability claims.
        for path, fd in list(self._open_files.items()):
            self._fsync_one(path, fd)
            try:
                os.close(fd)
            except OSError as exc:
                self._log.warning(
                    "mseed_writer_close_failed",
                    path=str(path),
                    error=str(exc),
                )
        self._open_files.clear()
        self._log.info("mseed_writer_closed")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write_one(self, sid: StreamID, nslc: str, trace: Trace) -> tuple[int, Path, str] | None:
        """Encode one (already day-aligned) trace and append it to disk.

        Returns ``(bytes_written, path, encoding_chosen)`` on success,
        or ``None`` if a soft failure occurred (the writer has emitted
        ``writeFailed``).
        """
        path = sds_path(
            device_sds_root(self._root, self._device_name),
            trace.stats.starttime,
            sid,
        )
        now = time.monotonic()
        if path in self._paused_until and self._paused_until[path] > now:
            # Path is paused after recent slow writes / ENOSPC. The drop
            # is NOT silent (terminal-signal invariant): the engine's
            # in-flight gauge needs exactly one ack per trace, and a
            # paused path dropping recorded data is worth surfacing on
            # ``archive_last_error`` anyway. No log here — the pause
            # itself was already logged once.
            self.writeFailed.emit(self._device_name, nslc, "path paused; trace dropped")
            return None

        try:
            buf, chosen_encoding = self._encode(nslc, trace)
        except _EncodingError as exc:
            reason = f"encoding error: {exc}"
            self._log.error(
                "mseed_writer_encode_failed",
                nslc=nslc,
                error=reason,
            )
            self.writeFailed.emit(self._device_name, nslc, reason)
            return None

        try:
            fd = self._open_or_get_fd(path)
        except OSError as exc:
            reason = f"open failed: {exc}"
            self._log.error("mseed_writer_open_failed", path=str(path), error=reason)
            self.writeFailed.emit(self._device_name, nslc, reason)
            return None

        encoded = buf.getvalue()
        t0 = time.monotonic()
        try:
            n = os.write(fd, encoded)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                # Pause this path for the rest of the session — retry
                # would just hammer a full disk.
                self._paused_until[path] = now + 365 * 24 * 3600.0
                self._log.error("mseed_writer_disk_full", path=str(path))
                self.writeFailed.emit(self._device_name, nslc, "disk full (ENOSPC)")
            else:
                self._log.error(
                    "mseed_writer_write_failed",
                    path=str(path),
                    error=str(exc),
                )
                self.writeFailed.emit(self._device_name, nslc, f"write failed: {exc}")
            return None

        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if n != len(encoded):
            # ``os.write`` is allowed to return short writes for pipes
            # and sockets but is effectively atomic for regular files
            # on Linux below sized boundaries. A short write here means
            # something is very wrong (e.g. ENOSPC partway through).
            self.writeFailed.emit(self._device_name, nslc, f"short write {n}/{len(encoded)}")
            return None

        self._note_write_timing(path, elapsed_ms)
        self._note_pending(path, nslc, trace, n)
        return n, path, chosen_encoding

    def _encode(self, nslc: str, trace: Trace) -> tuple[BytesIO, str]:
        """Return ``(buffer, chosen_encoding)`` ready for ``os.write``.

        Raises :class:`_EncodingError` for unrecoverable type mismatches.
        """
        configured = self._cfg.encoding
        # ``Stream.write`` used to reject masked arrays before plugin
        # dispatch; the direct ``_write_mseed`` binding skips that
        # check and would silently write fill garbage. Unreachable on
        # the archive path (the writer never merges), but keep the
        # defensive posture explicit.
        if isinstance(trace.data, np.ma.MaskedArray):
            raise _EncodingError("masked array cannot be archived")
        kind = trace.data.dtype.kind  # 'i', 'u', 'f', etc.

        chosen: str
        data = trace.data
        if configured in ("STEIM2", "STEIM1"):
            if kind in ("i", "u"):
                # STEIM requires int32. Cast carefully: int64 may
                # overflow; uint may exceed int32 range.
                if kind == "i" and data.dtype.itemsize == 4:
                    # Already int32-width and in range by construction. The
                    # only thing that can be wrong is the platform scalar-type
                    # class (Windows ``intc`` vs the ``int32`` obspy keys on):
                    # canonicalise with a zero-copy ``.view`` *only* when it
                    # actually differs, so the M6.5-C no-copy hot path
                    # (``data is trace.data``) survives where it already
                    # matches (Linux/macOS).
                    if data.dtype.type is not _OBSPY_INT32_TYPE:
                        data = data.view(np.int32)
                else:
                    # Real width/kind change (int8/16, any uint, int64): a
                    # range-checked copy. ``astype`` produces the canonical
                    # ``np.dtype(np.int32)``, so its ``.type`` matches obspy.
                    info = np.iinfo(np.int32)
                    if data.min() < info.min or data.max() > info.max:
                        raise _EncodingError(
                            f"int{data.dtype.itemsize * 8} sample value "
                            f"out of int32 range; cannot encode as {configured}"
                        )
                    data = data.astype(np.int32)
                chosen = configured
            elif kind == "f":
                # Float data cannot be STEIM-encoded; fall back to
                # FLOAT32. Log once per stream so a chatty dashboard
                # doesn't flood the journal.
                if nslc not in self._encoding_logged:
                    self._log.info(
                        "mseed_writer_encoding_downgraded",
                        nslc=nslc,
                        from_=configured,
                        to="FLOAT32",
                        reason="float dtype",
                    )
                    self._encoding_logged.add(nslc)
                chosen = "FLOAT32"
                if data.dtype != np.float32:
                    data = data.astype(np.float32, copy=False)
            else:
                raise _EncodingError(f"unsupported dtype kind {kind!r} for {configured} encoding")
        elif configured == "FLOAT32":
            if data.dtype != np.float32:
                data = data.astype(np.float32, copy=False)
            chosen = "FLOAT32"
        else:  # pragma: no cover — schema constrains the literal
            raise _EncodingError(f"unhandled configured encoding {configured!r}")

        # Build a one-trace Stream with the (possibly cast) data and
        # encode to a BytesIO. ObsPy's MiniSEED writer respects
        # ``encoding`` and ``reclen`` exactly. Hot path (device already
        # emits the configured dtype — the Echos int32/STEIM2 case):
        # write the original trace directly; ``_write_mseed`` never
        # mutates its input (pinned by the round-trip tests) and the
        # writer is the trace's last consumer, so the per-packet
        # ``trace.copy()`` deepcopy was pure overhead (M6.5-C profile).
        if data is trace.data:
            out_trace = trace
        else:
            out_trace = trace.copy()
            out_trace.data = data
        buf = BytesIO()
        _write_mseed(
            Stream([out_trace]),
            buf,
            encoding=chosen,
            reclen=self._cfg.record_length,
            byteorder=">",
            flush=True,
        )
        return buf, chosen

    def _open_or_get_fd(self, path: Path) -> int:
        # Fast path: already open. Move-to-end keeps LRU semantics.
        if path in self._open_files:
            self._open_files.move_to_end(path)
            return self._open_files[path]

        # Eviction first if at cap, so we never exceed it transiently.
        while len(self._open_files) >= self._cfg.max_open_files:
            evicted_path, evicted_fd = self._open_files.popitem(last=False)
            self._fsync_one(evicted_path, evicted_fd)
            try:
                os.close(evicted_fd)
            except OSError as exc:
                self._log.warning(
                    "mseed_writer_evict_close_failed",
                    path=str(evicted_path),
                    error=str(exc),
                )

        # Lazy mkdir + crash recovery on first session-touch.
        path.parent.mkdir(parents=True, exist_ok=True)
        if path not in self._validated_paths:
            self._validate_or_truncate(path)
            self._validated_paths.add(path)

        # ``_O_BINARY`` is mandatory on Windows: without it os.open uses TEXT
        # mode and translates every 0x0A byte in the binary MiniSEED to
        # 0x0D 0x0A on write, corrupting record alignment (obspy then reads
        # "Not a SEED record"). It is 0 on POSIX, so this is a no-op there.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_BINARY, 0o644)
        self._open_files[path] = fd
        return fd

    def _validate_or_truncate(self, path: Path) -> None:
        """Truncate ``path`` to the last well-aligned record_length boundary.

        We do not parse record headers; an unaligned tail is the only
        common torn-write failure mode under POSIX append semantics
        with ``record_length``-sized writes. A future, deeper
        implementation can use ``obspy.io.mseed.util.get_record_information``
        to validate the last record's checksum, but the cost-benefit
        for live archives is poor.
        """
        if not path.exists():
            return
        size = path.stat().st_size
        if size == 0:
            return
        reclen = self._cfg.record_length
        last_good = (size // reclen) * reclen
        if last_good < size:
            try:
                with path.open("rb+") as f:
                    f.truncate(last_good)
            except OSError as exc:
                self._log.error(
                    "mseed_writer_truncate_failed",
                    path=str(path),
                    error=str(exc),
                    size=size,
                )
                return
            self._log.info(
                "mseed_writer_truncated_to_valid_record",
                path=str(path),
                kept_bytes=last_good,
                lost_bytes=size - last_good,
            )

    def _note_write_timing(self, path: Path, elapsed_ms: float) -> None:
        if elapsed_ms <= _SLOW_IO_WARN_MS:
            self._slow_writes[path] = 0
            return
        self._log.warning(
            "mseed_writer_slow_write",
            path=str(path),
            elapsed_ms=round(elapsed_ms, 1),
        )
        n = self._slow_writes[path] + 1
        self._slow_writes[path] = n
        if n >= _SLOW_IO_THRESHOLD:
            now = time.monotonic()
            self._paused_until[path] = now + _PAUSE_DURATION_S
            self._slow_writes[path] = 0
            # No writeFailed here (terminal-signal invariant): this runs
            # on the SUCCESS path of the write that tripped the pause,
            # and write_trace will emit writeOk for it — a second
            # terminal would inject a spurious ack into the engine's
            # in-flight gauge, under-reporting backpressure exactly when
            # the filesystem is struggling. The very next write to the
            # paused path emits the per-trace "path paused" writeFailed,
            # which carries the news to ``archive_last_error``.
            self._log.error(
                "mseed_writer_path_paused",
                path=str(path),
                pause_seconds=_PAUSE_DURATION_S,
                reason="filesystem unresponsive",
            )

    def _note_pending(self, path: Path, nslc: str, trace: Trace, bytes_added: int) -> None:
        existing = self._pending.get(path)
        if existing is None:
            self._pending[path] = _PendingFlush(
                nslc=nslc,
                t_start=trace.stats.starttime,
                t_end=trace.stats.endtime,
                bytes_added=bytes_added,
            )
            return
        if trace.stats.starttime < existing.t_start:
            existing.t_start = trace.stats.starttime
        if trace.stats.endtime > existing.t_end:
            existing.t_end = trace.stats.endtime
        existing.bytes_added += bytes_added


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EncodingError(Exception):
    """Internal: encoding-rule violation that the slot caller catches."""


class _PendingFlush:
    """Per-path durability accumulator. Reset after each successful fsync."""

    __slots__ = ("bytes_added", "nslc", "t_end", "t_start")

    def __init__(
        self,
        nslc: str,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        bytes_added: int,
    ) -> None:
        self.nslc = nslc
        self.t_start = t_start
        self.t_end = t_end
        self.bytes_added = bytes_added
