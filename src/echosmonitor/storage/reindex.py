"""Rebuild a session root's ``archive.db`` from its SDS tree (M3-D).

Use case: an archive copied from another machine — the DB is missing
(nothing browsable) or stale (rows describe the old machine's paths and
old file states). The MiniSEED files are the source of truth (rule 8);
this module re-derives the index from them:

* the tree is walked under ``<session_root>/<device>/`` and each file
  is matched against the SDS grammar via :func:`storage.sds.parse_sds_path`
  (the per-device segment sits ABOVE the parsed five components and is
  read from the path itself — skill ``miniseed-sds``);
* spans come from obspy ``headonly`` reads, bytes from ``stat`` (rule 9
  — on-disk values at the call site, never carried accumulators);
* ``files`` rows are upserted IN PLACE (``files.path`` UNIQUE) with
  ``t_start`` overwritten (disk truth — see ``ArchiveDao.replace_file``);
  every row NOT upserted by this run is pruned — the candidate set of a
  completed run is exhaustive, so anything else is a foreign-machine
  path, a file outside this session root (a same-machine duplicate's
  original tree — mere existence on disk must not keep it), or a file
  this run refused as dirty; per-stream byte totals are recomputed from
  the corrected rows;
* dirty/foreign files (non-SDS names, unreadable MiniSEED, header NSLC
  disagreeing with the path) are skipped PER FILE and counted — never
  fatal;
* sessions cannot be reconstructed from the tree. If the DB holds no
  real session rows, ONE synthesized row is upserted (``reindexed=1``,
  schema v5): span = the indexed data extent, project name = the
  directory name (the raw name's only durable home was the lost DB),
  membership = the device dirs found. Real session rows are never
  touched or shadowed.

This is a WRITE path (unlike the M3-A browser): the DB is opened
through the normal ``ArchiveDao`` (creating/migrating as needed). It
must NEVER run against the ACTIVE session's DB — the engine holds that
open and is writing; the GUI guard lives in the main window
(``engine.archive_db_path()``), and the app-lifetime ``QLockFile`` on
the base root keeps OTHER EchosMonitor instances out.

No Qt in this module; cancellation and progress are plain callables
(rule 2 — the worker in ``core/archive_reindex_worker.py`` adapts them
to signals). The ``should_stop`` callable is polled per file (rule 7).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from echosmonitor.storage.sds import parse_sds_path, sanitize_device_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from obspy import UTCDateTime

    from echosmonitor.core.models import StreamID

_log = structlog.get_logger(__name__)

_DB_FILENAME = "archive.db"


@dataclass(frozen=True, slots=True)
class ReindexProgress:
    """One progress beat: ``files_done`` of ``files_total`` candidates."""

    files_done: int
    files_total: int
    files_skipped: int


@dataclass(frozen=True, slots=True)
class ReindexReport:
    """What a re-index actually did (counts are from the run itself)."""

    session_root: str
    devices: int
    streams: int
    files_indexed: int
    files_skipped: int
    files_pruned: int
    synthesized_session: bool
    cancelled: bool
    elapsed_s: float


@dataclass(frozen=True, slots=True)
class _Candidate:
    device_dir: str
    path: Path
    sid: StreamID


def _scan_candidates(
    session_root: Path,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[_Candidate], int]:
    """Walk the device dirs; return (SDS-grammar matches, foreign-file count).

    Only files under an immediate child directory of the session root
    are considered (the per-device segment of the rule-14 layout);
    ``archive.db`` and anything else sitting at the root itself are not
    part of any device tree and are ignored entirely. ``should_stop``
    is polled per entry (rule 7 — a big tree on slow media must not
    make the scan an uncancellable pre-phase; the caller treats a
    stopped scan as a cancelled run).
    """
    candidates: list[_Candidate] = []
    foreign = 0
    for device_dir in sorted(p for p in session_root.iterdir() if p.is_dir()):
        for path in sorted(device_dir.rglob("*")):
            if should_stop is not None and should_stop():
                return candidates, foreign
            if not path.is_file():
                continue
            parsed = parse_sds_path(path)
            if parsed is None:
                foreign += 1
                _log.debug("reindex_foreign_file_skipped", path=str(path))
                continue
            sid, _year, _doy = parsed
            candidates.append(_Candidate(device_dir=device_dir.name, path=path, sid=sid))
    return candidates, foreign


def _read_file_truth(
    candidate: _Candidate,
) -> tuple[UTCDateTime, UTCDateTime, float, int] | None:
    """(t_start, t_end, fs, bytes) from the file itself, or None if dirty.

    ``headonly`` keeps this a record-header walk (no sample decode). A
    file whose header NSLC disagrees with its SDS path is skipped: the
    index must describe what a reader will actually get from that path,
    and a mis-laid file satisfies neither the path nor the grammar
    honestly.
    """
    import obspy

    try:
        stream = obspy.read(str(candidate.path), format="MSEED", headonly=True)
    except Exception as exc:
        _log.debug(
            "reindex_unreadable_file_skipped",
            path=str(candidate.path),
            error=str(exc),
        )
        return None
    if not stream:
        return None
    sid = candidate.sid
    for trace in stream:
        s = trace.stats
        if (s.network, s.station, s.location, s.channel) != (
            sid.network,
            sid.station,
            sid.location,
            sid.channel,
        ):
            _log.debug(
                "reindex_nslc_mismatch_skipped",
                path=str(candidate.path),
                header=f"{s.network}.{s.station}.{s.location}.{s.channel}",
            )
            return None
    t_start = min(tr.stats.starttime for tr in stream)
    t_end = max(tr.stats.endtime for tr in stream)
    fs = float(stream[0].stats.sampling_rate)
    return t_start, t_end, fs, candidate.path.stat().st_size


def reindex_session_root(
    session_root: Path,
    *,
    host: str = "",
    version: str = "",
    progress: Callable[[ReindexProgress], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> ReindexReport:
    """Rebuild ``session_root/archive.db`` from the SDS tree below it.

    Idempotent: re-running converges to the same rows. Cancellation
    (``should_stop``) returns a partial report with ``cancelled=True``;
    the prune/totals/session passes are skipped so a re-run completes
    the job (a partially corrected index is still an index — the files
    win over it either way, rule 8).
    """
    from echosmonitor.storage.dao import ArchiveDao

    t0 = time.monotonic()
    candidates, skipped = _scan_candidates(session_root, should_stop)
    total = len(candidates)
    _log.info(
        "reindex_started",
        root=str(session_root),
        candidates=total,
        foreign_skipped=skipped,
    )
    indexed = 0
    # A stop during the scan returns a truncated candidate list — treat
    # it as a cancelled run (partial extent must not feed the synthesis).
    cancelled = bool(should_stop is not None and should_stop())
    device_ids: dict[str, int] = {}
    stream_ids: dict[tuple[int, tuple[str, str, str, str]], int] = {}
    devices_seen: list[str] = []
    span_min: str | None = None
    span_max: str | None = None
    indexed_paths: set[str] = set()
    dao = ArchiveDao(session_root / _DB_FILENAME)
    try:
        # Map device DIRS onto existing device rows by sanitized name: the
        # row keeps the RAW name (its only durable home, same reasoning as
        # project names) and a dir must never spawn a duplicate device row
        # for the device it already belongs to. A foreign DB can hold two
        # raw names sanitising to one dir (config-load injectivity only
        # protects the live config): keep the lowest id, deterministically
        # and loudly.
        dir_to_existing: dict[str, tuple[int, str]] = {}
        for existing_id, existing_name in dao.list_device_names():  # ordered by id
            key = sanitize_device_name(existing_name)
            if key in dir_to_existing:
                _log.warning(
                    "reindex_device_name_collision",
                    dir=key,
                    kept=dir_to_existing[key][1],
                    ignored=existing_name,
                )
                continue
            dir_to_existing[key] = (existing_id, existing_name)
        for done, cand in enumerate(candidates, start=1):
            if should_stop is not None and should_stop():
                cancelled = True
                _log.info("reindex_cancelled", root=str(session_root), done=done - 1)
                break
            truth = _read_file_truth(cand)
            if truth is None:
                skipped += 1
            else:
                t_start, t_end, fs, n_bytes = truth
                dev_id = device_ids.get(cand.device_dir)
                if dev_id is None:
                    existing = dir_to_existing.get(cand.device_dir)
                    if existing is not None:
                        dev_id = existing[0]
                        devices_seen.append(existing[1])
                    else:
                        # host/port describe the DEVICE's network address —
                        # unknowable from a tree; never fabricate them from
                        # the re-indexing machine (review finding).
                        dev_id = dao.upsert_device(cand.device_dir, "", 0, {})
                        devices_seen.append(cand.device_dir)
                    device_ids[cand.device_dir] = dev_id
                nslc = (
                    cand.sid.network,
                    cand.sid.station,
                    cand.sid.location,
                    cand.sid.channel,
                )
                stream_id = stream_ids.get((dev_id, nslc))
                if stream_id is None:
                    stream_id = dao.upsert_stream(dev_id, nslc, fs)
                    stream_ids[(dev_id, nslc)] = stream_id
                dao.replace_file(stream_id, cand.path, t_start, t_end, n_bytes)
                indexed_paths.add(str(cand.path))
                indexed += 1
                start_iso, end_iso = str(t_start), str(t_end)
                if span_min is None or start_iso < span_min:
                    span_min = start_iso
                if span_max is None or end_iso > span_max:
                    span_max = end_iso
            if progress is not None:
                progress(ReindexProgress(done, total, skipped))
        pruned = 0
        synthesized = False
        stale_ids: list[int] = []
        if not cancelled:
            # Prune by RUN MEMBERSHIP, not disk existence (review major):
            # the completed run's candidate set is exhaustive for this
            # root, so any row it did not upsert is a foreign-machine
            # path, a path outside this root (a same-machine duplicate's
            # original — its existence on disk must not keep it in THIS
            # root's index), or a file this run refused as dirty. Polled
            # per row (rule 7; a foreign stale DB can hold many rows).
            for row_id, path_str in dao.all_file_rows():
                if should_stop is not None and should_stop():
                    cancelled = True
                    _log.info("reindex_cancelled_in_prune", root=str(session_root))
                    break
                if path_str not in indexed_paths:
                    stale_ids.append(row_id)
        if not cancelled:
            pruned = dao.delete_files(stale_ids)
            dao.refresh_stream_byte_totals()
            real_sessions = [r for r in dao.list_sessions() if not r.reindexed]
            if not real_sessions and span_min is not None and span_max is not None:
                from echosmonitor.core.session import sanitize_project_name

                if sanitize_project_name(session_root.name) != session_root.name:
                    # The dir name is not a sanitiser fixed point: a future
                    # session under this raw name would resolve to a SIBLING
                    # dir, so the synthesized name buys this directory no
                    # collision protection (review finding). Still the most
                    # honest name available — record it, loudly.
                    _log.warning(
                        "reindex_project_dir_not_canonical",
                        dir=session_root.name,
                        sanitized=sanitize_project_name(session_root.name),
                    )
                dao.upsert_reindexed_session(
                    project_name=session_root.name,
                    started_at=span_min,
                    ended_at=span_max,
                    host=host,
                    version=version,
                    devices=devices_seen,
                )
                synthesized = True
        dao.flush_now()
    finally:
        dao.close()
    report = ReindexReport(
        session_root=str(session_root),
        devices=len(device_ids),
        streams=len(stream_ids),
        files_indexed=indexed,
        files_skipped=skipped,
        files_pruned=pruned,
        synthesized_session=synthesized,
        cancelled=cancelled,
        elapsed_s=round(time.monotonic() - t0, 3),
    )
    _log.info(
        "reindex_done",
        root=report.session_root,
        devices=report.devices,
        streams=report.streams,
        files_indexed=report.files_indexed,
        files_skipped=report.files_skipped,
        files_pruned=report.files_pruned,
        synthesized_session=report.synthesized_session,
        cancelled=report.cancelled,
        elapsed_s=report.elapsed_s,
    )
    return report
