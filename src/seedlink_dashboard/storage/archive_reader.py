"""Read historical waveforms back from the SDS MiniSEED archive (M9 Stage C).

This is the read counterpart to :class:`~seedlink_dashboard.storage.
mseed_writer.MseedWriter`. It lets an AI agent run over data the dashboard
already archived — the validation workflow: take a known event from your
own archive, run the picker, see the picks.

Strictly READ-ONLY (CLAUDE.md rule 8 — the MiniSEED file is the source of
truth; this never writes). It lives in ``storage/`` because all archive
file access does. Gaps are kept explicit (no ``fill_value``) so a caller
can tell real data from filled silence and skip windows that straddle a
gap.

File discovery uses the ``files`` SQLite index when a DAO is supplied
(``files_in_range`` — index-backed), and otherwise falls back to
enumerating the canonical SDS paths day by day across the requested range,
so it works on archives written before the index existed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import obspy
import structlog
from obspy import Stream

from seedlink_dashboard.storage.sds import day_bounds, device_sds_root, sds_path

if TYPE_CHECKING:
    from pathlib import Path

    from obspy import UTCDateTime

    from seedlink_dashboard.core.models import StreamID
    from seedlink_dashboard.storage.dao import ArchiveDao

_log = structlog.get_logger(__name__)

# Hard cap on the day-by-day SDS fallback scan so a nonsensical range
# (years) can't spin forever. One window read should never span this many
# days; if it does, the caller asked for something unreasonable.
_MAX_SCAN_DAYS = 400


class ArchiveReader:
    """Read trimmed waveform windows from an SDS archive. Read-only."""

    def __init__(self, root: Path, dao: ArchiveDao | None = None) -> None:
        """``root`` is the SDS archive root; ``dao`` (optional) accelerates
        file discovery via the ``files`` index and resolves stream ids."""
        self._root = root
        self._dao = dao

    def read_window(
        self,
        sid: StreamID,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        *,
        device_name: str | None = None,
    ) -> Stream:
        """Return the archived data for ``sid`` over ``[t_start, t_end]``.

        The returned :class:`~obspy.Stream` is merged (gaps left explicit
        as masked samples / separate traces — no fill) and trimmed to the
        window. Empty if nothing is archived for that span. ``device_name``
        (with a DAO) enables the index-backed file lookup; without it the
        SDS-path fallback scan is used.
        """
        paths = self._candidate_paths(sid, t_start, t_end, device_name)
        if not paths:
            _log.debug(
                "archive_reader_no_files",
                nslc=sid.nslc,
                t_start=str(t_start),
                t_end=str(t_end),
            )
            return Stream()

        merged = Stream()
        for path in paths:
            try:
                st = obspy.read(
                    str(path),
                    format="MSEED",
                    starttime=t_start,
                    endtime=t_end,
                )
            except Exception as exc:
                _log.warning("archive_reader_read_failed", path=str(path), error=str(exc))
                continue
            merged += st.select(
                network=sid.network,
                station=sid.station,
                location=sid.location,
                channel=sid.channel,
            )

        if len(merged) == 0:
            return merged
        # method=0: combine same-id traces, leaving gaps explicit (masked)
        # rather than filling them — the caller decides how to treat gaps.
        merged.merge(method=0)
        merged.trim(t_start, t_end)
        return merged

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _candidate_paths(
        self,
        sid: StreamID,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        device_name: str | None,
    ) -> list[Path]:
        """Files that may cover the window: index first, SDS scan fallback."""
        indexed: list[Path] = []
        if self._dao is not None and device_name is not None:
            stream_id = self._dao.find_stream_id(device_name, sid.nslc)
            if stream_id is not None:
                indexed = self._dao.files_in_range(stream_id, t_start, t_end)

        # Union the index hits with the canonical SDS paths for each day in
        # the range — the fallback covers archives written before the index
        # and any file the index missed. Dedupe, keep only existing files.
        seen: set[Path] = set()
        ordered: list[Path] = []
        for path in [*indexed, *self._scan_sds_days(sid, t_start, t_end, device_name)]:
            if path in seen:
                continue
            seen.add(path)
            if path.exists():
                ordered.append(path)
        return ordered

    def _scan_sds_days(
        self,
        sid: StreamID,
        t_start: UTCDateTime,
        t_end: UTCDateTime,
        device_name: str | None,
    ) -> list[Path]:
        """Canonical SDS path for each UTC day touched by the window.

        When ``device_name`` is given the scan is rooted at the per-device
        SDS subtree (matching the writer's device-namespaced layout); when
        ``None`` it falls back to the legacy base-rooted layout.
        """
        root = device_sds_root(self._root, device_name) if device_name is not None else self._root
        paths: list[Path] = []
        day_start, _ = day_bounds(t_start)
        cursor = day_start
        days = 0
        while cursor < t_end and days < _MAX_SCAN_DAYS:
            paths.append(sds_path(root, cursor, sid))
            _, next_midnight = day_bounds(cursor)
            cursor = next_midnight
            days += 1
        return paths
